"""
event_service.py — запись событий подписки, аудит-лога админов и админ-уведомлений.

Использование:

    from app.services.event_service import log_sub_event, log_admin_action, notify_admins

    # Запись события подписки
    await log_sub_event(db, config_id=1, client_id=1,
        event_type=SubscriptionEventType.PURCHASED,
        description="Подписка куплена: план Silver 30 дней",
        details={"plan": "Silver", "price": 199.0},
    )

    # Запись действия админа
    await log_admin_action(db, admin_id=1,
        action=AdminActionType.CLIENT_BAN,
        description="Клиент user@mail.ru заблокирован",
        target_type="client", target_id=42,
        ip="1.2.3.4",
    )

    # Уведомление для админов
    await notify_admins(db,
        notif_type=AdminNotifType.NEW_TICKET,
        title="Новый тикет от user@mail.ru",
        message="Тема: Не работает подключение",
        target_type="ticket", target_id=5,
    )
"""
import json
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    SubscriptionEvent, SubscriptionEventType,
    AdminAuditLog, AdminActionType,
    AdminNotification, AdminNotifType,
    Notification, NotificationType,
)

logger = logging.getLogger("privax.events")


# ── События подписки ──────────────────────────────────────────

async def log_sub_event(
    db: AsyncSession,
    config_id: int,
    client_id: int,
    event_type: SubscriptionEventType,
    description: str,
    details: Optional[dict] = None,
    initiated_by: Optional[int] = None,
) -> SubscriptionEvent:
    """Записывает событие в историю подписки."""
    event = SubscriptionEvent(
        config_id=config_id,
        client_id=client_id,
        event_type=event_type,
        description=description,
        details_json=json.dumps(details, ensure_ascii=False) if details else None,
        initiated_by=initiated_by,
    )
    db.add(event)
    logger.info(f"SubEvent [{event_type.value}] config={config_id} client={client_id}: {description}")
    return event


# ── Аудит-лог админов ─────────────────────────────────────────

async def log_admin_action(
    db: AsyncSession,
    admin_id: int,
    action: AdminActionType,
    description: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    details: Optional[dict] = None,
    ip: Optional[str] = None,
) -> AdminAuditLog:
    """Записывает действие админа в аудит-лог."""
    entry = AdminAuditLog(
        admin_id=admin_id,
        action=action,
        description=description,
        details_json=json.dumps(details, ensure_ascii=False) if details else None,
        target_type=target_type,
        target_id=target_id,
        ip_address=ip,
    )
    db.add(entry)
    logger.info(f"Audit [{action.value}] admin={admin_id}: {description}")
    return entry


# ── Уведомления для админов ────────────────────────────────────

async def notify_admins(
    db: AsyncSession,
    notif_type: AdminNotifType,
    title: str,
    message: str,
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    for_role: Optional[str] = None,
) -> AdminNotification:
    """Создаёт уведомление в ленте админов."""
    notif = AdminNotification(
        notif_type=notif_type,
        title=title,
        message=message,
        target_type=target_type,
        target_id=target_id,
        for_role=for_role,
    )
    db.add(notif)
    return notif


# ── Уведомление клиенту ───────────────────────────────────────

async def notify_client(
    db: AsyncSession,
    client_id: int,
    notif_type: NotificationType,
    title: str,
    message: str,
    config_id: Optional[int] = None,
) -> Notification:
    """Создаёт уведомление для клиента."""
    notif = Notification(
        client_id=client_id,
        config_id=config_id,
        notification_type=notif_type,
        title=title,
        message=message,
    )
    db.add(notif)
    return notif