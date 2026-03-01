from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from pydantic import BaseModel, Field
from auth.deps import get_current_admin, get_current_user
from database.database import get_db
from database.models import Client, Invoice, InvoiceStatus, ServicePlan

router = APIRouter()


# --- СХЕМЫ ---

class TopUpRequest(BaseModel):
    client_id: int
    amount: float = Field(gt=0, description="Сумма пополнения — должна быть больше нуля")
    comment: str = ""   # например "Тестовое пополнение" или "Оплата через ЮKassa #12345"


# --- АДМИН ---

@router.post("/admin/balance/topup")
async def admin_topup_balance(
    body: TopUpRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: Client = Depends(get_current_admin)   # нужен чтобы знать КТО пополнил
):
    """
    Пополняет баланс клиента вручную.
    
    Сейчас это заглушка — администратор пополняет руками.
    В будущем этот эндпоинт заменим на webhook от ЮKassa/Stripe:
    платёжка сама будет его вызывать когда клиент оплатил.
    
    Логика намеренно такая же как будет у webhook:
    находим клиента → создаём Invoice(PAID) → пополняем баланс.
    Так что переход на реальную платёжку будет минимальным.
    """
    # Находим клиента
    result = await db.execute(select(Client).where(Client.id == body.client_id))
    client = result.scalar_one_or_none()

    if not client:
        raise HTTPException(status_code=404, detail="Клиент не найден")

    # Создаём Invoice — важно фиксировать каждое пополнение
    # Это история платежей, нужна для поддержки и бухгалтерии
    invoice = Invoice(
        client_id=client.id,
        plan_id=None,
        topped_up_by=current_admin.id,   # фиксируем кто пополнил
        amount=body.amount,
        status=InvoiceStatus.PAID,
        external_id=f"manual_{body.comment}" if body.comment else None
    )
    db.add(invoice)

    # Пополняем баланс
    client.balance = round(client.balance + body.amount, 2)

    await db.commit()

    return {
        "status": "Баланс пополнен",
        "client_id": client.id,
        "email": client.email,
        "amount_added": body.amount,
        "new_balance": client.balance,
        "topped_up_by": current_admin.email   # показываем email админа в ответе
    }


@router.get("/admin/clients", dependencies=[Depends(get_current_admin)])
async def get_all_clients(db: AsyncSession = Depends(get_db)):
    """
    Список всех клиентов с балансами.
    Первый кирпич будущей админки.
    """
    result = await db.execute(select(Client).order_by(Client.id))
    clients = result.scalars().all()

    return [
        {
            "id": c.id,
            "email": c.email,
            "balance": c.balance,
            "is_admin": c.is_admin,
        }
        for c in clients
    ]


# --- ПОЛЬЗОВАТЕЛЬ ---

@router.get("/balance")
async def get_my_balance(
    current_user: Client = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    """
    Возвращает баланс и историю платежей текущего пользователя.
    """
    # История всех инвойсов пользователя
    result = await db.execute(
        select(Invoice)
        .where(Invoice.client_id == current_user.id)
        .order_by(Invoice.created_at.desc())
    )
    invoices = result.scalars().all()

    # Для каждого инвойса подтягиваем название плана если он есть
    history = []
    for inv in invoices:
        plan_name = None
        if inv.plan_id:
            plan_result = await db.execute(
                select(ServicePlan).where(ServicePlan.id == inv.plan_id)
            )
            plan = plan_result.scalar_one_or_none()
            plan_name = plan.name if plan else None

        # Если пополнение от админа — подтягиваем его email
        admin_email = None
        if inv.topped_up_by:
            admin_result = await db.execute(
                select(Client).where(Client.id == inv.topped_up_by)
            )
            admin = admin_result.scalar_one_or_none()
            admin_email = admin.email if admin else None

        history.append({
            "id": inv.id,
            "amount": inv.amount,
            "status": inv.status.value,
            "type": "Покупка подписки" if inv.plan_id else "Пополнение баланса",
            "plan": plan_name,
            "topped_up_by": admin_email,   # email админа или None если система
            "date": inv.created_at.isoformat(),
        })

    return {
        "balance": current_user.balance,
        "history": history
    }