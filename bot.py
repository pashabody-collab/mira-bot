import os
import logging
import sqlite3
from datetime import datetime, timezone, timedelta

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ========= ENV =========
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
SUB_DB_PATH = os.getenv("SUB_DB_PATH", "subscriptions.db")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing. Set it in Render Environment Variables.")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mira-bot")


# ========= DB (subscriptions) =========
def db_conn():
    return sqlite3.connect(SUB_DB_PATH)


def db_init():
    with db_conn() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions(
                user_id INTEGER PRIMARY KEY,
                expires_at TEXT NOT NULL
            )
            """
        )
        con.commit()


def set_subscription(user_id: int, days: int) -> datetime:
    exp = datetime.now(timezone.utc) + timedelta(days=days)
    with db_conn() as con:
        con.execute(
            """
            INSERT INTO subscriptions(user_id, expires_at) VALUES(?, ?)
            ON CONFLICT(user_id) DO UPDATE SET expires_at=excluded.expires_at
            """,
            (user_id, exp.isoformat()),
        )
        con.commit()
    return exp


def get_subscription_expiry(user_id: int):
    with db_conn() as con:
        row = con.execute(
            "SELECT expires_at FROM subscriptions WHERE user_id=?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    try:
        return datetime.fromisoformat(row[0])
    except Exception:
        return None


def has_active_subscription(user_id: int) -> bool:
    exp = get_subscription_expiry(user_id)
    if not exp:
        return False
    return datetime.now(timezone.utc) < exp


# ========= UI =========
def main_menu_kb() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("🖼 Генерация"), KeyboardButton("✨ Улучшить")],
        [KeyboardButton("🎨 Стиль"), KeyboardButton("💳 Подписка")],
        [KeyboardButton("📅 Статус доступа"), KeyboardButton("ℹ️ Помощь")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def style_inline_kb() -> InlineKeyboardMarkup:
    buttons = [
        [
            InlineKeyboardButton("📷 Реализм", callback_data="style:realism"),
            InlineKeyboardButton("🎌 Аниме", callback_data="style:anime"),
        ],
        [
            InlineKeyboardButton("🧊 3D", callback_data="style:3d"),
            InlineKeyboardButton("✏️ Скетч", callback_data="style:sketch"),
        ],
        [
            InlineKeyboardButton("🎬 Кино", callback_data="style:film"),
            InlineKeyboardButton("🚫 Без стиля", callback_data="style:none"),
        ],
    ]
    return InlineKeyboardMarkup(buttons)


def sub_inline_kb() -> InlineKeyboardMarkup:
    # ВАЖНО: выдачу подписки делаем через админа (чтобы никто не активировал сам себе)
    buttons = [
        [
            InlineKeyboardButton("✅ 7 дней", callback_data="sub:7"),
            InlineKeyboardButton("✅ 30 дней", callback_data="sub:30"),
        ],
        [InlineKeyboardButton("🧾 Как оплатить", callback_data="sub:how")],
    ]
    return InlineKeyboardMarkup(buttons)


def set_mode(context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    context.user_data["mode"] = mode


def get_mode(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("mode", "gen")


def set_style(context: ContextTypes.DEFAULT_TYPE, style: str) -> None:
    context.user_data["style"] = style


def get_style(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("style", "none")


# ========= Handlers =========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    set_mode(context, "gen")
    await update.message.reply_text(
        "👋 Привет! Я MIRA.\n\n"
        "🔒 Доступ по подписке.\n"
        "Нажми «💳 Подписка», чтобы оформить (пока активация после оплаты делает админ).\n\n"
        "Выбирай действия кнопками снизу 👇",
        reply_markup=main_menu_kb(),
    )
    log.info("start uid=%s", uid)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Как пользоваться:\n"
        "1) Нажми кнопку режима (Генерация / Улучшить / Стиль).\n"
        "2) Потом отправь текст-запрос.\n\n"
        "💳 Подписка: после оплаты админ активирует на 7 или 30 дней.\n"
        "📅 Статус: показывает до какого числа доступ активен.",
        reply_markup=main_menu_kb(),
    )


async def status_access(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    exp = get_subscription_expiry(uid)
    if exp and has_active_subscription(uid):
        await update.message.reply_text(
            f"✅ Подписка активна до: {exp.astimezone().strftime('%Y-%m-%d %H:%M')}",
            reply_markup=main_menu_kb(),
        )
    else:
        await update.message.reply_text(
            "❌ Подписки нет или она закончилась.\nНажми «💳 Подписка».",
            reply_markup=main_menu_kb(),
        )


async def on_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if text == "🖼 Генерация":
        set_mode(context, "gen")
        await update.message.reply_text(
            "🖼 Режим: Генерация.\nОтправь текст-запрос.",
            reply_markup=main_menu_kb(),
        )
        return

    if text == "✨ Улучшить":
        set_mode(context, "upscale")
        await update.message.reply_text(
            "✨ Режим: Улучшить.\nПока принимаю текст (позже добавим фото).",
            reply_markup=main_menu_kb(),
        )
        return

    if text == "🎨 Стиль":
        await update.message.reply_text("🎨 Выбери стиль:", reply_markup=style_inline_kb())
        return

    if text == "💳 Подписка":
        await update.message.reply_text(
            "💳 Подписка = безлимит на период.\n"
            "Выбери срок (после оплаты админ активирует):",
            reply_markup=sub_inline_kb(),
        )
        return

    if text == "📅 Статус доступа":
        await status_access(update, context)
        return

    if text == "ℹ️ Помощь":
        await help_cmd(update, context)
        return

    # Любой другой текст — основной обработчик
    await handle_user_input(update, context)


async def on_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("style:"):
        return

    style = data.split(":", 1)[1]
    set_style(context, style)

    await query.edit_message_text(
        f"✅ Стиль выбран: **{style}**\n\n"
        "Теперь выбери режим (Генерация/Улучшить) и отправь запрос.",
        parse_mode="Markdown",
    )


async def on_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    uid = query.from_user.id
    data = query.data or ""

    if data == "sub:how":
        await query.edit_message_text(
            "🧾 Как оплатить:\n"
            "1) Переводи оплату (ссылку/реквизиты добавим позже).\n"
            "2) После оплаты напиши: «Оплатил на 7 дней» или «Оплатил на 30 дней».\n"
            "3) Админ активирует подписку.\n\n"
            "💡 Следующим шагом подключим автоматическую оплату (Telegram Payments)."
        )
        return

    if data.startswith("sub:"):
        days = int(data.split(":", 1)[1])

        # Разрешаем выдавать подписку только админу
        if ADMIN_ID and uid != ADMIN_ID:
            await query.edit_message_text(
                "🔒 Подписку активирует админ после оплаты.\n"
                "Напиши админу: «Оплатил на 7/30 дней».",
            )
            return

        exp = set_subscription(uid, days)
        await query.edit_message_text(
            f"✅ Подписка активирована на {days} дней.\n"
            f"Действует до: {exp.astimezone().strftime('%Y-%m-%d %H:%M')}"
        )


async def handle_user_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    # Доступ только по активной подписке
    if not has_active_subscription(uid):
        await update.message.reply_text(
            "🔒 Доступ по подписке.\n"
            "Нажми «💳 Подписка» и оформи доступ.",
            reply_markup=main_menu_kb(),
        )
        return

    mode = get_mode(context)
    style = get_style(context)
    text = (update.message.text or "").strip()

    # Заглушка: тут будет реальный вызов FAL на следующем шаге
    await update.message.reply_text(
        "✅ Принято!\n"
        f"Режим: {mode}\n"
        f"Стиль: {style}\n"
        f"Текст: {text}\n\n"
        "Следующим шагом подключим генерацию через FAL.",
        reply_markup=main_menu_kb(),
    )


async def grant_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Команда для админа: /grant <user_id> <days>
    Пример: /grant 427067749 30
    """
    uid = update.effective_user.id
    if ADMIN_ID and uid != ADMIN_ID:
        return

    args = context.args
    if len(args) != 2:
        await update.message.reply_text("Использование: /grant <user_id> <days>")
        return

    try:
        target = int(args[0])
        days = int(args[1])
        exp = set_subscription(target, days)
        await update.message.reply_text(
            f"✅ Выдал подписку пользователю {target} на {days} дней (до {exp.astimezone().strftime('%Y-%m-%d %H:%M')})"
        )
    except Exception as e:
        await update.message.reply_text(f"Ошибка: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)
    if ADMIN_ID:
        try:
            await context.bot.send_message(chat_id=ADMIN_ID, text=f"⚠ Ошибка: {context.error}")
        except Exception:
            pass


def build_app():
    db_init()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("grant", grant_cmd))

    app.add_handler(CallbackQueryHandler(on_style_callback, pattern=r"^style:"))
    app.add_handler(CallbackQueryHandler(on_sub_callback, pattern=r"^sub:"))

    # Все тексты (включая нажатия кнопок ReplyKeyboard)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_text))

    app.add_error_handler(error_handler)
    return app


if __name__ == "__main__":
    application = build_app()
    application.run_polling(allowed_updates=Update.ALL_TYPES)
