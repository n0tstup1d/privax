from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user, require_role, get_admin_from_jwt
from database.database import get_db
from database.models import Client, Promocode, PromocodeUsage, PromocodeType, Invoice, InvoiceStatus, AdminRole

router = APIRouter()

_any_admin   = get_admin_from_jwt
_owner_or_op = require_role(AdminRole.OWNER, AdminRole.OPERATOR)
_owner_only  = require_role(AdminRole.OWNER)


class PromocodeCreate(BaseModel):
    code: str
    promo_type: PromocodeType = PromocodeType.BALANCE
    value: float = 0.0
    discount_percent: float = 0.0
    plan_id: Optional[int] = None
    max_usages: int = 1
    max_usages_per_client: int = 1
    expires_at: Optional[datetime] = None


class PromocodeApply(BaseModel):
    code: str
    plan_id: Optional[int] = None


async def _validate_promocode(code, client_id, plan_id, db):
    promo = (await db.execute(
        select(Promocode).where(Promocode.code == code.upper().strip())
    )).scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    if promo.expires_at and promo.expires_at < datetime.utcnow():
        raise HTTPException(status_code=400, detail="Промокод истёк")
    if promo.max_usages > 0 and promo.current_usages >= promo.max_usages:
        raise HTTPException(status_code=400, detail="Промокод исчерпан")
    if promo.max_usages_per_client > 0:
        uses = await db.scalar(select(func.count(PromocodeUsage.id)).where(
            PromocodeUsage.promocode_id == promo.id, PromocodeUsage.client_id == client_id
        ))
        if uses >= promo.max_usages_per_client:
            raise HTTPException(status_code=400, detail="Вы уже использовали этот промокод")
    if promo.promo_type == PromocodeType.DISCOUNT:
        if promo.plan_id and plan_id and promo.plan_id != plan_id:
            raise HTTPException(status_code=400, detail="Этот промокод действует только на другой тариф")
    return promo


# ─── Клиент ──────────────────────────────────

@router.post("/promocodes/apply")
async def apply_promocode(body: PromocodeApply, db: AsyncSession = Depends(get_db), current_user: Client = Depends(get_current_user)):
    promo = await _validate_promocode(body.code, current_user.id, body.plan_id, db)
    if promo.promo_type == PromocodeType.BALANCE:
        promo.current_usages += 1
        db.add(PromocodeUsage(promocode_id=promo.id, client_id=current_user.id))
        current_user.balance = round(current_user.balance + promo.value, 2)
        db.add(Invoice(client_id=current_user.id, amount=promo.value, status=InvoiceStatus.PAID, external_id=f"promo_{promo.code}"))
        await db.commit()
        return {"type": "balance", "status": "Промокод применён", "added": promo.value, "new_balance": current_user.balance}
    else:
        return {"type": "discount", "status": "Промокод действителен", "discount_percent": promo.discount_percent, "plan_id": promo.plan_id}


# ─── Админ ───────────────────────────────────

@router.post("/admin/promocodes")
async def create_promocode(body: PromocodeCreate, db: AsyncSession = Depends(get_db), _=Depends(_owner_or_op)):
    existing = (await db.execute(select(Promocode).where(Promocode.code == body.code.upper().strip()))).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=400, detail="Промокод с таким кодом уже существует")
    if body.promo_type == PromocodeType.BALANCE and body.value <= 0:
        raise HTTPException(status_code=400, detail="Для balance-промокода value должно быть > 0")
    if body.promo_type == PromocodeType.DISCOUNT and not (0 < body.discount_percent <= 100):
        raise HTTPException(status_code=400, detail="discount_percent должен быть от 1 до 100")

    promo = Promocode(
        code=body.code.upper().strip(), promo_type=body.promo_type,
        value=body.value, discount_percent=body.discount_percent,
        plan_id=body.plan_id, max_usages=body.max_usages,
        max_usages_per_client=body.max_usages_per_client,
        expires_at=body.expires_at.replace(tzinfo=None) if body.expires_at else None
    )
    db.add(promo)
    await db.commit()
    await db.refresh(promo)
    return {"status": "Промокод создан", "id": promo.id, "code": promo.code}


@router.get("/admin/promocodes")
async def list_promocodes(db: AsyncSession = Depends(get_db), _=Depends(_any_admin)):
    result = await db.execute(select(Promocode).order_by(Promocode.id.desc()))
    promos = result.scalars().all()
    now = datetime.utcnow()
    return [
        {
            "id": p.id, "code": p.code, "type": p.promo_type.value,
            "value": p.value if p.promo_type == PromocodeType.BALANCE else None,
            "discount_percent": p.discount_percent if p.promo_type == PromocodeType.DISCOUNT else None,
            "plan_id": p.plan_id,
            "usages": f"{p.current_usages}/{p.max_usages if p.max_usages > 0 else '∞'}",
            "per_client": p.max_usages_per_client if p.max_usages_per_client > 0 else "∞",
            "expires_at": p.expires_at.isoformat() if p.expires_at else None,
            "active": (p.max_usages == 0 or p.current_usages < p.max_usages) and (not p.expires_at or p.expires_at > now),
        }
        for p in promos
    ]


@router.delete("/admin/promocodes/{promo_id}")
async def delete_promocode(promo_id: int, db: AsyncSession = Depends(get_db), _=Depends(_owner_or_op)):
    result = await db.execute(select(Promocode).where(Promocode.id == promo_id))
    promo = result.scalar_one_or_none()
    if not promo:
        raise HTTPException(status_code=404, detail="Промокод не найден")
    await db.delete(promo)
    await db.commit()
    return {"status": "Промокод удалён"}