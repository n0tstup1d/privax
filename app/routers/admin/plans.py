from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from auth.deps import get_current_admin, get_current_user
from database.database import get_db
from database.models import ServicePlan
from schemas import ServicePlanCreate, ServicePlanUpdate, ServicePlanResponse
from typing import List

router = APIRouter()


# --- АДМИН: управление тарифами ---

@router.post("/admin/plans/add", dependencies=[Depends(get_current_admin)])
async def create_plan(body: ServicePlanCreate, db: AsyncSession = Depends(get_db)):
    # Проверяем нет ли уже плана с таким tier_level
    existing = await db.execute(select(ServicePlan).where(ServicePlan.tier_level == body.tier_level))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Тариф с таким уровнем уже существует")

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

    # Обновляем только те поля которые пришли в запросе
    update_data = body.model_dump(exclude_none=True)
    for field, value in update_data.items():
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
    result = await db.execute(select(ServicePlan).order_by(ServicePlan.tier_level))
    plans = result.scalars().all()
    return [ServicePlanResponse.from_orm_with_price(p) for p in plans]