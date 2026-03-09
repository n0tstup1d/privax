from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from auth.deps import get_current_admin
from database.database import get_db
from database.models import ServicePlan, ServerTier
from schemas import ServicePlanCreate, ServicePlanUpdate, ServicePlanResponse
from typing import List

router = APIRouter()


# --- АДМИН: управление тарифами ---

@router.post("/admin/plans/add", dependencies=[Depends(get_current_admin)])
async def create_plan(body: ServicePlanCreate, db: AsyncSession = Depends(get_db)):
    # Проверяем что тир существует
    tier = await db.execute(select(ServerTier).where(ServerTier.level == body.tier_level))
    if not tier.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Тир level={body.tier_level} не найден. Создайте через POST /server/tiers")

    new_plan = ServicePlan(**body.model_dump())
    db.add(new_plan)
    await db.commit()
    await db.refresh(new_plan)
    return {"status": "Тариф создан", "plan_id": new_plan.id}


@router.patch("/admin/plans/{plan_id}", dependencies=[Depends(get_current_admin)])
async def update_plan(plan_id: int, body: ServicePlanUpdate, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(plan, field, value)

    await db.commit()
    return {"status": "Тариф обновлён"}


@router.delete("/admin/plans/{plan_id}", dependencies=[Depends(get_current_admin)])
async def delete_plan(plan_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    await db.delete(plan)
    await db.commit()
    return {"status": "Тариф удалён"}


# --- КЛИЕНТ: просмотр тарифов ---

@router.get("/plans", response_model=List[ServicePlanResponse])
async def get_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan)
        .options(selectinload(ServicePlan.tier))
        .order_by(ServicePlan.tier_level)
    )
    plans = result.scalars().all()
    return [ServicePlanResponse.from_orm_with_price(p) for p in plans]