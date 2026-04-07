"""
Реферальная программа Privax.

Схема работы:
  1. У каждого пользователя есть уникальный ReferralCode (таблица referral_codes).
     При первом запросе к /referral/me запись создаётся автоматически с наследованием
     глобальных настроек (ReferralSettings).
  2. Новый пользователь регистрируется с ?ref=CODE.
  3. В таблице Referral создаётся запись (referrer → referred), bonus_given=False.
  4. При первой покупке referred → grant_referral_bonus():
       - проверяем is_active на уровне программы и конкретного кода
       - берём bonus_days из кода (или глобальные если не переопределено)
       - продлеваем подписку реферера через Marzban API
       - ставим bonus_given=True

Эндпоинты (пользователь):
  GET  /referral/me                          — мой код, ссылка, статистика

Эндпоинты (админ):
  GET  /referral/admin/settings              — глобальные настройки
  PUT  /referral/admin/settings              — обновить глобальные настройки

  GET  /referral/admin/codes                 — список всех реф. кодов с настройками
  GET  /referral/admin/codes/{code}          — конкретный код
  PUT  /referral/admin/codes/{code}          — изменить настройки кода
  POST /referral/admin/codes/{code}/reset    — сбросить до глобальных настроек

  GET  /referral/admin/referrals             — статистика (топ рефереров)
"""

import random
import string
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select, func, Integer
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from auth.deps import get_current_user, get_admin_from_jwt
from database.database import get_db
from database.models import Client, Config, Referral, ReferralCode, ReferralSettings
from app.services.marzban_service import extend_marzban_user
from app.services.crypto_service import decrypt

router = APIRouter()


# ── Хелперы ────────────────────────────────────────────────────────────

def _gen_code(length: int = 8) -> str:
    chars = string.ascii_uppercase + string.digits
    return "".join(random.choices(chars, k=length))


async def _get_global_settings(db: AsyncSession) -> ReferralSettings:
    """Возвращает глобальные настройки, создаёт дефолт если нет."""
    settings = await db.scalar(select(ReferralSettings).where(ReferralSettings.id == 1))
    if not settings:
        settings = ReferralSettings(id=1)
        db.add(settings)
        await db.commit()
        await db.refresh(settings)
    return settings


async def _ensure_referral_code(client: Client, db: AsyncSession) -> ReferralCode:
    """
    Возвращает ReferralCode клиента, создаёт если нет.
    При создании переносит старый client.referral_code если был.
    """
    # Ищем по client_id
    rc = await db.scalar(
        select(ReferralCode).where(ReferralCode.client_id == client.id)
    )
    if rc:
        return rc

    # Генерируем уникальный код
    # Если у клиента уже был старый код — пробуем его сохранить
    candidate = client.referral_code
    if candidate:
        exists = await db.scalar(
            select(ReferralCode).where(ReferralCode.code == candidate)
        )
        if exists:
            candidate = None

    if not candidate:
        while True:
            candidate = _gen_code()
            exists_rc = await db.scalar(select(ReferralCode).where(ReferralCode.code == candidate))
            exists_cl = await db.scalar(select(Client).where(Client.referral_code == candidate))
            if not exists_rc and not exists_cl:
                break

    rc = ReferralCode(client_id=client.id, code=candidate)
    db.add(rc)

    # Синхронизируем код обратно на Client для совместимости
    client.referral_code = candidate
    await db.commit()
    await db.refresh(rc)
    return rc


def _resolve(rc: ReferralCode, gs: ReferralSettings, field: str):
    """Возвращает значение поля из кода если переопределено, иначе из глобальных."""
    val = getattr(rc, field, None)
    return val if val is not None else getattr(gs, field)


# ── Основная логика бонуса (вызывается из subscriptions.py) ────────────

