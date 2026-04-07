"""
marzban_configurator.py — настройка Marzban при добавлении сервера.

При добавлении сервера через POST /server/servers/add:
1. Авторизуемся в Marzban
2. Применяем стандартный Xray конфиг (VLESS TCP Reality)
3. Получаем список inbounds и возвращаем их
"""

import httpx
import json


# ──────────────────────────────────────────────────────────
#  СТАНДАРТНЫЙ XRAY КОНФИГ
#  Применяется автоматически при добавлении сервера.
#  privateKey и shortIds генерируются на сервере через
#  xray x25519 — передаются как параметры.
# ──────────────────────────────────────────────────────────

def build_xray_config(
    private_key: str,
    short_ids: list[str],
    sni_domain: str = "cdnjs.com",
    port: int = 443,
) -> dict:
    """
    Собирает стандартный Xray конфиг для Marzban.
    Конфиг идентичен тому, что генерирует Marzban UI при создании
    VLESS TCP Reality inbound — проверен на Marzban 0.8.4.
    """
    return {
        "log": {
            "loglevel": "info",
            "dnsLog": True,
        },
        "dns": {
            "servers": [
                "tcp+local://8.8.8.8",
                "tcp+local://8.8.4.4",
                "tcp+local://1.1.1.1",
                "tcp+local://1.0.0.1",
            ],
            "disableCache": False,
            "queryStrategy": "UseIPv4",
        },
        "inbounds": [
            {
                "tag": "VLESS TCP REALITY",
                "listen": "0.0.0.0",
                "port": port,
                "protocol": "vless",
                "settings": {
                    "clients": [],
                    "decryption": "none",
                },
                "streamSettings": {
                    "network": "tcp",
                    "tcpSettings": {},
                    "security": "reality",
                    "realitySettings": {
                        "show": False,
                        "dest": f"{sni_domain}:443",
                        "xver": 0,
                        "serverNames": [sni_domain],
                        "privateKey": private_key,
                        "shortIds": short_ids,
                    },
                },
                "sniffing": {
                    "enabled": True,
                    "destOverride": ["http", "tls", "quic"],
                },
            },
        ],
        "outbounds": [
            {
                "protocol": "freedom",
                "tag": "DIRECT",
                "settings": {
                    "domainStrategy": "UseIP",
                },
            },
            {
                "protocol": "blackhole",
                "tag": "BLOCK",
            },
            {
                "protocol": "dns",
                "tag": "dns-out",
            },
        ],
        "routing": {
            "domainStrategy": "IPIfNonMatch",
            "rules": [
                {
                    "domain": ["geosite:private"],
                    "outboundTag": "BLOCK",
                },
                {
                    "ip": ["geoip:private"],
                    "outboundTag": "BLOCK",
                },
                {
                    "protocol": ["bittorrent"],
                    "outboundTag": "BLOCK",
                },
                {
                    "type": "field",
                    "network": "tcp,udp",
                    "port": 53,
                    "outboundTag": "dns-out",
                },
                {
                    "outboundTag": "DIRECT",
                    "domain": [
                        "full:cp.cloudflare.com",
                        "domain:msftconnecttest.com",
                        "domain:msftncsi.com",
                        "domain:connectivitycheck.gstatic.com",
                        "domain:captive.apple.com",
                        "full:detectportal.firefox.com",
                        "domain:networkcheck.kde.org",
                    ],
                    "type": "field",
                },
            ],
        },
    }


# ──────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────

async def _get_token(
    client: httpx.AsyncClient,
    base_url: str,
    username: str,
    password: str
) -> str:
    resp = await client.post(
        f"{base_url}/api/admin/token",
        data={"username": username, "password": password},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Marzban: HTTP {resp.status_code} при авторизации")
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError("Marzban: не получен access_token")
    return token


# ──────────────────────────────────────────────────────────
#  ГЕНЕРАЦИЯ КЛЮЧЕЙ X25519 ЧЕРЕЗ MARZBAN
# ──────────────────────────────────────────────────────────

async def generate_xray_keys(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    container_name: str = "marzban-marzban-1",
) -> dict:
    """
    Генерирует пару X25519 ключей через xray x25519 внутри контейнера.
    Выполняется через SSH на сервере — вызывается из admin/server.py.

    Возвращает:
        {"success": True, "private_key": "...", "public_key": "..."}
    """
    # Ключи генерируются через SSH в ssh_service, здесь не нужны
    # Этот метод — заглушка, реальная генерация в admin/server.py через asyncssh
    return {"success": False, "error": "Используй generate_reality_keys из ssh_service"}


# ──────────────────────────────────────────────────────────
#  ПРИМЕНЕНИЕ XRAY КОНФИГА
# ──────────────────────────────────────────────────────────

async def apply_xray_config(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    private_key: str,
    short_ids: list[str],
    sni_domain: str = "cdnjs.com",
    port: int = 443,
) -> dict:
    """
    Применяет стандартный Xray конфиг на Marzban-сервере.

    Вызывается автоматически при добавлении сервера через API.
    После применения Xray перезапускается внутри Marzban.

    Возвращает:
        {"success": True, "inbounds": {"vless": ["VLESS TCP REALITY"]}}
    """
    try:
        config = build_xray_config(
            private_key=private_key,
            short_ids=short_ids,
            sni_domain=sni_domain,
            port=port,
        )

        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            headers = {"Authorization": f"Bearer {token}"}

            # Применяем конфиг
            resp = await client.put(
                f"{marzban_url}/api/core/config",
                json=config,
                headers=headers,
            )
            if resp.status_code not in (200, 201):
                return {
                    "success": False,
                    "error": f"Marzban: HTTP {resp.status_code} при применении конфига — {resp.text[:200]}"
                }

            # Перезапускаем Xray core
            restart_resp = await client.post(
                f"{marzban_url}/api/core/restart",
                headers=headers,
            )
            if restart_resp.status_code not in (200, 204):
                return {
                    "success": False,
                    "error": f"Marzban: конфиг применён но Xray не перезапустился ({restart_resp.status_code})"
                }

            # Ждём пока Xray полностью поднимется после рестарта
            import asyncio
            await asyncio.sleep(3)

            # Получаем список inbounds после применения
            inbounds_resp = await client.get(
                f"{marzban_url}/api/inbounds",
                headers=headers,
            )
            inbounds = inbounds_resp.json() if inbounds_resp.status_code == 200 else {}

            return {
                "success": True,
                "inbounds": inbounds,
                "config_applied": True,
            }

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ТЕКУЩЕГО КОНФИГА
# ──────────────────────────────────────────────────────────

async def get_xray_config(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    """Возвращает текущий Xray конфиг с сервера."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.get(
                f"{marzban_url}/api/core/config",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            return {"success": True, "config": resp.json()}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ INBOUNDS (без изменений конфига)
# ──────────────────────────────────────────────────────────

async def check_and_get_inbounds(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    """Проверяет соединение и возвращает доступные inbounds."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.get(
                f"{marzban_url}/api/inbounds",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            return {"success": True, "inbounds": resp.json()}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  СИСТЕМНАЯ СТАТИСТИКА
# ──────────────────────────────────────────────────────────

async def get_marzban_system_stats(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.get(
                f"{marzban_url}/api/system",
                headers={"Authorization": f"Bearer {token}"},
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            return {"success": True, **resp.json()}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}