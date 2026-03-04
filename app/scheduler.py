from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, delete
from datetime import datetime, timedelta
from database.database import async_session
from database.models import Config, VPNServer, Client, ServicePlan, Invoice, InvoiceStatus, LoginAttempt
from app.services.marzban_service import delete_marzban_user, toggle_marzban_user
from app.services.crypto_service import decrypt

scheduler = AsyncIOScheduler()


async def process_expired_subscriptions():
    """
    Запускается каждый час.

    Для каждой истёкшей активной подписки:

    Если auto_renew=True и баланс хватает:
        - Списываем деньги
        - Продлеваем в Marzban
        - Обновляем expire_at в БД

    Если auto_renew=False или баланса нет:
        - Удаляем юзера из Marzban
        - Помечаем конфиг is_active=False
        - Уменьшаем счётчик сервера
    """
    print(f"[Scheduler] Проверка истёкших подписок — {datetime.utcnow()}")

    async with async_session() as db:
        result = await db.execute(
            select(Config).where(
                Config.is_active == True,
                Config.expire_at < datetime.utcnow()
            )
        )
        expired = result.scalars().all()

        if not expired:
            print("[Scheduler] Истёкших подписок нет")
            return

        print(f"[Scheduler] Найдено истёкших: {len(expired)}")

        for config in expired:
            server = await db.get(VPNServer, config.server_id)
            plan = await db.get(ServicePlan, config.plan_id)
            client = await db.get(Client, config.client_id)

            if not server or not plan or not client:
                config.is_active = False
                continue

            admin_user = decrypt(server.mar_admin_user)
            admin_pass = decrypt(server.mar_admin_pass)

            # --- Авто-продление ---
            if config.auto_renew:
                final_price = round(plan.price * plan.months * (1 - plan.discount_percent / 100), 2)

                if client.balance >= final_price:
                    # Продлеваем в Marzban — включаем если был выключен
                    toggle_result = await toggle_marzban_user(
                        ip=server.ip_address,
                        ssh_port=server.ssh_port,
                        marzban_port=server.marzban_port,
                        mar_admin_user=admin_user,
                        mar_admin_pass=admin_pass,
                        marzban_username=config.marzban_username,
                        active=True
                    )

                    if toggle_result["success"]:
                        client.balance = round(client.balance - final_price, 2)
                        config.expire_at = config.expire_at + timedelta(days=plan.months * 30)
                        config.is_active = True

                        db.add(Invoice(
                            client_id=client.id,
                            plan_id=plan.id,
                            amount=final_price,
                            status=InvoiceStatus.PAID,
                            external_id="auto_renew"
                        ))
                        print(f"[Scheduler] Авто-продлено: {config.marzban_username}, списано {final_price}₽")
                        continue
                    else:
                        print(f"[Scheduler] Ошибка авто-продления {config.marzban_username}: {toggle_result['error']}")

                else:
                    print(f"[Scheduler] Авто-продление {config.marzban_username}: недостаточно баланса ({client.balance}₽ < {final_price}₽)")

            # --- Удаление (нет авто-продления или не хватило денег) ---
            delete_result = await delete_marzban_user(
                ip=server.ip_address,
                ssh_port=server.ssh_port,
                marzban_port=server.marzban_port,
                mar_admin_user=admin_user,
                mar_admin_pass=admin_pass,
                marzban_username=config.marzban_username
            )

            if delete_result["success"]:
                if server.current_users_count > 0:
                    server.current_users_count -= 1
                print(f"[Scheduler] Удалён из Marzban: {config.marzban_username}")
            else:
                print(f"[Scheduler] Ошибка удаления {config.marzban_username}: {delete_result['error']}")

            config.is_active = False

        await db.commit()
        print(f"[Scheduler] Готово. Обработано: {len(expired)}")


async def cleanup_login_attempts():
    """
    Запускается раз в сутки.
    Удаляет старые записи попыток входа — старше 24 часов.
    Без этого таблица будет расти бесконечно.
    """
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with async_session() as db:
        await db.execute(
            delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff)
        )
        await db.commit()
    print(f"[Scheduler] Очистка login_attempts завершена")


def start_scheduler():
    scheduler.add_job(
        process_expired_subscriptions,
        trigger=IntervalTrigger(hours=1),
        id="process_expired",
        replace_existing=True
    )
    scheduler.add_job(
        cleanup_login_attempts,
        trigger=IntervalTrigger(hours=24),
        id="cleanup_attempts",
        replace_existing=True
    )
    scheduler.start()
    print("[Scheduler] Запущен.")