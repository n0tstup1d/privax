import math
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta

from auth.deps import get_admin_from_jwt, require_role
from database.database import get_db
from database.models import (
    Client, Config, VPNServer, ServicePlan,
    Invoice, InvoiceStatus, AdminRole
)
from app.services.marzban_service import (
    toggle_marzban_user, delete_marzban_user,
    create_marzban_user, extend_marzban_user,
)
from app.services.crypto_service import decrypt
from app.services.link_generator import generate_sub_token

router = APIRouter()

# Алиасы для удобства
_any_admin   = get_admin_from_jwt
_owner_or_op = require_role(AdminRole.OWNER, AdminRole.OPERATOR)
_owner_only  = require_role(AdminRole.OWNER)


# ═══════════════════════════════════════════════
#  КЛИЕНТЫ — СПИСОК
# ═══════════════════════════════════════════════

@router.get("/clients")
async def get_all_clients(
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
    search: Optional[str] = None,
    filter: Optional[str] = None,   # all | banned | active
    page: int = 1,
    page_size: int = 50,
):
    query = select(Client).order_by(Client.id.desc())

    if search:
        query = query.where(Client.email.ilike(f"%{search}%"))
    if filter == "banned":
        query = query.where(Client.is_banned == True)
    elif filter == "active":
        query = query.where(
            Client.id.in_(
                select(Config.client_id).where(
                    Config.is_active == True,
                    Config.expire_at > datetime.utcnow()
                )
            )
        )

    total = await db.scalar(select(func.count()).select_from(query.subquery()))
    query = query.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(query)
    clients = result.scalars().all()

    rows = []
    for c in clients:
        active_count = await db.scalar(
            select(func.count(Config.id)).where(
                Config.client_id == c.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow()
            )
        )
        rows.append({
            "id": c.id,
            "email": c.email,
            "balance": c.balance,
            "is_banned": c.is_banned,
            "active_subscriptions": active_count,
        })

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size),
        "items": rows,
    }


# ═══════════════════════════════════════════════
#  КЛИЕНТ — КАРТОЧКА
# ═══════════════════════════════════════════════

