from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional
from pydantic import BaseModel

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, ServicePlan, VPNServer, Config, Invoice, InvoiceStatus, Promocode, PromocodeUsage, PromocodeType, TrustedDomain
from app.services.xui_service import create_xui_client, delete_xui_client, check_xui_connection, get_xui_client_expiry
from app.services.xui_configurator import update_short_id_on_server
from app.services.crypto_service import decrypt
from app.services.link_generator import generate_vless_link, generate_vless_tcp_link, generate_short_id, generate_xhttp_path
from app.services.link_generator import generate_sub_token
from database.models import InboundType

router = APIRouter()

# Кулдаун между сбросами устройств (в часах)
RESET_COOLDOWN_HOURS = 1


def _calc_final_price(plan: ServicePlan, extra_discount: float = 0.0) -> float:
    """
    Считает итоговую цену плана.
    extra_discount — дополнительная скидка от промокода (%).
    Скидки суммируются: plan.discount_percent + extra_discount.
    """
    total_discount = min(plan.discount_percent + extra_discount, 100.0)
    base = plan.price  # price уже является итоговой ценой за весь период duration_days
    return round(base * (1 - total_discount / 100), 2)


class BuyRequest(BaseModel):
    promocode: Optional[str] = None


async def _find_working_server(tier_level: int, db: AsyncSession) -> VPNServer:
    active_q = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == True)
        .order_by(VPNServer.current_users_count.asc())
    )
    active_servers = active_q.scalars().all()

    if not active_servers:
        raise HTTPException(status_code=503, detail="Нет активных серверов для этого тарифа.")

    inactive_q = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == False)
        .order_by(VPNServer.current_users_count.asc())
    )
    inactive_servers = inactive_q.scalars().all()

    candidates = list(active_servers) + list(inactive_servers)

    for server in candidates:
        max_users = server.tier.default_max_users if server.tier else 0
        if server.current_users_count >= max_users:
            continue

        admin_user = decrypt(server.mar_admin_user)
        admin_pass = decrypt(server.mar_admin_pass)

        check = await check_xui_connection(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            panel_port=server.panel_port,
            username=admin_user,
            password=admin_pass,
            base_path=decrypt(server.panel_path) if server.panel_path else ""
        )

        import logging
        logging.warning(f"[CHECK] server={server.ip_address} port={server.panel_port} path={decrypt(server.panel_path) if server.panel_path else ''!r} result={check}")
        if check["success"]:
            if not server.is_active:
                server.is_active = True
                await db.flush()
            return server
        else:
            server.is_active = False
            await db.flush()

    await db.commit()
    raise HTTPException(
        status_code=503,
        detail="Все серверы этого тарифа переполнены или временно недоступны."
    )


# --- ЭНДПОИНТЫ ---

@router.get("/plans")
async def get_available_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .order_by(ServicePlan.tier_level, ServicePlan.duration_days)
    )
    plans = result.scalars().all()

    # Получаем серверы с тирами для подсчёта слотов
    servers_result = await db.execute(
        select(VPNServer).options(selectinload(VPNServer.tier))
    )
    all_servers = servers_result.scalars().all()

    def tier_slots(tier_level: int):
        """
        Возвращает (is_available, total_capacity, current_users, max_per_server)
        """
        tier_servers = [s for s in all_servers if s.tier_level == tier_level]
        if not tier_servers:
            return False, 0, 0, 0
        max_u = lambda s: s.tier.default_max_users if s.tier else 0
        total_cap = sum(max_u(s) for s in tier_servers)
        total_cur = sum(s.current_users_count for s in tier_servers)
        max_per_server = max(max_u(s) for s in tier_servers)
        available = any(s.current_users_count < max_u(s) for s in tier_servers)
        return available, total_cap, total_cur, max_per_server

    output = []
    for p in plans:
        is_avail, total_cap, total_cur, max_per_server = tier_slots(p.tier_level)
        output.append({
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name or "",
            "description": p.description or "",
            "tier_level": p.tier_level,
            "price_per_month": round(p.price / (p.duration_days / 30), 2),
            "duration_days": p.duration_days,
            "discount_percent": p.discount_percent,
            "base_price": p.price,
            "final_price": _calc_final_price(p),
            "purchase_limit": p.purchase_limit,
            "max_sessions": p.tier.max_sessions if p.tier else 0,
            "is_available": is_avail,
            "slots_total": total_cap,
            "slots_used": total_cur,
            "max_users_per_server": max_per_server,
        })
    return output


