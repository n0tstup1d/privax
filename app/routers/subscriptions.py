import json
import secrets
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime, timedelta
from uuid import uuid4

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, ServicePlan, VPNServer, Config, Invoice, InvoiceStatus, TrustedDomain
from app.services.marzban_service import create_marzban_user
from app.services.crypto_service import decrypt
from app.services.ssh_service import check_and_get_marzban_token
from app.services.link_generator import generate_short_id, generate_sub_token, generate_vless_link
from app.services.marzban_configurator import add_short_id_to_server

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
    1.  Проверяем план
    2.  Считаем цену
    3.  Проверяем баланс
    4.  Находим рабочий сервер
    5.  Генерируем уникальные параметры клиента:
            marzban_username, short_id, sub_token
    6.  Добавляем short_id в конфиг Xray на сервере
    7.  Создаём юзера в Marzban → получаем user_uuid
    8.  Сохраняем Config в БД
    9.  Создаём Invoice
    10. Списываем баланс, увеличиваем счётчик
    11. Возвращаем клиенту /sub/{token}
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

    # Шаг 5 — генерируем уникальные параметры клиента
    short_uuid = uuid4().hex[:4]
    marzban_username = f"privax_{current_user.id}_{short_uuid}"
    short_id = generate_short_id()      # личный 16-символьный HEX shortId
    sub_token = generate_sub_token()    # 64-символьный токен для /sub/

    expire_days = plan.months * 30

    # Шаг 6 — добавляем short_id клиента в конфиг Xray на сервере
    # Xray должен знать этот shortId иначе соединение отклонит
    add_result = await add_short_id_to_server(
        ip=server.ip_address,
        ssh_port=server.ssh_port,
        marzban_port=server.marzban_port,
        mar_admin_user=admin_user,
        mar_admin_pass=admin_pass,
        new_short_id=short_id
    )
    if not add_result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось настроить сервер: {add_result['error']}"
        )

    # Шаг 7 — создаём юзера в Marzban
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
        # Откатываем shortId если юзера создать не удалось
        await add_short_id_to_server(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            marzban_port=server.marzban_port,
            mar_admin_user=admin_user,
            mar_admin_pass=admin_pass,
            new_short_id=None,
            remove_short_id=short_id    # убираем добавленный shortId
        )
        raise HTTPException(
            status_code=500,
            detail=f"Не удалось создать VPN конфигурацию: {marzban_result['error']}"
        )

    user_uuid = marzban_result.get("user_uuid")
    subscription_url = marzban_result.get("subscription_url")

    expire_at = datetime.utcnow() + timedelta(days=expire_days)

    # Генерируем VLESS Reality ссылку
    # server_names хранится как JSON в БД — парсим в список доменов
    sni_domains = json.loads(server.server_names or "[]")
    public_key = server.reality_public_key

    vless_link = None
    if user_uuid and public_key and sni_domains:
        vless_link = generate_vless_link(
            user_uuid=user_uuid,
            server_ip=server.ip_address,
            public_key=public_key,
            short_id=short_id,
            sni_domains=sni_domains,
            label=server.name
        )

    # Шаг 8 — сохраняем Config
    new_config = Config(
        client_id=current_user.id,
        server_id=server.id,
        plan_id=plan.id,
        marzban_username=marzban_username,
        user_uuid=user_uuid,
        subscription_url=subscription_url,
        vless_link=vless_link,
        activation_code=uuid4().hex,
        reality_short_id=short_id,
        sub_token=sub_token,
        expire_at=expire_at,
        is_active=True
    )
    db.add(new_config)

    # Шаг 9
    invoice = Invoice(
        client_id=current_user.id,
        plan_id=plan.id,
        amount=final_price,
        status=InvoiceStatus.PAID
    )
    db.add(invoice)

    # Шаг 10 — списываем деньги, обновляем счётчики
    current_user.balance -= final_price
    server.current_users_count += 1

    # Обновляем список shortIds сервера в нашей БД
    current_ids = json.loads(server.reality_short_ids or "[]")
    current_ids.append(short_id)
    server.reality_short_ids = json.dumps(current_ids)

    await db.commit()

    # Шаг 11
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