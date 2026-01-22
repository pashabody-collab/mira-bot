import os
import re
import time
import json
import logging
import tempfile
from dataclasses import dataclass
from typing import Dict, Any, Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

import fal_client

# ---------------------------
# CONFIG (env vars on Render)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FAL_KEY = os.getenv("FAL_KEY", "").strip()
FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/ip-adapter-face-id").strip()

FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10").strip())  # local daily limit per user
ADMIN_ID = os.getenv("ADMIN_ID", "").strip()

DEFAULT_STRENGTH = float(os.getenv("IMG2IMG_STRENGTH", "0.65"))
DEFAULT_GUIDANCE = float(os.getenv("GUIDANCE_SCALE", "6.5"))
DEFAULT_STEPS = int(os.getenv("STEPS", "30"))

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mira-bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing (set Render env var BOT_TOKEN)")
if not FAL_KEY:
    raise RuntimeError("FAL_KEY is missing (set Render env var FAL_KEY)")

# fal-client uses env var FAL_KEY
os.environ["FAL_KEY"] = FAL_KEY


# ---------------------------
# SIMPLE IN-MEM USAGE LIMIT
# ---------------------------
@dataclass
class Usage:
    day: str
    count: int


_usage: Dict[int, Usage] = {}  # user_id -> Usage


def _today_key() -> str:
    # daily key in UTC
    return time.strftime("%Y-%m-%d", time.gmtime())


def can_use(user_id: int) -> bool:
    day = _today_key()
    u = _usage.get(user_id)
    if not u or u.day != day:
        _usage[user_id] = Usage(day=day, count=0)
        return True
    return u.count < FREE_LIMIT


def inc_use(user_id: int) -> None:
    day = _today_key()
    u = _usage.get(user_id)
    if not u or u.day != day:
        _usage[user_id] = Usage(day=day, count=1)
    else:
        u.count += 1


# ---------------------------
# USER STATE
# user_id -> {"face_path": str, "mode": "waiting_prompt", "style": str}
# ---------------------------
_state: Dict[int, Dict[str, Any]] = {}


