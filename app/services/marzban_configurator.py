import asyncssh
import httpx
import json
import secrets
import os
import base64
from dotenv import load_dotenv

load_dotenv()

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USER = os.getenv("SSH_USER", "root")

# Fingerprints для рандомизации — имитируем разные браузеры
# Чем больше разнообразие у клиентов — тем сложнее DPI выявить паттерн
FINGERPRINTS = ["chrome", "firefox", "safari", "ios", "android", "edge"]

# Минимальное количество shortIds — даже для пустого сервера
MIN_SHORT_IDS = 8


async def _get_token(client: httpx.AsyncClient, local_port: int, username: str, password: str) -> str:
    """Получаем токен Marzban."""
    response = await client.post(
        f"http://127.0.0.1:{local_port}/api/admin/token",
        data={"username": username, "password": password}
    )
    if response.status_code != 200:
        raise RuntimeError(f"Не удалось получить токен: {response.status_code}")
    return response.json()["access_token"]


def _generate_short_ids(count: int) -> list[str]:
    """
    Генерирует уникальные shortId для Reality.

    shortId — 16-символьный HEX код, вшивается в ссылку каждого клиента.
    Xray сервер должен знать shortId клиента чтобы принять соединение.

    Количество = max(current_users_count, MIN_SHORT_IDS)
    Если сервер уже используется — один на каждого клиента.
    Иначе — минимум 8 штук для запаса.
    """
    actual = max(count, MIN_SHORT_IDS)
    return [secrets.token_hex(8) for _ in range(actual)]


def _choose_best_dest(domains: list[str]) -> str:
    """
    Выбирает домен для параметра dest.

    dest — домен, на который Reality перенаправляет подозрительные соединения
    (от сканеров РКН). Сервер отвечает как настоящий этот сайт.
    """
    if not domains:
        raise RuntimeError("Список доменов пуст — нечего выбирать для dest")
    return domains[0]


def _derive_public_key_from_private(private_key_b64: str) -> str:
    """
    Вычисляет publicKey из privateKey через X25519.

    Marzban хранит только privateKey в конфиге Xray.
    publicKey нужен клиентам в ссылке (параметр pbk=).
    Вычисляем сами через cryptography — не нужны SSH вызовы.

    private_key_b64 — base64url строка без padding (формат Xray).
    Возвращает publicKey в том же формате.

    Требует: pip install cryptography
    """
    try:
        from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        # base64url может быть без padding — добавляем
        padded = private_key_b64 + "=" * (-len(private_key_b64) % 4)
        private_bytes = base64.urlsafe_b64decode(padded)

        priv_key = X25519PrivateKey.from_private_bytes(private_bytes)
        pub_bytes = priv_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)

        return base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode()

    except ImportError:
        raise RuntimeError(
            "Установите пакет: pip install cryptography\n"
            "Он нужен для вычисления publicKey из privateKey Reality."
        )
    except Exception as e:
        raise RuntimeError(f"Не удалось вычислить publicKey из privateKey: {e}")


def _patch_inbound_clients_flow(inbound: dict) -> dict:
    """
    Добавляет flow=xtls-rprx-vision каждому клиенту в инбаунде.

    Зачем: XTLS Vision скрывает TLS-in-TLS паттерн VLESS.
    Без него DPI может детектировать Reality по вложенному TLS.
    С ним трафик неотличим от обычного TLS к реальному сайту.

    Применяется к существующим клиентам при настройке сервера.
    Новые клиенты получают flow при создании через marzban_service.py.
    """
    settings = inbound.get("settings", {})
    clients = settings.get("clients", [])
    for c in clients:
        c["flow"] = "xtls-rprx-vision"
    settings["clients"] = clients
    inbound["settings"] = settings
    return inbound


