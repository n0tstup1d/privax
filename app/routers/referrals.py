"""
Реферальная программа Privax.

Схема работы:
  1. У каждого пользователя есть уникальный referral_code (генерируется при первом запросе).
  2. Новый пользователь регистрируется с параметром ?ref=CODE.
  3. В таблице Referral создаётся запись (referrer → referred), bonus_given=False.
  4. Когда referred совершает ПЕРВУЮ покупку → вызывается grant_referral_bonus():
       - находим реферера
       - берём его самую свежую активную подписку
       - продлеваем её на BONUS_DAYS в Marzban и в БД
       - ставим bonus_given=True, записываем bonus_at

Эндпоинты:
  GET  /referral/me          — мой код, ссылка, статистика
  POST /referral/register    — зафиксировать реферера при регистрации (внутренний)
"""

import random
import string
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user
from database.database import get_db
from database.models import Client, Config, Referral, ServicePlan
from app.services.xui_service import extend_xui_client
from app.services.crypto_service import decrypt

router = APIRouter()

BONUS_DAYS = 30  # дней за каждого приглашённого


# ── Хелперы ────────────────────────────────────────────────────────────

def _gen_code(length: int = 8) -> str:
    """Генерирует случайный буквенно-цифровой код верхнего регистра."""
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


async def _ensure_referral_code(client: Client, db: AsyncSession) -> str:
    """Возвращает referral_code пользователя, генерируя его если отсутствует."""
    if client.referral_code:
        return client.referral_code

    # Генерируем уникальный код
    while True:
        code = _gen_code()
        exists = await db.scalar(
            select(Client).where(Client.referral_code == code)
        )
        if not exists:
            break

    client.referral_code = code
    await db.commit()
    await db.refresh(client)
    return code


# ── Основная логика бонуса (вызывается из subscriptions.py) ────────────

async def grant_referral_bonus(referred_id: int, db: AsyncSession) -> bool:
    """
    Начисляет бонус рефереру за первую покупку пользователя referred_id.

    Возвращает True если бонус был начислен, False если нет реферера
    или бонус уже был выдан.

    Алгоритм:
    1. Ищем запись Referral для referred_id с bonus_given=False
    2. Берём реферера и его самую свежую активную подписку
    3. Продлеваем подписку на BONUS_DAYS в Marzban и в БД
    4. Помечаем bonus_given=True
    """
    referral = await db.scalar(
        select(Referral)
        .options(selectinload(Referral.referrer))
        .where(
            Referral.referred_id == referred_id,
            Referral.bonus_given == False,
        )
    )
    if not referral:
        return False

    referrer = referral.referrer

    # Находим самую свежую активную подписку реферера
    config_q = await db.execute(
        select(Config)
        .options(selectinload(Config.server))
        .where(
            Config.client_id == referrer.id,
            Config.is_active == True,
            Config.expire_at > datetime.utcnow(),
        )
        .order_by(Config.expire_at.desc())
    )
    config = config_q.scalars().first()

    if config and config.server:
        server = config.server
        admin_user = decrypt(server.mar_admin_user)
        admin_pass = decrypt(server.mar_admin_pass)

        result = await extend_xui_client(
            ip=server.ip_address,
            ssh_port=server.ssh_port,
            panel_port=server.panel_port,
            xui_admin_user=admin_user,
            xui_admin_pass=admin_pass,
            xui_uuid=config.xui_uuid,
            xui_inbound_id=config.xui_inbound_id,
            extra_days=BONUS_DAYS,
        )

        if result["success"]:
            # Обновляем expire_at в нашей БД
            config.expire_at = config.expire_at + timedelta(days=BONUS_DAYS)
        # Если Marzban недоступен — всё равно помечаем бонус выданным
        # чтобы не начислять повторно. Дату в БД обновляем только при успехе.

    # Даже если нет активной подписки — помечаем, бонус «сгорает» как задумано
    referral.bonus_given = True
    referral.bonus_at = datetime.utcnow()
    await db.commit()
    return True


# ── Эндпоинты ──────────────────────────────────────────────────────────

@router.get("/me")
async def get_my_referral(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user),
):
    code = await _ensure_referral_code(current_user, db)

    referrals_q = await db.execute(
        select(Referral).where(Referral.referrer_id == current_user.id)
    )
    referrals = referrals_q.scalars().all()

    total     = len(referrals)
    activated = sum(1 for r in referrals if r.bonus_given)
    pending   = total - activated

    # Берём домен из Origin заголовка (фронтенд всегда его шлёт)
    # Фолбэк: Referer, затем сам host запроса
    origin = (
        request.headers.get("origin")
        or request.headers.get("referer", "").rstrip("/").rsplit("/register", 1)[0]
        or str(request.base_url).rstrip("/")
    )

    return {
        "referral_code": code,
        "referral_link": f"{origin}/register?ref={code}",
        "total_referred": total,
        "bonuses_earned": activated,
        "pending": pending,
        "bonus_days_per_referral": BONUS_DAYS,
    }