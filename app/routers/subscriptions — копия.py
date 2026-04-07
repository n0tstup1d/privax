from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional
from pydantic import BaseModel

from auth.deps import get_current_user
from database.database import get_db
from database.models import (
    Client, ServicePlan, VPNServer, Config,
    Invoice, InvoiceStatus, Promocode, PromocodeUsage, PromocodeType
)
from app.services.marzban_service import (
    create_marzban_user,
    delete_marzban_user,
    check_marzban_connection,
    get_marzban_user,
)
from app.services.crypto_service import decrypt
from app.services.link_generator import generate_sub_token

router = APIRouter()

# Кулдаун между сбросами устройств (в часах)
RESET_COOLDOWN_HOURS = 1


# ─────────────────────────────────────────────────────────────────────────────

def _calc_final_price(plan: ServicePlan, extra_discount: float = 0.0) -> float:
    total_discount = min(plan.discount_percent + extra_discount, 100.0)
    return round(plan.price * (1 - total_discount / 100), 2)


class BuyRequest(BaseModel):
    promocode: Optional[str] = None


async def _find_working_server(tier_level: int, db: AsyncSession) -> VPNServer:
    """
    Находит первый рабочий сервер нужного тира с свободными слотами.
    Сначала проверяет активные серверы (по возрастанию загрузки),
    затем неактивные.
    """
    active_q = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == True)
        .order_by(VPNServer.current_users_count.asc())
    )
    active_servers = active_q.scalars().all()

    inactive_q = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == False)
        .order_by(VPNServer.current_users_count.asc())
    )
    inactive_servers = inactive_q.scalars().all()

    if not active_servers and not inactive_servers:
        raise HTTPException(status_code=503, detail="Нет серверов для этого тарифа.")

    candidates = list(active_servers) + list(inactive_servers)

    for server in candidates:
        max_users = server.tier.default_max_users if server.tier else 0
        if server.current_users_count >= max_users:
            continue

        check = await check_marzban_connection(
            marzban_url=server.marzban_url,
            admin_username=decrypt(server.mar_admin_user),
            admin_password=decrypt(server.mar_admin_pass),
        )

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


def _pick_vless_link(links: list) -> Optional[str]:
    """Выбирает первую vless:// ссылку из списка links."""
    for link in links:
        if link.startswith("vless://"):
            return link
    return links[0] if links else None


# --- ЭНДПОИНТЫ ---

