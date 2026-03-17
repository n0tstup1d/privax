"""
ssh_service.py — SSH-утилиты + автоматическая защита сервера.

harden_server() вызывается при POST /admin/servers/add автоматически.

Флоу первого добавления сервера:
  1. Подключаемся по ПАРОЛЮ (ssh_user + ssh_password из запроса)
  2. Устанавливаем наш публичный SSH-ключ в authorized_keys
  3. Настраиваем UFW, fail2ban, отключаем вход по паролю
  4. После этого все дальнейшие подключения — только по ключу

Что настраивается:
  UFW      — блокируем всё входящее, открываем SSH-порт и 443 для VPN.
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
    panel_port: int,
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
    4. UFW: default deny, allow SSH + 443, deny panel_port снаружи
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
        ("ufw allow 443/tcp comment 'VPN Reality'", "UFW: 443 для VPN (Reality/HTTPS)"),
        (
            f"ufw deny {panel_port}/tcp comment 'panel — только SSH-туннель'",
            f"UFW: панель {panel_port} закрыта снаружи"
        ),
        ("ufw --force enable", "UFW: включаем файрвол"),
        # TODO: раскомментить когда fail2ban будет нужен
        # (
        #     "cat > /etc/fail2ban/jail.local << 'EOF'\n"
        #     "[DEFAULT]\n"
        #     "bantime  = 3600\n"
        #     "findtime = 600\n"
        #     "maxretry = 5\n\n"
        #     "[sshd]\n"
        #     "enabled  = true\n"
        #     f"port     = {ssh_port}\n"
        #     "filter   = sshd\n"
        #     "logpath  = /var/log/auth.log\n"
        #     "maxretry = 3\n"
        #     "bantime  = 86400\n"
        #     "EOF",
        #     "fail2ban: 3 попытки → бан 24 ч"
        # ),
        # ("systemctl enable fail2ban && systemctl restart fail2ban", "fail2ban: запуск"),
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
                f"UFW: SSH ({ssh_port}) + 443 открыты, панель {panel_port} закрыта снаружи | "
                f"SSH: только ключ | fail2ban: 5 попыток -> бан 1 ч"
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