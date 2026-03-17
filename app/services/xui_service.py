"""
xui_service.py — работа с клиентами 3x-ui (MHSanaei форк) через SSH-туннель.

Отличия MHSanaei форка от оригинала:
- API путь: /panel/api/ вместо /xui/API/
- Клиент содержит доп. поля: comment, created_at, updated_at, security, password
- tgId — integer (0), не строка
- publicKey доступен напрямую: realitySettings.settings.publicKey
- flow = "" для xHTTP (xtls-rprx-vision только для TCP)
"""

import asyncssh
import httpx
import json
import os
import secrets
from datetime import datetime, timedelta
from uuid import uuid4
from dotenv import load_dotenv

load_dotenv()

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USER     = os.getenv("SSH_USER", "root")


# ──────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────

async def _login(client: httpx.AsyncClient, local_port: int, username: str, password: str, base_path: str = "") -> None:
    """Авторизация в 3x-ui. Куки сохраняются в client.cookies автоматически."""
    base_path = base_path.rstrip("/")
    resp = await client.post(
        f"http://127.0.0.1:{local_port}{base_path}/login",
        data={"username": username, "password": password}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"3x-ui: HTTP {resp.status_code} при логине")
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"3x-ui: неверный логин/пароль — {body.get('msg', 'ошибка')}")


def _parse(value):
    """3x-ui хранит settings/streamSettings как JSON-строку. Парсим безопасно."""
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


async def _get_reality_inbound(
    client: httpx.AsyncClient,
    local_port: int,
    base_path: str = "",
    inbound_type: str = "xhttp_reality"
) -> dict:
    """
    Универсальный геттер — возвращает Reality инбаунд нужного типа.
    inbound_type: 'xhttp_reality' или 'tcp_reality'
    """
    base_path = base_path.rstrip("/")
    resp = await client.get(f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/list")
    if resp.status_code != 200:
        raise RuntimeError(f"3x-ui: не удалось получить инбаунды ({resp.status_code})")

    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"3x-ui: ошибка при получении инбаундов — {body.get('msg')}")

    target_network = "xhttp" if inbound_type == "xhttp_reality" else "tcp"

    for inbound in body.get("obj", []):
        if not inbound.get("enable", True):
            continue
        if inbound.get("protocol") != "vless":
            continue

        stream = _parse(inbound.get("streamSettings", {}))

        if stream.get("security") != "reality":
            continue
        if stream.get("network") != target_network:
            continue

        settings      = _parse(inbound.get("settings", {}))
        reality       = _parse(stream.get("realitySettings", {}))
        reality_inner = _parse(reality.get("settings", {}))
        public_key    = reality_inner.get("publicKey", "")

        inbound["_stream_parsed"]   = stream
        inbound["_settings_parsed"] = settings
        inbound["_public_key"]      = public_key
        inbound["_mldsa65_verify"]  = reality_inner.get("mldsa65Verify", "")
        inbound["_reality"]         = reality

        # xHTTP специфичные поля
        if target_network == "xhttp":
            xhttp = _parse(stream.get("xhttpSettings", {}))
            inbound["_xhttp_path"] = xhttp.get("path", "/pwa/v1/update")
            inbound["_xhttp_host"] = xhttp.get("host", "")
        else:
            inbound["_xhttp_path"] = ""
            inbound["_xhttp_host"] = ""

        return inbound

    raise RuntimeError(
        f"3x-ui: не найден активный VLESS {target_network.upper()} Reality инбаунд."
    )



# ──────────────────────────────────────────────────────────
#  ПРОВЕРКА СОЕДИНЕНИЯ
# ──────────────────────────────────────────────────────────

async def check_xui_connection(
    ip: str,
    ssh_port: int,
    panel_port: int,
    username: str,
    password: str,
    base_path: str = ""
) -> dict:
    """Проверяет SSH-доступ и авторизацию в 3x-ui."""
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()
                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, username, password, base_path)
                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  СОЗДАНИЕ КЛИЕНТА
# ──────────────────────────────────────────────────────────

