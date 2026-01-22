import os
import re
import json
import time
import asyncio
import logging
import tempfile
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

import fal_client
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ----------------------------
# CONFIG
# ----------------------------
DEFAULT_NEGATIVE = "blurry, low resolution, bad, ugly, low quality, pixelated, compression artifacts, noisy, grainy"
FAL_MODEL = "fal-ai/ip-adapter-face-id"

# Качество/скорость (можешь потом крутить)
DEFAULT_CFG = 7.5
DEFAULT_STEPS = 40
DEFAULT_NUM_SAMPLES = 4
DEFAULT_W = 768
DEFAULT_H = 1024
DEFAULT_FACE_DET = 640
DEFAULT_MODEL_TYPE = "1_5-v1"  # см. schema fal

FREE_LIMIT_PER_DAY = 20  # лимит на пользователя в сутки

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("mira-bot")

# ----------------------------
# STATE (простая память в RAM)
# ----------------------------
@dataclass
class UserState:
    face_path: Optional[str] = None
    day_key: str = ""
    used_today: int = 0

_users: Dict[int, UserState] = {}

# ----------------------------
# UI
# ----------------------------
def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("▶️ Запустить"), KeyboardButton("📊 Лимит")],
            [KeyboardButton("☕ Кофейня"), KeyboardButton("🏝️ Мальдивы")],
            [KeyboardButton("🌆 Город"), KeyboardButton("⛰️ Горы")],
            [KeyboardButton("♻️ Сбросить лицо")],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

# ----------------------------
# HELPERS
# ----------------------------
def _today_key() -> str:
    # UTC+0 достаточно. Если хочешь по своему часовому поясу — можно поменять.
    return time.strftime("%Y-%m-%d", time.gmtime())

def _get_user(uid: int) -> UserState:
    u = _users.get(uid)
    if not u:
        u = UserState(day_key=_today_key(), used_today=0)
        _users[uid] = u
    # сброс лимита по дню
    d = _today_key()
    if u.day_key != d:
        u.day_key = d
        u.used_today = 0
    return u

def _sanitize_text(t: str) -> str:
    t = t.strip()
    t = re.sub(r"\s+", " ", t)
    return t

def _is_short_request(t: str) -> bool:
    # короткий запрос типа "мальдивы", "кофейня", "город"
    t = _sanitize_text(t).lower()
    return len(t) <= 30 and len(t.split()) <= 3

def build_prompt(user_text: str) -> str:
    """
    Главная идея: пользователь пишет коротко,
    а мы превращаем это в фотореалистичную сцену с человеком.
    """
    t = _sanitize_text(user_text).lower()

    presets = {
        "☕ кофейня": (
            "Ultra photorealistic travel photo, candid lifestyle shot. "
            "A man sitting in a stylish coffee shop, holding a cup of coffee, natural smile. "
            "Beautiful view outside the window (different country vibe), cinematic natural light, "
            "shallow depth of field, 35mm photo, high detail, realistic skin texture."
        ),
        "🏝️ мальдивы": (
            "Ultra photorealistic vacation photo on the Maldives. "
            "A man sitting near the ocean on a tropical beach, turquoise water, palm trees, "
            "bright sunny day, natural shadows, realistic colors, 35mm photo, "
            "high detail, sharp focus, realistic skin texture."
        ),
        "🌆 город": (
            "Ultra photorealistic street photo. "
            "A man walking in a modern city downtown, beautiful architecture, evening golden hour, "
            "cinematic light, 35mm photo, high detail, natural pose, realistic skin texture."
        ),
        "⛰️ горы": (
            "Ultra photorealistic travel photo in the mountains. "
            "A man standing on a viewpoint with epic mountain landscape, fresh air vibe, "
            "sunrise light, cinematic atmosphere, 35mm photo, high detail, realistic skin texture."
        ),
    }

    # Нажатия кнопок / короткие слова
    if t in ["кофейня", "в кофейне", "кафе", "coffee", "cafe"]:
        return presets["☕ кофейня"]
    if t in ["мальдивы", "maldives", "на мальдивах"]:
        return presets["🏝️ мальдивы"]
    if t in ["город", "улица", "city"]:
        return presets["🌆 город"]
    if t in ["горы", "mountains"]:
        return presets["⛰️ горы"]

    # Если человек пишет фразу типа "я на мальдивах" — тоже нормализуем:
    if "мальдив" in t:
        return presets["🏝️ мальдивы"]
    if "кофе" in t or "кофейн" in t or "кафе" in t:
        return presets["☕ кофейня"]
    if "город" in t or "улиц" in t or "downtown" in t:
        return presets["🌆 город"]
    if "гор" in t or "mount" in t:
        return presets["⛰️ горы"]

    # Универсально: превращаем любой текст в сцену
    return (
        "Ultra photorealistic lifestyle photo of a man. "
        f"Scene: {user_text}. "
        "Natural pose, realistic skin texture, sharp focus, 35mm photo, high detail, "
        "natural lighting, cinematic look."
    )

def _ensure_env():
    if not os.getenv("TELEGRAM_BOT_TOKEN"):
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not os.getenv("FAL_KEY"):
        raise RuntimeError("FAL_KEY is not set (your fal.ai API key)")

