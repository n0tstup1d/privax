"""
subscription_serve.py — раздача подписок клиентам VPN-приложений.

Два режима:
1. GET /sub/{token}        — plain-text vless_link (для AmneziaVPN и аналогов)
2. GET /sub/{token}/proxy  — 302-редирект на subscription_url Marzban
                             (для Hiddify, v2rayNG, Clash и др.)

Marzban сам поддерживает subscription_url и умеет отдавать правильный контент
с заголовками под каждый клиент. Для таких приложений лучше использовать /proxy.
"""
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
from sqlalchemy import select

from database.database import async_session
from database.models import Config

router = APIRouter()


@router.get("/sub/{token}", response_class=PlainTextResponse)
async def serve_subscription(token: str):
    """
    Эндпоинт для AmneziaVPN и аналогичных клиентов.
    Возвращает первую vless_link plain-text.
    """
    async with async_session() as db:
        result = await db.execute(
            select(Config).where(Config.sub_token == token)
        )
        config = result.scalar_one_or_none()

        if not config:
            raise HTTPException(status_code=404, detail="Подписка не найдена")
        if not config.is_active:
            raise HTTPException(status_code=403, detail="Подписка отключена")
        if config.expire_at < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Подписка истекла")

        if not config.vless_link:
            # Фолбэк: редирект на subscription_url Marzban
            if config.subscription_url:
                return RedirectResponse(url=config.subscription_url, status_code=302)
            raise HTTPException(
                status_code=500,
                detail="VPN ссылка не найдена — обратитесь к администратору"
            )

        return config.vless_link


@router.get("/sub/{token}/proxy")
async def proxy_to_marzban(token: str):
    """
    302-редирект на subscription_url Marzban.
    Для приложений которые поддерживают формат подписок Marzban
    (Hiddify, v2rayNG, Clash и др.) — так они получат все конфиги сразу.
    """
    async with async_session() as db:
        result = await db.execute(
            select(Config).where(Config.sub_token == token)
        )
        config = result.scalar_one_or_none()

        if not config:
            raise HTTPException(status_code=404, detail="Подписка не найдена")
        if not config.is_active:
            raise HTTPException(status_code=403, detail="Подписка отключена")
        if config.expire_at < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Подписка истекла")

        if not config.subscription_url:
            raise HTTPException(
                status_code=500,
                detail="URL подписки Marzban не найден — обратитесь к администратору"
            )

        return RedirectResponse(url=config.subscription_url, status_code=302)