@router.get("/plans")
async def get_available_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .order_by(ServicePlan.tier_level, ServicePlan.duration_days)
    )
    plans = result.scalars().all()

    servers_result = await db.execute(
        select(VPNServer).options(selectinload(VPNServer.tier))
    )
    all_servers = servers_result.scalars().all()

    def tier_slots(tier_level: int):
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
        if p.is_hidden:
            continue
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
    # Шаг 1 — план
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .where(ServicePlan.id == plan_id)
    )
    plan = result.scalar_one_or_none()
    if not plan or plan.is_hidden:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Шаг 2 — скидка / промокод
    extra_discount = 0.0
    applied_promo = None
    referral_discount_applied = False

    existing_count = await db.scalar(
        select(func.count(Config.id)).where(Config.client_id == current_user.id)
    )
    is_first_purchase = (existing_count == 0)

    # Лимит покупок
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

    if is_first_purchase and current_user.referred_by_id:
        from app.routers.referrals import get_referral_discount
        referral_disc = await get_referral_discount(current_user.id, db)
        if referral_disc > 0:
            if body.promocode:
                raise HTTPException(
                    status_code=400,
                    detail=f"При первой покупке по реферальной ссылке промокод недоступен — скидка {referral_disc:.0f}% уже применена автоматически"
                )
            extra_discount = referral_disc
            referral_discount_applied = True
    elif body.promocode:
        from app.routers.promocodes import _validate_promocode, _record_usage
        promo = await _validate_promocode(body.promocode, current_user.id, plan_id, db)
        if promo.promo_type != PromocodeType.DISCOUNT:
            raise HTTPException(status_code=400, detail="Этот промокод не является скидочным. Используйте /promocodes/apply")
        extra_discount = promo.discount_percent
        applied_promo = promo

    final_price = _calc_final_price(plan, extra_discount)

    # Шаг 3 — баланс
    if current_user.balance < final_price:
        raise HTTPException(
            status_code=402,
            detail=f"Недостаточно средств. Нужно: {final_price}₽, у вас: {current_user.balance}₽"
        )

    # Шаг 3.5 — лимит одновременных подписок
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

    # Шаг 4 — сервер
    server = await _find_working_server(plan.tier_level, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    # Шаг 5 — генерируем username
    short_uuid = uuid4().hex[:4]
    mar_username = f"privax_{current_user.id}_{short_uuid}"
    sub_token = generate_sub_token()
    expire_days = plan.duration_days

    # inbounds для нового пользователя (берём из сервера, если есть)
    import json as _json
    _raw_inbounds = _json.loads(server.inbounds_json) if server.inbounds_json else {}
    # Нормализуем: Marzban API ожидает {"vless": ["TAG"]}, а в БД хранится {"vless": [{"tag": "TAG", ...}]}
    inbounds = {
        proto: [item["tag"] if isinstance(item, dict) else item for item in items]
        for proto, items in _raw_inbounds.items()
    }

    # Шаг 6 — создаём пользователя в Marzban
    mar_result = await create_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=mar_username,
        expire_days=expire_days,
        inbounds=inbounds,
    )

    if not mar_result["success"]:
        import logging
        logging.error(f"[BUY] create_marzban_user failed: {mar_result.get('error')}")
        raise HTTPException(status_code=500, detail="ERR_CLIENT_CREATE")

    subscription_url = mar_result.get("subscription_url", "")
    links = mar_result.get("links", [])
    vless_link = _pick_vless_link(links)

    # expire_at — конец дня через N дней
    _base = datetime.utcnow() + timedelta(days=expire_days)
    expire_at = _base.replace(hour=23, minute=59, second=59, microsecond=0)

    # Шаг 7 — конфиг в БД
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        mar_username=mar_username,
        subscription_url=subscription_url,
        vless_link=vless_link,
        sub_token=sub_token,
        expire_at=expire_at,
        is_active=True
    )
    db.add(new_config)

    # Шаг 8 — инвойс
    invoice = Invoice(
        client_id=current_user.id,
        plan_id=plan.id,
        amount=final_price,
        status=InvoiceStatus.PAID
    )
    db.add(invoice)

    # Шаг 9 — финансы и счётчики
    current_user.balance -= final_price
    server.current_users_count += 1

    if applied_promo:
        from app.routers.promocodes import _record_usage
        await _record_usage(applied_promo, current_user.id, db)

    if is_first_purchase:
        from app.routers.referrals import grant_referral_bonus
        await grant_referral_bonus(current_user.id, db)

    await db.commit()

    max_devices = plan.tier.max_sessions if plan.tier else None

    return {
        "status": "Подписка активирована!",
        "sub_url": f"/sub/{sub_token}",
        "subscription_url": subscription_url,
        "expires_at": expire_at.isoformat() + "Z",
        "server": server.name,
        "plan": plan.name,
        "max_devices": max_devices,
        "maxDevices": max_devices,
        "device_limit": max_devices,
        "amount_paid": final_price,
        "discount_applied": extra_discount if extra_discount else None,
        "referral_discount": referral_discount_applied,
        "note": "Вставьте subscription_url в AmneziaVPN — ссылка обновляется автоматически"
    }