# ----------------------------
# FAL CALL (важное место)
# ----------------------------
def _fal_generate_sync(face_path: str, prompt: str) -> str:
    """
    СИНХРОННЫЙ вызов fal-client (мы завернём в asyncio.to_thread).
    ВАЖНО:
    - input требует face_image_url (или face_images_data_url)
    - output: result["image"]["url"]
    """
    face_url = fal_client.upload_file(face_path)

    args = {
        "prompt": prompt,
        "face_image_url": face_url,             # ✅ правильный параметр
        "negative_prompt": DEFAULT_NEGATIVE,
        "guidance_scale": DEFAULT_CFG,
        "num_inference_steps": DEFAULT_STEPS,
        "num_samples": DEFAULT_NUM_SAMPLES,
        "width": DEFAULT_W,
        "height": DEFAULT_H,
        "face_id_det_size": DEFAULT_FACE_DET,
        "model_type": DEFAULT_MODEL_TYPE,
        # seed можно не задавать — будет случайный
    }

    handler = fal_client.submit(FAL_MODEL, arguments=args)
    result = handler.get()

    # ✅ правильный путь (у этой модели один image)
    return result["image"]["url"]

async def generate_with_fal(face_path: str, prompt: str) -> str:
    return await asyncio.to_thread(_fal_generate_sync, face_path, prompt)

# ----------------------------
# HANDLERS
# ----------------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Привет! Я генерирую **фотореалистичные** фото с твоим лицом.\n\n"
        "1) Пришли фото лица (селфи/портрет, лицо крупно).\n"
        "2) Нажми сценарий кнопкой (☕/🏝️/🌆/⛰️) или напиши коротко (например: «я на Мальдивах»).\n\n"
        "Команды:\n"
        "/status — лимит\n"
        "/reset — сбросить лицо\n\n"
        "⚠️ Отправляй фото только с согласия человека."
    )
    await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="Markdown")

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    u = _get_user(uid)
    await update.message.reply_text(f"Лимит на сегодня: {u.used_today}/{FREE_LIMIT_PER_DAY}", reply_markup=main_keyboard())

async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    u = _get_user(uid)
    u.face_path = None
    await update.message.reply_text("Лицо сброшено ✅ Пришли новое фото лица.", reply_markup=main_keyboard())

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    u = _get_user(uid)

    photo = update.message.photo[-1]
    tg_file = await photo.get_file()

    tmpdir = tempfile.gettempdir()
    face_path = os.path.join(tmpdir, f"mira_face_{uid}.jpg")
    await tg_file.download_to_drive(custom_path=face_path)

    u.face_path = face_path
    await update.message.reply_text(
        "Лицо принято ✅\nТеперь нажми сценарий кнопкой (☕/🏝️/🌆/⛰️) или напиши коротко (например: «я на Мальдивах»).",
        reply_markup=main_keyboard(),
    )

async def _handle_generation(update: Update, user_text: str) -> None:
    uid = update.effective_user.id
    u = _get_user(uid)

    if not u.face_path or not os.path.exists(u.face_path):
        await update.message.reply_text("Сначала пришли фото лица 📸", reply_markup=main_keyboard())
        return

    if u.used_today >= FREE_LIMIT_PER_DAY:
        await update.message.reply_text(
            f"Лимит на сегодня исчерпан: {u.used_today}/{FREE_LIMIT_PER_DAY}\nПопробуй завтра 🙂",
            reply_markup=main_keyboard(),
        )
        return

    prompt = build_prompt(user_text)

    await update.message.chat.send_action(action=ChatAction.UPLOAD_PHOTO)
    await update.message.reply_text("Генерирую фотореалистичное фото с твоим лицом…")

    try:
        img_url = await generate_with_fal(u.face_path, prompt)
        u.used_today += 1
        await update.message.reply_photo(photo=img_url, caption=f"Готово ✅\nЛимит: {u.used_today}/{FREE_LIMIT_PER_DAY}")
    except Exception as e:
        log.exception("Generation error")
        await update.message.reply_text(
            "Ошибка генерации ❌\n"
            f"{type(e).__name__}: {e}\n\n"
            "Если это повторяется — пришли ещё раз фото лица и попробуй снова.",
            reply_markup=main_keyboard(),
        )

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = _sanitize_text(update.message.text)

    # кнопки
    if txt == "▶️ Запустить":
        await cmd_start(update, context)
        return
    if txt == "📊 Лимит":
        await cmd_status(update, context)
        return
    if txt == "♻️ Сбросить лицо":
        await cmd_reset(update, context)
        return

    # сценарии-кнопки
    if txt in ["☕ Кофейня", "🏝️ Мальдивы", "🌆 Город", "⛰️ Горы"]:
        await _handle_generation(update, txt)
        return

    # обычный текст от пользователя
    await _handle_generation(update, txt)

    def main():
    _ensure_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # ✅ если Telegram ругнётся на конфликт — просто логируем и не валим процесс
    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        err = context.error
        log.exception("PTB error: %s", err)

    app.add_error_handler(on_error)

    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
