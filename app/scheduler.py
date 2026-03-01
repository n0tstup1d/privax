from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, update
from datetime import datetime
from database.database import async_session
from database.models import Config, VPNServer
from app.services.marzban_service import toggle_marzban_user
from app.services.crypto_service import decrypt

# Создаём планировщик — он будет жить всё время пока работает сервер
scheduler = AsyncIOScheduler()


async def deactivate_expired_subscriptions():
    """
    Задача запускается каждый час.
    
    Что делает:
    1. Находит все конфиги у которых expire_at < сейчас, но is_active = True
       (то есть истекли, но мы ещё не успели их выключить)
    2. Для каждого такого конфига:
       - Выключает юзера в Marzban (status = disabled)
       - Ставит is_active = False в нашей БД
       - Уменьшает счётчик юзеров на сервере
    
    Почему выключаем в Marzban а не удаляем?
    Потому что клиент может продлить подписку — тогда просто включим обратно.
    Удалять = терять конфигурацию юзера в Marzban, это плохо.
    """
    print(f"[Scheduler] Запуск проверки истёкших подписок — {datetime.utcnow()}")

    async with async_session() as db:
        # Находим все истёкшие но ещё "активные" конфиги
        result = await db.execute(
            select(Config)
            .where(
                Config.is_active == True,
                Config.expire_at < datetime.utcnow()
            )
        )
        expired_configs = result.scalars().all()

        if not expired_configs:
            print("[Scheduler] Истёкших подписок нет")
            return

        print(f"[Scheduler] Найдено истёкших подписок: {len(expired_configs)}")

        for config in expired_configs:
            # Получаем сервер чтобы достать credentials и уменьшить счётчик
            server_result = await db.execute(
                select(VPNServer).where(VPNServer.id == config.server_id)
            )
            server = server_result.scalar_one_or_none()

            if server:
                # Выключаем юзера в Marzban
                toggle_result = await toggle_marzban_user(
                    ip=server.ip_address,
                    ssh_port=server.ssh_port,
                    marzban_port=server.marzban_port,
                    mar_admin_user=decrypt(server.mar_admin_user),
                    mar_admin_pass=decrypt(server.mar_admin_pass),
                    marzban_username=config.marzban_username,
                    active=False
                )

                if toggle_result["success"]:
                    # Уменьшаем счётчик юзеров на сервере
                    if server.current_users_count > 0:
                        server.current_users_count -= 1
                    print(f"[Scheduler] Отключён: {config.marzban_username}")
                else:
                    # Marzban не ответил — всё равно помечаем в БД
                    # При следующем запуске задача снова попробует выключить в Marzban
                    print(f"[Scheduler] Ошибка Marzban для {config.marzban_username}: {toggle_result['error']}")

            # В любом случае помечаем конфиг как неактивный в нашей БД
            config.is_active = False

        await db.commit()
        print(f"[Scheduler] Готово. Обработано: {len(expired_configs)}")


def start_scheduler():
    """
    Запускает планировщик.
    Вызывается в main.py при старте приложения.
    """
    scheduler.add_job(
        deactivate_expired_subscriptions,
        trigger=IntervalTrigger(hours=1),   # каждый час
        id="deactivate_expired",
        replace_existing=True               # если задача уже есть — заменяем
    )
    scheduler.start()
    print("[Scheduler] Запущен. Проверка истёкших подписок каждый час.")