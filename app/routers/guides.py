"""
guides.py — API управления гайдами и приложениями.

Публичные:
    GET /guides/platforms          — все платформы с приложениями и шагами (для фронта)

Админские:
    GET    /admin/guides/platforms          — все платформы (для админки)
    POST   /admin/guides/platforms          — создать платформу
    PATCH  /admin/guides/platforms/{id}     — обновить платформу
    DELETE /admin/guides/platforms/{id}     — удалить платформу

    POST   /admin/guides/apps              — добавить приложение к платформе
    PATCH  /admin/guides/apps/{id}         — обновить приложение
    DELETE /admin/guides/apps/{id}         — удалить приложение

    POST   /admin/guides/steps             — добавить шаг к приложению
    PATCH  /admin/guides/steps/{id}        — обновить шаг
    DELETE /admin/guides/steps/{id}        — удалить шаг

    POST   /admin/guides/seed              — заполнить начальными данными (если таблица пуста)
"""
from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from auth.deps import get_current_admin
from database.database import get_db
from database.models import GuidePlatform, GuideApp, GuideStep

router = APIRouter()


# ═══════════════════════════════════════════════
#  СХЕМЫ
# ═══════════════════════════════════════════════

class StepOut(BaseModel):
    id: int
    text: str
    hint: Optional[str] = None
    sort_order: int

class AppOut(BaseModel):
    id: int
    name: str
    store_name: str
    download_url: str
    is_primary: bool
    sort_order: int
    steps: list[StepOut]

class PlatformOut(BaseModel):
    id: int
    key: str
    label: str
    subtitle: str
    emoji: str
    icon_url: Optional[str] = None
    color: str
    category: str
    sort_order: int
    is_visible: bool
    apps: list[AppOut]

class PlatformCreate(BaseModel):
    key: str
    label: str
    subtitle: str = ""
    emoji: str = "📱"
    icon_url: Optional[str] = None
    color: str = "#ffffff"
    category: str = "smartphones"
    sort_order: int = 0
    is_visible: bool = True

class PlatformUpdate(BaseModel):
    key: Optional[str] = None
    label: Optional[str] = None
    subtitle: Optional[str] = None
    emoji: Optional[str] = None
    icon_url: Optional[str] = None
    color: Optional[str] = None
    category: Optional[str] = None
    sort_order: Optional[int] = None
    is_visible: Optional[bool] = None

class AppCreate(BaseModel):
    platform_id: int
    name: str
    store_name: str = ""
    download_url: str
    is_primary: bool = False
    sort_order: int = 0

class AppUpdate(BaseModel):
    name: Optional[str] = None
    store_name: Optional[str] = None
    download_url: Optional[str] = None
    is_primary: Optional[bool] = None
    sort_order: Optional[int] = None

class StepCreate(BaseModel):
    app_id: int
    text: str
    hint: Optional[str] = None
    sort_order: int = 0

class StepUpdate(BaseModel):
    text: Optional[str] = None
    hint: Optional[str] = None
    sort_order: Optional[int] = None


# ═══════════════════════════════════════════════
#  HELPER
# ═══════════════════════════════════════════════

def _serialize_platform(p: GuidePlatform) -> dict:
    return {
        "id": p.id,
        "key": p.key,
        "label": p.label,
        "subtitle": p.subtitle,
        "emoji": p.emoji,
        "icon_url": p.icon_url,
        "color": p.color,
        "category": p.category,
        "sort_order": p.sort_order,
        "is_visible": p.is_visible,
        "apps": [
            {
                "id": a.id,
                "name": a.name,
                "store_name": a.store_name,
                "download_url": a.download_url,
                "is_primary": a.is_primary,
                "sort_order": a.sort_order,
                "steps": [
                    {"id": s.id, "text": s.text, "hint": s.hint, "sort_order": s.sort_order}
                    for s in (a.steps or [])
                ],
            }
            for a in (p.apps or [])
        ],
    }


async def _load_all(db: AsyncSession, visible_only: bool = False):
    q = (
        select(GuidePlatform)
        .options(
            selectinload(GuidePlatform.apps).selectinload(GuideApp.steps)
        )
        .order_by(GuidePlatform.sort_order)
    )
    if visible_only:
        q = q.where(GuidePlatform.is_visible == True)
    result = await db.execute(q)
    return result.scalars().unique().all()


# ═══════════════════════════════════════════════
#  ПУБЛИЧНЫЙ — для фронта
# ═══════════════════════════════════════════════

@router.get("/guides/platforms")
async def get_public_platforms(db: AsyncSession = Depends(get_db)):
    """Все видимые платформы с приложениями и шагами."""
    platforms = await _load_all(db, visible_only=True)
    return [_serialize_platform(p) for p in platforms]


