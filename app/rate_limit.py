"""
rate_limit.py — настройка rate limiting.

Лимиты:
  - Общий: 60 запросов/минуту на IP
  - Авторизация: 10 запросов/минуту на IP
  - Подписки: 20 запросов/минуту на IP
  - Админ: 120 запросов/минуту на IP

Использование в main.py:
    from app.rate_limit import limiter, rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
"""
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from starlette.requests import Request
from starlette.responses import JSONResponse


def _key_func(request: Request) -> str:
    """Ключ по IP. Учитывает X-Forwarded-For для проксированных запросов."""
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return get_remote_address(request)


limiter = Limiter(
    key_func=_key_func,
    default_limits=["60/minute"],
    storage_uri="memory://",
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    return JSONResponse(
        status_code=429,
        content={
            "detail": "Слишком много запросов. Подождите немного и попробуйте снова.",
            "retry_after": str(exc.detail),
        },
    )


# Декораторы для разных групп эндпоинтов
# Использование: @limiter.limit("10/minute")
# Можно навесить на конкретный эндпоинт:
#   @router.post("/login")
#   @limiter.limit("10/minute")
#   async def login(request: Request, ...):