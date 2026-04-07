from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select, delete, and_
from datetime import datetime, timedelta
import asyncio
import httpx
import logging

from database.database import async_session
from database.models import (
    Config, VPNServer, Client, ServicePlan, Invoice, InvoiceStatus,
    LoginAttempt, Notification, NotificationType
)
from app.services.marzban_service import delete_marzban_user, toggle_marzban_user, extend_marzban_user
from app.services.crypto_service import decrypt

logger = logging.getLogger("tugoka.scheduler")

scheduler = AsyncIOScheduler()


async def check_servers_online():
    """
    Запускается каждые 5 минут.
    Проверяет доступность Marzban API на каждом активном сервере.
    Обновляет is_online и last_checked_at в БД.
    """
    logger.info(f"Проверка доступности серверов — {datetime.utcnow()}")

    async with async_session() as db:
        result = await db.execute(select(VPNServer).where(VPNServer.is_active == True))
        servers = result.scalars().all()

        if not servers:
            return

        async def ping_marzban(server: VPNServer):
            try:
                async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
                    resp = await client.get(f"{server.marzban_url}/api/core/stats")
                    return server.id, resp.status_code < 500
            except BaseException:
                return server.id, False

        results = await asyncio.gather(*[ping_marzban(s) for s in servers])

        server_map = {s.id: s for s in servers}
        prev_states = {s.id: s.is_online for s in servers}

        for server_id, online in results:
            server_map[server_id].is_online = online
            server_map[server_id].last_checked_at = datetime.utcnow()

            was_online = prev_states.get(server_id, True)

            # Сервер упал — уведомляем админов
            if was_online and not online:
                s = server_map[server_id]
                logger.warning(f"Сервер {s.name} ({s.ip_address}) — НЕДОСТУПЕН")
                from app.services.event_service import notify_admins
                from database.models import AdminNotifType
                await notify_admins(db,
                    notif_type=AdminNotifType.SERVER_DOWN,
                    title=f"Сервер {s.name} недоступен",
                    message=f"Сервер {s.name} ({s.ip_address}) не отвечает на запросы Marzban API.",
                    target_type="server", target_id=server_id,
                )

            # Сервер поднялся — уведомляем
            if not was_online and online:
                s = server_map[server_id]
                logger.info(f"Сервер {s.name} ({s.ip_address}) — снова ОНЛАЙН")
                from app.services.event_service import notify_admins
                from database.models import AdminNotifType
                await notify_admins(db,
                    notif_type=AdminNotifType.SERVER_UP,
                    title=f"Сервер {s.name} снова онлайн",
                    message=f"Сервер {s.name} ({s.ip_address}) восстановил работу.",
                    target_type="server", target_id=server_id,
                )

        await db.commit()
        logger.info(f"Проверено серверов: {len(servers)}, недоступных: {sum(1 for _, ok in results if not ok)}")


async def send_expiry_notifications():
    """
    Запускается каждый час.

    Отправляет уведомления пользователям перед истечением подписки:
      — за 24 часа (EXPIRY_24H)
      — за 3 часа  (EXPIRY_3H)

    Дедупликация: проверяем, не было ли уже такого уведомления
    для этой конфигурации за последние 25 часов, чтобы повторный
    запуск не создавал дубли.
    """
    now = datetime.utcnow()
    logger.info(f"Проверка уведомлений об истечении — {now}")

    # (тип, нижняя граница окна, верхняя граница окна)
    windows = [
        (NotificationType.EXPIRY_24H, timedelta(hours=23), timedelta(hours=25)),
        (NotificationType.EXPIRY_3H,  timedelta(hours=2),  timedelta(hours=4)),
    ]

    texts = {
        NotificationType.EXPIRY_24H: (
            "Подписка истекает через 24 часа",
            "Ваша подписка заканчивается через ~24 часа. "
            "Пополните баланс или включите авто-продление, чтобы не потерять доступ."
        ),
        NotificationType.EXPIRY_3H: (
            "Подписка истекает через 3 часа",
            "До конца подписки осталось ~3 часа. "
            "Пополните баланс или продлите подписку прямо сейчас."
        ),
    }

    async with async_session() as db:
        total_created = 0

        for notif_type, delta_min, delta_max in windows:
            window_start = now + delta_min
            window_end   = now + delta_max

            # Активные подписки, истекающие в нужном окне
            result = await db.execute(
                select(Config).where(
                    Config.is_active == True,
                    Config.expire_at >= window_start,
                    Config.expire_at <  window_end,
                )
            )
            configs = result.scalars().all()

            for config in configs:
                # Дедупликация: уведомление этого типа для этого конфига за ~сутки
                dedup_since = now - timedelta(hours=25)
                already = await db.execute(
                    select(Notification).where(
                        Notification.config_id         == config.id,
                        Notification.notification_type == notif_type,
                        Notification.created_at        >= dedup_since,
                    )
                )
                if already.scalars().first():
                    continue

                title, message = texts[notif_type]
                db.add(Notification(
                    client_id=config.client_id,
                    config_id=config.id,
                    notification_type=notif_type,
                    title=title,
                    message=message,
                ))
                total_created += 1

        await db.commit()

    if total_created:
        logger.info(f"Создано уведомлений об истечении: {total_created}")
    else:
        logger.info("Новых уведомлений об истечении нет")