@router.get("/my-subscriptions")
async def get_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Config)
        .options(
            selectinload(Config.plan).selectinload(ServicePlan.tier),
            selectinload(Config.server),
        )
        .where(Config.client_id == current_user.id)
        .order_by(Config.expire_at.desc())
    )
    configs = result.scalars().all()

    now = datetime.utcnow()
    return [
        {
            "id": c.id,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "subscription_url": c.subscription_url,
            "expires_at": c.expire_at.isoformat() + "Z",
            "expired": c.expire_at < now,
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
            "max_devices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "maxDevices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "last_reset_at": c.last_reset_at.isoformat() + "Z" if c.last_reset_at else None,
            "next_reset_at": (c.last_reset_at + timedelta(hours=RESET_COOLDOWN_HOURS)).isoformat() + "Z" if c.last_reset_at else None,
            "reset_cooldown_hours": RESET_COOLDOWN_HOURS,
            "server_name": c.server.name if c.server else None,
            "server_ip": c.server.ip_address if c.server else None,
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
        select(Config).where(Config.id == config_id, Config.client_id == current_user.id)
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
    Сбрасывает подписку: пересоздаёт пользователя в Marzban с новым username.
    Срок действия сохраняется. Все текущие сессии отключаются.
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

    # Шаг 1 — получаем точный expire из Marzban
    user_info = await get_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=config.mar_username,
    )
    exact_expire_at = user_info["expire_at"] if user_info["success"] and user_info.get("expire_at") else config.expire_at

    # Шаг 2 — удаляем старого пользователя
    del_result = await delete_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=config.mar_username,
    )
    if not del_result["success"]:
        raise HTTPException(status_code=500, detail="ERR_RESET_DELETE")

    # Шаг 3 — новый username
    short_uuid = uuid4().hex[:4]
    new_mar_username = f"privax_{current_user.id}_{short_uuid}"
    new_sub_token = generate_sub_token()

    # Шаг 4 — создаём нового пользователя с тем же expire
    import json as _json
    _raw_inbounds = _json.loads(server.inbounds_json) if server.inbounds_json else {}
    # Нормализуем: Marzban API ожидает {"vless": ["TAG"]}, а в БД хранится {"vless": [{"tag": "TAG", ...}]}
    inbounds = {
        proto: [item["tag"] if isinstance(item, dict) else item for item in items]
        for proto, items in _raw_inbounds.items()
    }

    create_result = await create_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=new_mar_username,
        expire_days=0,
        expire_at=exact_expire_at,
        inbounds=inbounds,
    )
    if not create_result["success"]:
        raise HTTPException(status_code=500, detail="ERR_RESET_CREATE")

    new_links = create_result.get("links", [])
    new_vless_link = _pick_vless_link(new_links)
    new_subscription_url = create_result.get("subscription_url", "")

    # Шаг 5 — обновляем в БД
    config.mar_username = new_mar_username
    config.subscription_url = new_subscription_url
    config.vless_link = new_vless_link
    config.sub_token = new_sub_token
    config.expire_at = exact_expire_at
    config.last_reset_at = datetime.utcnow()
    await db.commit()

    next_reset_at = config.last_reset_at + timedelta(hours=RESET_COOLDOWN_HOURS)

    return {
        "status": "ok",
        "message": "Подписка сброшена. Все устройства отключены.",
        "subscription_url": new_subscription_url,
        "sub_url": f"/sub/{new_sub_token}",
        "next_reset_at": next_reset_at.isoformat() + "Z",
        "cooldown_hours": RESET_COOLDOWN_HOURS,
    }


@router.get("/{config_id}/server-status")
async def get_server_status(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user),
):
    """
    Статус VPN-сервера — читается из БД, обновляется фоновой задачей каждые 5 мин.
    """
    result = await db.execute(
        select(Config)
        .options(selectinload(Config.server))
        .where(Config.id == config_id, Config.client_id == current_user.id)
    )
    config = result.scalar_one_or_none()
    if not config or not config.server:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = config.server
    return {
        "online": server.is_online,
        "last_checked_at": server.last_checked_at.isoformat() + "Z" if server.last_checked_at else None,
    }


@router.delete("/{config_id}")
async def delete_subscription(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """Удаляет подписку и пользователя из Marzban."""
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
        del_result = await delete_marzban_user(
            marzban_url=server.marzban_url,
            admin_username=decrypt(server.mar_admin_user),
            admin_password=decrypt(server.mar_admin_pass),
            username=config.mar_username,
        )
        if del_result["success"] and server.current_users_count > 0:
            server.current_users_count -= 1

    config.is_active = False
    config.vless_link = None
    config.subscription_url = None
    await db.commit()

    return {"status": "ok", "message": "Подписка удалена"}


@router.delete("")
async def delete_all_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """Удаляет ВСЕ активные подписки клиента."""
    result = await db.execute(
        select(Config)
        .options(selectinload(Config.server))
        .where(Config.client_id == current_user.id, Config.is_active == True)
    )
    configs = result.scalars().all()

    deleted = 0
    errors = []

    for config in configs:
        server = config.server
        if server:
            del_result = await delete_marzban_user(
                marzban_url=server.marzban_url,
                admin_username=decrypt(server.mar_admin_user),
                admin_password=decrypt(server.mar_admin_pass),
                username=config.mar_username,
            )
            if del_result["success"] and server.current_users_count > 0:
                server.current_users_count -= 1
            elif not del_result["success"]:
                errors.append(f"Config {config.id}: {del_result['error']}")

        config.is_active = False
        config.vless_link = None
        config.subscription_url = None
        deleted += 1

    await db.commit()

    return {
        "status": "ok",
        "deleted": deleted,
        "errors": errors if errors else None,
    }