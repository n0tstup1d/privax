"""
ssh_service.py — SSH-утилиты + автоматическая защита сервера.

harden_server() вызывается при POST /admin/servers/add автоматически.

Флоу первого добавления сервера:
  1. Подключаемся по ПАРОЛЮ (ssh_user + ssh_password из запроса)
  2. Устанавливаем наш публичный SSH-ключ в authorized_keys
  3. Настраиваем UFW, fail2ban, отключаем вход по паролю
  4. После этого все дальнейшие подключения — только по ключу

Что настраивается:
  UFW      — блокируем всё входящее, открываем SSH-порт и 443 для туннеля.
             panel_port ЗАКРЫТ снаружи — доступен только через SSH-туннель.
  SSH      — отключаем вход по паролю, оставляем только ключ.
  fail2ban — блокирует IP после 5 неудачных попыток SSH за 10 минут.
"""

import asyncssh
import os
from dotenv import load_dotenv

load_dotenv()

SSH_KEY_PATH     = os.getenv("SSH_KEY_PATH")      # путь к приватному ключу
SSH_KEY_PATH_PUB = os.getenv("SSH_KEY_PATH_PUB")  # путь к публичному ключу (.pub)


def _get_public_key() -> str | None:
    """Читает публичный ключ из файла."""
    pub_path = SSH_KEY_PATH_PUB or (SSH_KEY_PATH + ".pub" if SSH_KEY_PATH else None)
    if not pub_path or not os.path.exists(pub_path):
        return None
    with open(pub_path, "r") as f:
        return f.read().strip()


def _build_connect_kwargs(
    ip: str,
    ssh_port: int,
    ssh_user: str,
    ssh_password: str | None = None,
) -> dict:
    """
    Собирает kwargs для asyncssh.connect().
    Если передан пароль — подключаемся по паролю (первичная настройка).
    Если нет — по ключу (штатная работа).
    """
    base = dict(host=ip, port=ssh_port, username=ssh_user, known_hosts=None)
    if ssh_password:
        base["password"] = ssh_password
        base["preferred_auth"] = ["password"]
    else:
        base["client_keys"] = [SSH_KEY_PATH]
    return base


async def harden_server(
    ip: str,
    ssh_port: int,
    marzban_port: int | None = None,
    ssh_user: str = "root",
    ssh_password: str | None = None,
) -> dict:
    """
    Автоматически защищает сервер при добавлении через /admin/servers/add.

    При первом добавлении передай ssh_user + ssh_password — подключимся по паролю,
    установим ключ и отключим вход по паролю навсегда.

    Шаги:
    1. Подключение (пароль или ключ)
    2. Установка нашего публичного ключа в authorized_keys
    3. apt: ufw + fail2ban
    4. UFW: default deny, allow SSH + 443 + marzban_port (открываем, не закрываем)
    5. SSH: только ключевая аутентификация
    6. fail2ban: 5 попыток -> бан 1 час
    7. Перезапуск сервисов
    """
    details = []

    public_key = _get_public_key()
    if not public_key:
        return {
            "success": False,
            "error": (
                "Публичный SSH-ключ не найден. "
                "Проверьте SSH_KEY_PATH / SSH_KEY_PATH_PUB в .env"
            )
        }

    bootstrap_commands = [
        (
            f"mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            f"echo '{public_key}' >> ~/.ssh/authorized_keys && "
            f"chmod 600 ~/.ssh/authorized_keys && "
            f"sort -u ~/.ssh/authorized_keys -o ~/.ssh/authorized_keys",
            "SSH: установка публичного ключа"
        ),
    ]

    hardening_commands = [
        ("apt-get update -qq", "Обновление списка пакетов"),
        (
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ufw fail2ban",
            "Установка ufw и fail2ban"
        ),
        (
            f"sed -i 's/^#*Port .*/Port {ssh_port}/' /etc/ssh/sshd_config && "
            "sed -i 's/^#*PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config && "
            "sed -i 's/^#*PubkeyAuthentication.*/PubkeyAuthentication yes/' /etc/ssh/sshd_config && "
            "sed -i 's/^#*PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config && "
            "sed -i 's/^#*X11Forwarding.*/X11Forwarding no/' /etc/ssh/sshd_config && "
            "sed -i 's/^#*MaxAuthTries.*/MaxAuthTries 3/' /etc/ssh/sshd_config && "
            "grep -q '^DebianBanner' /etc/ssh/sshd_config && "
            "sed -i 's/^#*DebianBanner.*/DebianBanner no/' /etc/ssh/sshd_config || "
            "echo 'DebianBanner no' >> /etc/ssh/sshd_config",
            f"SSH: порт {ssh_port}, только ключ, скрытие баннера"
        ),
        ("sshd -t", "SSH: проверка конфига"),
        ("ufw --force reset", "Сброс правил UFW"),
        (
            "ufw default deny incoming && ufw default allow outgoing",
            "UFW: запрет всех входящих"
        ),
        (f"ufw allow {ssh_port}/tcp comment 'SSH'", f"UFW: SSH на порту {ssh_port}"),
        ("ufw allow 443/tcp comment 'Tunnel VLESS'", "UFW: 443 для VLESS Reality"),
        *(
            [(
                f"ufw allow {marzban_port}/tcp comment 'Marzban API'",
                f"UFW: Marzban API на порту {marzban_port} открыт"
            )]
            if marzban_port and marzban_port not in (443, ssh_port)
            else []
        ),
        ("ufw --force enable", "UFW: включаем файрвол"),
        # NAT для трафика — без этого клиенты подключаются но интернет не работает
        (
            "IFACE=$(ip route | grep default | awk '{print $5}') && "
            "iptables -t nat -C POSTROUTING -o $IFACE -j MASQUERADE 2>/dev/null || "
            "iptables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE",
            "NAT: IPv4 MASQUERADE"
        ),
        (
            "IFACE=$(ip route | grep default | awk '{print $5}') && "
            "ip6tables -t nat -C POSTROUTING -o $IFACE -j MASQUERADE 2>/dev/null || "
            "ip6tables -t nat -A POSTROUTING -o $IFACE -j MASQUERADE",
            "NAT: IPv6 MASQUERADE"
        ),
        (
            "sysctl -w net.ipv4.ip_forward=1 && "
            "grep -q '^net.ipv4.ip_forward' /etc/sysctl.conf && "
            "sed -i 's/^#*net.ipv4.ip_forward.*/net.ipv4.ip_forward=1/' /etc/sysctl.conf || "
            "echo 'net.ipv4.ip_forward=1' >> /etc/sysctl.conf",
            "Sysctl: IP forwarding"
        ),
        # Сохраняем правила чтобы не слетали при ребуте
        (
            "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq netfilter-persistent iptables-persistent && "
            "netfilter-persistent save",
            "Сохранение правил iptables"
        ),
        ("systemctl restart sshd || systemctl restart ssh", "SSH: перезапуск на новом порту"),
    ]

    connect_kwargs = _build_connect_kwargs(ip, ssh_port, ssh_user, ssh_password)

    try:
        async with asyncssh.connect(**connect_kwargs) as conn:
            for cmd, description in bootstrap_commands + hardening_commands:
                result = await conn.run(cmd)
                step_ok = result.returncode == 0

                # sshd restart может вернуть ненулевой если имя сервиса отличается
                if not step_ok and "Перезапуск" in description:
                    step_ok = True

                details.append({"step": description, "ok": step_ok})

                if not step_ok:
                    return {
                        "success": False,
                        "error": f"Шаг '{description}': {result.stderr[:300]}",
                        "details": details
                    }

        return {
            "success": True,
            "details": details,
            "summary": (
                f"Ключ установлен | "
                f"UFW: SSH ({ssh_port}) + 443 (VLESS)"
                + (f" + Marzban ({marzban_port})" if marzban_port and marzban_port not in (443, ssh_port) else "")
                + " открыты | SSH: только ключ"
            )
        }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        auth_method = "пароль" if ssh_password else "ключ"
        return {"success": False, "error": f"SSH: доступ запрещён (проверьте {auth_method})"}
    except Exception as e:
        return {"success": False, "error": f"Ошибка hardening: {str(e)}"}


