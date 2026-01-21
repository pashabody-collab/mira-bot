import os
import json
import sqlite3
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Tuple, Dict, Any, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ----------------------------
# CONFIG
# ----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or "0")
FAL_KEY = os.getenv("FAL_KEY", "").strip()
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "5").strip() or "5")

DATA_DIR = Path(os.getenv("DATA_DIR", "/tmp/mira-bot")).resolve()
DB_PATH = DATA_DIR / "mira.sqlite3"
FACES_DIR = DATA_DIR / "faces"

# Upload flow limits
MAX_FACE_PHOTOS = 3

# ----------------------------
# LOGGING
# ----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("mira-bot")

# ----------------------------
# DB
# ----------------------------

def db_init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    FACES_DIR.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(DB_PATH) as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                trial_left INTEGER NOT NULL,
                subscription_until TEXT,
                face_profile_json TEXT
            )
            """
        )
        con.commit()


def db_get_user(user_id: int) -> Dict[str, Any]:
    with sqlite3.connect(DB_PATH) as con:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()

        if row is None:
            now = datetime.utcnow().isoformat()
            con.execute(
                "INSERT INTO users (user_id, created_at, trial_left, subscription_until, face_profile_json) VALUES (?,?,?,?,?)",
                (user_id, now, FREE_LIMIT, None, None),
            )
            con.commit()
            return {
                "user_id": user_id,
                "created_at": now,
                "trial_left": FREE_LIMIT,
                "subscription_until": None,
                "face_profile_json": None,
            }

        return dict(row)


def db_update_user(user_id: int, **fields) -> None:
    if not fields:
        return
    keys = list(fields.keys())
    values = [fields[k] for k in keys]
    set_clause = ", ".join([f"{k}=?" for k in keys])

    with sqlite3.connect(DB_PATH) as con:
        con.execute(f"UPDATE users SET {set_clause} WHERE user_id=?", (*values, user_id))
        con.commit()


def user_has_active_sub(user: Dict[str, Any]) -> bool:
    until = user.get("subscription_until")
    if not until:
        return False
    try:
        dt = datetime.fromisoformat(until)
        return dt > datetime.utcnow()
    except Exception:
        return False


def get_face_profile(user: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    raw = user.get("face_profile_json")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def set_face_profile(user_id: int, profile: Dict[str, Any]) -> None:
    db_update_user(user_id, face_profile_json=json.dumps(profile, ensure_ascii=False))


def clear_face_profile(user_id: int) -> None:
    db_update_user(user_id, face_profile_json=None)


# ----------------------------
# UX TEXTS
# ----------------------------

START_TEXT = (
    "Привет! Я <b>MIRA</b> — создаю реалистичные фото с твоим лицом ✨\n"
    "Подходит для Instagram, сторис, аватаров и контента.\n\n"
    "Выбирай действие кнопками 👇"
)

HOW_IT_WORKS_TEXT = (
    "<b>Как это работает</b>\n\n"
    "1) Ты загружаешь 1–3 фото лица (один раз)\n"
    "2) Выбираешь стиль / локацию кнопками\n"
    "3) Я генерирую реалистичное фото с твоим лицом\n\n"
    "<b>Важно для реализма:</b>\n"
    "• лицо по центру\n"
    "• хорошее освещение\n"
    "• без очков/маски\n"
    "• нейтральное выражение\n\n"
    "<b>Конфиденциальность:</b>\n"
    "• фото не публикуются\n"
    "• не используются для обучения\n"
    "• можно удалить в 1 клик"
)

UPLOAD_REQUIREMENTS = (
    "Пришли <b>1–3 фото</b>, где хорошо видно лицо.\n\n"
    "<b>Требования:</b>\n"
    "• лицо по центру\n"
    "• хорошее освещение\n"
    "• без очков / масок\n"
    "• нейтральное выражение\n\n"
    "Когда отправишь — я сохраню профиль лица ✅\n"
    "Можно отправить 1 фото (минимум), но 2–3 обычно лучше."
)

TRIAL_INFO_TEXT = (
    "🎁 <b>Пробный доступ</b>\n\n"
    f"Сейчас доступно <b>{FREE_LIMIT}</b> бесплатных генераций при первом использовании.\n"
    "Счётчик не показываю в интерфейсе, чтобы не давить 🙂\n\n"
    "После окончания — можно оформить подписку и генерировать без ограничений."
)

LIMIT_ENDED_TEXT = (
    "Твой пробный доступ закончился 💔\n"
    "Оформи подписку и создавай фото без ограничений."
)

SUB_TEXT = (
    "💎 <b>Подписка MIRA</b>\n\n"
    "• <b>7 дней</b> — для теста\n"
    "• <b>30 дней</b> — основной вариант\n\n"
    "С подпиской — <b>безлимит</b> генераций."
)

PRIVACY_TEXT = (
    "🔒 <b>Безопасность</b>\n\n"
    "• Фото не публикуются\n"
    "• Не используются для обучения\n"
    "• Лицо можно удалить в 1 клик"
)

# ----------------------------
# MENUS
# ----------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Создать фото", callback_data="m:create")],
        [InlineKeyboardButton("👤 Загрузить / сменить лицо", callback_data="m:face")],
        [InlineKeyboardButton("🎁 Пробный доступ", callback_data="m:trial")],
        [InlineKeyboardButton("💎 Подписка", callback_data="m:sub")],
        [InlineKeyboardButton("ℹ️ Как это работает", callback_data="m:how")],
    ])


def create_categories_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌴 Путешествия", callback_data="c:travel")],
        [InlineKeyboardButton("👗 Fashion / Lifestyle", callback_data="c:fashion")],
        [InlineKeyboardButton("🌸 Женственность / эстетика", callback_data="c:aesthetic")],
        [InlineKeyboardButton("📖 Storytelling", callback_data="c:story")],
        [InlineKeyboardButton("🎭 Эксперименты", callback_data="c:fun")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="b:home")],
    ])


STYLE_MAP: Dict[str, Dict[str, Any]] = {
    # TRAVEL
    "s:travel:bali": {"title": "🏝 Бали / Мальдивы", "category": "travel"},
    "s:travel:paris": {"title": "🗼 Париж", "category": "travel"},
    "s:travel:dubai": {"title": "🌆 Дубай", "category": "travel"},
    "s:travel:sunset": {"title": "🌊 Море на закате", "category": "travel"},
    "s:travel:alps": {"title": "🏔 Горы / Альпы", "category": "travel"},

    # FASHION
    "s:fashion:fashionshoot": {"title": "💄 Fashion-съёмка", "category": "fashion"},
    "s:fashion:cafe": {"title": "☕ Уютное кафе", "category": "fashion"},
    "s:fashion:street": {"title": "👜 Street style", "category": "fashion"},
    "s:fashion:luxury": {"title": "🕶 Luxury образ", "category": "fashion"},
    "s:fashion:studio": {"title": "🖤 Minimal studio", "category": "fashion"},

    # AESTHETIC
    "s:aesthetic:flowers": {"title": "🌸 Цветы", "category": "aesthetic"},
    "s:aesthetic:evening": {"title": "🌙 Вечерний свет", "category": "aesthetic"},
    "s:aesthetic:morning": {"title": "🛏 Утро у окна", "category": "aesthetic"},
    "s:aesthetic:candles": {"title": "🕯 Свечи и уют", "category": "aesthetic"},

    # STORY
    "s:story:candid": {"title": "📸 Как будто сняли случайно", "category": "story"},
    "s:story:movie": {"title": "🎬 Кадр из фильма", "category": "story"},
    "s:story:ex": {"title": "😏 Фото бывшему", "category": "story"},
    "s:story:newme": {"title": "✨ Новая я", "category": "story"},

    # FUN
    "s:fun:cinema": {"title": "🎬 Кино-образ", "category": "fun"},
    "s:fun:queen": {"title": "👑 Королева", "category": "fun"},
    "s:fun:fairy": {"title": "🧚 Фэнтези", "category": "fun"},
    "s:fun:dark": {"title": "🖤 Dark aesthetic", "category": "fun"},
}


def styles_kb(category: str) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for key, meta in STYLE_MAP.items():
        if meta["category"] == category:
            rows.append([InlineKeyboardButton(meta["title"], callback_data=key)])
    rows.append([InlineKeyboardButton("⬅️ Назад к категориям", callback_data="b:categories")])
    return InlineKeyboardMarkup(rows)


def postgen_kb(style_key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔁 Ещё в этом стиле", callback_data=f"r:{style_key}")],
        [InlineKeyboardButton("✏️ Изменить образ", callback_data="b:categories")],
        [InlineKeyboardButton("👤 Сменить лицо", callback_data="m:face")],
        [InlineKeyboardButton("🗑 Удалить мои фото", callback_data="m:delete_face")],
        [InlineKeyboardButton("⬅️ В меню", callback_data="b:home")],
    ])


def sub_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 7 дней", callback_data="sub:7")],
        [InlineKeyboardButton("👑 30 дней", callback_data="sub:30")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="b:home")],
    ])


def delete_confirm_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Да, удалить", callback_data="del:yes")],
        [InlineKeyboardButton("Отмена", callback_data="del:no")],
    ])


# ----------------------------
# STATE (per-user in memory)
# ----------------------------

# We keep lightweight runtime state in memory.
# For production, you can store this in DB too, but this is enough for UX.
USER_STATE: Dict[int, Dict[str, Any]] = {}


def set_state(user_id: int, **kwargs) -> None:
    st = USER_STATE.get(user_id, {})
    st.update(kwargs)
    USER_STATE[user_id] = st


def get_state(user_id: int) -> Dict[str, Any]:
    return USER_STATE.get(user_id, {})


# ----------------------------
# GENERATION (stub)
# ----------------------------

async def generate_photo_bytes(face_paths: List[str], style_key: str) -> bytes:
    """
    TODO: Replace this stub with real generation:
    - Use your FAL_KEY and actual model call
    - Return final JPEG/PNG bytes

    For now returns a tiny placeholder text as bytes (will be sent as a document),
    so you can verify UX, limits, flows without breaking.
    """
    meta = STYLE_MAP.get(style_key, {"title": style_key})
    payload = (
        f"MIRA placeholder\n"
        f"style={meta.get('title')}\n"
        f"faces={len(face_paths)}\n"
        f"time={datetime.utcnow().isoformat()}Z\n"
    )
    return payload.encode("utf-8")


# ----------------------------
# HELPERS
# ----------------------------

def can_generate(user: Dict[str, Any]) -> Tuple[bool, str]:
    if user_has_active_sub(user):
        return True, ""

    if int(user.get("trial_left", 0)) > 0:
        return True, ""

    return False, LIMIT_ENDED_TEXT


def decrement_trial(user: Dict[str, Any]) -> None:
    left = int(user.get("trial_left", 0))
    if left > 0:
        db_update_user(user["user_id"], trial_left=left - 1)


def face_files_for_user(user_id: int) -> List[str]:
    base = FACES_DIR / str(user_id)
    if not base.exists():
        return []
    return sorted([str(p) for p in base.glob("*.jpg")] + [str(p) for p in base.glob("*.jpeg")] + [str(p) for p in base.glob("*.png")])


async def download_photo(update: Update, context: ContextTypes.DEFAULT_TYPE, msg: Message, dst_path: Path) -> None:
    photo = msg.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    await file.download_to_drive(custom_path=str(dst_path))


def ensure_user_face_dir(user_id: int) -> Path:
    d = FACES_DIR / str(user_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def clear_user_face_files(user_id: int) -> None:
    d = FACES_DIR / str(user_id)
    if not d.exists():
        return
    for p in d.glob("*"):
        try:
            p.unlink()
        except Exception:
            pass


# ----------------------------
# HANDLERS
# ----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = db_get_user(user_id)

    logger.info("start uid=%s", user_id)

    await update.message.reply_text(
        START_TEXT,
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        HOW_IT_WORKS_TEXT,
        reply_markup=main_menu_kb(),
        parse_mode=ParseMode.HTML,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user_id = update.effective_user.id
    user = db_get_user(user_id)

    data = q.data or ""
    logger.info("callback uid=%s data=%s", user_id, data)

    # Back navigation
    if data == "b:home":
        await q.edit_message_text(START_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    if data == "b:categories":
        await q.edit_message_text("Выбери категорию 👇", reply_markup=create_categories_kb())
        return

    # Main menu actions
    if data == "m:how":
        await q.edit_message_text(HOW_IT_WORKS_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    if data == "m:trial":
        await q.edit_message_text(TRIAL_INFO_TEXT, reply_markup=main_menu_kb(), parse_mode=ParseMode.HTML)
        return

    if data == "m:sub":
        await q.edit_message_text(SUB_TEXT, reply_markup=sub_kb(), parse_mode=ParseMode.HTML)
        return

    if data.startswith("sub:"):
        # Пока без реальной оплаты: делаем "ручную" активацию админом (чтобы не тормозить запуск).
        # Потом подключим Telegram Payments / Stripe.
        days = int(data.split(":")[1])
        await q.edit_message_text(
            "💎 Подписка оформляется через оплату (следующим шагом подключим платежи).\n\n"
            "Пока что: напиши администратору, и я активирую подписку вручную.\n"
            f"План: <b>{days} дней</b>",
            reply_markup=main_menu_kb(),
            parse_mode=ParseMode.HTML,
        )
        # Админу — уведомление
        if ADMIN_ID:
            try:
                await context.bot.send_message(
                    chat_id=ADMIN_ID,
                    text=f"Запрос подписки: uid={user_id} план={days} дней",
                )
            except Exception:
                pass
        return

    if data == "m:face":
        set_state(user_id, mode="upload_face", face_count=0)
        await q.edit_message_text(UPLOAD_REQUIREMENTS, parse_mode=ParseMode.HTML)
        return

    if data == "m:delete_face":
        await q.edit_message_text(
            "Точно удалить сохранённое лицо и фото-профиль?\nЭто действие нельзя отменить.",
            reply_markup=delete_confirm_kb(),
        )
        return

    if data == "del:yes":
        clear_user_face_files(user_id)
        clear_face_profile(user_id)
        set_state(user_id, mode=None, face_count=0)
        await q.edit_message_text("🗑 Готово. Лицо удалено.", reply_markup=main_menu_kb())
        return

    if data == "del:no":
        await q.edit_message_text("Ок, ничего не удаляю ✅", reply_markup=main_menu_kb())
        return

    if data == "m:create":
        # Must have face first
        face_profile = get_face_profile(user)
        if not face_profile:
            await q.edit_message_text(
                "Сначала нужно загрузить лицо 👤\n\nНажми: «Загрузить / сменить лицо».",
                reply_markup=main_menu_kb(),
            )
            return

        await q.edit_message_text("Выбери категорию 👇", reply_markup=create_categories_kb())
        return

    # Categories
    if data.startswith("c:"):
        cat = data.split(":")[1]
        await q.edit_message_text("Выбери стиль 👇", reply_markup=styles_kb(cat))
        return

    # Repeat same style
    if data.startswith("r:"):
        style_key = data[len("r:"):]
        await handle_generation(update, context, style_key, edit_instead_of_reply=True)
        return

    # Styles
    if data.startswith("s:"):
        style_key = data
        await handle_generation(update, context, style_key, edit_instead_of_reply=True)
        return

    # Fallback
    await q.edit_message_text("Не понял действие. Вернёмся в меню 👇", reply_markup=main_menu_kb())


async def handle_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, style_key: str, edit_instead_of_reply: bool = False) -> None:
    q = update.callback_query
    user_id = update.effective_user.id
    user = db_get_user(user_id)

    ok, reason = can_generate(user)
    if not ok:
        if edit_instead_of_reply and q:
            await q.edit_message_text(reason, reply_markup=sub_kb(), parse_mode=ParseMode.HTML)
        else:
            await update.effective_message.reply_text(reason, reply_markup=sub_kb(), parse_mode=ParseMode.HTML)
        return

    face_profile = get_face_profile(user)
    if not face_profile:
        text = "Сначала загрузи лицо 👤"
        if edit_instead_of_reply and q:
            await q.edit_message_text(text, reply_markup=main_menu_kb())
        else:
            await update.effective_message.reply_text(text, reply_markup=main_menu_kb())
        return

    face_paths = face_files_for_user(user_id)
    if not face_paths:
        # DB says face exists but files not present (restart / ephemeral storage)
        text = (
            "Похоже, фото лица не найдено (возможен перезапуск сервера).\n"
            "Пожалуйста, загрузи лицо ещё раз 👤"
        )
        clear_face_profile(user_id)
        if edit_instead_of_reply and q:
            await q.edit_message_text(text, reply_markup=main_menu_kb())
        else:
            await update.effective_message.reply_text(text, reply_markup=main_menu_kb())
        return

    title = STYLE_MAP.get(style_key, {}).get("title", "Фото")
    status_text = f"✨ Делаю: <b>{title}</b>\nПодожди пару секунд…"

    if edit_instead_of_reply and q:
        await q.edit_message_text(status_text, parse_mode=ParseMode.HTML)
    else:
        await update.effective_message.reply_text(status_text, parse_mode=ParseMode.HTML)

    try:
        # 1) generate
        img_bytes = await generate_photo_bytes(face_paths=face_paths, style_key=style_key)

        # 2) decrement trial (only if no sub)
        if not user_has_active_sub(user):
            decrement_trial(user)

        # 3) send result
        # Пока это placeholder bytes — отправляем как документ.
        # Когда подключишь реальную генерацию (jpeg/png) — поменяем на send_photo.
        filename = "mira_result.txt"
        await context.bot.send_document(
            chat_id=user_id,
            document=img_bytes,
            filename=filename,
            caption="Готово ✅",
            reply_markup=postgen_kb(style_key),
        )

    except Exception as e:
        logger.exception("generation failed uid=%s style=%s", user_id, style_key)
        await context.bot.send_message(
            chat_id=user_id,
            text="Упс 😔 Что-то пошло не так при генерации. Попробуй ещё раз.",
            reply_markup=postgen_kb(style_key),
        )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    user = db_get_user(user_id)
    st = get_state(user_id)

    # Only handle photo uploads in face upload mode
    if st.get("mode") != "upload_face":
        await update.message.reply_text(
            "Фото принято ✅\nНо чтобы использовать его как лицо — нажми «Загрузить / сменить лицо» 👤",
            reply_markup=main_menu_kb(),
        )
        return

    face_dir = ensure_user_face_dir(user_id)
    count = int(st.get("face_count", 0))

    if count >= MAX_FACE_PHOTOS:
        await update.message.reply_text(
            "Ты уже отправила 3 фото ✅\nЛицо сохранено. Теперь можно создавать фото 📸",
            reply_markup=main_menu_kb(),
        )
        set_state(user_id, mode=None, face_count=0)
        return

    # Save photo
    count += 1
    dst = face_dir / f"{count}.jpg"
    try:
        await download_photo(update, context, update.message, dst)
    except Exception:
        logger.exception("failed to download photo uid=%s", user_id)
        await update.message.reply_text("Не смог сохранить фото 😔 Попробуй ещё раз.")
        return

    set_state(user_id, face_count=count)

    # Update face profile in DB (mark as ready when at least 1 photo exists)
    profile = {
        "created_at": datetime.utcnow().isoformat() + "Z",
        "photos": count,
    }
    set_face_profile(user_id, profile)

    if count < MAX_FACE_PHOTOS:
        await update.message.reply_text(
            f"✅ Фото {count} сохранено.\n"
            "Можешь отправить ещё 1–2 фото для лучшего реализма, или нажми /done",
            reply_markup=main_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "✅ Лицо сохранено. Теперь можно создавать фото 📸",
            reply_markup=main_menu_kb(),
        )
        set_state(user_id, mode=None, face_count=0)


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    st = get_state(user_id)

    if st.get("mode") != "upload_face":
        await update.message.reply_text("Ок ✅", reply_markup=main_menu_kb())
        return

    user = db_get_user(user_id)
    face_profile = get_face_profile(user)
    face_paths = face_files_for_user(user_id)

    if not face_profile or not face_paths:
        await update.message.reply_text(
            "Пока нет загруженного лица.\nНужно отправить хотя бы 1 фото 🙂",
            parse_mode=ParseMode.HTML,
        )
        return

    set_state(user_id, mode=None, face_count=0)
    await update.message.reply_text("✅ Лицо сохранено. Теперь можно создавать фото 📸", reply_markup=main_menu_kb())


async def cmd_admin_sub(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Админ-команда для ручной выдачи подписки:
    /sub 427067749 30
    """
    user_id = update.effective_user.id
    if ADMIN_ID and user_id != ADMIN_ID:
        return

    parts = (update.message.text or "").split()
    if len(parts) != 3:
        await update.message.reply_text("Формат: /sub <user_id> <days>")
        return

    target_id = int(parts[1])
    days = int(parts[2])
    until = (datetime.utcnow() + timedelta(days=days)).isoformat()

    db_get_user(target_id)
    db_update_user(target_id, subscription_until=until)

    await update.message.reply_text(f"✅ Подписка активирована: uid={target_id} на {days} дней")
    try:
        await context.bot.send_message(
            chat_id=target_id,
            text=f"✅ Подписка активирована на <b>{days} дней</b>.\nТеперь генерации безлимитны 👑",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def build_app() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var.")

    db_init()

    app = Application.builder().token(BOT_TOKEN).build()

    # Commands
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("sub", cmd_admin_sub))

    # Callbacks
    app.add_handler(CallbackQueryHandler(on_callback))

    # Photos
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    return app


def main() -> None:
    app = build_app()

    # IMPORTANT: if you run the bot in two places, you'll get 409 Conflict.
    # Keep only ONE running instance.
    logger.info("Starting polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
