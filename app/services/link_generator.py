import secrets
import random
from urllib.parse import quote

# Fingerprints — имитируем разные браузеры
# Каждый клиент получает случайный при генерации ссылки
FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge"]


def generate_vless_link(
    user_uuid: str,         # UUID юзера в Marzban
    server_ip: str,         # IP VPN сервера
    public_key: str,        # publicKey Reality сервера
    short_id: str,          # личный shortId этого клиента
    sni_domains: list[str], # пул доменов-масок (из TrustedDomain + server_names)
    label: str = "Privax"   # название соединения в приложении
) -> str:
    """
    Генерирует уникальную VLESS Reality ссылку для клиента.

    Каждый вызов возвращает немного разную ссылку:
    - sni      → случайный домен из пула
    - fp       → случайный браузер
    - spx      → случайный путь (имитирует запрос к файлу)

    Неизменные параметры (привязаны к клиенту):
    - user_uuid → его UUID в Marzban
    - short_id  → его личный shortId

    Это означает: при каждом обновлении подписки в AmneziaVPN
    клиент получает свежую ссылку с новым sni и fp — 
    для DPI это выглядит как разные пользователи разных сайтов.

    Формат ссылки:
    vless://UUID@IP:443?encryption=none&flow=xtls-rprx-vision
            &fp=chrome&pbk=PUBLIC_KEY&security=reality
            &sid=SHORT_ID&sni=www.microsoft.com
            &spx=%2Fabc123&type=tcp#Privax
    """
    if not sni_domains:
        raise ValueError("Пул доменов пуст — нельзя сгенерировать ссылку")

    if not public_key:
        raise ValueError("publicKey сервера не задан — нельзя сгенерировать ссылку")

    # Каждый раз разный домен — создаём "шум" для DPI
    sni = random.choice(sni_domains)

    # Каждый раз разный браузер
    fp = random.choice(FINGERPRINTS)

    # Случайный путь — имитирует запрос к реальному файлу на сайте
    # Например: /a3f9b2c1 выглядит как запрос к какому-то ресурсу
    random_path = secrets.token_hex(8)
    spx = quote(f"/{random_path}", safe="")  # URL-encode слэша → %2F

    link = (
        f"vless://{user_uuid}@{server_ip}:443?"
        f"encryption=none"
        f"&flow=xtls-rprx-vision"
        f"&fp={fp}"
        f"&pbk={public_key}"
        f"&security=reality"
        f"&sid={short_id}"
        f"&sni={sni}"
        f"&spx={spx}"
        f"&type=tcp"
        f"#{label}"
    )

    return link


def generate_sub_token() -> str:
    """
    Генерирует уникальный токен для /sub/{token} эндпоинта.
    64 символа — достаточно чтобы перебор был невозможен.
    """
    return secrets.token_urlsafe(48)  # 48 байт → 64 символа base64url


def generate_short_id() -> str:
    """
    Генерирует уникальный shortId для одного клиента.
    16 HEX символов — стандарт Reality.
    """
    return secrets.token_hex(8)