import json
from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from database.database import async_session
from database.models import Config, VPNServer, TrustedDomain
from app.services.link_generator import generate_vless_link

router = APIRouter()


@router.get("/sub/{token}", response_class=PlainTextResponse)
async def serve_subscription(token: str):
    """
    Эндпоинт который клиент вставляет в AmneziaVPN один раз.

    Приложение периодически сюда обращается и получает свежую VLESS ссылку.
    При каждом запросе генерируются новые случайные параметры:
      - sni  → случайный домен из пула
      - fp   → случайный браузер (chrome/firefox/safari/...)
      - spx  → случайный путь

    Неизменные параметры (привязаны к конкретному клиенту):
      - user_uuid   → его UUID в Marzban
      - short_id    → его личный shortId на сервере

    Формат ответа — plain text, именно это ожидают все VPN приложения.
    """
    async with async_session() as db:

        # Ищем подписку по токену
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

        # Проверяем наличие всех данных для генерации ссылки
        if not config.user_uuid:
            raise HTTPException(status_code=500, detail="UUID пользователя не найден — обратитесь к администратору")

        if not config.reality_short_id:
            raise HTTPException(status_code=500, detail="ShortId не настроен — обратитесь к администратору")

        # Получаем сервер
        server = await db.get(VPNServer, config.server_id)
        if not server:
            raise HTTPException(status_code=500, detail="Сервер не найден")

        if not server.reality_public_key:
            raise HTTPException(status_code=500, detail="PublicKey сервера не настроен — обратитесь к администратору")

        # Собираем пул доменов из двух источников:
        # 1. Домены прописанные в конфиге этого сервера (server_names)
        server_domains = []
        if server.server_names:
            try:
                server_domains = json.loads(server.server_names)
            except (json.JSONDecodeError, TypeError):
                server_domains = []

        # 2. Глобальные домены из таблицы TrustedDomain (управляются через админку)
        trusted_result = await db.execute(
            select(TrustedDomain).where(
                TrustedDomain.is_active == True,
                (TrustedDomain.country_code == server.country_code) |
                (TrustedDomain.country_code == None)
            ) 
        )
        trusted_domains = [d.domain for d in trusted_result.scalars().all()]

        # Объединяем, убираем дубли
        all_domains = list(set(server_domains + trusted_domains))

        if not all_domains:
            raise HTTPException(
                status_code=500,
                detail="Нет доменов-масок — администратор должен добавить домены через /admin/domains"
            )

        # Генерируем свежую VLESS ссылку
        vless_link = generate_vless_link(
            user_uuid=config.user_uuid,
            server_ip=server.ip_address,
            public_key=server.reality_public_key,
            short_id=config.reality_short_id,
            sni_domains=all_domains,
            label="Privax"
        )

        return vless_link