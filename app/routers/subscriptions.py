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

router = APIRouter()


def _calc_final_price(plan: ServicePlan) -> float:
    """
    Считает итоговую цену подписки с учётом скидки.
    
    Например:
        plan.price = 299 (за месяц)
        plan.months = 3
        plan.discount_percent = 10
        
        base  = 299 * 3 = 897
        final = 897 * (1 - 10/100) = 807.30
    """
    base = plan.price * plan.months
    return round(base * (1 - plan.discount_percent / 100), 2)


async def _find_working_server(tier_level: int, db: AsyncSession) -> VPNServer:
    """
    Находит рабочий сервер нужного уровня.
    
    Алгоритм:
    1. Берём все активные серверы нужного tier, сортируем по загрузке (меньше юзеров = первый)
    2. Идём по списку и проверяем каждый "вживую"
    3. Первый рабочий — возвращаем
    4. Нерабочий — помечаем is_active=False в БД и идём дальше
    5. Если все недоступны — бросаем исключение
    
    Почему проверяем вживую, а не доверяем флагу is_active?
    Сервер мог упасть в любой момент — флаг в БД этого не знает.
    Реальная проверка гарантирует что клиент получит рабочий сервер.
    """
    result = await db.execute(
        select(VPNServer)
        .where(VPNServer.tier_level == tier_level, VPNServer.is_active == True)
        .order_by(VPNServer.current_users_count.asc())
    )
    servers = result.scalars().all()

    if not servers:
        raise HTTPException(
            status_code=503,
            detail="Нет доступных серверов для этого тарифа. Попробуйте позже."
        )

    for server in servers:
        # Расшифровываем credentials для проверки
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
            return server  # нашли рабочий — возвращаем
        else:
            # Сервер не отвечает — помечаем как неактивный
            # Администратор увидит это в панели и разберётся
            server.is_active = False
            await db.flush()  # сохраняем изменение но не коммитим ещё

    # Все серверы оказались недоступны
    await db.commit()  # фиксируем все is_active=False
    raise HTTPException(
        status_code=503,
        detail="Все серверы этого уровня временно недоступны. Мы уже знаем об этом."
    )


# --- ЭНДПОИНТЫ ---

@router.get("/plans")
async def get_available_plans(db: AsyncSession = Depends(get_db)):
    """
    Возвращает все планы с финальной ценой.
    Публичный эндпоинт — авторизация не нужна, клиент смотрит планы до покупки.
    """
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
    Покупка подписки. Основной эндпоинт всего проекта.
    
    Что происходит по шагам:
    1. Проверяем что план существует
    2. Считаем итоговую цену
    3. Проверяем баланс клиента
    4. Находим рабочий сервер (с реальной проверкой)
    5. Создаём юзера в Marzban → получаем subscription_url
    6. Создаём Config в БД
    7. Создаём Invoice (PAID) в БД
    8. Списываем деньги с баланса
    9. Увеличиваем счётчик юзеров на сервере
    10. Возвращаем subscription_url клиенту
    """

    # Шаг 1 — получаем план
    result = await db.execute(select(ServicePlan).where(ServicePlan.id == plan_id))
    plan = result.scalar_one_or_none()
    if not plan:
        raise HTTPException(status_code=404, detail="Тариф не найден")

    # Шаг 2 — считаем цену
    final_price = _calc_final_price(plan)

    # Шаг 3 — проверяем баланс
    if current_user.balance < final_price:
        raise HTTPException(
            status_code=402,  # 402 = Payment Required — специально для этого случая
            detail=f"Недостаточно средств. Нужно: {final_price}₽, у вас: {current_user.balance}₽"
        )

    # Шаг 4 — ищем рабочий сервер
    # Эта функция сама бросит HTTPException если серверов нет
    server = await _find_working_server(plan.tier_level, db)

    # Шаг 5 — придумываем уникальное имя для юзера в Marzban
    # Формат: privax_{client_id}_{4 символа uuid}
    # Пример: privax_42_a3f9
    short_uuid = uuid4().hex[:4]
    marzban_username = f"privax_{current_user.id}_{short_uuid}"

    expire_days = plan.months * 30

    # Шаг 6 — создаём юзера в Marzban
    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

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

    subscription_url = marzban_result["subscription_url"]

    # Шаг 7 — считаем дату истечения подписки
    expire_at = datetime.utcnow() + timedelta(days=expire_days)

    # Шаг 8 — создаём Config в БД
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        marzban_username=marzban_username,
        subscription_url=subscription_url,
        activation_code=uuid4().hex,    # уникальный код — пригодится позже
        expire_at=expire_at,
        is_active=True
    )
    db.add(new_config)

    # Шаг 9 — создаём инвойс (уже PAID, т.к. баланс списали здесь же)
    invoice = Invoice(
        client_id=current_user.id,
        plan_id=plan.id,
        amount=final_price,
        status=InvoiceStatus.PAID
    )
    db.add(invoice)

    # Шаг 10 — списываем деньги и увеличиваем счётчик сервера
    current_user.balance -= final_price
    server.current_users_count += 1

    await db.commit()

    # Возвращаем клиенту всё что ему нужно
    return {
        "status": "Подписка активирована!",
        "subscription_url": subscription_url,   # вставить в AmneziaVPN
        "expires_at": expire_at.isoformat(),
        "server": server.name,
        "plan": plan.name,
        "amount_paid": final_price
    }


@router.get("/my-subscriptions")
async def get_my_subscriptions(
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user)
):
    """
    Возвращает все активные подписки клиента.
    
    Это и есть тот эндпоинт который вызывается при логине —
    приложение получает subscription_url и предлагает добавить в AmneziaVPN.
    """
    result = await db.execute(
        select(Config)
        .where(Config.client_id == current_user.id, Config.is_active == True)
        .order_by(Config.expire_at.desc())
    )
    configs = result.scalars().all()

    return [
        {
            "id": c.id,
            "subscription_url": c.subscription_url,  # главное что нужно клиенту
            "expires_at": c.expire_at.isoformat(),
            "is_active": c.is_active,
            "auto_renew": c.auto_renew,
        }
        for c in configs
    ]