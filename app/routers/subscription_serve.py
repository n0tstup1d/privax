from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from sqlalchemy import select

from database.database import async_session
from database.models import Config

router = APIRouter()


@router.get("/sub/{token}", response_class=PlainTextResponse)
async def serve_subscription(token: str):
    """
    Эндпоинт который клиент вставляет в AmneziaVPN один раз.

    Приложение периодически сюда обращается и получает актуальную VLESS ссылку.
    Ссылка хранится в БД в том виде в котором её отдал Marzban —
    со всеми параметрами включая flow, spx, sni и т.д.

    Формат ответа — plain text, именно это ожидают все VPN приложения.
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
            raise HTTPException(
                status_code=500,
                detail="VPN ссылка не найдена — обратитесь к администратору"
            )

        return config.vless_link