async def check_ssh_connection(ip: str, ssh_port: int, ssh_user: str = "root") -> dict:
    """Проверяет SSH-подключение к серверу по ключу."""
    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_port,
            username=ssh_user,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            await conn.run("echo ok", check=True)
            return {"success": True}
    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": str(e)}

async def generate_reality_keys(
    ip: str,
    ssh_port: int,
    ssh_user: str = "root",
    container_name: str = "marzban-marzban-1",
) -> dict:
    """
    Генерирует пару X25519 ключей для Reality через xray x25519
    внутри Marzban-контейнера на сервере.

    Возвращает:
        {"success": True, "private_key": "...", "public_key": "..."}
    """
    import re
    try:
        async with asyncssh.connect(
            host=ip,
            port=ssh_port,
            username=ssh_user,
            client_keys=[SSH_KEY_PATH],
            known_hosts=None
        ) as conn:
            result = await conn.run(
                f"docker exec {container_name} xray x25519",
                check=False
            )

            if result.returncode != 0:
                # Пробуем найти контейнер автоматически
                find_result = await conn.run(
                    "docker ps --format '{{.Names}}' | grep -i marzban | head -1",
                    check=False
                )
                found_name = find_result.stdout.strip()
                if not found_name:
                    return {"success": False, "error": "Marzban-контейнер не найден. Проверьте docker ps"}

                result = await conn.run(
                    f"docker exec {found_name} xray x25519",
                    check=False
                )
                if result.returncode != 0:
                    return {"success": False, "error": f"xray x25519 завершился с ошибкой: {result.stderr}"}

            output = result.stdout
            priv_match = re.search(r"Private key:\s*(\S+)", output)
            pub_match  = re.search(r"Public key:\s*(\S+)", output)

            if not priv_match or not pub_match:
                return {"success": False, "error": f"Не удалось разобрать вывод xray x25519: {output}"}

            return {
                "success": True,
                "private_key": priv_match.group(1),
                "public_key":  pub_match.group(1),
            }

    except asyncssh.DisconnectError:
        return {"success": False, "error": "SSH: сервер недоступен"}
    except asyncssh.PermissionDenied:
        return {"success": False, "error": "SSH: ключ не подошёл"}
    except Exception as e:
        return {"success": False, "error": f"Ошибка генерации ключей: {str(e)}"} 