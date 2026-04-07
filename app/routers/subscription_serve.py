"""
subscription_serve.py — раздача подписок клиентам приложений.

Два эндпоинта:

GET /sub/{token}       — VLESS-ссылка plain-text.
                         Клиент копирует из дашборда и вставляет в приложение.
                         Также работает как subscription URL — клиент при обновлении
                         получает актуальную ссылку.

GET /sub/{token}/info  — JSON с данными подписки (для фронта, если нужно).

Все ссылки запрашиваются у Marzban в реальном времени, а не из БД.
"""
import os
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from database.database import async_session
from database.models import Config, VPNServer
from app.services.crypto_service import decrypt

router = APIRouter()
logger = logging.getLogger("privax.sub")

SERVICE_NAME = os.getenv("SERVICE_NAME", "Tugoka")


async def _get_config_with_server(token: str) -> tuple:
    """Загружает конфиг + сервер по sub_token, проверяет валидность."""
    async with async_session() as db:
        result = await db.execute(
            select(Config)
            .options(selectinload(Config.server))
            .where(Config.sub_token == token)
        )
        config = result.scalar_one_or_none()

        if not config:
            raise HTTPException(status_code=404, detail="Подписка не найдена")
        if not config.is_active:
            raise HTTPException(status_code=403, detail="Подписка отключена")
        if config.expire_at < datetime.utcnow():
            raise HTTPException(status_code=403, detail="Подписка истекла")

        server = config.server
        if not server:
            raise HTTPException(status_code=500, detail="Сервер не найден")

        return config, server


async def _fetch_links_from_marzban(server: VPNServer, mar_username: str) -> list:
    """
    Запрашивает актуальные links у Marzban API.
    Возвращает список строк (vless://..., trojan://... и т.д.)
    """
    from app.services.marzban_service import get_marzban_user

    admin_user = decrypt(server.mar_admin_user)
    admin_pass = decrypt(server.mar_admin_pass)

    user_data = await get_marzban_user(
        marzban_url=server.marzban_url,
        admin_username=admin_user,
        admin_password=admin_pass,
        username=mar_username,
    )

    if not user_data.get("success"):
        logger.error(f"Marzban недоступен для {mar_username}: {user_data.get('error')}")
        raise HTTPException(
            status_code=502,
            detail=f"Не удалось получить данные с сервера: {user_data.get('error', 'unknown')}"
        )

    links = user_data.get("links", [])
    if not links:
        logger.warning(f"Marzban вернул пустые links для {mar_username}")
        raise HTTPException(
            status_code=502,
            detail="Сервер не вернул ссылки — попробуйте позже или обратитесь в поддержку"
        )

    return links


def _clean_link_name(link: str, username: str) -> str:
    """
    Заменяет Marzban-овское название ссылки на чистое.
    🚀 Marz (privax_1_f8d4) [VLESS - tcp] → privax_1_f8d4
    """
    from urllib.parse import quote
    if "#" in link:
        base = link.split("#")[0]
        return f"{base}#{quote(username)}"
    return link


def _filter_vless_links(links: list, username: str) -> list:
    """Оставляет только VLESS-ссылки и чистит названия."""
    result = []
    for link in links:
        if isinstance(link, str) and link.startswith("vless://"):
            result.append(_clean_link_name(link, username))
    return result


def _pick_vless_link(links: list, username: str) -> str | None:
    """Выбирает первую vless:// ссылку с чистым названием."""
    for link in links:
        if isinstance(link, str) and link.startswith("vless://"):
            return _clean_link_name(link, username)
    return None


@router.get("/sub/{token}")
async def serve_subscription(token: str, request: Request):
    """
    Отдаёт VLESS-ссылку plain-text с чистым названием.
    SS-ссылки отфильтровываются.

    Ссылки запрашиваются у Marzban в реальном времени — всегда актуальные.
    """
    config, server = await _get_config_with_server(token)
    links = await _fetch_links_from_marzban(server, config.mar_username)

    # Только VLESS, с чистым названием
    clean_links = _filter_vless_links(links, config.mar_username)
    if not clean_links:
        raise HTTPException(status_code=502, detail="VLESS-ссылка не найдена")

    content = "\n".join(clean_links)

    headers = {
        "content-type": "text/plain; charset=utf-8",
        "profile-update-interval": "12",
        "subscription-userinfo": f"upload=0; download=0; total=0; expire={int(config.expire_at.timestamp())}",
        "profile-title": SERVICE_NAME,
    }

    return PlainTextResponse(content=content, headers=headers)


@router.get("/sub/{token}/info")
async def subscription_info(token: str):
    """JSON с данными подписки — для фронта или отладки."""
    config, server = await _get_config_with_server(token)
    links = await _fetch_links_from_marzban(server, config.mar_username)
    vless_link = _pick_vless_link(links, config.mar_username)

    return {
        "vless_link": vless_link,
        "expires_at": config.expire_at.isoformat() + "Z",
        "server_name": server.name,
        "server_country": server.country_code,
    }