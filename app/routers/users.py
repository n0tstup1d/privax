import math
from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, Config, Invoice, ServicePlan, Notification

router = APIRouter()


@router.get("/me")
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    now = datetime.utcnow()

    # Конфиги — безопасно, без зависимости от новых колонок
    configs_result = await db.execute(
        select(Config)
        .options(
            selectinload(Config.server),
            selectinload(Config.plan).selectinload(ServicePlan.tier),
        )
        .where(Config.client_id == current_user.id)
        .order_by(Config.expire_at.desc())
    )
    configs = configs_result.scalars().all()

    # Группируем по group_id. getattr везде — защита от незапущенных миграций
    groups: dict = {}
    for c in configs:
        plan = c.plan
        gid  = getattr(c, 'group_id', None) or str(c.id)

        if gid not in groups:
            expired = c.expire_at < now
            devices_limit = plan.tier.max_sessions if plan and plan.tier else 1
            groups[gid] = {
                "group_id":           gid,
                "plan":               plan.name if plan else "Неизвестен",
                "plan_id":            plan.id if plan else None,
                "plan_duration_days": plan.duration_days if plan else None,
                "tier_level":         plan.tier_level if plan else None,
                "expires_at":         c.expire_at.isoformat() + "Z",
                "days_left":          max(0, math.floor((c.expire_at - now).total_seconds() / 86400)),
                "expired":            expired,
                "is_active":          c.is_active and not expired,
                "max_devices": devices_limit,
                "maxDevices": devices_limit,
                "device_limit": devices_limit,
                "devices_limit": devices_limit,
                "max_sessions": devices_limit,
                "auto_renew":         getattr(c, "auto_renew", False),
                "grace_period_end":    c.grace_period_end.isoformat() + "Z" if getattr(c, "grace_period_end", None) else None,
                "next_reset_at":       c.last_reset_at.isoformat() + "Z" if getattr(c, "last_reset_at", None) else None,
                "devices":    [],
            }

        dev_idx  = getattr(c, 'device_index', len(groups[gid]["devices"]) + 1)
        dev_name = getattr(c, 'device_name', None) or f"Устройство {dev_idx}"

        groups[gid]["devices"].append({
            "id":           c.id,
            "device_index": dev_idx,
            "device_name":  dev_name,
            "vless_link":   c.vless_link,
            "sub_url":      f"/sub/{c.sub_token}" if c.sub_token else None,
            "is_active":    c.is_active,
            "country_code": c.server.country_code if c.server else "?",
        })

    all_groups = list(groups.values())
    for g in all_groups:
        devices_used = len(g["devices"])
        devices_total = g.get("max_devices") or devices_used
        g["devices_used"] = devices_used
        g["devicesUsed"] = devices_used
        g["devices_connected"] = devices_used
        g["devicesConnected"] = devices_used
        g["devices_total"] = devices_total
        g["devicesTotal"] = devices_total
        g["devices_ratio"] = f"{devices_used}/{devices_total}"

    # История платежей (последние 20)
    invoices_result = await db.execute(
        select(Invoice)
        .where(Invoice.client_id == current_user.id)
        .order_by(Invoice.created_at.desc())
        .limit(20)
    )
    invoices = invoices_result.scalars().all()

    history = []
    for inv in invoices:
        plan = await db.get(ServicePlan, inv.plan_id) if inv.plan_id else None
        history.append({
            "id":     inv.id,
            "amount": inv.amount,
            "type":   "Покупка подписки" if inv.plan_id else "Пополнение баланса",
            "plan":   plan.name if plan else None,
            "status": inv.status.value,
            "date":   inv.created_at.isoformat(),
        })

    # Уведомления
    notif_result = await db.execute(
        select(Notification)
        .where(Notification.client_id == current_user.id, Notification.is_read == False)
        .order_by(Notification.created_at.desc())
    )
    notifications = notif_result.scalars().all()

    unread_notifications = [
        {"id": n.id, "title": n.title, "message": n.message, "created_at": n.created_at.isoformat()}
        for n in notifications
    ]

    return {
        "id":            current_user.id,
        "email":         current_user.email,
        "balance":       current_user.balance,
        "referred_by_id": current_user.referred_by_id,
        "subscriptions": {
            "active":  [g for g in all_groups if g["is_active"]],
            "expired": [g for g in all_groups if not g["is_active"]],
        },
        "payment_history":      history,
        "unread_notifications": unread_notifications,
    }


@router.get("/notifications")
async def get_notifications(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Notification)
        .where(Notification.client_id == current_user.id)
        .order_by(Notification.created_at.desc())
    )
    notifications = result.scalars().all()
    return [
        {
            "id":         n.id,
            "title":      n.title,
            "message":    n.message,
            "is_read":    n.is_read,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifications
    ]


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Notification)
        .where(Notification.client_id == current_user.id, Notification.is_read == False)
    )
    for n in result.scalars().all():
        n.is_read = True
    await db.commit()
    return {"status": "ok"}


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    notif = await db.get(Notification, notif_id)
    if not notif or notif.client_id != current_user.id:
        return {"status": "ok"}
    notif.is_read = True
    await db.commit()
    return {"status": "ok"}