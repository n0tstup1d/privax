import asyncssh
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

SSH_KEY_PATH = os.getenv("SSH_KEY_PATH")
SSH_USER = os.getenv("SSH_USER", "root")


async def check_and_get_marzban_token(ip: str, ssh_port, marzban_port: int, username: str, password: str) -> dict:
    """
    1. Подключается к серверу по SSH
    2. Пробрасывает туннель к Marzban
    3. Получает токен — проверяет что сервер живой и credentials верные
    4. Закрывает соединение
    """
    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_port,
            username=SSH_USER,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None  # отключаем проверку known_hosts
        ) as conn:

            async with conn.forward_local_port(
                listen_host="127.0.0.1",
                listen_port=0,          # 0 = система сама выберет свободный порт
                dest_host="127.0.0.1",
                dest_port=marzban_port
            ) as tunnel:

                local_port = tunnel.get_port()  # узнаём какой порт выбрала система

                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"http://127.0.0.1:{local_port}/api/admin/token",
                        data={
                            "username": username,
                            "password": password
                        }
                    )

                if response.status_code == 200:
                    token = response.json().get("access_token")
                    return {"success": True, "token": token}
                else:
                    return {"success": False, "error": f"Marzban вернул статус {response.status_code}"}

    except asyncssh.DisconnectError:
        return {"success": False, "error": "Не удалось подключиться по SSH — сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH ключ не подошёл — доступ запрещён"}
    except Exception as e:
        return {"success": False, "error": str(e)}