from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user, get_current_admin
from database.database import get_db
from database.models import Client, Promocode, PromocodeUsage, PromocodeType, Invoice, InvoiceStatus, ServicePlan

router = APIRouter()


# ═══════════════════════════════════════════════
#  СХЕМЫ
# ═══════════════════════════════════════════════

class PromocodeCreate(BaseModel):
    code: str
    promo_type: PromocodeType = PromocodeType.BALANCE

    # Для balance
    value: float = 0.0

    # Для discount
    discount_percent: float = 0.0
    plan_id: Optional[int] = None        # None = скидка на любой план

    max_usages: int = 1                  # 0 = безлимит
    max_usages_per_client: int = 1       # 0 = без ограничений на клиента
    expires_at: Optional[datetime] = None


class PromocodeApply(BaseModel):
    code: str
    plan_id: Optional[int] = None        # нужен если тип discount — для проверки плана


# ═══════════════════════════════════════════════
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════

async def _validate_promocode(
    code: str,
    client_id: int,
    plan_id: Optional[int],
    db: AsyncSession
) -> Promocode:
    """
    Проверяет промокод и возвращает объект если всё ок.
    Бросает HTTPException с понятным сообщением если нет.
    """
    promo = (await db.execute(
        select(Promocode).where(Promocode.code == code.upper().strip())
    )).scalar_one_or_none()

    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")

    if promo.expires_at and promo.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Промокод истёк")

    if promo.max_usages > 0 and promo.current_usages >= promo.max_usages:
        raise HTTPException(status_code=400, detail="Промокод исчерпан")

    # Проверяем лимит на клиента
    if promo.max_usages_per_client > 0:
        client_uses = await db.scalar(
            select(func.count(PromocodeUsage.id)).where(
                PromocodeUsage.promocode_id == promo.id,
                PromocodeUsage.client_id == client_id
            )
        )
        if client_uses >= promo.max_usages_per_client:
            raise HTTPException(status_code=400, detail="Вы уже использовали этот промокод")

    # Для скидочных промокодов — проверяем привязку к плану
    if promo.promo_type == PromocodeType.DISCOUNT:
        if promo.plan_id and plan_id and promo.plan_id != plan_id:
            raise HTTPException(
                status_code=400,
                detail="Этот промокод действует только на другой тариф"
            )

    return promo


async def _record_usage(promo: Promocode, client_id: int, db: AsyncSession):
    """Записывает использование промокода."""
    promo.current_usages += 1
    db.add(PromocodeUsage(promocode_id=promo.id, client_id=client_id))


# ═══════════════════════════════════════════════
#  КЛИЕНТ
# ═══════════════════════════════════════════════

@router.post("/apply")
async def apply_promocode(
    body: PromocodeApply,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Применяет промокод.

    balance  → сразу зачисляет рубли на баланс
    discount → возвращает процент скидки — фронт должен применить
               его при вызове /subscriptions/buy/{plan_id}
               (передать promocode в теле запроса)
    """
    promo = await _validate_promocode(body.code, current_user.id, body.plan_id, db)

    if promo.promo_type == PromocodeType.BALANCE:
        await _record_usage(promo, current_user.id, db)
        current_user.balance = round(current_user.balance + promo.value, 2)

        db.add(Invoice(
            client_id=current_user.id,
            amount=promo.value,
            status=InvoiceStatus.PAID,
            external_id=f"promo_{promo.code}"
        ))
        await db.commit()

        return {
            "type": "balance",
            "status": "Промокод применён",
            "added": promo.value,
            "new_balance": current_user.balance
        }

    elif promo.promo_type == PromocodeType.DISCOUNT:
        # Скидку не применяем здесь — возвращаем информацию
        # Реальное применение происходит в /subscriptions/buy
        return {
            "type": "discount",
            "status": "Промокод действителен",
            "discount_percent": promo.discount_percent,
            "plan_id": promo.plan_id,
            "message": "Используйте код при покупке подписки"
        }


# ═══════════════════════════════════════════════
#  АДМИН
# ═══════════════════════════════════════════════

@router.post("/admin/promocodes", dependencies=[Depends(get_current_admin)])
async def create_promocode(body: PromocodeCreate, db: AsyncSession = Depends(get_db)):
    """Создаёт промокод."""
    existing = (await db.execute(
        select(Promocode).where(Promocode.code == body.code.upper().strip())
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Промокод с таким кодом уже существует")

    if body.promo_type == PromocodeType.BALANCE and body.value <= 0:
        raise HTTPException(status_code=400, detail="Для balance-промокода value должно быть > 0")

    if body.promo_type == PromocodeType.DISCOUNT and not (0 < body.discount_percent <= 100):
        raise HTTPException(status_code=400, detail="discount_percent должен быть от 1 до 100")

    promo = Promocode(
        code=body.code.upper().strip(),
        promo_type=body.promo_type,
        value=body.value,
        discount_percent=body.discount_percent,
        plan_id=body.plan_id,
        max_usages=body.max_usages,
        max_usages_per_client=body.max_usages_per_client,
        expires_at=body.expires_at.replace(tzinfo=None) if body.expires_at else None
    )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)

    return {
        "status": "Промокод создан",
        "id": promo.id,
        "code": promo.code,
        "type": promo.promo_type.value,
        "value": promo.value,
        "discount_percent": promo.discount_percent,
        "plan_id": promo.plan_id,
        "max_usages": promo.max_usages,
        "max_usages_per_client": promo.max_usages_per_client,
        "expires_at": promo.expires_at.isoformat() if promo.expires_at else None
    }


@router.get("/admin/promocodes", dependencies=[Depends(get_current_admin)])
async def list_promocodes(db: AsyncSession = Depends(get_db)):
    """Список всех промокодов со статистикой."""
    result = await db.execute(select(Promocode).order_by(Promocode.id.desc()))
    promos = result.scalars().all()

    now = datetime.utcnow()
    return [
        {
            "id": p.id,
            "code": p.code,
            "type": p.promo_type.value,
            "value": p.value if p.promo_type == PromocodeType.BALANCE else None,
            "discount_percent": p.discount_percent if p.promo_type == PromocodeType.DISCOUNT else None,
            "plan_id": p.plan_id,
            "usages": f"{p.current_usages}/{p.max_usages if p.max_usages > 0 else '∞'}",
            "per_client": p.max_usages_per_client if p.max_usages_per_client > 0 else "∞",
            "expires_at": p.expires_at.isoformat() if p.expires_at else "бессрочный",
            "active": (
                (p.max_usages == 0 or p.current_usages < p.max_usages) and
                (not p.expires_at or p.expires_at > now)
            )
        }
        for p in promos
    ]


@router.delete("/admin/promocodes/{promo_id}", dependencies=[Depends(get_current_admin)])
async def delete_promocode(promo_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Promocode).where(Promocode.id == promo_id))
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    await db.delete(promo)
    await db.commit()
    return {"status": "Промокод удалён"}