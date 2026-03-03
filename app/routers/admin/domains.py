from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel
from typing import Optional
from schemas import DomainCreate, DomainUpdate
from database.database import get_db
from database.models import TrustedDomain
from auth.deps import get_current_admin, get_current_user
from database.models import Client

router = APIRouter()

@router.post("/domains/add")
async def add_domain(
    body: DomainCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Client = Depends(get_current_admin)
):
    existing = await db.execute(
        select(TrustedDomain).where(TrustedDomain.domain == body.domain)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Домен уже существует")

    domain = TrustedDomain(
    domain=body.domain,
    country_code=body.country_code.upper() if body.country_code else None,
    is_active=True,
    added_by=current_admin.id
    )
    db.add(domain)
    await db.commit()
    await db.refresh(domain)

    return {
        "status": "Домен добавлен",
        "id": domain.id,
        "domain": domain.domain
    }


@router.get("/domains")
async def list_domains(
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(
        select(TrustedDomain).order_by(TrustedDomain.id)
    )
    domains = result.scalars().all()

    return [
        {
            "id": d.id,
            "domain": d.domain,
            "is_active": d.is_active,
            "created_at": d.created_at.isoformat(),
        }
        for d in domains
    ]


@router.patch("/domains/{domain_id}")
async def update_domain(
    domain_id: int,
    body: DomainUpdate,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(
        select(TrustedDomain).where(TrustedDomain.id == domain_id)
    )
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Домен не найден")

    if body.domain is not None:
        # Проверяем что такой домен не занят
        existing = await db.execute(
            select(TrustedDomain).where(
                TrustedDomain.domain == body.domain,
                TrustedDomain.id != domain_id
            )
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Такой домен уже существует")
        domain.domain = body.domain

    if body.is_active is not None:
        domain.is_active = body.is_active

    await db.commit()
    await db.refresh(domain)

    return {
        "status": "Домен обновлён",
        "id": domain.id,
        "domain": domain.domain,
        "is_active": domain.is_active
    }


@router.delete("/domains/{domain_id}")
async def delete_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(get_current_admin)
):
    result = await db.execute(
        select(TrustedDomain).where(TrustedDomain.id == domain_id)
    )
    domain = result.scalar_one_or_none()
    if not domain:
        raise HTTPException(status_code=404, detail="Домен не найден")

    await db.delete(domain)
    await db.commit()

    return {"status": "Домен удалён"}