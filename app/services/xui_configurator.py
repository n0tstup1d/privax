"""
xui_configurator.py — настройка VLESS TCP Reality инбаунда на сервере 3x-ui (MHSanaei форк).

Отличия от оригинала:
- API путь: /panel/api/ вместо /xui/API/
- publicKey лежит в realitySettings.settings.publicKey — не надо вычислять из privateKey
- base_path — секретный путь панели (например /IowuXQyUA8bB)
"""

import asyncssh
import httpx
import json
import secrets
import os
import base64
from dotenv import load_dotenv
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding, PublicFormat, PrivateFormat, NoEncryption
)

load_dotenv()

SSH_KEY_PATH  = os.getenv("SSH_KEY_PATH")
SSH_USER      = os.getenv("SSH_USER", "root")

MIN_SHORT_IDS = 8


# ──────────────────────────────────────────────────────────
#  ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ──────────────────────────────────────────────────────────

def _generate_x25519_keypair() -> tuple[str, str]:
    """
    Генерирует пару ключей X25519 для Reality локально.
    Возвращает (private_key_b64url, public_key_b64url).
    """
    priv = X25519PrivateKey.generate()
    priv_bytes = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    pub_bytes  = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    priv_b64 = base64.urlsafe_b64encode(priv_bytes).rstrip(b'=').decode()
    pub_b64  = base64.urlsafe_b64encode(pub_bytes).rstrip(b'=').decode()
    return priv_b64, pub_b64


async def _login(client: httpx.AsyncClient, local_port: int, username: str, password: str, base_path: str = "") -> None:
    base_path = base_path.rstrip("/")
    resp = await client.post(
        f"http://127.0.0.1:{local_port}{base_path}/login",
        data={"username": username, "password": password}
    )
    if resp.status_code != 200:
        raise RuntimeError(f"3x-ui: HTTP {resp.status_code} при логине")
    body = resp.json()
    if not body.get("success"):
        raise RuntimeError(f"3x-ui: неверный логин — {body.get('msg', 'ошибка')}")


def _generate_short_ids(count: int) -> list:
    actual = max(count, MIN_SHORT_IDS)
    return [secrets.token_hex(8) for _ in range(actual)]


def _parse(value):
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return {}


# ──────────────────────────────────────────────────────────
#  СОЗДАНИЕ / ОБНОВЛЕНИЕ TCP REALITY ИНБАУНДА
# ──────────────────────────────────────────────────────────

