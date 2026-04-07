from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import require_role, get_admin_from_jwt
from database.database import get_db
from database.models import Client, Config, Notification, AdminRole

router = APIRouter()

_owner_or_op = require_role(AdminRole.OWNER, AdminRole.OPERATOR)
_any_admin   = get_admin_from_jwt


class NotificationSend(BaseModel):
    title: str
    message: str
    client_ids: Optional[List[int]] = None
    only_active: Optional[bool] = None
    only_expired: Optional[bool] = None
    plan_id: Optional[int] = None


async def _get_recipients(body: NotificationSend, db: AsyncSession) -> List[Client]:
    if body.client_ids:
        result = await db.execute(select(Client).where(Client.id.in_(body.client_ids)))
        return result.scalars().all()

    now = datetime.utcnow()

    if body.only_active or body.only_expired or body.plan_id:
        conditions = []
        if body.only_active:
            conditions += [Config.is_active == True, Config.expire_at > now]
        if body.only_expired:
            conditions.append(Config.expire_at < now)
        if body.plan_id:
            conditions.append(Config.plan_id == body.plan_id)

        configs_result = await db.execute(
            select(Config.client_id).where(and_(*conditions)).distinct()
        )
        client_ids = [row[0] for row in configs_result.all()]
        if not client_ids:
            return []

        result = await db.execute(select(Client).where(Client.id.in_(client_ids)))
        return result.scalars().all()

    result = await db.execute(select(Client))
    return result.scalars().all()


@router.post("/admin/notifications/send")
async def send_notification(
    body: NotificationSend,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_op),
):
    recipients = await _get_recipients(body, db)
    if not recipients:
        raise HTTPException(status_code=404, detail="Нет получателей по заданным фильтрам")

    for client in recipients:
        db.add(Notification(client_id=client.id, title=body.title, message=body.message))

    await db.commit()
    return {
        "status": "Рассылка выполнена",
        "recipients": len(recipients),
        "emails": [c.email for c in recipients],
    }


@router.get("/admin/notifications")
async def list_notifications(
    db: AsyncSession = Depends(get_db),
    _=Depends(_any_admin),
    page: int = 1,
    page_size: int = 50,
):
    """Последние уведомления с пагинацией."""
    total = await db.scalar(select(func.count(Notification.id)))
    result = await db.execute(
        select(Notification).order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    )
    notifs = result.scalars().all()

    # Подгружаем email клиентов
    client_ids = list({n.client_id for n in notifs})
    clients = {}
    if client_ids:
        res = await db.execute(select(Client).where(Client.id.in_(client_ids)))
        clients = {c.id: c.email for c in res.scalars().all()}

    return {
        "total": total,
        "pages": -(-total // page_size),
        "items": [
            {
                "id": n.id,
                "client_id": n.client_id,
                "client_email": clients.get(n.client_id),
                "title": n.title,
                "message": n.message,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat(),
            }
            for n in notifs
        ],
    }