from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from auth.deps import get_current_admin, get_current_user
from database.database import get_db
from database.models import Client, Invoice, InvoiceStatus

router = APIRouter()


class TopUpRequest(BaseModel):
    client_id: int
    amount: float = Field(gt=0)
    comment: str = ""


@router.post("/admin/topup", dependencies=[Depends(get_current_admin)])
async def admin_topup_balance(
    body: TopUpRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: Client = Depends(get_current_admin)
):
    """Пополняет баланс клиента вручную."""
    result = await db.execute(select(Client).where(Client.id == body.client_id))
    client = result.scalar_one_or_none()
    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    db.add(Invoice(
        client_id=client.id,
        topped_up_by=current_admin.id,
        amount=body.amount,
        status=InvoiceStatus.PAID,
        external_id=f"manual_{body.comment}" if body.comment else None
    ))
    client.balance = round(client.balance + body.amount, 2)
    await db.commit()

    return {
        "status": "Баланс пополнен",
        "client_id": client.id,
        "email": client.email,
        "amount_added": body.amount,
        "new_balance": client.balance,
    }