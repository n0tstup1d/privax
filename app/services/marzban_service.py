"""
marzban_service.py — работа с пользователями через Marzban REST API.

Отличия от 3x-ui:
- Нет SSH-туннелей — работаем напрямую через HTTP(S)
- Аутентификация: POST /api/admin/token → Bearer JWT
- Пользователь идентифицируется по username (строка), не UUID
- expire_at передаётся как Unix-timestamp (int), 0 = бессрочно
- Статус пользователя: "active" | "disabled" | "expired" | "limited" | "on_hold"
- Ссылки отдаёт сам Marzban в поле links[] или subscription_url
"""

import httpx
import asyncssh
import re
from datetime import datetime, timedelta
from typing import Optional

# ──────────────────────────────────────────────────────────
#  АВТОРИЗАЦИЯ
# ──────────────────────────────────────────────────────────

async def _get_token(client: httpx.AsyncClient, base_url: str, username: str, password: str) -> str:
    """Получает Bearer-токен администратора Marzban."""
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
        raise RuntimeError(f"Marzban: не получен access_token — {data}")
    return token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ──────────────────────────────────────────────────────────
#  ПРОВЕРКА СОЕДИНЕНИЯ
# ──────────────────────────────────────────────────────────

async def check_marzban_connection(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    """Проверяет доступность Marzban напрямую."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=10) as client:
            await _get_token(client, marzban_url, admin_username, admin_password)
            return {"success": True, "via": "direct"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


def _extract_marzban_port(marzban_url: str) -> int:
    """Извлекает порт из marzban_url."""
    match = re.search(r":(\d+)$", marzban_url.rstrip("/"))
    if match:
        return int(match.group(1))
    return 443 if marzban_url.startswith("https") else 80


async def _open_marzban_tunnel(
    conn: asyncssh.SSHClientConnection,
    marzban_url: str,
    admin_username: str,
    admin_password: str,
) -> dict:
    """Открывает туннель через уже установленное SSH-соединение и проверяет Marzban."""
    import asyncio
    marzban_port = _extract_marzban_port(marzban_url)
    local_port   = 18000
    scheme       = "https" if marzban_url.startswith("https") else "http"
    tunnel_url   = f"{scheme}://127.0.0.1:{local_port}"

    async with conn.forward_local_port(
        "127.0.0.1", local_port,
        "127.0.0.1", marzban_port,
    ):
        await asyncio.sleep(0.3)
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as client:
                await _get_token(client, tunnel_url, admin_username, admin_password)
                return {"success": True}
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        except Exception as e:
            return {"success": False, "error": f"Туннель установлен, но Marzban не ответил: {e}"}


async def check_marzban_via_ssh(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    ssh_ip: str,
    ssh_port: int,
    ssh_user: str,
    ssh_key_path: str,
    ssh_password: str | None = None,
) -> dict:
    """
    Проверяет Marzban через SSH-туннель.
    Сначала пробует по ключу, потом по паролю (если передан).
    """
    # ── Попытка 1: по SSH ключу ──────────────────────────────
    try:
        async with asyncssh.connect(
            host=ssh_ip, port=ssh_port, username=ssh_user,
            client_keys=[ssh_key_path],
            known_hosts=None, connect_timeout=10,
        ) as conn:
            result = await _open_marzban_tunnel(conn, marzban_url, admin_username, admin_password)
            if result["success"]:
                return {"success": True, "via": "ssh_key_tunnel"}
            return result
    except (asyncssh.PermissionDenied, asyncssh.DisconnectError, FileNotFoundError, Exception):
        pass  # ключ не подошёл — пробуем пароль

    # ── Попытка 2: по паролю ─────────────────────────────────
    if not ssh_password:
        return {
            "success": False,
            "error": "SSH: ключ не подошёл, пароль не передан — туннель невозможен"
        }

    try:
        async with asyncssh.connect(
            host=ssh_ip, port=ssh_port, username=ssh_user,
            password=ssh_password,
            known_hosts=None, connect_timeout=10,
        ) as conn:
            result = await _open_marzban_tunnel(conn, marzban_url, admin_username, admin_password)
            if result["success"]:
                return {"success": True, "via": "ssh_password_tunnel"}
            return result
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: неверный пароль"}
    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: соединение разорвано"}
    except ConnectionRefusedError:
        return {"success": False, "error": f"SSH: порт {ssh_port} недоступен на {ssh_ip}"}
    except Exception as e:
        return {"success": False, "error": f"SSH туннель (пароль): {e}"}


# ──────────────────────────────────────────────────────────
#  СОЗДАНИЕ ПОЛЬЗОВАТЕЛЯ
# ──────────────────────────────────────────────────────────

async def create_marzban_user(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
    expire_days: int,
    expire_at: Optional[datetime] = None,
    data_limit_gb: float = 0,       # 0 = безлимит
    inbounds: Optional[dict] = None,  # {"vless": ["VLESS TCP REALITY"], ...}
) -> dict:
    """
    Создаёт пользователя в Marzban.

    Возвращает:
        {
            "success": True,
            "username": "privax_42_abc1",
            "subscription_url": "https://...",
            "links": ["vless://..."],
            "expire": <unix_timestamp>,
        }
    """
    try:
        if expire_at is not None:
            import calendar
            expire_ts = int(calendar.timegm(expire_at.timetuple()))
        elif expire_days > 0:
            import calendar
            dt = datetime.utcnow() + timedelta(days=expire_days)
            expire_ts = int(calendar.timegm(dt.timetuple()))
        else:
            expire_ts = 0

        _inbounds = inbounds or {}
        # Для каждого протокола формируем правильные proxies
        _proxies = {}
        for proto in (_inbounds.keys() if _inbounds else ["vless"]):
            if proto == "vless":
                _proxies["vless"] = {"flow": "xtls-rprx-vision"}
            else:
                _proxies[proto] = {}

        payload = {
            "username": username,
            "proxies": _proxies,
            "inbounds": _inbounds,
            "expire": expire_ts,
            "data_limit": int(data_limit_gb * 1024 ** 3),
            "data_limit_reset_strategy": "no_reset",
            "status": "active",
        }

        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            headers = _auth_headers(token)

            resp = await client.post(
                f"{marzban_url}/api/user",
                json=payload,
                headers=headers,
            )

            if resp.status_code == 409:
                return {"success": False, "error": f"Пользователь '{username}' уже существует"}
            if resp.status_code not in (200, 201):
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code} — {resp.text[:200]}"}

            user = resp.json()
            return {
                "success": True,
                "username": user["username"],
                "subscription_url": user.get("subscription_url", ""),
                "links": user.get("links", []),
                "expire": user.get("expire", expire_ts),
            }

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ИНФОРМАЦИИ О ПОЛЬЗОВАТЕЛЕ
# ──────────────────────────────────────────────────────────

async def get_marzban_user(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
) -> dict:
    """
    Возвращает данные пользователя из Marzban.
    """
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.get(
                f"{marzban_url}/api/user/{username}",
                headers=_auth_headers(token),
            )
            if resp.status_code == 404:
                return {"success": False, "error": "Пользователь не найден"}
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}

            user = resp.json()
            expire_ts = user.get("expire") or 0
            expire_at = datetime.utcfromtimestamp(expire_ts) if expire_ts else None
            return {
                "success": True,
                "username": user["username"],
                "status": user.get("status", "unknown"),
                "expire": expire_ts,
                "expire_at": expire_at,
                "data_limit": user.get("data_limit", 0),
                "used_traffic": user.get("used_traffic", 0),
                "subscription_url": user.get("subscription_url", ""),
                "links": user.get("links", []),
            }

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  УДАЛЕНИЕ ПОЛЬЗОВАТЕЛЯ
# ──────────────────────────────────────────────────────────

async def delete_marzban_user(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
) -> dict:
    """Удаляет пользователя из Marzban."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.delete(
                f"{marzban_url}/api/user/{username}",
                headers=_auth_headers(token),
            )
            if resp.status_code == 404:
                # Уже удалён — считаем успехом
                return {"success": True}
            if resp.status_code not in (200, 204):
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code} при удалении"}
            return {"success": True}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ВКЛЮЧЕНИЕ / ВЫКЛЮЧЕНИЕ ПОЛЬЗОВАТЕЛЯ