@router.post("/buy/{plan_id}")
async def buy_subscription(
    plan_id: int,
    body: BuyRequest = BuyRequest(),
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    # Шаг 1
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .where(ServicePlan.id == plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Шаг 2 — скидка / промокод
    extra_discount = 0.0
    applied_promo = None
    referral_discount_applied = False

    # Проверяем: это первая покупка?
    existing_count = await db.scalar(
        select(func.count(Config.id)).where(Config.client_id == current_user.id)
    )
    is_first_purchase = (existing_count == 0)

    # Проверяем лимит покупок данного тарифа
    if plan.purchase_limit > 0:
        plan_purchase_count = await db.scalar(
            select(func.count(Config.id)).where(
                Config.client_id == current_user.id,
                Config.plan_id == plan.id,
            )
        )
        if plan_purchase_count >= plan.purchase_limit:
            raise HTTPException(
                status_code=400,
                detail=f"Этот тариф можно купить не более {plan.purchase_limit} раз"
            )

    # Если пришёл по реферальной ссылке и это первая покупка — автоскидка 15%, промокод заблокирован
    if is_first_purchase and current_user.referred_by_id:
        if body.promocode:
            raise HTTPException(
                status_code=400,
                detail="При первой покупке по реферальной ссылке промокод недоступен — скидка 15% уже применена автоматически"
            )
        extra_discount = 15.0
        referral_discount_applied = True

    elif body.promocode:
        from app.routers.promocodes import _validate_promocode, _record_usage
        promo = await _validate_promocode(body.promocode, current_user.id, plan_id, db)

        if promo.promo_type != PromocodeType.DISCOUNT:
            raise HTTPException(status_code=400, detail="Этот промокод не является скидочным. Используйте /promocodes/apply")

        extra_discount = promo.discount_percent
        applied_promo = promo

    final_price = _calc_final_price(plan, extra_discount)

    # Шаг 3
    if current_user.balance < final_price:
        raise HTTPException(
            status_code=402,
            detail=f"Недостаточно средств. Нужно: {final_price}₽, у вас: {current_user.balance}₽"
        )

    # Шаг 3.5 — проверка лимита одновременных подписок (max_sessions)
    if plan.tier:
        active_configs_q = await db.execute(
            select(Config)
            .join(ServicePlan, Config.plan_id == ServicePlan.id)
            .where(
                Config.client_id == current_user.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow(),
                ServicePlan.tier_level == plan.tier_level,
            )
        )
        active_count = len(active_configs_q.scalars().all())
        if active_count >= plan.tier.max_sessions:
            raise HTTPException(
                status_code=400,
                detail=f"Достигнут лимит подписок для этого уровня — максимум {plan.tier.max_sessions}. "
                       f"Удалите одну из существующих подписок чтобы купить новую."
            )

    # Шаг 4
    server = await _find_working_server(plan.tier_level, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    # Шаг 5
    short_uuid = uuid4().hex[:4]
    xui_username = f"privax_{current_user.id}_{short_uuid}"
    sub_token = generate_sub_token()
    expire_days = plan.duration_days

    # Шаг 6 — создаём клиента в 3x-ui
    max_devices = plan.tier.max_sessions if plan.tier else None
    xui_result = await create_xui_client(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        username=xui_username,
        expire_days=expire_days,
        device_limit=max_devices,
        base_path=decrypt(server.panel_path) if server.panel_path else "",
        inbound_type=server.inbound_type.value if server.inbound_type else "tcp_reality",
    )

    if not xui_result["success"]:
        import logging
        logging.error(f"[BUY] create_xui_client failed: {xui_result.get('error')}")
        raise HTTPException(
            status_code=500,
            detail="ERR_CLIENT_CREATE"
        )

    # Генерируем VLESS-ссылку сами (3x-ui её не возвращает)
    xui_uuid      = xui_result["uuid"]
    xui_inbound_id = xui_result["inbound_id"]
    xhttp_path      = xui_result.get("xhttp_path", "/pwa/v1/update")
    short_id      = generate_short_id()

    # Загружаем домены из БД (TrustedDomain)
    domains_q = await db.execute(
        select(TrustedDomain).where(TrustedDomain.is_active == True)
    )
    trusted_domains = [d.domain for d in domains_q.scalars().all()]
    # Если в БД нет доменов — фолбэк на server_names
    import json as _json
    if not trusted_domains and server.server_names:
        trusted_domains = _json.loads(server.server_names)

    # Используем ссылку с панели (она правильная), либо генерируем сами как фолбэк
    vless_link = xui_result.get("vless_link")

    if not vless_link:
        public_key_to_use = xui_result.get("public_key") or server.reality_public_key
        if public_key_to_use and trusted_domains:
            if server.inbound_type == InboundType.TCP_REALITY:
                vless_link = generate_vless_tcp_link(
                    user_uuid=xui_uuid,
                    server_ip=server.ip_address,
                    public_key=public_key_to_use,
                    short_ids=[short_id],
                    sni_domains=trusted_domains,
                )
            else:
                vless_link = generate_vless_link(
                    user_uuid=xui_uuid,
                    server_ip=server.ip_address,
                    public_key=public_key_to_use,
                    short_ids=[short_id],
                    sni_domains=trusted_domains,
                    xhttp_path=xhttp_path,
                )

    # shortId всё равно нужно добавить в инбаунд чтобы Xray принял соединение
    await update_short_id_on_server(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        new_short_id=short_id,
        base_path=decrypt(server.panel_path) if server.panel_path else ""
    )

    expire_at = datetime.utcnow() + timedelta(days=expire_days)

    # Шаг 7
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        xui_username=xui_username,
        xui_uuid=xui_uuid,
        xui_inbound_id=xui_inbound_id,
        xui_short_id=short_id,
        vless_link=vless_link,
        sub_token=sub_token,
        expire_at=expire_at,
        is_active=True
    )
    db.add(new_config)

    # Шаг 8
    invoice = Invoice(
        client_id=current_user.id,
        plan_id=plan.id,
        amount=final_price,
        status=InvoiceStatus.PAID
    )
    db.add(invoice)

    # Шаг 9
    current_user.balance -= final_price
    server.current_users_count += 1

    if applied_promo:
        from app.routers.promocodes import _record_usage
        await _record_usage(applied_promo, current_user.id, db)

    # Реферальный бонус рефереру: только при первой покупке
    if is_first_purchase:
        from app.routers.referrals import grant_referral_bonus
        await grant_referral_bonus(current_user.id, db)

    await db.commit()

    return {
        "status": "Подписка активирована!",
        "sub_url": f"/sub/{sub_token}",
        "expires_at": expire_at.isoformat() + "Z",
        "server": server.name,
        "plan": plan.name,
        "max_devices": max_devices,
        "maxDevices": max_devices,
        "device_limit": max_devices,
        "devices_limit": max_devices,
        "max_sessions": max_devices,
        "amount_paid": final_price,
        "discount_applied": extra_discount if extra_discount else None,
        "referral_discount": referral_discount_applied,
        "note": "Вставьте sub_url в AmneziaVPN — ссылка обновляется автоматически"
    }


@router.get("/my-subscriptions")
async def get_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Config)
        .options(selectinload(Config.plan).selectinload(ServicePlan.tier))
        .where(Config.client_id == current_user.id)
        .order_by(Config.expire_at.desc())
    )
    configs = result.scalars().all()

    now = datetime.utcnow()
    return [
        {
            "id": c.id,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "expires_at": c.expire_at.isoformat() + "Z",
            "expired": c.expire_at < now,
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
            "max_devices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "maxDevices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "device_limit": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "devices_limit": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "max_sessions": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "last_reset_at": c.last_reset_at.isoformat() + "Z" if c.last_reset_at else None,
            "next_reset_at": (c.last_reset_at + timedelta(hours=RESET_COOLDOWN_HOURS)).isoformat() + "Z" if c.last_reset_at else None,
            "reset_cooldown_hours": RESET_COOLDOWN_HOURS,
        }
        for c in configs
    ]


