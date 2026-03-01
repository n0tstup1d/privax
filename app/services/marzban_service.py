import asyncssh
import httpx
import os
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USER = os.getenv("SSH_USER", "root")


# --- ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ---

async def _get_token_via_tunnel(client: httpx.AsyncClient, local_port: int, username: str, password: str) -> str:
    """
    Получает токен Marzban через уже открытый туннель.
    Вынесли в отдельную функцию чтобы не дублировать код во всех методах.
    
    client      — httpx клиент (передаём снаружи, внутри туннеля)
    local_port  — локальный порт туннеля (система выбирает сама)
    username    — логин админа Marzban (уже расшифрованный)
    password    — пароль админа Marzban (уже расшифрованный)
    """
    response = await client.post(
        f"http://127.0.0.1:{local_port}/api/admin/token",
        data={"username": username, "password": password}
    )
    if response.status_code != 200:
        raise RuntimeError(f"Не удалось получить токен Marzban: {response.status_code}")
    return response.json()["access_token"]


async def _get_inbounds(client: httpx.AsyncClient, local_port: int, token: str) -> dict:
    """
    Запрашивает у Marzban список инбаундов — протоколов и их тегов.
    
    Возвращает словарь вида:
    {
        "vless": ["VLESS TCP REALITY", "VLESS GRPC REALITY"],
        "vmess": ["VMess TCP", "VMess Websocket"]
    }
    
    Почему запрашиваем динамически, а не хардкодим?
    Потому что на каждом сервере инбаунды могут называться по-разному.
    Так сервис работает с любым сервером автоматически.
    """
    headers = {"Authorization": f"Bearer {token}"}
    response = await client.get(
        f"http://127.0.0.1:{local_port}/api/inbounds",
        headers=headers
    )
    if response.status_code != 200:
        raise RuntimeError(f"Не удалось получить инбаунды: {response.status_code}")

    raw = response.json()  # {"vless": [...], "vmess": [...]}

    # Marzban возвращает список объектов, нам нужны только теги (названия)
    result = {}
    for protocol, inbound_list in raw.items():
        result[protocol] = [inbound["tag"] for inbound in inbound_list]

    return result


# --- ОСНОВНЫЕ ФУНКЦИИ ---

async def create_marzban_user(
    ip: str,
    ssh_port: int,
    marzban_port: int,
    mar_admin_user: str,    # уже расшифрованный
    mar_admin_pass: str,    # уже расшифрованный
    marzban_username: str,  # имя которое мы придумали для клиента, например "privax_42"
    expire_days: int        # на сколько дней создаём (plan.months * 30)
) -> dict:
    """
    Создаёт пользователя в Marzban.
    
    Возвращает:
        {"success": True, "subscription_url": "https://..."}
        {"success": False, "error": "..."}
    
    Что происходит внутри:
    1. Открываем SSH туннель к серверу
    2. Получаем токен админа
    3. Запрашиваем список инбаундов (чтобы подключить все протоколы)
    4. Создаём пользователя со всеми инбаундами и датой истечения
    5. Возвращаем subscription_url
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
                    # Шаг 1 — получаем токен
                    token = await _get_token_via_tunnel(client, local_port, mar_admin_user, mar_admin_pass)
                    headers = {"Authorization": f"Bearer {token}"}

                    # Шаг 2 — узнаём какие инбаунды есть на этом сервере
                    inbounds = await _get_inbounds(client, local_port, token)

                    # Шаг 3 — считаем дату истечения в unix timestamp
                    # Marzban принимает expire как unix timestamp (секунды с 1970 года)
                    # expire=0 означает безлимит, поэтому нам нужно явно указать дату
                    expire_timestamp = int((datetime.utcnow() + timedelta(days=expire_days)).timestamp())

                    # Шаг 4 — создаём пользователя
                    payload = {
                        "username": marzban_username,
                        "proxies": {proto: {} for proto in inbounds.keys()},  # {"vless": {}, "vmess": {}}
                        "inbounds": inbounds,           # все инбаунды сервера
                        "expire": expire_timestamp,
                        "data_limit": 0,                # безлимитный трафик
                        "data_limit_reset_strategy": "no_reset",
                        "status": "active"
                    }

                    response = await client.post(
                        f"http://127.0.0.1:{local_port}/api/user",
                        json=payload,
                        headers=headers
                    )

                    if response.status_code != 200:
                        return {"success": False, "error": f"Marzban вернул {response.status_code}: {response.text}"}

                    data = response.json()
                    return {
                        "success": True,
                        "subscription_url": data.get("subscription_url")
                    }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except RuntimeError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


async def delete_marzban_user(
    ip: str,
    ssh_port: int,
    marzban_port: int,
    mar_admin_user: str,
    mar_admin_pass: str,
    marzban_username: str
) -> dict:
    """
    Удаляет пользователя из Marzban.
    Вызывается когда подписка истекла или клиент отписался.
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
                    token = await _get_token_via_tunnel(client, local_port, mar_admin_user, mar_admin_pass)
                    headers = {"Authorization": f"Bearer {token}"}

                    response = await client.delete(
                        f"http://127.0.0.1:{local_port}/api/user/{marzban_username}",
                        headers=headers
                    )

                    if response.status_code != 200:
                        return {"success": False, "error": f"Marzban вернул {response.status_code}"}

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}


async def toggle_marzban_user(
    ip: str,
    ssh_port: int,
    marzban_port: int,
    mar_admin_user: str,
    mar_admin_pass: str,
    marzban_username: str,
    active: bool            # True = включить, False = выключить
) -> dict:
    """
    Включает или выключает пользователя в Marzban.
    
    Пригодится например когда:
    - Клиент не оплатил продление → выключаем (disabled)
    - Клиент оплатил → включаем обратно (active)
    
    Это лучше чем удалять — конфиги у клиента сохраняются,
    просто перестают работать до следующей оплаты.
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
                    token = await _get_token_via_tunnel(client, local_port, mar_admin_user, mar_admin_pass)
                    headers = {"Authorization": f"Bearer {token}"}

                    status = "active" if active else "disabled"
                    response = await client.put(
                        f"http://127.0.0.1:{local_port}/api/user/{marzban_username}",
                        json={"status": status},
                        headers=headers
                    )

                    if response.status_code != 200:
                        return {"success": False, "error": f"Marzban вернул {response.status_code}"}

                    return {"success": True}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Неожиданная ошибка: {str(e)}"}