from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from database.database import get_db
from database.models import ServicePlan, ServerTier, AdminRole
from schemas import ServicePlanCreate, ServicePlanUpdate, ServicePlanResponse
from auth.deps import require_role, get_admin_from_jwt
from typing import List

router = APIRouter()

_owner_or_dev = require_role(AdminRole.OWNER, AdminRole.DEVELOPER)
_any_admin    = get_admin_from_jwt


# ─── ТАРИФЫ — управление ─────────────────────

@router.post("/admin/plans/add")
async def create_plan(
    body: ServicePlanCreate,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_dev),
):
    tier = await db.execute(select(ServerTier).where(ServerTier.level == body.tier_level))
    if not tier.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Тир level={body.tier_level} не найден")
    new_plan = ServicePlan(**body.model_dump())
    db.add(new_plan)
    await db.commit()
    await db.refresh(new_plan)
    return {"status": "Тариф создан", "plan_id": new_plan.id}


@router.patch("/admin/plans/{plan_id}")
async def update_plan(
    plan_id: int,
    body: ServicePlanUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(_owner_or_dev),
):
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(plan, field, value)
    await db.commit()
    return {"status": "Тариф обновлён"}


@router.delete("/admin/plans/{plan_id}")
async def delete_plan(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role(AdminRole.OWNER)),
):
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")
    await db.delete(plan)
    await db.commit()
    return {"status": "Тариф удалён"}


# ─── ТАРИФЫ — публичный список ────────────────

@router.get("/plans", response_model=List[ServicePlanResponse])
async def get_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .where(ServicePlan.is_hidden == False)
        .order_by(ServicePlan.tier_level, ServicePlan.duration_days)
    )
    return [ServicePlanResponse.from_orm_with_price(p) for p in result.scalars().all()]


# ─── ТАРИФЫ — admin список (включая скрытые) ─

@router.get("/admin/plans")
async def get_all_plans(
    db: AsyncSession = Depends(get_db),
    _=Depends(_any_admin),
):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .order_by(ServicePlan.tier_level, ServicePlan.duration_days)
    )
    plans = result.scalars().all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "display_name": p.display_name,
            "description": p.description,
            "tier_level": p.tier_level,
            "tier_name": p.tier.display_name if p.tier else None,
            "price": p.price,
            "duration_days": p.duration_days,
            "discount_percent": p.discount_percent,
            "final_price": round(p.price * (1 - p.discount_percent / 100), 2),
            "is_hidden": p.is_hidden,
            "purchase_limit": p.purchase_limit,
        }
        for p in plans
    ]