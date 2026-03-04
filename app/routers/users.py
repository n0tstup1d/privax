from datetime import datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select
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
    """
    Полный профиль клиента — всё что нужно приложению на главном экране:
    баланс, активные подписки, история платежей, уведомления.
    """
    now = datetime.utcnow()

    # Подписки
    configs_result = await db.execute(
        select(Config)
        .where(Config.client_id == current_user.id)
        .order_by(Config.expire_at.desc())
    )
    configs = configs_result.scalars().all()

    subscriptions = []
    for c in configs:
        plan = await db.get(ServicePlan, c.plan_id)
        subscriptions.append({
            "id": c.id,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "plan": plan.name if plan else "Неизвестен",
            "expires_at": c.expire_at.isoformat(),
            "days_left": max(0, (c.expire_at - now).days),
            "expired": c.expire_at < now,
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
        })

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
            "id": inv.id,
            "amount": inv.amount,
            "type": "Покупка подписки" if inv.plan_id else "Пополнение баланса",
            "plan": plan.name if plan else None,
            "status": inv.status.value,
            "date": inv.created_at.isoformat(),
        })

    # Непрочитанные уведомления
    notif_result = await db.execute(
        select(Notification)
        .where(
            Notification.client_id == current_user.id,
            Notification.is_read == False
        )
        .order_by(Notification.created_at.desc())
    )
    notifications = notif_result.scalars().all()

    unread_notifications = [
        {
            "id": n.id,
            "title": n.title,
            "message": n.message,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifications
    ]

    return {
        "id": current_user.id,
        "email": current_user.email,
        "balance": current_user.balance,
        "subscriptions": {
            "active":  [s for s in subscriptions if s["is_active"] and not s["expired"]],
            "expired": [s for s in subscriptions if s["expired"] or not s["is_active"]],
        },
        "payment_history": history,
        "unread_notifications": unread_notifications,
    }


@router.post("/notifications/{notif_id}/read")
async def mark_notification_read(
    notif_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """Помечает уведомление как прочитанное."""
    notif = await db.get(Notification, notif_id)
    if not notif or notif.client_id != current_user.id:
        return {"status": "ok"}  # тихо игнорируем — не раскрываем существование

    notif.is_read = True
    await db.commit()
    return {"status": "ok"}