async def create_xui_client(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    username: str,
    expire_days: int,
    expire_at: datetime | None = None,
    device_limit: int | None = None,
    base_path: str = "",
    inbound_type: str = "xhttp_reality",  # 'xhttp_reality' или 'tcp_reality'
) -> dict:
    """
    Добавляет клиента в xHTTP Reality инбаунд 3x-ui (MHSanaei форк).

    Возвращает:
        {
            "success": True,
            "uuid": "...",
            "inbound_id": 1,
            "email": "privax_42_abc1",
            "xhttp_path": "/pwa/v1/update",
            "xhttp_host": "www.microsoft.com",
            "public_key": "KeMa366S8h...",   # ← берём из инбаунда напрямую
            "short_ids": [...],               # ← список shortIds из инбаунда
        }
    """
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base_path)

                    inbound    = await _get_reality_inbound(client, local_port, base_path, inbound_type)
                    inbound_id = inbound["id"]
                    xhttp_path = inbound["_xhttp_path"]
                    xhttp_host = inbound["_xhttp_host"]
                    public_key = inbound["_public_key"]
                    short_ids  = inbound["_reality"].get("shortIds", [])
                    is_tcp     = inbound_type == "tcp_reality"

                    client_uuid = str(uuid4())
                    import calendar
                    if expire_at is not None:
                        # expire_at — naive UTC datetime; используем calendar.timegm чтобы
                        # не получить смещение локального timezone сервера (баг с .timestamp())
                        expire_ms = int(calendar.timegm(expire_at.timetuple()) * 1000)
                    else:
                        dt = datetime.utcnow() + timedelta(days=expire_days)
                        expire_ms = int(calendar.timegm(dt.timetuple()) * 1000)
                    now_ms = int(calendar.timegm(datetime.utcnow().timetuple()) * 1000)

                    # MHSanaei форк — расширенная структура клиента
                    # speedLimit убран: не поддерживается в addClient API форка
                    new_client = {
                        "id":           client_uuid,
                        "email":        username,
                        "limitIp":      device_limit or 0,
                        "totalGB":      0,
                        "expiryTime":   expire_ms,
                        "enable":       True,
                        "flow":         "xtls-rprx-vision" if is_tcp else "",
                        "subId":        secrets.token_hex(8),
                        "tgId":         0,
                        "reset":        0,
                        "comment":      "",
                        "created_at":   now_ms,
                        "updated_at":   now_ms,
                    }

                    payload = {
                        "id":       inbound_id,
                        "settings": json.dumps({"clients": [new_client]})
                    }

                    resp = await client.post(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/addClient",
                        json=payload
                    )

                    if resp.status_code != 200:
                        return {"success": False, "error": f"3x-ui: HTTP {resp.status_code} при создании клиента"}

                    body = resp.json()
                    if not body.get("success"):
                        return {"success": False, "error": f"3x-ui: {body.get('msg', 'ошибка создания')}"}

                    reality      = inbound["_reality"]
                    server_names = reality.get("serverNames", [])
                    sni          = server_names[0] if server_names else xhttp_host
                    sid          = short_ids[0] if short_ids else ""

                    if is_tcp:
                        vless_link = (
                            f"vless://{client_uuid}@{ip}:443?"
                            f"type=tcp"
                            f"&encryption=none"
                            f"&flow=xtls-rprx-vision"
                            f"&security=reality"
                            f"&pbk={public_key}"
                            f"&fp=chrome"
                            f"&sni={sni}"
                            f"&sid={sid}"
                            f"&spx=%2F"
                            f"#{username}"
                        )
                    else:
                        from urllib.parse import quote
                        path_encoded = quote(xhttp_path, safe="")
                        vless_link = (
                            f"vless://{client_uuid}@{ip}:443?"
                            f"type=xhttp"
                            f"&encryption=none"
                            f"&path={path_encoded}"
                            f"&host={xhttp_host}"
                            f"&mode=auto"
                            f"&security=reality"
                            f"&pbk={public_key}"
                            f"&fp=chrome"
                            f"&sni={sni}"
                            f"&sid={sid}"
                            f"&spx=%2F"
                            f"#{username}"
                        )

                    return {
                        "success":    True,
                        "uuid":       client_uuid,
                        "inbound_id": inbound_id,
                        "email":      username,
                        "vless_link": vless_link,
                        "public_key": public_key,
                        "xhttp_path": xhttp_path,
                    }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПОЛУЧЕНИЕ ВРЕМЕНИ ИСТЕЧЕНИЯ КЛИЕНТА
# ──────────────────────────────────────────────────────────

async def get_xui_client_expiry(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    xui_uuid: str,
    xui_inbound_id: int,
    base_path: str = ""
) -> dict:
    """
    Возвращает точное время истечения клиента из панели 3x-ui.

    Возвращает:
        {"success": True, "expire_at": datetime}  — если клиент найден
        {"success": False, "error": "..."}        — если нет
    """
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base_path)

                    resp = await client.get(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/list"
                    )
                    if resp.status_code != 200:
                        return {"success": False, "error": f"3x-ui: HTTP {resp.status_code}"}

                    body = resp.json()
                    if not body.get("success"):
                        return {"success": False, "error": f"3x-ui: {body.get('msg')}"}

                    for inbound in body.get("obj", []):
                        if inbound.get("id") != xui_inbound_id:
                            continue
                        settings = _parse(inbound.get("settings", {}))
                        for c in settings.get("clients", []):
                            if c.get("id") == xui_uuid:
                                expiry_ms = c.get("expiryTime", 0)
                                if not expiry_ms:
                                    return {"success": False, "error": "expiryTime = 0 (бессрочный)"}
                                expire_at = datetime.utcfromtimestamp(expiry_ms / 1000)
                                return {"success": True, "expire_at": expire_at}

                    return {"success": False, "error": "Клиент не найден в инбаунде"}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  УДАЛЕНИЕ КЛИЕНТА