# ═══════════════════════════════════════════════
#  АДМИН — ПЛАТФОРМЫ
# ═══════════════════════════════════════════════

@router.get("/admin/guides/platforms", dependencies=[Depends(get_current_admin)])
async def admin_get_platforms(db: AsyncSession = Depends(get_db)):
    platforms = await _load_all(db, visible_only=False)
    return [_serialize_platform(p) for p in platforms]


@router.post("/admin/guides/platforms", dependencies=[Depends(get_current_admin)])
async def admin_create_platform(body: PlatformCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(GuidePlatform).where(GuidePlatform.key == body.key))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail=f"Платформа с ключом '{body.key}' уже существует")

    p = GuidePlatform(**body.model_dump())
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return {"id": p.id, "status": "created"}


@router.patch("/admin/guides/platforms/{platform_id}", dependencies=[Depends(get_current_admin)])
async def admin_update_platform(platform_id: int, body: PlatformUpdate, db: AsyncSession = Depends(get_db)):
    p = await db.get(GuidePlatform, platform_id)
    if not p:
        raise HTTPException(status_code=404, detail="Платформа не найдена")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(p, field, val)
    await db.commit()
    return {"status": "updated"}


@router.delete("/admin/guides/platforms/{platform_id}", dependencies=[Depends(get_current_admin)])
async def admin_delete_platform(platform_id: int, db: AsyncSession = Depends(get_db)):
    p = await db.get(GuidePlatform, platform_id)
    if not p:
        raise HTTPException(status_code=404, detail="Платформа не найдена")
    await db.delete(p)
    await db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════
#  АДМИН — ПРИЛОЖЕНИЯ
# ═══════════════════════════════════════════════

@router.post("/admin/guides/apps", dependencies=[Depends(get_current_admin)])
async def admin_create_app(body: AppCreate, db: AsyncSession = Depends(get_db)):
    platform = await db.get(GuidePlatform, body.platform_id)
    if not platform:
        raise HTTPException(status_code=404, detail="Платформа не найдена")
    a = GuideApp(**body.model_dump())
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return {"id": a.id, "status": "created"}


@router.patch("/admin/guides/apps/{app_id}", dependencies=[Depends(get_current_admin)])
async def admin_update_app(app_id: int, body: AppUpdate, db: AsyncSession = Depends(get_db)):
    a = await db.get(GuideApp, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="Приложение не найдено")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(a, field, val)
    await db.commit()
    return {"status": "updated"}


@router.delete("/admin/guides/apps/{app_id}", dependencies=[Depends(get_current_admin)])
async def admin_delete_app(app_id: int, db: AsyncSession = Depends(get_db)):
    a = await db.get(GuideApp, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="Приложение не найдено")
    await db.delete(a)
    await db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════
#  АДМИН — ШАГИ
# ═══════════════════════════════════════════════

@router.post("/admin/guides/steps", dependencies=[Depends(get_current_admin)])
async def admin_create_step(body: StepCreate, db: AsyncSession = Depends(get_db)):
    app = await db.get(GuideApp, body.app_id)
    if not app:
        raise HTTPException(status_code=404, detail="Приложение не найдено")
    s = GuideStep(**body.model_dump())
    db.add(s)
    await db.commit()
    await db.refresh(s)
    return {"id": s.id, "status": "created"}


@router.patch("/admin/guides/steps/{step_id}", dependencies=[Depends(get_current_admin)])
async def admin_update_step(step_id: int, body: StepUpdate, db: AsyncSession = Depends(get_db)):
    s = await db.get(GuideStep, step_id)
    if not s:
        raise HTTPException(status_code=404, detail="Шаг не найден")
    for field, val in body.model_dump(exclude_unset=True).items():
        setattr(s, field, val)
    await db.commit()
    return {"status": "updated"}


@router.delete("/admin/guides/steps/{step_id}", dependencies=[Depends(get_current_admin)])
async def admin_delete_step(step_id: int, db: AsyncSession = Depends(get_db)):
    s = await db.get(GuideStep, step_id)
    if not s:
        raise HTTPException(status_code=404, detail="Шаг не найден")
    await db.delete(s)
    await db.commit()
    return {"status": "deleted"}


# ═══════════════════════════════════════════════
#  SEED — начальные данные
# ═══════════════════════════════════════════════