async def process_expired_subscriptions():
    """
    Запускается каждый час.

    Для каждой истёкшей активной подписки:

    Если auto_renew=True и баланс хватает:
        - Списываем деньги
        - Продлеваем пользователя в Marzban
        - Обновляем expire_at в БД
        - Создаём уведомление AUTO_RENEW

    Если auto_renew=False или баланса нет:
        - Создаём уведомление LOW_BALANCE (если денег не хватило)
        - Удаляем пользователя из Marzban
        - Помечаем конфиг is_active=False
        - Создаём уведомление EXPIRED
    """
    logger.info(f"Проверка истёкших подписок — {datetime.utcnow()}")

    async with async_session() as db:
        result = await db.execute(
            select(Config).where(
                Config.is_active == True,
                Config.expire_at < datetime.utcnow()
            )
        )
        expired = result.scalars().all()

        if not expired:
            logger.info("Истёкших подписок нет")
            return

        logger.info(f"Найдено истёкших: {len(expired)}")

        for config in expired:
            server = await db.get(VPNServer, config.server_id)
            plan   = await db.get(ServicePlan, config.plan_id)
            client = await db.get(Client, config.client_id)

            if not server or not plan or not client:
                config.is_active = False
                continue

            admin_user = decrypt(server.mar_admin_user)
            admin_pass = decrypt(server.mar_admin_pass)

            # --- Авто-продление ---
            if config.auto_renew:
                final_price = round(plan.price * (1 - plan.discount_percent / 100), 2)

                if client.balance >= final_price:
                    extend_result = await extend_marzban_user(
                        marzban_url=server.marzban_url,
                        admin_username=admin_user,
                        admin_password=admin_pass,
                        username=config.mar_username,
                        extra_days=plan.duration_days,
                    )

                    if extend_result["success"]:
                        client.balance = round(client.balance - final_price, 2)
                        config.expire_at = config.expire_at + timedelta(days=plan.duration_days)
                        config.is_active = True

                        db.add(Invoice(
                            client_id=client.id,
                            plan_id=plan.id,
                            amount=final_price,
                            status=InvoiceStatus.PAID,
                            external_id="auto_renew"
                        ))
                        db.add(Notification(
                            client_id=client.id,
                            config_id=config.id,
                            notification_type=NotificationType.AUTO_RENEW,
                            title="Подписка продлена автоматически",
                            message=(
                                f"Подписка продлена на {plan.duration_days} дней. "
                                f"Списано {final_price}₽. Новый баланс: {client.balance}₽."
                            ),
                        ))
                        logger.info(f"Авто-продлено: {config.mar_username}, списано {final_price}₽")

                        # Событие подписки
                        from app.services.event_service import log_sub_event
                        from database.models import SubscriptionEventType
                        await log_sub_event(db, config_id=config.id, client_id=client.id,
                            event_type=SubscriptionEventType.AUTO_RENEWED,
                            description=f"Авто-продление на {plan.duration_days} дн., списано {final_price}₽",
                        )

                        continue
                    else:
                        logger.error(f"Ошибка авто-продления {config.mar_username}: {extend_result['error']}")

                        # Уведомление админам
                        from app.services.event_service import notify_admins
                        from database.models import AdminNotifType
                        await notify_admins(db,
                            notif_type=AdminNotifType.RENEW_FAILED,
                            title=f"Авто-продление не прошло: {client.email}",
                            message=f"Ошибка Marzban при продлении {config.mar_username}: {extend_result['error']}",
                            target_type="config", target_id=config.id,
                        )
                else:
                    logger.warning(f"Авто-продление {config.mar_username}: недостаточно баланса ({client.balance}₽ < {final_price}₽)")
                    db.add(Notification(
                        client_id=client.id,
                        config_id=config.id,
                        notification_type=NotificationType.LOW_BALANCE,
                        title="Не хватает средств для продления",
                        message=(
                            f"Не удалось продлить подписку автоматически: "
                            f"баланс {client.balance}₽, нужно {final_price}₽. "
                            f"Пополните баланс, чтобы восстановить доступ."
                        ),
                    ))

            # --- Удаление ---
            delete_result = await delete_marzban_user(
                marzban_url=server.marzban_url,
                admin_username=admin_user,
                admin_password=admin_pass,
                username=config.mar_username,
            )

            if delete_result["success"]:
                if server.current_users_count > 0:
                    server.current_users_count -= 1
                logger.info(f"Удалён из Marzban: {config.mar_username}")
            else:
                logger.error(f"Ошибка удаления {config.mar_username}: {delete_result['error']}")

            config.is_active = False
            db.add(Notification(
                client_id=config.client_id,
                config_id=config.id,
                notification_type=NotificationType.EXPIRED,
                title="Подписка истекла",
                message="Ваша подписка завершена. Оформите новую или пополните баланс для продления.",
            ))

        await db.commit()
        logger.info(f"Готово. Обработано: {len(expired)}")


async def cleanup_login_attempts():
    """Раз в сутки удаляет устаревшие записи попыток входа."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    async with async_session() as db:
        await db.execute(
            delete(LoginAttempt).where(LoginAttempt.attempted_at < cutoff)
        )
        await db.commit()
    logger.info(f"Очистка login_attempts завершена")


def start_scheduler():
    scheduler.add_job(
        send_expiry_notifications,
        trigger=IntervalTrigger(hours=1),
        id="send_expiry_notifications",
        replace_existing=True,
        next_run_time=datetime.utcnow(),   # запустить сразу при старте
    )
    scheduler.add_job(
        process_expired_subscriptions,
        trigger=IntervalTrigger(hours=1),
        id="process_expired",
        replace_existing=True,
        next_run_time=datetime.utcnow(),   # запустить сразу при старте
    )
    scheduler.add_job(
        cleanup_login_attempts,
        trigger=IntervalTrigger(hours=24),
        id="cleanup_attempts",
        replace_existing=True
    )
    scheduler.add_job(
        check_servers_online,
        trigger=IntervalTrigger(minutes=5),
        id="check_servers_online",
        replace_existing=True,
        next_run_time=datetime.utcnow(),
    )
    scheduler.start()
    logger.info("Запущен.")