async def grant_referral_bonus(referred_id: int, db: AsyncSession) -> bool:
    gs = await _get_global_settings(db)
    if not gs.is_active:
        return False

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

    # Получаем реферальный код реферера
    rc = await db.scalar(
        select(ReferralCode).where(ReferralCode.client_id == referrer.id)
    )

    # Если код деактивирован — не начисляем бонус, но помечаем выданным
    if rc and not rc.is_active:
        referral.bonus_given = True
        referral.bonus_at = datetime.utcnow()
        await db.commit()
        return False

    bonus_days = _resolve(rc, gs, 'bonus_days') if rc else gs.bonus_days
    max_bd = _resolve(rc, gs, 'max_bonus_days') if rc else gs.max_bonus_days

    # Самая свежая активная подписка реферера
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

    # Лимит накопленных бонусных дней
    if max_bd and max_bd > 0 and config:
        already_given = await db.scalar(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == referrer.id,
                Referral.bonus_given == True,
            )
        ) or 0
        days_so_far = already_given * (_resolve(rc, gs, 'bonus_days') if rc else gs.bonus_days)
        if days_so_far >= max_bd:
            bonus_days = 0
        else:
            bonus_days = min(bonus_days, max_bd - days_so_far)

    if bonus_days > 0 and config and config.server:
        server = config.server
        result = await extend_marzban_user(
            marzban_url=server.marzban_url,
            admin_username=decrypt(server.mar_admin_user),
            admin_password=decrypt(server.mar_admin_pass),
            username=config.mar_username,
            extra_days=bonus_days,
        )
        if result["success"]:
            config.expire_at = config.expire_at + timedelta(days=bonus_days)

    referral.bonus_given = True
    referral.bonus_at = datetime.utcnow()
    await db.commit()
    return True


async def get_referral_discount(referred_id: int, db: AsyncSession) -> float:
    """
    Возвращает скидку для конкретного приглашённого.
    Берёт её из ReferralCode реферера (или глобальных если не переопределено).
    Используется в subscriptions.py.
    """
    gs = await _get_global_settings(db)
    if not gs.is_active:
        return 0.0

    # Находим кто пригласил этого пользователя
    referral = await db.scalar(
        select(Referral).where(
            Referral.referred_id == referred_id,
            Referral.bonus_given == False,
        )
    )
    if not referral:
        return 0.0

    rc = await db.scalar(
        select(ReferralCode).where(ReferralCode.client_id == referral.referrer_id)
    )

    if rc and not rc.is_active:
        return 0.0

    return _resolve(rc, gs, 'referred_discount') if rc else gs.referred_discount


# ── Схемы ──────────────────────────────────────────────────────────────

class GlobalSettingsUpdate(BaseModel):
    bonus_days: Optional[int] = Field(None, ge=1, le=3650)
    referred_discount: Optional[float] = Field(None, ge=0.0, le=100.0)
    is_active: Optional[bool] = None
    min_purchase_amount: Optional[float] = Field(None, ge=0.0)
    max_bonus_days: Optional[int] = Field(None, ge=0)
    invite_limit: Optional[int] = Field(None, ge=0)  # 0 = без лимита


class ReferralCodeUpdate(BaseModel):
    bonus_days: Optional[int] = Field(None, ge=1, le=3650, description="None = наследовать глобальные")
    referred_discount: Optional[float] = Field(None, ge=0.0, le=100.0, description="None = наследовать глобальные")
    is_active: Optional[bool] = None
    max_bonus_days: Optional[int] = Field(None, ge=0, description="None = наследовать глобальные")


# ── Эндпоинты: пользователь ────────────────────────────────────────────

@router.get("/me")
async def get_my_referral(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: Client = Depends(get_current_user),
):
    rc = await _ensure_referral_code(current_user, db)
    gs = await _get_global_settings(db)

    referrals_q = await db.execute(
        select(Referral).where(Referral.referrer_id == current_user.id)
    )
    referrals = referrals_q.scalars().all()

    total     = len(referrals)
    activated = sum(1 for r in referrals if r.bonus_given)

    origin = (
        request.headers.get("origin")
        or request.headers.get("referer", "").rstrip("/").rsplit("/register", 1)[0]
        or str(request.base_url).rstrip("/")
    )

    return {
        "referral_code": rc.code,
        "referral_link": f"{origin}/register?ref={rc.code}",
        "total_referred": total,
        "bonuses_earned": activated,
        "pending": total - activated,
        "bonus_days_per_referral": _resolve(rc, gs, 'bonus_days'),
        "referred_discount_percent": _resolve(rc, gs, 'referred_discount'),
        "program_active": gs.is_active and rc.is_active,
        "invite_limit": gs.invite_limit,
        "invites_used": total,
        "invites_left": max(0, gs.invite_limit - total) if gs.invite_limit > 0 else None,
    }


# ── Эндпоинты: глобальные настройки ────────────────────────────────────

