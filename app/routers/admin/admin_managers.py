from datetime import datetime
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import require_role, get_admin_from_jwt
from database.database import get_db
from database.models import Client, AdminProfile, AdminRole

router = APIRouter(prefix="/admin/managers", tags=["Admin Managers"])

_owner_only = require_role(AdminRole.OWNER)


class AdminCreate(BaseModel):
    email: str
    role: AdminRole = AdminRole.OPERATOR
    note: Optional[str] = None


class AdminUpdate(BaseModel):
    role: Optional[AdminRole] = None
    is_active: Optional[bool] = None
    note: Optional[str] = None


def fmt_profile(p: AdminProfile) -> dict:
    return {
        "id": p.id,
        "client_id": p.client_id,
        "email": p.client.email if p.client else None,
        "role": p.role.value,
        "is_active": p.is_active,
        "note": p.note,
        "created_at": p.created_at.isoformat(),
        "last_login_at": p.last_login_at.isoformat() if p.last_login_at else None,
        "last_login_ip": p.last_login_ip,
        "created_by_email": p.created_by_client.email if p.created_by_client else None,
    }


@router.get("")
async def list_admins(
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_only),
):
    """Список всех администраторов."""
    result = await db.execute(
        select(AdminProfile).order_by(AdminProfile.created_at)
    )
    profiles = result.scalars().all()

    # Подгружаем клиентов
    for p in profiles:
        if p.client_id:
            p.client = await db.get(Client, p.client_id)
        if p.created_by:
            p.created_by_client = await db.get(Client, p.created_by)
        else:
            p.created_by_client = None

    return [fmt_profile(p) for p in profiles]


@router.post("")
async def add_admin(
    body: AdminCreate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_owner_only),
):
    """Выдать права администратора существующему клиенту."""
    _, owner_profile = admin

    # Ищем клиента по email
    result = await db.execute(select(Client).where(Client.email == body.email))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент с таким email не найден")

    # Проверяем нет ли уже профиля
    existing = await db.execute(
        select(AdminProfile).where(AdminProfile.client_id == client.id)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="У этого клиента уже есть права администратора")

    profile = AdminProfile(
        client_id=client.id,
        role=body.role,
        is_active=True,
        created_by=owner_profile.client_id,
        note=body.note,
    )
    db.add(profile)
    await db.commit()
    await db.refresh(profile)

    profile.client = client
    profile.created_by_client = await db.get(Client, owner_profile.client_id)

    return fmt_profile(profile)


@router.patch("/{profile_id}")
async def update_admin(
    profile_id: int,
    body: AdminUpdate,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_owner_only),
):
    """Изменить роль, активность или заметку администратора."""
    _, owner_profile = admin

    result = await db.execute(select(AdminProfile).where(AdminProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Администратор не найден")

    # Нельзя изменить самого себя
    if profile.client_id == owner_profile.client_id:
        raise HTTPException(status_code=400, detail="Нельзя изменить собственный профиль")

    # Нельзя изменить другого owner
    if profile.role == AdminRole.OWNER and body.role and body.role != AdminRole.OWNER:
        raise HTTPException(status_code=400, detail="Нельзя понизить другого владельца")

    if body.role is not None:
        profile.role = body.role
    if body.is_active is not None:
        profile.is_active = body.is_active
    if body.note is not None:
        profile.note = body.note

    await db.commit()

    profile.client = await db.get(Client, profile.client_id)
    profile.created_by_client = await db.get(Client, profile.created_by) if profile.created_by else None

    return fmt_profile(profile)


@router.delete("/{profile_id}")
async def remove_admin(
    profile_id: int,
    db: AsyncSession = Depends(get_db),
    admin=Depends(_owner_only),
):
    """Полностью убрать права администратора."""
    _, owner_profile = admin

    result = await db.execute(select(AdminProfile).where(AdminProfile.id == profile_id))
    profile = result.scalar_one_or_none()
    if not profile:
        raise HTTPException(status_code=404, detail="Администратор не найден")

    if profile.client_id == owner_profile.client_id:
        raise HTTPException(status_code=400, detail="Нельзя удалить самого себя")

    if profile.role == AdminRole.OWNER:
        raise HTTPException(status_code=400, detail="Нельзя удалить другого владельца")

    await db.delete(profile)
    await db.commit()
    return {"status": "ok"}