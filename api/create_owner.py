"""
create_owner.py — создание первого администратора (owner).

Запуск:
    python create_owner.py admin@example.com my_password

Если пользователь с таким email уже существует — просто добавит AdminProfile.
"""
import sys
import asyncio
from passlib.context import CryptContext

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


async def main():
    if len(sys.argv) < 3:
        print("Использование: python create_owner.py <email> <password>")
        print("Пример:        python create_owner.py admin@privax.com MyStr0ngPass!")
        sys.exit(1)

    email = sys.argv[1]
    password = sys.argv[2]

    # Импорты после парсинга аргументов чтобы ошибки БД не вылезали при --help
    from database.database import engine, async_session
    from database.models import Base, Client, AdminProfile, AdminRole

    # Создаём таблицы если не существуют
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with async_session() as db:
        from sqlalchemy import select

        # Проверяем существует ли пользователь
        result = await db.execute(select(Client).where(Client.email == email))
        client = result.scalar_one_or_none()

        if client:
            print(f"Пользователь {email} уже существует (ID: {client.id})")
        else:
            client = Client(
                email=email,
                hashed_password=pwd_context.hash(password),
                is_verified=True,
                balance=0.0,
            )
            db.add(client)
            await db.flush()
            print(f"Создан пользователь {email} (ID: {client.id})")

        # Проверяем есть ли AdminProfile
        result = await db.execute(
            select(AdminProfile).where(AdminProfile.client_id == client.id)
        )
        profile = result.scalar_one_or_none()

        if profile:
            print(f"AdminProfile уже существует: роль {profile.role.value}")
            if profile.role != AdminRole.OWNER:
                profile.role = AdminRole.OWNER
                print(f"Роль обновлена до owner")
        else:
            profile = AdminProfile(
                client_id=client.id,
                role=AdminRole.OWNER,
                is_active=True,
            )
            db.add(profile)
            print(f"Создан AdminProfile с ролью owner")

        await db.commit()
        print(f"\n✅ Готово! Войдите в админку: {email} / {password}")
        print(f"   Затем настройте 2FA через Telegram в /admin/auth/setup-2fa")


if __name__ == "__main__":
    asyncio.run(main())