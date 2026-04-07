"""
admin_feed.py — аудит-лог действий админов + лента уведомлений.

Эндпоинты:
    GET  /admin/audit          — аудит-лог с фильтрами
    GET  /admin/feed           — лента уведомлений для админов
    GET  /admin/feed/unread    — количество непрочитанных
    POST /admin/feed/read-all  — пометить все прочитанными
    POST /admin/feed/{id}/read — пометить одно прочитанным
"""
import json
from typing import Optional
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, func, and_
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_admin_from_jwt, require_role
from database.database import get_db
from database.models import (
    AdminAuditLog, AdminActionType, AdminRole,
    AdminNotification, AdminNotifType,
    Client, AdminProfile,
)

router = APIRouter(prefix="/admin", tags=["Admin Feed"])

_any_admin = get_admin_from_jwt


# ═══════════════════════════════════════════════
#  АУДИТ-ЛОГ
# ═══════════════════════════════════════════════

@router.get("/audit")
async def get_audit_log(
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_admin),
    page: int = 1,
    page_size: int = 50,
    admin_id: Optional[int] = None,
    action: Optional[str] = None,
    target_type: Optional[str] = None,
    days: Optional[int] = None,
):
    """
    Аудит-лог действий админов.

    Фильтры:
      - admin_id — показать действия конкретного админа
      - action — тип действия (server_add, client_ban, ...)
      - target_type — тип объекта (server, client, plan, ...)
      - days — за последние N дней
    """
    query = select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc())

    conditions = []
    if admin_id:
        conditions.append(AdminAuditLog.admin_id == admin_id)
    if action:
        try:
            conditions.append(AdminAuditLog.action == AdminActionType(action))
        except ValueError:
            pass
    if target_type:
        conditions.append(AdminAuditLog.target_type == target_type)
    if days:
        since = datetime.utcnow() - timedelta(days=days)
        conditions.append(AdminAuditLog.created_at >= since)

    if conditions:
        query = query.where(and_(*conditions))

    # Общее количество
    count_query = select(func.count(AdminAuditLog.id))
    if conditions:
        count_query = count_query.where(and_(*conditions))
    total = await db.scalar(count_query) or 0

    # Пагинация
    result = await db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    )
    entries = result.scalars().all()

    # Подгружаем имена админов
    admin_ids = list({e.admin_id for e in entries})
    admins = {}
    if admin_ids:
        res = await db.execute(select(Client).where(Client.id.in_(admin_ids)))
        admins = {c.id: c.email for c in res.scalars().all()}

    return {
        "total": total,
        "page": page,
        "pages": -(-total // page_size) if total else 0,
        "items": [
            {
                "id": e.id,
                "admin_id": e.admin_id,
                "admin_email": admins.get(e.admin_id),
                "action": e.action.value,
                "description": e.description,
                "details": json.loads(e.details_json) if e.details_json else None,
                "target_type": e.target_type,
                "target_id": e.target_id,
                "ip_address": e.ip_address,
                "created_at": e.created_at.isoformat() + "Z",
            }
            for e in entries
        ],
    }


@router.get("/audit/actions")
async def get_available_actions(
    _=Depends(_any_admin),
):
    """Список всех типов действий для фильтра."""
    return [
        {"value": a.value, "label": a.value.replace("_", " ").title()}
        for a in AdminActionType
    ]


# ═══════════════════════════════════════════════
#  ЛЕНТА УВЕДОМЛЕНИЙ АДМИНОВ
# ═══════════════════════════════════════════════

@router.get("/feed")
async def get_admin_feed(
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_admin),
    page: int = 1,
    page_size: int = 30,
    unread_only: bool = False,
):
    """Лента уведомлений для админов."""
    _, profile = admin
    role = profile.role.value

    query = select(AdminNotification).order_by(AdminNotification.created_at.desc())

    conditions = []
    # Фильтр по роли: показываем уведомления для всех (for_role=null) + для моей роли
    conditions.append(
        (AdminNotification.for_role.is_(None)) | (AdminNotification.for_role == role)
    )
    if unread_only:
        conditions.append(AdminNotification.is_read == False)

    if conditions:
        query = query.where(and_(*conditions))

    total = await db.scalar(
        select(func.count(AdminNotification.id)).where(and_(*conditions))
    ) or 0

    result = await db.execute(
        query.offset((page - 1) * page_size).limit(page_size)
    )
    notifs = result.scalars().all()

    return {
        "total": total,
        "page": page,
        "pages": -(-total // page_size) if total else 0,
        "items": [
            {
                "id": n.id,
                "type": n.notif_type.value,
                "title": n.title,
                "message": n.message,
                "is_read": n.is_read,
                "target_type": n.target_type,
                "target_id": n.target_id,
                "created_at": n.created_at.isoformat() + "Z",
            }
            for n in notifs
        ],
    }


@router.get("/feed/unread")
async def get_unread_count(
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_admin),
):
    """Количество непрочитанных уведомлений."""
    _, profile = admin
    role = profile.role.value

    count = await db.scalar(
        select(func.count(AdminNotification.id)).where(
            AdminNotification.is_read == False,
            (AdminNotification.for_role.is_(None)) | (AdminNotification.for_role == role),
        )
    ) or 0

    return {"unread": count}


@router.post("/feed/read-all")
async def mark_all_read(
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_admin),
):
    """Помечает все уведомления прочитанными."""
    _, profile = admin
    role = profile.role.value

    result = await db.execute(
        select(AdminNotification).where(
            AdminNotification.is_read == False,
            (AdminNotification.for_role.is_(None)) | (AdminNotification.for_role == role),
        )
    )
    for n in result.scalars().all():
        n.is_read = True

    await db.commit()
    return {"status": "ok"}


@router.post("/feed/{notif_id}/read")
async def mark_one_read(
    notif_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_any_admin),
):
    """Помечает одно уведомление прочитанным."""
    notif = await db.get(AdminNotification, notif_id)
    if notif:
        notif.is_read = True
        await db.commit()
    return {"status": "ok"}