# ──────────────────────────────────────────────────────────

async def toggle_marzban_user(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
    active: bool,
) -> dict:
    """Включает или выключает пользователя (active / disabled)."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            headers = _auth_headers(token)

            resp = await client.put(
                f"{marzban_url}/api/user/{username}",
                json={"status": "active" if active else "disabled"},
                headers=headers,
            )
            if resp.status_code == 404:
                return {"success": False, "error": "Пользователь не найден"}
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            return {"success": True}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПРОДЛЕНИЕ ПОДПИСКИ
# ──────────────────────────────────────────────────────────

async def extend_marzban_user(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
    extra_days: int,
) -> dict:
    """
    Продлевает подписку пользователя на extra_days дней.
    Если подписка уже истекла — продление от текущего момента.
    """
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            headers = _auth_headers(token)

            # Получаем текущий expire
            get_resp = await client.get(
                f"{marzban_url}/api/user/{username}",
                headers=headers,
            )
            if get_resp.status_code == 404:
                return {"success": False, "error": "Пользователь не найден"}
            if get_resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {get_resp.status_code}"}

            user = get_resp.json()
            import calendar
            now_ts = int(calendar.timegm(datetime.utcnow().timetuple()))
            current_expire = user.get("expire") or 0
            base_ts = current_expire if current_expire and current_expire > now_ts else now_ts
            new_expire_ts = base_ts + extra_days * 86400

            put_resp = await client.put(
                f"{marzban_url}/api/user/{username}",
                json={"expire": new_expire_ts, "status": "active"},
                headers=headers,
            )
            if put_resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {put_resp.status_code}"}

            return {
                "success": True,
                "new_expire": new_expire_ts,
                "new_expire_dt": datetime.utcfromtimestamp(new_expire_ts).isoformat(),
            }

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  СБРОС ТРАФИКА
# ──────────────────────────────────────────────────────────

async def reset_marzban_user_traffic(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
) -> dict:
    """Сбрасывает счётчик трафика пользователя."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=15) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.post(
                f"{marzban_url}/api/user/{username}/reset",
                headers=_auth_headers(token),
            )
            if resp.status_code not in (200, 204):
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            return {"success": True}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ССЫЛОК ПОЛЬЗОВАТЕЛЯ
# ──────────────────────────────────────────────────────────

async def get_marzban_user_links(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    username: str,
) -> dict:
    """
    Возвращает актуальные links[] и subscription_url пользователя.
    Используется при сбросе устройств.
    """
    return await get_marzban_user(marzban_url, admin_username, admin_password, username)


# ──────────────────────────────────────────────────────────
#  СПИСОК ВСЕХ ПОЛЬЗОВАТЕЛЕЙ СЕРВЕРА
# ──────────────────────────────────────────────────────────

async def list_marzban_users(
    marzban_url: str,
    admin_username: str,
    admin_password: str,
    offset: int = 0,
    limit: int = 100,
) -> dict:
    """Возвращает список пользователей Marzban (постранично)."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=30) as client:
            token = await _get_token(client, marzban_url, admin_username, admin_password)
            resp = await client.get(
                f"{marzban_url}/api/users",
                params={"offset": offset, "limit": limit},
                headers=_auth_headers(token),
            )
            if resp.status_code != 200:
                return {"success": False, "error": f"Marzban: HTTP {resp.status_code}"}
            data = resp.json()
            return {"success": True, "users": data.get("users", []), "total": data.get("total", 0)}

    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except httpx.ConnectError:
        return {"success": False, "error": "Marzban: сервер недоступен"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}