@router.get("/clients/{client_id}")
async def get_client_profile(
    client_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    configs_result = await db.execute(
        select(Config).where(Config.client_id == client_id).order_by(Config.expire_at.desc())
    )
    configs = configs_result.scalars().all()

    configs_data = []
    for c in configs:
        server = await db.get(VPNServer, c.server_id)
        plan   = await db.get(ServicePlan, c.plan_id)
        now    = datetime.utcnow()
        configs_data.append({
            "id": c.id,
            "mar_username": c.mar_username,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "subscription_url": c.subscription_url,
            "status": "active" if c.is_active and c.expire_at > now else "expired" if c.expire_at < now else "disabled",
            "expire_at": c.expire_at.isoformat() + "Z",
            "days_left": max(0, math.ceil((c.expire_at - now).total_seconds() / 86400)),
            "auto_renew": c.auto_renew,
            "server": {
                "id": server.id if server else None,
                "name": server.name if server else "Удалён",
                "country": server.country_code if server else None,
                "tier": server.tier_level if server else None,
            },
            "plan": {
                "id": plan.id if plan else None,
                "name": plan.name if plan else "Удалён",
                "duration_days": plan.duration_days if plan else None,
            }
        })

    invoices_result = await db.execute(
        select(Invoice).where(Invoice.client_id == client_id).order_by(Invoice.created_at.desc()).limit(50)
    )
    invoices = invoices_result.scalars().all()

    invoices_data = []
    for inv in invoices:
        plan  = await db.get(ServicePlan, inv.plan_id) if inv.plan_id else None
        admin = await db.get(Client, inv.topped_up_by) if inv.topped_up_by else None
        invoices_data.append({
            "id": inv.id,
            "amount": inv.amount,
            "status": inv.status.value,
            "type": "Покупка подписки" if inv.plan_id else "Пополнение баланса",
            "plan": plan.name if plan else None,
            "topped_up_by": admin.email if admin else None,
            "date": inv.created_at.isoformat(),
        })

    return {
        "id": client.id,
        "email": client.email,
        "balance": client.balance,
        "is_banned": client.is_banned,
        "referral_code": client.referral_code,
        "subscriptions": configs_data,
        "payment_history": invoices_data,
    }


# ═══════════════════════════════════════════════
#  КЛИЕНТ — ОБНОВЛЕНИЕ
# ═══════════════════════════════════════════════

class ClientUpdate(BaseModel):
    is_banned: Optional[bool] = None
    balance: Optional[float] = None


@router.patch("/clients/{client_id}")
async def update_client(
    client_id: int,
    body: ClientUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_owner_or_op),
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    _, profile = admin

    if body.is_banned is not None:
        client.is_banned = body.is_banned

    if body.balance is not None:
        # Пополнение баланса — создаём инвойс для истории
        diff = round(body.balance - client.balance, 2)
        if diff != 0:
            db.add(Invoice(
                client_id=client.id,
                amount=abs(diff),
                status=InvoiceStatus.PAID,
                external_id=f"admin_topup_by_{profile.client_id}",
                topped_up_by=profile.client_id,
            ))
        client.balance = round(body.balance, 2)

    await db.commit()
    return {
        "status": "ok",
        "id": client.id,
        "email": client.email,
        "is_banned": client.is_banned,
        "balance": client.balance,
    }


# ═══════════════════════════════════════════════
#  КЛИЕНТ — УДАЛЕНИЕ (только owner)
# ═══════════════════════════════════════════════

@router.delete("/clients/{client_id}")
async def delete_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_only),
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    configs_result = await db.execute(
        select(Config).where(Config.client_id == client_id, Config.is_active == True)
    )
    for config in configs_result.scalars().all():
        server = await db.get(VPNServer, config.server_id)
        if server:
            await delete_marzban_user(
                marzban_url=server.marzban_url,
                admin_username=decrypt(server.mar_admin_user),
                admin_password=decrypt(server.mar_admin_pass),
                username=config.mar_username,
            )
            if server.current_users_count > 0:
                server.current_users_count -= 1

    await db.delete(client)
    await db.commit()
    return {"status": "Клиент удалён"}


# ═══════════════════════════════════════════════
#  ПОДПИСКИ — УПРАВЛЕНИЕ
# ═══════════════════════════════════════════════

@router.patch("/configs/{config_id}/toggle")
async def toggle_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = await db.get(VPNServer, config.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Сервер не найден")

    new_state = not config.is_active
    toggle_result = await toggle_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=decrypt(server.mar_admin_user),
        admin_password=decrypt(server.mar_admin_pass),
        username=config.mar_username,
        active=new_state,
    )

    if not toggle_result["success"]:
        raise HTTPException(status_code=500, detail=f"Ошибка Marzban: {toggle_result['error']}")

    config.is_active = new_state
    await db.commit()
    return {"status": "включена" if new_state else "выключена", "config_id": config_id}


class ConfigExtend(BaseModel):
    days: int


@router.patch("/configs/{config_id}/extend")
async def extend_config(
    config_id: int,
    body: ConfigExtend,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = await db.get(VPNServer, config.server_id)
    extended = False

    if server:
        extend_result = await extend_marzban_user(
            marzban_url=server.marzban_url,
            admin_username=decrypt(server.mar_admin_user),
            admin_password=decrypt(server.mar_admin_pass),
            username=config.mar_username,
            extra_days=body.days,
        )
        if extend_result["success"]:
            new_expire_ts = extend_result.get("new_expire")
            if new_expire_ts:
                config.expire_at = datetime.utcfromtimestamp(new_expire_ts)
            extended = True

    if not extended:
        base = max(config.expire_at, datetime.utcnow())
        config.expire_at = base + timedelta(days=body.days)

    await db.commit()
    return {
        "status": f"Продлено на {body.days} дней",
        "config_id": config_id,
        "new_expire_at": config.expire_at.isoformat() + "Z",
    }


@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_only),
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = await db.get(VPNServer, config.server_id)
    if server:
        await delete_marzban_user(
            marzban_url=server.marzban_url,
            admin_username=decrypt(server.mar_admin_user),
            admin_password=decrypt(server.mar_admin_pass),
            username=config.mar_username,
        )
        if server.current_users_count > 0:
            server.current_users_count -= 1

    await db.delete(config)
    await db.commit()
    return {"status": "Подписка удалена"}


