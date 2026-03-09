from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from sqlalchemy.orm import selectinload
from datetime import datetime, timedelta
from uuid import uuid4
from typing import Optional
from pydantic import BaseModel

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, ServicePlan, VPNServer, Config, Invoice, InvoiceStatus, Promocode, PromocodeUsage, PromocodeType
from app.services.marzban_service import create_marzban_user, delete_marzban_user
from app.services.crypto_service import decrypt
from app.services.ssh_service import check_and_get_marzban_token
from app.services.link_generator import generate_sub_token

router = APIRouter()


def _calc_final_price(plan: ServicePlan, extra_discount: float = 0.0) -> float:
    """
    Считает итоговую цену плана.
    extra_discount — дополнительная скидка от промокода (%).
    Скидки суммируются: plan.discount_percent + extra_discount.
    """
    total_discount = min(plan.discount_percent + extra_discount, 100.0)
    base = plan.price * plan.months
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

        check = await check_and_get_marzban_token(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            username=admin_user,
            password=admin_pass
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


# --- ЭНДПОИНТЫ ---

@router.get("/plans")
async def get_available_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .order_by(ServicePlan.tier_level, ServicePlan.months)
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
            "price_per_month": p.price,
            "months": p.months,
            "discount_percent": p.discount_percent,
            "final_price": _calc_final_price(p),
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

    # Шаг 2 — промокод
    extra_discount = 0.0
    applied_promo = None

    if body.promocode:
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
    marzban_username = f"privax_{current_user.id}_{short_uuid}"
    sub_token = generate_sub_token()
    expire_days = plan.months * 30

    # Шаг 6
    max_devices = plan.tier.max_sessions if plan.tier else None
    marzban_result = await create_marzban_user(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=admin_user,
        mar_admin_pass=admin_pass,
        marzban_username=marzban_username,
        expire_days=expire_days,
        data_limit_gb=plan.data_limit_gb,
        device_limit=max_devices
    )

    if not marzban_result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось создать VPN конфигурацию: {marzban_result['error']}"
        )

    vless_link = marzban_result.get("vless_link")
    expire_at = datetime.utcnow() + timedelta(days=expire_days)

    # Шаг 7
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        marzban_username=marzban_username,
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

    await db.commit()

    return {
        "status": "Подписка активирована!",
        "sub_url": f"/sub/{sub_token}",
        "expires_at": expire_at.isoformat(),
        "server": server.name,
        "plan": plan.name,
        "max_devices": max_devices,
        "maxDevices": max_devices,
        "device_limit": max_devices,
        "devices_limit": max_devices,
        "max_sessions": max_devices,
        "amount_paid": final_price,
        "discount_applied": extra_discount if extra_discount else None,
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
            "expires_at": c.expire_at.isoformat(),
            "expired": c.expire_at < now,
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
            "max_devices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "maxDevices": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "device_limit": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "devices_limit": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
            "max_sessions": c.plan.tier.max_sessions if c.plan and c.plan.tier else None,
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
    Сбрасывает подписку: удаляет старого пользователя в Marzban,
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

    server = config.server
    if not server:
        raise HTTPException(status_code=500, detail="Сервер не найден")

    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    # Шаг 1 — удаляем старого пользователя в Marzban
    del_result = await delete_marzban_user(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=admin_user,
        mar_admin_pass=admin_pass,
        marzban_username=config.marzban_username,
    )
    if not del_result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось удалить старый конфиг: {del_result['error']}")

    # Шаг 2 — считаем оставшиеся дни
    now = datetime.utcnow()
    days_left = max(1, (config.expire_at - now).days)

    # Шаг 3 — новый username и токен
    short_uuid = uuid4().hex[:4]
    new_marzban_username = f"privax_{current_user.id}_{short_uuid}"
    new_sub_token = generate_sub_token()

    max_devices = config.plan.tier.max_sessions if config.plan and config.plan.tier else None
    data_limit_gb = config.plan.data_limit_gb if config.plan else 0

    # Шаг 4 — создаём нового пользователя в Marzban
    create_result = await create_marzban_user(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=admin_user,
        mar_admin_pass=admin_pass,
        marzban_username=new_marzban_username,
        expire_days=days_left,
        data_limit_gb=data_limit_gb,
        device_limit=max_devices,
    )
    if not create_result["success"]:
        raise HTTPException(status_code=500, detail=f"Не удалось создать новый конфиг: {create_result['error']}")

    # Шаг 5 — обновляем запись в БД
    config.marzban_username = new_marzban_username
    config.vless_link = create_result.get("vless_link")
    config.sub_token = new_sub_token
    await db.commit()

    return {
        "status": "ok",
        "message": "Подписка сброшена. Все устройства отключены.",
        "vless_link": config.vless_link,
        "sub_url": f"/sub/{new_sub_token}",
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
    2. Удаляет пользователя из Marzban
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

        del_result = await delete_marzban_user(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            mar_admin_user=admin_user,
            mar_admin_pass=admin_pass,
            marzban_username=config.marzban_username,
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

            del_result = await delete_marzban_user(
                ip=server.ip_address,
                ssh_port=server.ssh_port,
                marzban_port=server.marzban_port,
                mar_admin_user=admin_user,
                mar_admin_pass=admin_pass,
                marzban_username=config.marzban_username,
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