@router.patch("/{config_id}/auto-renew")
async def toggle_auto_renew(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Config).where(
            Config.id == config_id,
            Config.client_id == current_user.id
        )
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    config.auto_renew = not config.auto_renew
    await db.commit()

    return {
        "config_id": config_id,
        "auto_renew": config.auto_renew,
        "status": "Авто-продление включено" if config.auto_renew else "Авто-продление выключено"
    }

@router.post("/{config_id}/reset")
async def reset_subscription(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Сбрасывает подписку: удаляет старого клиента в 3x-ui,
    создаёт нового с новой VLESS-ссылкой. Срок действия сохраняется.
    Используется когда нужно сбросить все подключённые устройства.
    """
    result = await db.execute(
        select(Config)
        .options(
            selectinload(Config.server),
            selectinload(Config.plan).selectinload(ServicePlan.tier)
        )
        .where(Config.id == config_id, Config.client_id == current_user.id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")
    if not config.is_active:
        raise HTTPException(status_code=400, detail="Подписка неактивна")

    # Проверка кулдауна
    if config.last_reset_at is not None:
        cooldown_until = config.last_reset_at + timedelta(hours=RESET_COOLDOWN_HOURS)
        now_utc = datetime.utcnow()
        if now_utc < cooldown_until:
            seconds_left = int((cooldown_until - now_utc).total_seconds())
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "RESET_COOLDOWN",
                    "message": f"Сброс доступен раз в {RESET_COOLDOWN_HOURS} ч. Следующий сброс через {seconds_left} сек.",
                    "next_reset_at": cooldown_until.isoformat() + "Z",
                    "seconds_left": seconds_left,
                    "cooldown_hours": RESET_COOLDOWN_HOURS,
                }
            )

    server = config.server
    if not server:
        raise HTTPException(status_code=500, detail="Сервер не найден")

    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    # Шаг 1 — получаем точное время истечения с панели
    expiry_result = await get_xui_client_expiry(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        xui_uuid=config.xui_uuid,
        xui_inbound_id=config.xui_inbound_id,
        base_path=decrypt(server.panel_path) if server.panel_path else ""
    )
    # Если панель вернула точное время — используем его, иначе берём из БД
    exact_expire_at = expiry_result["expire_at"] if expiry_result["success"] else config.expire_at

    # Шаг 2 — удаляем старого клиента из 3x-ui
    del_result = await delete_xui_client(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        xui_uuid=config.xui_uuid,
        xui_inbound_id=config.xui_inbound_id,
        base_path=decrypt(server.panel_path) if server.panel_path else ""
    )
    if not del_result["success"]:
        raise HTTPException(status_code=500, detail="ERR_RESET_DELETE")

    # Шаг 3 — новый username
    short_uuid = uuid4().hex[:4]
    new_xui_username = f"privax_{current_user.id}_{short_uuid}"
    new_sub_token = generate_sub_token()

    max_devices = config.plan.tier.max_sessions if config.plan and config.plan.tier else None

    # Шаг 4 — создаём нового клиента в 3x-ui (передаём точную дату, не дни)
    create_result = await create_xui_client(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        username=new_xui_username,
        expire_days=0,
        expire_at=exact_expire_at,
        device_limit=max_devices,
        base_path=decrypt(server.panel_path) if server.panel_path else "",
        inbound_type=server.inbound_type.value if server.inbound_type else "tcp_reality",
    )
    if not create_result["success"]:
        raise HTTPException(status_code=500, detail="ERR_RESET_CREATE")
    # Шаг 4.5 — новая VLESS-ссылка с нашим new_short_id
    import json as _json
    new_short_id = generate_short_id()

    new_xhttp_path = create_result.get("xhttp_path", "/pwa/v1/update")
    new_public_key = create_result.get("public_key") or server.reality_public_key
    new_mldsa_verify = create_result.get("mldsa65_verify", "")

    reset_domains_q = await db.execute(
        select(TrustedDomain).where(TrustedDomain.is_active == True)
    )
    reset_domains = [d.domain for d in reset_domains_q.scalars().all()]
    if not reset_domains and server.server_names:
        reset_domains = _json.loads(server.server_names)

    new_vless_link = None
    if new_public_key and reset_domains:
        if server.inbound_type == InboundType.TCP_REALITY:
            new_vless_link = generate_vless_tcp_link(
                user_uuid=create_result["uuid"],
                server_ip=server.ip_address,
                public_key=new_public_key,
                short_ids=[new_short_id],
                sni_domains=reset_domains,
                label=new_xui_username,
            )
        else:
            new_vless_link = generate_vless_link(
                user_uuid=create_result["uuid"],
                server_ip=server.ip_address,
                public_key=new_public_key,
                short_ids=[new_short_id],
                sni_domains=reset_domains,
                xhttp_path=new_xhttp_path,
                label=new_xui_username,
            )

    await update_short_id_on_server(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        panel_port=server.panel_port,
        xui_admin_user=admin_user,
        xui_admin_pass=admin_pass,
        new_short_id=new_short_id,
        remove_short_id=config.xui_short_id,  # удаляем старый shortId
        base_path=decrypt(server.panel_path) if server.panel_path else ""
    )

    # Шаг 5 — обновляем запись в БД
    config.xui_username = new_xui_username
    config.xui_uuid = create_result["uuid"]
    config.xui_inbound_id = create_result["inbound_id"]
    config.xui_short_id = new_short_id
    config.vless_link = new_vless_link
    config.sub_token = new_sub_token
    config.expire_at = exact_expire_at
    config.last_reset_at = datetime.utcnow()
    await db.commit()

    next_reset_at = config.last_reset_at + timedelta(hours=RESET_COOLDOWN_HOURS)

    return {
        "status": "ok",
        "message": "Подписка сброшена. Все устройства отключены.",
        "vless_link": config.vless_link,
        "sub_url": f"/sub/{new_sub_token}",
        "next_reset_at": next_reset_at.isoformat() + "Z",
        "cooldown_hours": RESET_COOLDOWN_HOURS,
    }


@router.delete("/{config_id}")
async def delete_subscription(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Удаляет конкретную подписку:
    1. Проверяет принадлежность клиенту
    2. Удаляет клиента из 3x-ui
    3. Уменьшает счётчик сервера
    4. Помечает конфиг как неактивный (не удаляем из БД — история)
    """
    result = await db.execute(
        select(Config)
        .options(selectinload(Config.server))
        .where(Config.id == config_id, Config.client_id == current_user.id)
    )
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = config.server
    if server and config.is_active:
        admin_user = decrypt(server.mar_admin_user)
        admin_pass = decrypt(server.mar_admin_pass)

        del_result = await delete_xui_client(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            panel_port=server.panel_port,
            xui_admin_user=admin_user,
            xui_admin_pass=admin_pass,
            xui_uuid=config.xui_uuid,
            xui_inbound_id=config.xui_inbound_id,
            base_path=decrypt(server.panel_path) if server.panel_path else ""
        )

        # Уменьшаем счётчик только если удаление прошло успешно
        if del_result["success"] and server.current_users_count > 0:
            server.current_users_count -= 1

    # Деактивируем в БД в любом случае
    config.is_active = False
    config.vless_link = None  # очищаем ссылку
    await db.commit()

    return {"status": "ok", "message": "Подписка удалена"}


@router.delete("")
async def delete_all_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Удаляет ВСЕ активные подписки клиента.
    """
    result = await db.execute(
        select(Config)
        .options(selectinload(Config.server))
        .where(
            Config.client_id == current_user.id,
            Config.is_active == True,
        )
    )
    configs = result.scalars().all()

    deleted = 0
    errors = []

    for config in configs:
        server = config.server
        if server:
            admin_user = decrypt(server.mar_admin_user)
            admin_pass = decrypt(server.mar_admin_pass)

            del_result = await delete_xui_client(
                ip=server.ip_address,
                ssh_port=server.ssh_port,
                panel_port=server.panel_port,
                xui_admin_user=admin_user,
                xui_admin_pass=admin_pass,
                xui_uuid=config.xui_uuid,
                xui_inbound_id=config.xui_inbound_id,
                base_path=decrypt(server.panel_path) if server.panel_path else ""
            )

            if del_result["success"] and server.current_users_count > 0:
                server.current_users_count -= 1
            elif not del_result["success"]:
                errors.append(f"Config {config.id}: {del_result['error']}")

        config.is_active = False
        config.vless_link = None
        deleted += 1

    await db.commit()

    return {
        "status": "ok",
        "deleted": deleted,
        "errors": errors if errors else None, 
    }