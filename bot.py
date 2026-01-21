import os
import logging
from collections import defaultdict

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
FAL_KEY = os.getenv("FAL_KEY")  # пригодится дальше
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mira-bot")

# простой счетчик (сбросится при перезапуске)
user_usage = defaultdict(int)


def remaining(uid: int) -> int:
    return max(0, FREE_LIMIT - user_usage[uid])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я MIRA.\n\n"
        "Команды:\n"
        "/help — как пользоваться\n"
        "/limit — сколько попыток осталось\n\n"
        "Просто напиши сообщение — я отвечу (дальше подключим FAL)."
    )
    log.info("start uid=%s", uid)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ Как пользоваться:\n"
        "1) Напиши запрос текстом.\n"
        "2) Я обработаю и верну результат.\n\n"
        "Команды:\n"
        "/limit — остаток лимита"
    )


async def limit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        f"📌 Осталось попыток: {remaining(uid)} из {FREE_LIMIT}"
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if remaining(uid) <= 0:
        await update.message.reply_text(
            "🚫 Лимит бесплатных попыток закончился.\n"
            "Напиши /help — подскажy варианты дальше."
        )
        return

    user_usage[uid] += 1

    # пока заглушка — позже подключим FAL по твоей логике
    text = update.message.text.strip()
    await update.message.reply_text(
        f"✅ Принято: «{text}»\n"
        f"Осталось: {remaining(uid)} из {FREE_LIMIT}"
    )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Unhandled error: %s", context.error)
    # опционально — сообщать админу
    if ADMIN_ID:
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"⚠️ Ошибка в боте: {context.error}"
            )
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("limit", limit_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_error_handler(error_handler)

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