def _sanitize_text(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _build_prompt(style: str, user_prompt: str) -> str:
    style = (style or "realistic").strip()
    user_prompt = _sanitize_text(user_prompt)
    return (
        f"{style}. photorealistic, ultra realistic, natural skin texture, "
        f"sharp focus, high detail. {user_prompt}"
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Привет! Я генерирую изображения **с сохранением лица**.\n\n"
        "Как пользоваться:\n"
        "1) Пришли **фото лица** (селфи/портрет, лицо крупно).\n"
        "2) Потом пришли **текстом**, что нужно получить.\n\n"
        "Команды:\n"
        "/style — стиль (по умолчанию: realistic)\n"
        "/status — лимит\n"
        "/reset — сбросить лицо\n\n"
        "⚠️ Отправляй фото только с согласия человека."
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day = _today_key()
    u = _usage.get(uid)
    used = 0 if not u or u.day != day else u.count
    await update.message.reply_text(f"Лимит на сегодня: {used}/{FREE_LIMIT} генераций.")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    st = _state.pop(uid, None)
    if st and st.get("face_path") and os.path.exists(st["face_path"]):
        try:
            os.remove(st["face_path"])
        except Exception:
            pass
    await update.message.reply_text("Ок, лицо сброшено. Пришли новое фото лица.")


async def style(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    parts = (update.message.text or "").split(maxsplit=1)
    if len(parts) == 1:
        current = _state.get(uid, {}).get("style", "realistic")
        await update.message.reply_text(
            "Задай стиль так:\n"
            "`/style realistic`\n"
            "`/style luxury studio portrait`\n"
            "`/style street photo`\n\n"
            f"Текущий стиль: **{current}**",
            parse_mode="Markdown",
        )
        return

    new_style = _sanitize_text(parts[1], limit=300)
    _state.setdefault(uid, {})["style"] = new_style
    await update.message.reply_text(f"Стиль сохранён: {new_style}")


async def handle_face_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    if not can_use(uid):
        await update.message.reply_text(
            f"Лимит на сегодня исчерпан: {FREE_LIMIT}/{FREE_LIMIT}. Попробуй завтра."
        )
        return

    if not update.message.photo:
        await update.message.reply_text("Пришли фото как изображение (не как файл).")
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)

    tmp_dir = tempfile.gettempdir()
    face_path = os.path.join(tmp_dir, f"face_{uid}_{int(time.time())}.jpg")
    await tg_file.download_to_drive(face_path)

    prev = _state.get(uid, {}).get("face_path")
    if prev and os.path.exists(prev):
        try:
            os.remove(prev)
        except Exception:
            pass

    _state[uid] = _state.get(uid, {})
    _state[uid]["face_path"] = face_path
    _state[uid]["mode"] = "waiting_prompt"

    await update.message.reply_text(
        "Лицо принято ✅\nТеперь пришли **промпт текстом** (что генерируем).",
        parse_mode="Markdown",
    )


def _extract_image_url(result: Any) -> Optional[str]:
    """
    FAL responses differ by model.
    Try common shapes and return first URL.
    """
    if isinstance(result, dict):
        # {"images":[{"url": "..."}]}
        imgs = result.get("images")
        if isinstance(imgs, list) and imgs:
            x = imgs[0]
            if isinstance(x, dict) and isinstance(x.get("url"), str):
                return x["url"]
            if isinstance(x, str) and x.startswith("http"):
                return x

        # {"image":{"url":"..."}}
        img = result.get("image")
        if isinstance(img, dict) and isinstance(img.get("url"), str):
            return img["url"]
        if isinstance(img, str) and img.startswith("http"):
            return img

        # sometimes: {"output":"http..."} or {"url":"http..."}
        for k in ("output", "url", "result_url"):
            v = result.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v

    return None


async def generate_with_fal(face_path: str, prompt: str, strength: float, guidance: float, steps: int) -> str:
    # Upload face image to fal storage
    face_url = fal_client.upload_file(face_path)

    # Many “face-id” models accept one of these keys.
    # We pass several common aliases to be compatible.
    args = {
        "prompt": prompt,

        # common aliases for reference/identity image:
        "image_url": face_url,
        "input_image_url": face_url,
        "reference_image_url": face_url,
        "face_image_url": face_url,

        # tuning:
        "strength": strength,
        "guidance_scale": guidance,
        "num_inference_steps": steps,

        "seed": -1,
        "output_format": "jpeg",
    }

    handler = fal_client.submit(FAL_MODEL, arguments=args)
    result = handler.get()

    url = _extract_image_url(result)
    if not url:
        raise RuntimeError(f"Unexpected model response: {json.dumps(result)[:900]}")
    return url


async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    st = _state.get(uid)
    if not st or st.get("mode") != "waiting_prompt" or not st.get("face_path"):
        await update.message.reply_text("Сначала пришли фото лица, потом промпт 🙂")
        return

    if not can_use(uid):
        await update.message.reply_text(
            f"Лимит на сегодня исчерпан: {FREE_LIMIT}/{FREE_LIMIT}. Попробуй завтра."
        )
        return

    face_path = st["face_path"]
    style_txt = st.get("style", "realistic")
    prompt = _build_prompt(style_txt, text)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Генерирую…")

    try:
        t0 = time.time()
        out_url = await generate_with_fal(
            face_path=face_path,
            prompt=prompt,
            strength=DEFAULT_STRENGTH,
            guidance=DEFAULT_GUIDANCE,
            steps=DEFAULT_STEPS,
        )
        dt = time.time() - t0

        inc_use(uid)

        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        await update.message.reply_photo(
            photo=out_url,
            caption=f"Готово ✅ ({dt:.1f}s)\nМодель: {FAL_MODEL}\nСтиль: {style_txt}",
        )

        st["mode"] = "waiting_prompt"

    except Exception as e:
        log.exception("generation failed")
        await update.message.reply_text(
            "Ошибка генерации ❌\n\n"
            f"Модель: {FAL_MODEL}\n"
            "Частые причины:\n"
            "• на fal.ai не добавлен payment method/credits\n"
            "• FAL_MODEL указан неверно\n"
            "• модель ждёт другой ключ для фото\n\n"
            f"Текст ошибки:\n{str(e)[:900]}"
        )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Не понял команду. Напиши /start")


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("style", style))

    app.add_handler(MessageHandler(filters.PHOTO, handle_face_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        close_loop=False,
    )


if __name__ == "__main__":
    main()