async def configure_server(
    ip: str,
    ssh_port: int,
    marzban_port: int,
    mar_admin_user: str,
    mar_admin_pass: str,
    trusted_domains: list[str],
    current_users_count: int = 0
) -> dict:
    """
    Полностью настраивает сервер под Reality + XTLS-Vision.

    Шаги:
    1. SSH туннель → токен Marzban
    2. Читает текущий конфиг Xray
    3. Для каждого Reality инбаунда:
       - serverNames   = домены (кол-во = current_users_count или все)
       - shortIds      = max(current_users_count, 8) уникальных ID
       - dest          = первый домен
       - fingerprint   = ротация по пулу FINGERPRINTS
       - clients.flow  = xtls-rprx-vision для всех существующих клиентов
    4. Сохраняет конфиг, перезапускает Xray
    5. Вычисляет publicKey из privateKey (X25519 через cryptography)

    Возвращает public_key и short_ids для сохранения в БД.
    """
    if not trusted_domains:
        return {"success": False, "error": "Нет доменов-масок в базе. Добавьте через /admin/domains"}

    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_port,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1",
                listen_port=0,
                dest_host="127.0.0.1",
                dest_port=marzban_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:

                    # Токен
                    token = await _get_token(client, local_port, mar_admin_user, mar_admin_pass)
                    headers = {"Authorization": f"Bearer {token}"}

                    # Читаем конфиг
                    config_response = await client.get(
                        f"http://127.0.0.1:{local_port}/api/core/config",
                        headers=headers
                    )
                    if config_response.status_code != 200:
                        return {"success": False, "error": f"Не удалось прочитать конфиг: {config_response.status_code}"}

                    config = config_response.json()

                    # shortIds: один на каждого клиента, минимум MIN_SHORT_IDS
                    new_short_ids = _generate_short_ids(current_users_count)

                    # serverNames: берём столько доменов сколько клиентов (если > 0)
                    # иначе все домены из базы
                    if current_users_count > 0:
                        domains_to_use = trusted_domains[:max(current_users_count, len(trusted_domains))]
                    else:
                        domains_to_use = trusted_domains

                    best_dest = _choose_best_dest(domains_to_use)
                    updated_count = 0
                    private_key_found = None
                    last_reality = {}

                    for i, inbound in enumerate(config.get("inbounds", [])):
                        stream = inbound.get("streamSettings", {})
                        if stream.get("security") != "reality":
                            continue

                        reality = stream.get("realitySettings", {})
                        reality["serverNames"] = domains_to_use
                        reality["shortIds"] = new_short_ids
                        reality["dest"] = f"{best_dest}:443"
                        # Каждый инбаунд — свой fingerprint из пула (ротация по индексу)
                        reality["fingerprint"] = FINGERPRINTS[i % len(FINGERPRINTS)]

                        stream["realitySettings"] = reality
                        inbound["streamSettings"] = stream

                        # Патчим flow для существующих клиентов
                        inbound = _patch_inbound_clients_flow(inbound)

                        # Сохраняем privateKey для вычисления publicKey
                        if private_key_found is None:
                            private_key_found = reality.get("privateKey")

                        last_reality = reality
                        config["inbounds"][i] = inbound
                        updated_count += 1

                    if updated_count == 0:
                        return {"success": False, "error": "Не найдено ни одного Reality инбаунда на сервере"}

                    # Сохраняем
                    save_response = await client.put(
                        f"http://127.0.0.1:{local_port}/api/core/config",
                        json=config,
                        headers=headers
                    )
                    if save_response.status_code != 200:
                        return {"success": False, "error": f"Не удалось сохранить конфиг: {save_response.text}"}

                    # Перезапускаем Xray
                    restart_response = await client.post(
                        f"http://127.0.0.1:{local_port}/api/core/restart",
                        headers=headers
                    )
                    if restart_response.status_code != 200:
                        return {"success": False, "error": f"Конфиг сохранён, но перезапуск не удался: {restart_response.text}"}

                    # Вычисляем publicKey из privateKey (X25519)
                    # Marzban не хранит publicKey — только privateKey
                    public_key = None
                    if private_key_found:
                        try:
                            public_key = _derive_public_key_from_private(private_key_found)
                        except RuntimeError:
                            public_key = None  # будет None в БД — можно перенастроить через /reconfigure

                    return {
                        "success": True,
                        "public_key": public_key,
                        "short_ids": new_short_ids,
                        "server_names": domains_to_use,
                        "dest": best_dest,
                        "fingerprint": last_reality.get("fingerprint"),
                        "updated_inbounds": updated_count,
                    }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


async def add_short_id_to_server(
    ip: str,
    ssh_port: int,
    marzban_port: int,
    mar_admin_user: str,
    mar_admin_pass: str,
    new_short_id: str | None,
    remove_short_id: str | None = None
) -> dict:
    """
    Добавляет или удаляет shortId в конфиге Xray на сервере.

    Вызывается при покупке подписки — каждый клиент получает
    уникальный shortId, который Xray должен знать для аутентификации.

    Zero-downtime: добавляем в список и перезапускаем.
    Старые shortIds остаются — существующие клиенты не отваливаются.
    """
    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_port,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            async with conn.forward_local_port(
                listen_host="127.0.0.1",
                listen_port=0,
                dest_host="127.0.0.1",
                dest_port=marzban_port
            ) as tunnel:
                local_port = tunnel.get_port()

                async with httpx.AsyncClient() as client:
                    token = await _get_token(client, local_port, mar_admin_user, mar_admin_pass)
                    headers = {"Authorization": f"Bearer {token}"}

                    resp = await client.get(
                        f"http://127.0.0.1:{local_port}/api/core/config",
                        headers=headers
                    )
                    if resp.status_code != 200:
                        return {"success": False, "error": f"Не удалось прочитать конфиг: {resp.status_code}"}

                    config = resp.json()

                    for inbound in config.get("inbounds", []):
                        stream = inbound.get("streamSettings", {})
                        if stream.get("security") != "reality":
                            continue

                        reality = stream.get("realitySettings", {})
                        short_ids = reality.get("shortIds", [])

                        if new_short_id and new_short_id not in short_ids:
                            short_ids.append(new_short_id)
                        if remove_short_id and remove_short_id in short_ids:
                            short_ids.remove(remove_short_id)

                        reality["shortIds"] = short_ids
                        stream["realitySettings"] = reality
                        inbound["streamSettings"] = stream

                    save = await client.put(
                        f"http://127.0.0.1:{local_port}/api/core/config",
                        json=config,
                        headers=headers
                    )
                    if save.status_code != 200:
                        return {"success": False, "error": f"Не удалось сохранить конфиг: {save.text}"}

                    await client.post(
                        f"http://127.0.0.1:{local_port}/api/core/restart",
                        headers=headers
                    )

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": str(e)}