async def ensure_tcp_reality_inbound(
    ip: str,
    ssh_port: int,
    panel_port: int,
    trusted_domains: list,
    xui_admin_user: str = "",
    xui_admin_pass: str = "",
    panel_path: str = "",
    inbound_port: int = 443,
) -> dict:
    """
    Создаёт или обновляет VLESS TCP Reality инбаунд на порту 443.

    Логика:
    1. Получаем список инбаундов
    2. Ищем VLESS на порту 443
       - Если нашли — обновляем serverNames/target, сохраняем ключи и клиентов
       - Если нет — создаём с нуля, генерируем X25519 ключи локально
    3. Возвращает public_key, short_ids, server_names

    Настройки:
    - Transmission: TCP (RAW)
    - Security: Reality, uTLS: chrome
    - Sniffing: HTTP/TLS/QUIC/FAKEDNS, routeOnly: true
    """
    if not trusted_domains:
        return {"success": False, "error": "Нет доменов-масок"}

    base = panel_path.rstrip("/")

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

                async with httpx.AsyncClient(follow_redirects=True) as client:
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base)

                    sni_domain = trusted_domains[0]

                    list_resp = await client.get(
                        f"http://127.0.0.1:{local_port}{base}/panel/api/inbounds/list"
                    )
                    if list_resp.status_code != 200:
                        return {"success": False, "error": f"Не удалось получить инбаунды: {list_resp.status_code}"}

                    existing_inbound = None
                    for inb in list_resp.json().get("obj", []):
                        if inb.get("port") == inbound_port and inb.get("protocol") == "vless":
                            existing_inbound = inb
                            break

                    if existing_inbound:
                        # Сохраняем старые ключи — не сломаем существующих клиентов
                        ex_stream  = _parse(existing_inbound.get("streamSettings", {}))
                        ex_reality = _parse(ex_stream.get("realitySettings", {}))
                        ex_inner   = _parse(ex_reality.get("settings", {}))
                        private_key  = ex_reality.get("privateKey", "")
                        public_key   = ex_inner.get("publicKey", "")
                        mldsa_seed   = ex_reality.get("mldsa65Seed", "")
                        mldsa_verify = ex_inner.get("mldsa65Verify", "")
                        short_ids    = ex_reality.get("shortIds", _generate_short_ids(0))
                        if not private_key or not public_key:
                            private_key, public_key = _generate_x25519_keypair()
                            mldsa_seed   = ""
                            mldsa_verify = ""
                    else:
                        private_key, public_key = _generate_x25519_keypair()
                        mldsa_seed   = ""
                        mldsa_verify = ""
                        short_ids    = _generate_short_ids(0)

                    reality_settings = {
                        "show":         False,
                        "xver":         0,
                        "target":       f"{sni_domain}:443",
                        "serverNames":  trusted_domains,
                        "privateKey":   private_key,
                        "minClientVer": "",
                        "maxClientVer": "",
                        "maxTimediff":  0,
                        "shortIds":     short_ids,
                        "settings": {
                            "publicKey":     public_key,
                            "fingerprint":   "chrome",
                            "serverName":    "",
                            "spiderX":       "/",
                            "mldsa65Verify": mldsa_verify,
                        }
                    }
                    if mldsa_seed:
                        reality_settings["mldsa65Seed"] = mldsa_seed

                    stream_settings = {
                        "network":         "tcp",
                        "security":        "reality",
                        "externalProxy":   [],
                        "realitySettings": reality_settings,
                        "tcpSettings": {
                            "acceptProxyProtocol": False,
                            "header": {"type": "none"}
                        }
                    }

                    sniffing = {
                        "enabled":      True,
                        "destOverride": ["http", "tls", "quic", "fakedns"],
                        "metadataOnly": False,
                        "routeOnly":    True
                    }

                    inbound_payload = {
                        "enable":         True,
                        "remark":         "",
                        "listen":         "",
                        "port":           inbound_port,
                        "protocol":       "vless",
                        "expiryTime":     0,
                        "settings":       json.dumps({
                            "clients":    [],
                            "decryption": "none",
                            "encryption": "none"
                        }),
                        "streamSettings": json.dumps(stream_settings),
                        "sniffing":       json.dumps(sniffing),
                        "tag":            f"inbound-{inbound_port}",
                    }

                    if existing_inbound:
                        # Сохраняем существующих клиентов
                        existing_settings = _parse(existing_inbound.get("settings", "{}"))
                        inbound_payload["settings"] = json.dumps({
                            "clients":    existing_settings.get("clients", []),
                            "decryption": "none",
                            "encryption": "none"
                        })
                        inbound_payload["id"] = existing_inbound["id"]

                        update_resp = await client.post(
                            f"http://127.0.0.1:{local_port}{base}/panel/api/inbounds/update/{existing_inbound['id']}",
                            json=inbound_payload
                        )
                        resp_body = update_resp.json()
                        if not resp_body.get("success"):
                            return {"success": False, "error": f"Ошибка обновления инбаунда: {resp_body.get('msg')}"}
                        action = "updated"
                    else:
                        add_resp = await client.post(
                            f"http://127.0.0.1:{local_port}{base}/panel/api/inbounds/add",
                            json=inbound_payload
                        )
                        resp_body = add_resp.json()
                        if not resp_body.get("success"):
                            return {"success": False, "error": f"Ошибка создания инбаунда: {resp_body.get('msg')}"}
                        action = "created"

                    return {
                        "success":        True,
                        "action":         action,
                        "public_key":     public_key,
                        "short_ids":      short_ids,
                        "server_names":   trusted_domains,
                        "mldsa65_verify": mldsa_verify,
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
#  ДОБАВЛЕНИЕ / УДАЛЕНИЕ shortId
# ──────────────────────────────────────────────────────────

async def update_short_id_on_server(
    ip: str,
    ssh_port: int,
    panel_port: int,
    xui_admin_user: str,
    xui_admin_pass: str,
    new_short_id: str | None,
    remove_short_id: str | None = None,
    base_path: str = ""
) -> dict:
    """Добавляет или удаляет shortId в TCP Reality инбаунде."""
    base = base_path.rstrip("/")

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
                    await _login(client, local_port, xui_admin_user, xui_admin_pass, base)

                    list_resp = await client.get(
                        f"http://127.0.0.1:{local_port}{base}/panel/api/inbounds/list"
                    )
                    if list_resp.status_code != 200:
                        return {"success": False, "error": "Не удалось получить инбаунды"}

                    for inbound in list_resp.json().get("obj", []):
                        if inbound.get("protocol") != "vless":
                            continue

                        stream = _parse(inbound.get("streamSettings", {}))
                        if stream.get("security") != "reality" or stream.get("network") != "tcp":
                            continue

                        reality   = _parse(stream.get("realitySettings", {}))
                        short_ids = reality.get("shortIds", [])

                        if new_short_id and new_short_id not in short_ids:
                            short_ids.append(new_short_id)
                        if remove_short_id and remove_short_id in short_ids:
                            short_ids.remove(remove_short_id)

                        reality["shortIds"]       = short_ids
                        stream["realitySettings"] = reality
                        inbound["streamSettings"] = json.dumps(stream)

                        raw_settings = inbound.get("settings", "{}")
                        if isinstance(raw_settings, dict):
                            inbound["settings"] = json.dumps(raw_settings)

                        await client.post(
                            f"http://127.0.0.1:{local_port}{base}/panel/api/inbounds/update/{inbound['id']}",
                            json=inbound
                        )

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": str(e)}