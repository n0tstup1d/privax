import secrets
import random
from urllib.parse import quote

# Fingerprints — имитируем разные браузеры
FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge"]

# Шаблоны путей — имитируют системный / CDN трафик
PATH_TEMPLATES = [
    "/pwa/v1/update",
    "/api/v2/check-updates",
    "/cdn/v1/assets",
    "/app/v3/sync",
    "/static/v2/config",
    "/service/v1/manifest",
    "/ws/v2/push",
    "/api/v1/telemetry",
    "/cdn/v2/bootstrap",
    "/app/v1/health",
]


def generate_xhttp_path() -> str:
    """
    Возвращает случайный путь.
    Используется как xhttpSettings.path при создании инбаунда в 3x-ui.
    """
    return random.choice(PATH_TEMPLATES)


def generate_vless_link(
    user_uuid: str,
    server_ip: str,
    public_key: str,
    short_ids: list,
    sni_domains: list,
    xhttp_path: str = "/pwa/v1/update",
    label: str = "Privax",
) -> str:
    """
    Генерирует VLESS Reality xHTTP ссылку для клиента (Максимум).

    short_ids — список из инбаунда, выбираем случайный.
    ВАЖНО: для xHTTP flow должен быть ПУСТЫМ.
    """
    if not sni_domains:
        raise ValueError("Пул доменов пуст — нельзя сгенерировать ссылку")
    if not public_key:
        raise ValueError("publicKey сервера не задан — нельзя сгенерировать ссылку")
    if not short_ids:
        raise ValueError("shortIds инбаунда пусты — нельзя сгенерировать ссылку")

    sni = random.choice(sni_domains)
    fp  = random.choice(FINGERPRINTS)
    sid = random.choice(short_ids)
    path_encoded = quote(xhttp_path, safe="")

    return (
        f"vless://{user_uuid}@{server_ip}:443?"
        f"type=xhttp"
        f"&encryption=none"
        f"&path={path_encoded}"
        f"&host={sni}"
        f"&mode=auto"
        f"&security=reality"
        f"&pbk={public_key}"
        f"&fp={fp}"
        f"&sni={sni}"
        f"&sid={sid}"
        f"&spx=%2F"
        f"#{label}"
    )


def generate_vless_tcp_link(
    user_uuid: str,
    server_ip: str,
    public_key: str,
    short_ids: list,
    sni_domains: list,
    label: str = "Privax",
) -> str:
    """
    Генерирует VLESS Reality TCP ссылку для клиента (Стандарт).

    Отличия от xHTTP:
    - type=tcp вместо type=xhttp
    - flow=xtls-rprx-vision (обязателен для TCP Reality)
    - нет path и host параметров
    """
    if not sni_domains:
        raise ValueError("Пул доменов пуст — нельзя сгенерировать ссылку")
    if not public_key:
        raise ValueError("publicKey сервера не задан — нельзя сгенерировать ссылку")
    if not short_ids:
        raise ValueError("shortIds инбаунда пусты — нельзя сгенерировать ссылку")

    sni = random.choice(sni_domains)
    fp  = random.choice(FINGERPRINTS)
    sid = random.choice(short_ids)

    return (
        f"vless://{user_uuid}@{server_ip}:443?"
        f"type=tcp"
        f"&encryption=none"
        f"&flow=xtls-rprx-vision"
        f"&security=reality"
        f"&pbk={public_key}"
        f"&fp={fp}"
        f"&sni={sni}"
        f"&sid={sid}"
        f"&spx=%2F"
        f"#{label}"
    )


def generate_sub_token() -> str:
    return secrets.token_urlsafe(48)


def generate_short_id() -> str:
    return secrets.token_hex(8)