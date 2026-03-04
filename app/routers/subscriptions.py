from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta
from uuid import uuid4

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, ServicePlan, VPNServer, Config, Invoice, InvoiceStatus
from app.services.marzban_service import create_marzban_user
from app.services.crypto_service import decrypt
from app.services.ssh_service import check_and_get_marzban_token
from app.services.link_generator import generate_sub_token

router = APIRouter()


def _calc_final_price(plan: ServicePlan) -> float:
    base = plan.price * plan.months
    return round(base * (1 - plan.discount_percent / 100), 2)


async def _find_working_server(tier_level: int, db: AsyncSession) -> VPNServer:
    active_q = await db.execute(
        select(VPNServer)
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == True)
        .order_by(VPNServer.current_users_count.asc())
    )
    active_servers = active_q.scalars().all()

    if not active_servers:
        raise HTTPException(status_code=503, detail="Нет активных серверов для этого тарифа.")

    inactive_q = await db.execute(
        select(VPNServer)
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == False)
        .order_by(VPNServer.current_users_count.asc())
    )
    inactive_servers = inactive_q.scalars().all()

    candidates = list(active_servers) + list(inactive_servers)

    for server in candidates:
        if server.current_users_count >= server.max_users:
            continue

        admin_user = decrypt(server.mar_admin_user)
        admin_pass = decrypt(server.mar_admin_pass)

        check = await check_and_get_marzban_token(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            username=admin_user,
            password=admin_pass
        )

        if check["success"]:
            if not server.is_active:
                server.is_active = True
                await db.flush()
            return server
        else:
            server.is_active = False
            await db.flush()

    await db.commit()
    raise HTTPException(
        status_code=503,
        detail="Все серверы этого тарифа переполнены или временно недоступны."
    )


# --- ЭНДПОИНТЫ ---

@router.get("/plans")
async def get_available_plans(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(ServicePlan).order_by(ServicePlan.tier_level, ServicePlan.months)
    )
    plans = result.scalars().all()

    return [
        {
            "id": p.id,
            "name": p.name,
            "tier_level": p.tier_level,
            "price_per_month": p.price,
            "months": p.months,
            "discount_percent": p.discount_percent,
            "final_price": _calc_final_price(p),
            "max_sessions": p.max_sessions,
        }
        for p in plans
    ]


@router.post("/buy/{plan_id}")
async def buy_subscription(
    plan_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Покупка подписки.

    Шаги:
    1. Проверяем план
    2. Считаем цену
    3. Проверяем баланс
    4. Находим рабочий сервер
    5. Генерируем marzban_username и sub_token
    6. Создаём юзера в Marzban → получаем готовую vless_link
    7. Сохраняем Config в БД
    8. Создаём Invoice
    9. Списываем баланс, увеличиваем счётчик
    10. Возвращаем клиенту sub_url
    """

    # Шаг 1
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Шаг 2
    final_price = _calc_final_price(plan)

    # Шаг 3
    if current_user.balance < final_price:
        raise HTTPException(
            status_code=402,
            detail=f"Недостаточно средств. Нужно: {final_price}₽, у вас: {current_user.balance}₽"
        )

    # Шаг 4
    server = await _find_working_server(plan.tier_level, db)
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    # Шаг 5
    short_uuid = uuid4().hex[:4]
    marzban_username = f"privax_{current_user.id}_{short_uuid}"
    sub_token = generate_sub_token()
    expire_days = plan.months * 30

    # Шаг 6 — создаём юзера в Marzban и сразу получаем готовую ссылку.
    # Marzban сам формирует vless_link со всеми параметрами включая spx и flow,
    # при условии что SpiderX прописан в конфиге сервера (через /server/configure).
    marzban_result = await create_marzban_user(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=admin_user,
        mar_admin_pass=admin_pass,
        marzban_username=marzban_username,
        expire_days=expire_days
    )

    if not marzban_result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось создать VPN конфигурацию: {marzban_result['error']}"
        )

    vless_link = marzban_result.get("vless_link")
    expire_at = datetime.utcnow() + timedelta(days=expire_days)

    # Шаг 7 — сохраняем Config
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        marzban_username=marzban_username,
        vless_link=vless_link,
        sub_token=sub_token,
        expire_at=expire_at,
        is_active=True
    )
    db.add(new_config)

    # Шаг 8
    invoice = Invoice(
        client_id=current_user.id,
        plan_id=plan.id,
        amount=final_price,
        status=InvoiceStatus.PAID
    )
    db.add(invoice)

    # Шаг 9
    current_user.balance -= final_price
    server.current_users_count += 1

    await db.commit()

    # Шаг 10
    return {
        "status": "Подписка активирована!",
        "sub_url": f"/sub/{sub_token}",
        "expires_at": expire_at.isoformat(),
        "server": server.name,
        "plan": plan.name,
        "amount_paid": final_price,
        "note": "Вставьте sub_url в AmneziaVPN — ссылка обновляется автоматически"
    }


@router.get("/my-subscriptions")
async def get_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    result = await db.execute(
        select(Config)
        .where(Config.client_id == current_user.id)
        .order_by(Config.expire_at.desc())
    )
    configs = result.scalars().all()

    now = datetime.utcnow()
    return [
        {
            "id": c.id,
            "sub_url": f"/sub/{c.sub_token}" if c.sub_token else None,
            "expires_at": c.expire_at.isoformat(),
            "expired": c.expire_at < now,
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
        }
        for c in configs
    ]