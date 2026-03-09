from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from typing import Optional
from datetime import datetime, timedelta

from auth.deps import get_current_admin
from database.database import get_db
from database.models import Client, Config, VPNServer, ServicePlan, Invoice, InvoiceStatus
from app.services.marzban_service import toggle_marzban_user, delete_marzban_user, create_marzban_user
from app.services.crypto_service import decrypt
from app.services.link_generator import generate_sub_token

router = APIRouter()


# ═══════════════════════════════════════════════
#  КЛИЕНТЫ
# ═══════════════════════════════════════════════

@router.get("/clients")
async def get_all_clients(db: AsyncSession = Depends(get_db), _=Depends(get_current_admin)):
    result = await db.execute(select(Client).order_by(Client.id))
    clients = result.scalars().all()

    response = []
    for c in clients:
        configs_result = await db.execute(
            select(func.count(Config.id)).where(
                Config.client_id == c.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow()
            )
        )
        active_count = configs_result.scalar()
        response.append({
            "id": c.id,
            "email": c.email,
            "balance": c.balance,
            "is_admin": c.is_admin,
            "is_banned": c.is_banned,
            "active_subscriptions": active_count,
        })

    return response


@router.get("/clients/{client_id}")
async def get_client_profile(
    client_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
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
        plan = await db.get(ServicePlan, c.plan_id)
        now = datetime.utcnow()
        configs_data.append({
            "id": c.id,
            "marzban_username": c.marzban_username,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "status": "active" if c.is_active and c.expire_at > now else "expired" if c.expire_at < now else "disabled",
            "expire_at": c.expire_at.isoformat(),
            "days_left": max(0, (c.expire_at - now).days),
            "auto_renew": c.auto_renew,
            "server": {
                "id": server.id if server else None,
                "name": server.name if server else "Удалён",
                "ip": server.ip_address if server else None,
                "country": server.country_code if server else None,
                "tier": server.tier_level if server else None,
            },
            "plan": {
                "id": plan.id if plan else None,
                "name": plan.name if plan else "Удалён",
                "months": plan.months if plan else None,
            }
        })

    invoices_result = await db.execute(
        select(Invoice).where(Invoice.client_id == client_id).order_by(Invoice.created_at.desc())
    )
    invoices = invoices_result.scalars().all()

    invoices_data = []
    for inv in invoices:
        plan = await db.get(ServicePlan, inv.plan_id) if inv.plan_id else None
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
        "is_admin": client.is_admin,
        "is_banned": client.is_banned,
        "subscriptions": configs_data,
        "payment_history": invoices_data,
    }


class ClientUpdate(BaseModel):
    is_admin: Optional[bool] = None
    is_banned: Optional[bool] = None
    balance: Optional[float] = None


