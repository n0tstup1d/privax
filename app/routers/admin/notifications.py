from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, and_
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_admin
from database.database import get_db
from database.models import Client, Config, Notification

router = APIRouter()


class NotificationSend(BaseModel):
    title: str
    message: str

    # Фильтры получателей — можно комбинировать
    # Если ничего не указано — рассылка всем
    client_ids: Optional[List[int]] = None      # конкретные клиенты по id
    only_active: Optional[bool] = None          # только с активной подпиской
    only_expired: Optional[bool] = None         # только с истёкшей подпиской
    plan_id: Optional[int] = None               # только подписчики конкретного тарифа


async def _get_recipients(body: NotificationSend, db: AsyncSession) -> List[Client]:
    """
    Собирает список получателей по фильтрам.
    Фильтры можно комбинировать — например only_active + plan_id.
    """
    # Конкретные клиенты по id — игнорируем остальные фильтры
    if body.client_ids:
        result = await db.execute(
            select(Client).where(Client.id.in_(body.client_ids))
        )
        return result.scalars().all()

    now = datetime.utcnow()

    # Если нужна фильтрация по подпискам
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

        result = await db.execute(
            select(Client).where(Client.id.in_(client_ids))
        )
        return result.scalars().all()

    # Без фильтров — все клиенты
    result = await db.execute(select(Client))
    return result.scalars().all()


@router.post("/notifications/send", dependencies=[Depends(get_current_admin)])
async def send_notification(
    body: NotificationSend,
    db: AsyncSession = Depends(get_db)
):
    """
    Отправляет уведомление в личный кабинет.

    Примеры фильтров:
    {} — всем
    {"client_ids": [1, 5, 12]} — конкретным клиентам
    {"only_active": true} — только активным подписчикам
    {"only_expired": true} — только тем у кого истекла подписка
    {"only_active": true, "plan_id": 2} — активным подписчикам тарифа #2
    """
    recipients = await _get_recipients(body, db)

    if not recipients:
        raise HTTPException(status_code=404, detail="Нет получателей по заданным фильтрам")

    for client in recipients:
        db.add(Notification(
            client_id=client.id,
            title=body.title,
            message=body.message,
        ))

    await db.commit()

    return {
        "status": "Рассылка выполнена",
        "recipients": len(recipients),
        "emails": [c.email for c in recipients]
    }


@router.get("/notifications", dependencies=[Depends(get_current_admin)])
async def list_notifications(db: AsyncSession = Depends(get_db)):
    """Последние 100 уведомлений."""
    result = await db.execute(
        select(Notification).order_by(Notification.created_at.desc()).limit(100)
    )
    notifs = result.scalars().all()

    return [
        {
            "id": n.id,
            "client_id": n.client_id,
            "title": n.title,
            "message": n.message,
            "is_read": n.is_read,
            "created_at": n.created_at.isoformat(),
        }
        for n in notifs
    ]