@router.get("/admin/settings")
async def get_global_settings(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    gs = await _get_global_settings(db)
    return {
        "bonus_days": gs.bonus_days,
        "referred_discount": gs.referred_discount,
        "is_active": gs.is_active,
        "min_purchase_amount": gs.min_purchase_amount,
        "max_bonus_days": gs.max_bonus_days,
        "invite_limit": gs.invite_limit,
        "updated_at": gs.updated_at,
    }


@router.put("/admin/settings")
async def update_global_settings(
    body: GlobalSettingsUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    gs = await _get_global_settings(db)
    if body.bonus_days is not None:         gs.bonus_days = body.bonus_days
    if body.referred_discount is not None:  gs.referred_discount = body.referred_discount
    if body.is_active is not None:          gs.is_active = body.is_active
    if body.min_purchase_amount is not None: gs.min_purchase_amount = body.min_purchase_amount
    if body.max_bonus_days is not None:     gs.max_bonus_days = body.max_bonus_days
    if body.invite_limit is not None:       gs.invite_limit = body.invite_limit
    gs.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(gs)
    return {
        "bonus_days": gs.bonus_days,
        "referred_discount": gs.referred_discount,
        "is_active": gs.is_active,
        "min_purchase_amount": gs.min_purchase_amount,
        "max_bonus_days": gs.max_bonus_days,
        "updated_at": gs.updated_at,
    }


# ── Эндпоинты: индивидуальные коды ─────────────────────────────────────

def _rc_to_dict(rc: ReferralCode, gs: ReferralSettings, email: str | None = None) -> dict:
    return {
        "id": rc.id,
        "client_id": rc.client_id,
        "email": email,
        "code": rc.code,
        "is_active": rc.is_active,
        # Эффективные значения (с учётом наследования)
        "effective_bonus_days": _resolve(rc, gs, 'bonus_days'),
        "effective_discount": _resolve(rc, gs, 'referred_discount'),
        "effective_max_bonus_days": _resolve(rc, gs, 'max_bonus_days'),
        # Переопределения (None = наследуется)
        "override_bonus_days": rc.bonus_days,
        "override_discount": rc.referred_discount,
        "override_max_bonus_days": rc.max_bonus_days,
        "created_at": rc.created_at,
        "updated_at": rc.updated_at,
    }


@router.get("/admin/codes")
async def list_referral_codes(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
    page: int = 1,
    page_size: int = 30,
    search: Optional[str] = None,
):
    """Список всех реферальных кодов с настройками и статистикой."""
    gs = await _get_global_settings(db)

    q = select(ReferralCode).options(selectinload(ReferralCode.client))

    if search:
        from sqlalchemy import or_
        q = q.join(Client, ReferralCode.client_id == Client.id).where(
            or_(
                ReferralCode.code.ilike(f"%{search}%"),
                Client.email.ilike(f"%{search}%"),
            )
        )

    total = await db.scalar(select(func.count()).select_from(q.subquery()))

    q = q.order_by(ReferralCode.created_at.desc()).limit(page_size).offset((page - 1) * page_size)
    result = await db.execute(q)
    codes = result.scalars().all()

    # Статистика по каждому коду
    code_ids = [rc.client_id for rc in codes]
    stats_q = await db.execute(
        select(
            Referral.referrer_id,
            func.count(Referral.id).label("total"),
            func.sum(func.cast(Referral.bonus_given, Integer)).label("activated"),
        )
        .where(Referral.referrer_id.in_(code_ids))
        .group_by(Referral.referrer_id)
    )
    stats = {row.referrer_id: row for row in stats_q.all()}

    items = []
    for rc in codes:
        d = _rc_to_dict(rc, gs, rc.client.email if rc.client else None)
        s = stats.get(rc.client_id)
        d["total_referred"]  = s.total if s else 0
        d["bonuses_given"]   = int(s.activated or 0) if s else 0
        d["pending"]         = (s.total - int(s.activated or 0)) if s else 0
        items.append(d)

    return {
        "items": items,
        "total": total,
        "page": page,
        "pages": -(-total // page_size) if total else 1,
    }


@router.get("/admin/codes/{code}")
async def get_referral_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    gs = await _get_global_settings(db)
    rc = await db.scalar(
        select(ReferralCode)
        .options(selectinload(ReferralCode.client))
        .where(ReferralCode.code == code.upper())
    )
    if not rc:
        raise HTTPException(status_code=404, detail="Реферальный код не найден")

    d = _rc_to_dict(rc, gs, rc.client.email if rc.client else None)

    # История рефералов этого кода
    refs_q = await db.execute(
        select(Referral)
        .options(selectinload(Referral.referred))
        .where(Referral.referrer_id == rc.client_id)
        .order_by(Referral.created_at.desc())
    )
    refs = refs_q.scalars().all()
    d["referrals"] = [
        {
            "referred_id": r.referred_id,
            "email": r.referred.email if r.referred else None,
            "bonus_given": r.bonus_given,
            "created_at": r.created_at,
            "bonus_at": r.bonus_at,
        }
        for r in refs
    ]
    return d


@router.put("/admin/codes/{code}")
async def update_referral_code(
    code: str,
    body: ReferralCodeUpdate,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    """
    Обновляет настройки конкретного реферального кода.
    Передай null для поля — сбросит на наследование глобальных.
    """
    gs = await _get_global_settings(db)
    rc = await db.scalar(
        select(ReferralCode)
        .options(selectinload(ReferralCode.client))
        .where(ReferralCode.code == code.upper())
    )
    if not rc:
        raise HTTPException(status_code=404, detail="Реферальный код не найден")

    # Явная проверка на None vs не передано — используем model_fields_set
    if 'bonus_days' in body.model_fields_set:
        rc.bonus_days = body.bonus_days
    if 'referred_discount' in body.model_fields_set:
        rc.referred_discount = body.referred_discount
    if 'is_active' in body.model_fields_set and body.is_active is not None:
        rc.is_active = body.is_active
    if 'max_bonus_days' in body.model_fields_set:
        rc.max_bonus_days = body.max_bonus_days

    rc.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(rc)
    return _rc_to_dict(rc, gs, rc.client.email if rc.client else None)


@router.post("/admin/codes/{code}/reset")
async def reset_referral_code(
    code: str,
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
):
    """Сбрасывает все переопределения кода — он начнёт наследовать глобальные настройки."""
    gs = await _get_global_settings(db)
    rc = await db.scalar(
        select(ReferralCode)
        .options(selectinload(ReferralCode.client))
        .where(ReferralCode.code == code.upper())
    )
    if not rc:
        raise HTTPException(status_code=404, detail="Реферальный код не найден")

    rc.bonus_days = None
    rc.referred_discount = None
    rc.max_bonus_days = None
    rc.updated_at = datetime.utcnow()
    await db.commit()
    await db.refresh(rc)
    return _rc_to_dict(rc, gs, rc.client.email if rc.client else None)


# ── Эндпоинты: статистика ───────────────────────────────────────────────

@router.get("/admin/referrals")
async def admin_referral_stats(
    db: AsyncSession = Depends(get_db),
    _=Depends(get_admin_from_jwt),
    page: int = 1,
    page_size: int = 30,
):
    gs = await _get_global_settings(db)

    total_referrals = await db.scalar(select(func.count(Referral.id)))
    total_activated = await db.scalar(
        select(func.count(Referral.id)).where(Referral.bonus_given == True)
    )
    total_pending = total_referrals - total_activated

    top_q = await db.execute(
        select(
            Referral.referrer_id,
            func.count(Referral.id).label("total"),
            func.sum(func.cast(Referral.bonus_given, Integer)).label("activated"),
        )
        .group_by(Referral.referrer_id)
        .order_by(func.count(Referral.id).desc())
        .limit(page_size)
        .offset((page - 1) * page_size)
    )

    rows = top_q.all()
    referrer_ids = [r.referrer_id for r in rows]

    clients, ref_codes = {}, {}
    if referrer_ids:
        res = await db.execute(select(Client).where(Client.id.in_(referrer_ids)))
        clients = {c.id: c for c in res.scalars().all()}
        rc_res = await db.execute(
            select(ReferralCode).where(ReferralCode.client_id.in_(referrer_ids))
        )
        ref_codes = {rc.client_id: rc for rc in rc_res.scalars().all()}

    top_referrers = []
    for row in rows:
        client = clients.get(row.referrer_id)
        rc = ref_codes.get(row.referrer_id)
        top_referrers.append({
            "client_id": row.referrer_id,
            "email": client.email if client else None,
            "referral_code": rc.code if rc else (client.referral_code if client else None),
            "is_active": rc.is_active if rc else True,
            "effective_bonus_days": _resolve(rc, gs, 'bonus_days') if rc else gs.bonus_days,
            "effective_discount": _resolve(rc, gs, 'referred_discount') if rc else gs.referred_discount,
            "total_referred": row.total,
            "bonuses_given": int(row.activated or 0),
            "pending": row.total - int(row.activated or 0),
        })

    total_referrers = await db.scalar(
        select(func.count(func.distinct(Referral.referrer_id)))
    )

    return {
        "summary": {
            "total_referrers": total_referrers,
            "total_referrals": total_referrals,
            "total_activated": total_activated,
            "total_pending": total_pending,
            "global_bonus_days": gs.bonus_days,
            "global_discount": gs.referred_discount,
            "program_active": gs.is_active,
        },
        "top_referrers": top_referrers,
        "page": page,
        "pages": -(-total_referrers // page_size) if total_referrers else 1,
    }