@router.patch("/clients/{client_id}")
async def update_client(
    client_id: int,
    body: ClientUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    if body.is_admin is not None:
        client.is_admin = body.is_admin
    if body.is_banned is not None:
        client.is_banned = body.is_banned
    if body.balance is not None:
        client.balance = round(body.balance, 2)

    await db.commit()
    return {
        "status": "Клиент обновлён",
        "id": client.id,
        "email": client.email,
        "is_admin": client.is_admin,
        "is_banned": client.is_banned,
        "balance": client.balance,
    }


@router.delete("/clients/{client_id}")
async def delete_client(
    client_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
):
    result = await db.execute(select(Client).where(Client.id == client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    configs_result = await db.execute(
        select(Config).where(Config.client_id == client_id, Config.is_active == True)
    )
    configs = configs_result.scalars().all()

    for config in configs:
        server = await db.get(VPNServer, config.server_id)
        if server:
            await delete_marzban_user(
                ip=server.ip_address,
                ssh_port=server.ssh_port,
                marzban_port=server.marzban_port,
                mar_admin_user=decrypt(server.mar_admin_user),
                mar_admin_pass=decrypt(server.mar_admin_pass),
                marzban_username=config.marzban_username
            )
            if server.current_users_count > 0:
                server.current_users_count -= 1

    await db.delete(client)
    await db.commit()
    return {"status": "Клиент удалён"}


# ═══════════════════════════════════════════════
#  ПОДПИСКИ (КОНФИГИ)
# ═══════════════════════════════════════════════

@router.patch("/configs/{config_id}/toggle")
async def toggle_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = await db.get(VPNServer, config.server_id)
    if not server:
        raise HTTPException(status_code=404, detail="Сервер подписки не найден")

    new_state = not config.is_active

    toggle_result = await toggle_marzban_user(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=decrypt(server.mar_admin_user),
        mar_admin_pass=decrypt(server.mar_admin_pass),
        marzban_username=config.marzban_username,
        active=new_state
    )

    if not toggle_result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Marzban не ответил: {toggle_result['error']}. Статус в БД не изменён."
        )

    config.is_active = new_state
    await db.commit()
    return {
        "status": "включена" if new_state else "выключена",
        "config_id": config_id,
        "marzban_username": config.marzban_username,
    }


class ConfigExtend(BaseModel):
    days: int


@router.patch("/configs/{config_id}/extend")
async def extend_config(
    config_id: int,
    body: ConfigExtend,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    base = max(config.expire_at, datetime.utcnow())
    config.expire_at = base + timedelta(days=body.days)

    await db.commit()
    return {
        "status": f"Подписка продлена на {body.days} дней",
        "config_id": config_id,
        "new_expire_at": config.expire_at.isoformat(),
    }


@router.delete("/configs/{config_id}")
async def delete_config(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_current_admin)
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    server = await db.get(VPNServer, config.server_id)
    if server:
        await delete_marzban_user(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            mar_admin_user=decrypt(server.mar_admin_user),
            mar_admin_pass=decrypt(server.mar_admin_pass),
            marzban_username=config.marzban_username
        )
        if server.current_users_count > 0:
            server.current_users_count -= 1

    await db.delete(config)
    await db.commit()
    return {"status": "Подписка удалена"}


# ═══════════════════════════════════════════════
#  СЕРВЕРЫ — ОБЩИЙ ОБЗОР
# ═══════════════════════════════════════════════

@router.get("/overview")
async def get_overview(db: AsyncSession = Depends(get_db), _=Depends(get_current_admin)):
    total_clients = await db.scalar(select(func.count(Client.id)))
    total_revenue = await db.scalar(
        select(func.sum(Invoice.amount)).where(Invoice.status == InvoiceStatus.PAID)
    )

    servers_result = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .order_by(VPNServer.tier_level)
    )
    servers = servers_result.scalars().all()

    servers_data = []
    for s in servers:
        active_configs = await db.scalar(
            select(func.count(Config.id)).where(
                Config.server_id == s.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow()
            )
        )
        servers_data.append({
            "id": s.id,
            "name": s.name,
            "ip": s.ip_address,
            "country": s.country_code,
            "tier": s.tier_level,
            "is_active": s.is_active,
            "users_in_db": active_configs,
            "users_counter": s.current_users_count,
            "max_users": s.tier.default_max_users if s.tier else 0,
            "marzban_port": s.marzban_port,
            "ssh_port": s.ssh_port,
        })

    plans_result = await db.execute(select(ServicePlan).order_by(ServicePlan.tier_level))
    plans = plans_result.scalars().all()

    plans_data = []
    for p in plans:
        active = await db.scalar(
            select(func.count(Config.id)).where(
                Config.plan_id == p.id,
                Config.is_active == True,
                Config.expire_at > datetime.utcnow()
            )
        )
        plans_data.append({
            "id": p.id,
            "name": p.name,
            "tier": p.tier_level,
            "price": p.price,
            "months": p.months,
            "active_subscribers": active,
        })

    return {
        "summary": {
            "total_clients": total_clients,
            "total_revenue": round(total_revenue or 0, 2),
        },
        "servers": servers_data,
        "plans": plans_data,
    }


# ═══════════════════════════════════════════════
#  ПЕРЕНОС КЛИЕНТА НА ДРУГОЙ СЕРВЕР
# ═══════════════════════════════════════════════

class MigrateConfigRequest(BaseModel):
    target_server_id: int


@router.post("/configs/{config_id}/migrate", dependencies=[Depends(get_current_admin)])
async def migrate_config(
    config_id: int,
    body: MigrateConfigRequest,
    db: AsyncSession = Depends(get_db)
):
    result = await db.execute(select(Config).where(Config.id == config_id))
    config = result.scalar_one_or_none()
    if not config:
        raise HTTPException(status_code=404, detail="Подписка не найдена")

    # Загружаем целевой сервер вместе с тиром
    target_result = await db.execute(
        select(VPNServer)
        .options(selectinload(VPNServer.tier))
        .where(VPNServer.id == body.target_server_id)
    )
    target_server = target_result.scalar_one_or_none()
    if not target_server:
        raise HTTPException(status_code=404, detail="Целевой сервер не найден")

    if not target_server.is_active:
        raise HTTPException(status_code=400, detail="Целевой сервер неактивен")

    if target_server.id == config.server_id:
        raise HTTPException(status_code=400, detail="Клиент уже на этом сервере")

    max_users = target_server.tier.default_max_users if target_server.tier else 0
    if target_server.current_users_count >= max_users:
        raise HTTPException(status_code=400, detail="Целевой сервер заполнен")

    old_server = await db.get(VPNServer, config.server_id)

    # Шаг 1 — удаляем со старого сервера
    if old_server:
        await delete_marzban_user(
            ip=old_server.ip_address,
            ssh_port=old_server.ssh_port,
            marzban_port=old_server.marzban_port,
            mar_admin_user=decrypt(old_server.mar_admin_user),
            mar_admin_pass=decrypt(old_server.mar_admin_pass),
            marzban_username=config.marzban_username
        )
        if old_server.current_users_count > 0:
            old_server.current_users_count -= 1

    # Шаг 2 — создаём на новом сервере
    days_left = max(1, (config.expire_at - datetime.utcnow()).days)

    marzban_result = await create_marzban_user(
        ip=target_server.ip_address,
        ssh_port=target_server.ssh_port,
        marzban_port=target_server.marzban_port,
        mar_admin_user=decrypt(target_server.mar_admin_user),
        mar_admin_pass=decrypt(target_server.mar_admin_pass),
        marzban_username=config.marzban_username,
        expire_days=days_left
    )

    if not marzban_result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось создать юзера на новом сервере: {marzban_result['error']}"
        )

    # Шаг 3 — обновляем Config
    config.server_id  = target_server.id
    config.vless_link = marzban_result.get("vless_link")
    config.sub_token  = generate_sub_token()

    # Шаг 4 — счётчик нового сервера
    target_server.current_users_count += 1

    await db.commit()
 
    return {
        "status": "Клиент перенесён",
        "config_id": config_id,
        "old_server": old_server.name if old_server else "удалён",
        "new_server": target_server.name,
        "new_sub_url": f"/sub/{config.sub_token}",
        "days_remaining": days_left,
    } 