# ──────────────────────────────────────────────────────────

async def delete_xui_client(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    xui_uuid: str,
    xui_inbound_id: int,
    base_path: str = ""
) -> dict:
    """Удаляет клиента из 3x-ui."""
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base_path)

                    resp = await client.post(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/{xui_inbound_id}/delClient/{xui_uuid}"
                    )

                    if resp.status_code != 200:
                        return {"success": False, "error": f"3x-ui: HTTP {resp.status_code} при удалении"}

                    body = resp.json()
                    if not body.get("success"):
                        return {"success": False, "error": f"3x-ui: {body.get('msg', 'ошибка удаления')}"}

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ВКЛЮЧЕНИЕ / ВЫКЛЮЧЕНИЕ КЛИЕНТА
# ──────────────────────────────────────────────────────────

async def toggle_xui_client(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    xui_uuid: str,
    xui_inbound_id: int,
    active: bool,
    base_path: str = ""
) -> dict:
    """Включает или выключает клиента."""
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base_path)

                    # Получаем текущие данные клиента из инбаунда
                    list_resp = await client.get(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/list"
                    )
                    client_data = {}
                    if list_resp.status_code == 200:
                        for inb in list_resp.json().get("obj", []):
                            if inb.get("id") != xui_inbound_id:
                                continue
                            for c in _parse(inb.get("settings", {})).get("clients", []):
                                if c.get("id") == xui_uuid:
                                    client_data = c
                                    break

                    now_ms = int(datetime.utcnow().timestamp() * 1000)
                    updated_client = {
                        **client_data,
                        "enable":     active,
                        "flow":       "",
                        "tgId":       client_data.get("tgId", 0),
                        "updated_at": now_ms,
                    }

                    payload = {
                        "id":       xui_inbound_id,
                        "settings": json.dumps({"clients": [updated_client]})
                    }

                    resp = await client.post(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/updateClient/{xui_uuid}",
                        json=payload
                    )

                    if resp.status_code != 200:
                        return {"success": False, "error": f"3x-ui: HTTP {resp.status_code}"}

                    body = resp.json()
                    if not body.get("success"):
                        return {"success": False, "error": f"3x-ui: {body.get('msg', 'ошибка')}"}

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


# ──────────────────────────────────────────────────────────
#  ПРОДЛЕНИЕ ПОДПИСКИ
# ──────────────────────────────────────────────────────────

async def extend_xui_client(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    xui_uuid: str,
    xui_inbound_id: int,
    extra_days: int,
    base_path: str = ""
) -> dict:
    """Продлевает подписку клиента на extra_days дней."""
    base_path = base_path.rstrip("/")
    try:
        async with asyncssh.connect(
            host=ip, port=ssh_port,
            username=SSH_USER, client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1", listen_port=0,
                dest_host="127.0.0.1", dest_port=panel_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base_path)

                    list_resp = await client.get(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/list"
                    )
                    if list_resp.status_code != 200:
                        return {"success": False, "error": "Не удалось получить список инбаундов"}

                    target_client = None
                    for inb in list_resp.json().get("obj", []):
                        if inb.get("id") != xui_inbound_id:
                            continue
                        for c in _parse(inb.get("settings", {})).get("clients", []):
                            if c.get("id") == xui_uuid:
                                target_client = c
                                break
                        if target_client:
                            break

                    if not target_client:
                        return {"success": False, "error": f"Клиент {xui_uuid} не найден"}

                    now_ms            = int(datetime.utcnow().timestamp() * 1000)
                    current_expire_ms = target_client.get("expiryTime", 0)
                    base_ms           = current_expire_ms if current_expire_ms and current_expire_ms > now_ms else now_ms
                    new_expire_ms     = base_ms + extra_days * 24 * 60 * 60 * 1000

                    updated = {
                        **target_client,
                        "expiryTime": new_expire_ms,
                        "flow":       "",
                        "updated_at": now_ms,
                    }

                    payload = {
                        "id":       xui_inbound_id,
                        "settings": json.dumps({"clients": [updated]})
                    }

                    resp = await client.post(
                        f"http://127.0.0.1:{local_port}{base_path}/panel/api/inbounds/updateClient/{xui_uuid}",
                        json=payload
                    )

                    if resp.status_code != 200:
                        return {"success": False, "error": f"3x-ui: HTTP {resp.status_code}"}

                    body = resp.json()
                    if not body.get("success"):
                        return {"success": False, "error": f"3x-ui: {body.get('msg', 'ошибка')}"}

                    return {
                        "success":       True,
                        "new_expire_ms": new_expire_ms,
                        "new_expire_dt": datetime.utcfromtimestamp(new_expire_ms / 1000).isoformat()
                    }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}