@router.post("/admin/guides/seed", dependencies=[Depends(get_current_admin)])
async def admin_seed_guides(db: AsyncSession = Depends(get_db)):
    """Заполняет начальными данными если таблица пуста."""
    count = await db.scalar(select(func.count(GuidePlatform.id)))
    if count and count > 0:
        raise HTTPException(status_code=400, detail=f"Данные уже есть ({count} платформ). Удалите вручную для пересоздания.")

    SEED = [
        {
            "key": "ios", "label": "iPhone & iPad", "subtitle": "iOS 14+", "emoji": "🍎",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/apple.svg",
            "color": "#c8cdd0", "category": "smartphones", "sort_order": 0,
            "apps": [
                {
                    "name": "V2RayTun", "store_name": "App Store",
                    "download_url": "https://apps.apple.com/app/v2raytun/id6476628951",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Скачайте V2RayTun из App Store", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте V2RayTun — нажмите «+» в правом верхнем углу", "sort_order": 2},
                        {"text": "Выберите «Вставить из буфера обмена»", "sort_order": 3},
                        {"text": "Нажмите «Сохранить» — сервер появится в списке", "sort_order": 4},
                        {"text": "Нажмите на сервер, затем кнопку подключения", "sort_order": 5},
                    ],
                },
            ],
        },
        {
            "key": "android", "label": "Android", "subtitle": "Android 8.0+", "emoji": "🤖",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/android.svg",
            "color": "#3DDC84", "category": "smartphones", "sort_order": 1,
            "apps": [
                {
                    "name": "V2RayTun", "store_name": "Google Play",
                    "download_url": "https://play.google.com/store/apps/details?id=com.v2raytun.android",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Скачайте V2RayTun из Google Play", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте V2RayTun — нажмите «+» в правом верхнем углу", "sort_order": 2},
                        {"text": "Выберите «Вставить из буфера обмена»", "sort_order": 3},
                        {"text": "Нажмите «Сохранить» — сервер появится в списке", "sort_order": 4},
                        {"text": "Нажмите на сервер, затем кнопку подключения", "sort_order": 5},
                    ],
                },
                {
                    "name": "Happ", "store_name": "Google Play",
                    "download_url": "https://play.google.com/store/apps/details?id=com.happproxy",
                    "is_primary": False, "sort_order": 1,
                    "steps": [
                        {"text": "Скачайте Happ из Google Play", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте Happ", "sort_order": 2},
                        {"text": "Нажмите кнопку «Из буфера» внизу главного экрана", "sort_order": 3},
                        {"text": "Конфигурация добавится автоматически", "sort_order": 4},
                        {"text": "Нажмите кнопку подключения", "sort_order": 5},
                    ],
                },
            ],
        },
        {
            "key": "windows", "label": "Windows", "subtitle": "Windows 10 / 11", "emoji": "🪟",
            "icon_url": None, "color": "#0078d4", "category": "desktop", "sort_order": 2,
            "apps": [
                {
                    "name": "Happ", "store_name": "GitHub",
                    "download_url": "https://github.com/Happ-proxy/happ-desktop/releases/latest/download/setup-Happ.x64.exe",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Скачайте Happ и установите", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "В Happ нажмите кнопку «Из буфера» внизу главного экрана", "sort_order": 2},
                        {"text": "Конфигурация добавится автоматически", "sort_order": 3},
                        {"text": "Нажмите кнопку подключения", "sort_order": 4},
                    ],
                },
                {
                    "name": "V2RayN", "store_name": "GitHub",
                    "download_url": "https://github.com/2dust/v2rayN/releases/latest",
                    "is_primary": False, "sort_order": 1,
                    "steps": [
                        {"text": "Скачайте V2RayN с GitHub и распакуйте архив", "sort_order": 0},
                        {"text": "Запустите v2rayN.exe", "sort_order": 1},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 2},
                        {"text": "В V2RayN нажмите «Серверы» → «Импорт из буфера обмена»", "sort_order": 3},
                        {"text": "Нажмите F2 или кнопку подключения в трее", "sort_order": 4},
                    ],
                },
            ],
        },
        {
            "key": "macos", "label": "macOS", "subtitle": "macOS 12+", "emoji": "💻",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/apple.svg",
            "color": "#a0a0a0", "category": "desktop", "sort_order": 3,
            "apps": [
                {
                    "name": "V2RayTun", "store_name": "App Store",
                    "download_url": "https://apps.apple.com/us/app/v2raytun/id6476628951?platform=mac",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Скачайте V2RayTun из Mac App Store", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте V2RayTun — нажмите «+» в правом верхнем углу", "sort_order": 2},
                        {"text": "Выберите «Вставить из буфера обмена»", "sort_order": 3},
                        {"text": "Нажмите «Сохранить» — сервер появится в списке", "sort_order": 4},
                        {"text": "Нажмите на сервер, затем кнопку подключения", "sort_order": 5},
                    ],
                },
                {
                    "name": "Happ", "store_name": "App Store",
                    "download_url": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?platform=mac",
                    "is_primary": False, "sort_order": 1,
                    "steps": [
                        {"text": "Скачайте Happ из Mac App Store", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте Happ", "sort_order": 2},
                        {"text": "Нажмите кнопку «Из буфера» внизу главного экрана", "sort_order": 3},
                        {"text": "Конфигурация добавится автоматически", "sort_order": 4},
                        {"text": "Нажмите кнопку подключения", "sort_order": 5},
                    ],
                },
            ],
        },
        {
            "key": "linux", "label": "Linux", "subtitle": "Ubuntu / Debian / Arch", "emoji": "🐧",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/linux.svg",
            "color": "#ffb700", "category": "desktop", "sort_order": 4,
            "apps": [
                {
                    "name": "V2RayN x64", "store_name": "Direct",
                    "download_url": "https://v2rayn.2dust.link/v2rayN-linux-64.deb",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Скачайте .deb пакет под вашу архитектуру", "sort_order": 0},
                        {"text": "Установите: sudo dpkg -i v2rayN-linux-64.deb", "sort_order": 1},
                        {"text": "Запустите V2RayN", "sort_order": 2},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 3},
                        {"text": "В V2RayN нажмите «Серверы» → «Импорт из буфера обмена»", "sort_order": 4},
                        {"text": "Нажмите кнопку подключения", "sort_order": 5},
                    ],
                },
                {
                    "name": "V2RayN arm64", "store_name": "Direct",
                    "download_url": "https://v2rayn.2dust.link/v2rayN-linux-arm64.deb",
                    "is_primary": False, "sort_order": 1,
                    "steps": [
                        {"text": "Скачайте .deb пакет для arm64", "sort_order": 0},
                        {"text": "Установите: sudo dpkg -i v2rayN-linux-arm64.deb", "sort_order": 1},
                        {"text": "Запустите V2RayN", "sort_order": 2},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 3},
                        {"text": "В V2RayN нажмите «Серверы» → «Импорт из буфера обмена»", "sort_order": 4},
                        {"text": "Нажмите кнопку подключения", "sort_order": 5},
                    ],
                },
            ],
        },
        {
            "key": "androidtv", "label": "Android TV", "subtitle": "Android TV / Fire TV", "emoji": "📺",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/googletv.svg",
            "color": "#3DDC84", "category": "tv", "sort_order": 5,
            "apps": [
                {
                    "name": "Happ", "store_name": "Google Play",
                    "download_url": "https://play.google.com/store/apps/details?id=com.happproxy",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Установите Happ из Google Play на вашем TV", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте Happ на TV", "sort_order": 2},
                        {"text": "Нажмите кнопку «Из буфера» или введите ключ вручную", "sort_order": 3},
                        {"text": "Нажмите кнопку подключения", "sort_order": 4},
                    ],
                },
            ],
        },
        {
            "key": "appletv", "label": "Apple TV", "subtitle": "tvOS 16+", "emoji": "📺",
            "icon_url": "https://cdnjs.cloudflare.com/ajax/libs/simple-icons/15.16.0/appletv.svg",
            "color": "#c8cdd0", "category": "tv", "sort_order": 6,
            "apps": [
                {
                    "name": "Happ", "store_name": "App Store",
                    "download_url": "https://apps.apple.com/us/app/happ-proxy-utility-for-tv/id6748297274",
                    "is_primary": True, "sort_order": 0,
                    "steps": [
                        {"text": "Установите Happ из App Store на Apple TV", "sort_order": 0},
                        {"text": "В личном кабинете нажмите «Скопировать ключ доступа»", "sort_order": 1},
                        {"text": "Откройте Happ на Apple TV", "sort_order": 2},
                        {"text": "Введите ключ вручную или отсканируйте QR с телефона", "sort_order": 3},
                        {"text": "Нажмите кнопку подключения", "sort_order": 4},
                    ],
                },
            ],
        },
    ]

    for pdata in SEED:
        apps_data = pdata.pop("apps", [])
        p = GuidePlatform(**pdata)
        db.add(p)
        await db.flush()

        for adata in apps_data:
            steps_data = adata.pop("steps", [])
            a = GuideApp(platform_id=p.id, **adata)
            db.add(a)
            await db.flush()

            for sdata in steps_data:
                s = GuideStep(app_id=a.id, **sdata)
                db.add(s)

    await db.commit()
    return {"status": "seeded", "platforms": len(SEED)}