# ═══════════════════════════════════════════════
#  ПЕРЕНОС КЛИЕНТА НА ДРУГОЙ СЕРВЕР (только owner)
# ═══════════════════════════════════════════════

class MigrateConfigRequest(BaseModel):
    target_server_id: int


@router.post("/configs/{config_id}/migrate")
async def migrate_config(
    config_id: int,
    body: MigrateConfigRequest,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_only),
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    target_result = await db.execute(
        select(VPNServer).options(selectinload(VPNServer.tier)).where(VPNServer.id == body.target_server_id)
    )
    target_server = target_result.scalar_one_or_none()
    if not target_server:
        raise HTTPException(status_code=404, detail="Целевой сервер не найден")
    if not target_server.is_active:
        raise HTTPException(status_code=400, detail="Целевой сервер неактивен")
    if target_server.id == config.server_id:
        raise HTTPException(status_code=400, detail="Клиент уже на этом сервере")

    old_server = await db.get(VPNServer, config.server_id)
    if old_server:
        await delete_marzban_user(
            marzban_url=old_server.marzban_url,
            admin_username=decrypt(old_server.mar_admin_user),
            admin_password=decrypt(old_server.mar_admin_pass),
            username=config.mar_username,
        )
        if old_server.current_users_count > 0:
            old_server.current_users_count -= 1

    import json as _json
    days_left  = max(1, math.ceil((config.expire_at - datetime.utcnow()).total_seconds() / 86400))
    inbounds   = _json.loads(target_server.inbounds_json) if target_server.inbounds_json else {}
    mar_result = await create_marzban_user(
        marzban_url=target_server.marzban_url,
        admin_username=decrypt(target_server.mar_admin_user),
        admin_password=decrypt(target_server.mar_admin_pass),
        username=config.mar_username,
        expire_days=days_left,
        inbounds=inbounds,
    )

    if not mar_result["success"]:
        raise HTTPException(status_code=500, detail=f"Ошибка создания на новом сервере: {mar_result['error']}")

    config.server_id        = target_server.id
    config.subscription_url = mar_result.get("subscription_url", "")
    new_links               = mar_result.get("links", [])
    config.vless_link       = new_links[0] if new_links else None
    config.sub_token        = generate_sub_token()
    target_server.current_users_count += 1

    await db.commit()
    return {
        "status": "Клиент перенесён",
        "config_id": config_id,
        "old_server": old_server.name if old_server else "удалён",
        "new_server": target_server.name,
        "days_remaining": days_left,
    }


# ═══════════════════════════════════════════════
#  ОБЗОР (overview)
# ═══════════════════════════════════════════════

@router.get("/overview")
async def get_overview(
    db: AsyncSession = Depends(get_db),
    _=Depends(_any_admin),
):
    total_clients = await db.scalar(select(func.count(Client.id)))
    total_revenue = await db.scalar(
        select(func.sum(Invoice.amount)).where(Invoice.status == InvoiceStatus.PAID)
    )

    servers_result = await db.execute(
        select(VPNServer).options(selectinload(VPNServer.tier)).order_by(VPNServer.tier_level)
    )
    servers_data = []
    for s in servers_result.scalars().all():
        active_configs = await db.scalar(
            select(func.count(Config.id)).where(
                Config.server_id == s.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow()
            )
        )
        servers_data.append({
            "id": s.id, "name": s.name, "ip": s.ip_address,
            "country": s.country_code, "tier": s.tier_level,
            "is_active": s.is_active, "is_online": s.is_online,
            "users_in_db": active_configs,
            "users_counter": s.current_users_count,
            "max_users": s.tier.default_max_users if s.tier else 0,
        })

    return {
        "summary": {"total_clients": total_clients, "total_revenue": round(total_revenue or 0, 2)},
        "servers": servers_data,
    }