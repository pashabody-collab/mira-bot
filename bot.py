import os
import re
import json
import time
import random
import logging
import tempfile
from typing import Dict, Optional, Any, Tuple
from datetime import datetime, timezone

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

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
FAL_KEY = (os.getenv("FAL_KEY") or os.getenv("FAL_API_KEY") or "").strip()

# fal.ai model
FAL_MODEL = "fal-ai/ip-adapter-face-id"

# Limits (free for now)
FREE_LIMIT_PER_DAY = int(os.getenv("FREE_LIMIT_PER_DAY", "30"))  # генераций в день на пользователя

# Quality defaults (под реальный фотостайл)
DEFAULT_STYLE = "realistic"

DEFAULT_GUIDANCE = float(os.getenv("DEFAULT_GUIDANCE", "7.5"))
DEFAULT_STEPS = int(os.getenv("DEFAULT_STEPS", "35"))
DEFAULT_WIDTH = int(os.getenv("DEFAULT_WIDTH", "768"))
DEFAULT_HEIGHT = int(os.getenv("DEFAULT_HEIGHT", "1024"))
DEFAULT_FACE_DET = int(os.getenv("DEFAULT_FACE_DET", "640"))
DEFAULT_SEED = int(os.getenv("DEFAULT_SEED", "42"))

# Negative prompt (анти-артефакты)
NEGATIVE = (
    "lowres, blurry, out of focus, cartoon, anime, illustration, painting, cgi, 3d render, "
    "deformed face, distorted face, extra fingers, extra arms, extra legs, bad hands, bad anatomy, "
    "duplicate person, two faces, missing person, cropped head, watermark, text, logo, oversaturated"
)

# Storage in-memory (для Render достаточно; при рестарте лицо надо прислать снова)
_user_face_path: Dict[int, str] = {}
_user_style: Dict[int, str] = {}
_usage: Dict[int, Dict[str, Any]] = {}  # {uid: {"day": "YYYY-MM-DD", "count": int}}

# =========================
# LOGGING
# =========================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("mira-bot")


# =========================
# HELPERS
# =========================

def _today_key() -> str:
    # UTC day key (стабильно для сервера)
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _inc_usage(uid: int) -> Tuple[int, int]:
    day = _today_key()
    u = _usage.get(uid)
    if not u or u.get("day") != day:
        u = {"day": day, "count": 0}
        _usage[uid] = u
    u["count"] += 1
    return u["count"], FREE_LIMIT_PER_DAY


def _get_usage(uid: int) -> Tuple[int, int]:
    day = _today_key()
    u = _usage.get(uid)
    if not u or u.get("day") != day:
        return 0, FREE_LIMIT_PER_DAY
    return int(u.get("count", 0)), FREE_LIMIT_PER_DAY


def _limit_ok(uid: int) -> bool:
    used, lim = _get_usage(uid)
    return used < lim


def _sanitize_text(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def main_keyboard() -> ReplyKeyboardMarkup:
    # Кнопки сценариев (простые)
    keyboard = [
        [KeyboardButton("▶️ Запустить"), KeyboardButton("📊 Лимит")],
        [KeyboardButton("☕ Кофейня"), KeyboardButton("🏝️ Мальдивы")],
        [KeyboardButton("🏙️ Город"), KeyboardButton("🏔️ Горы")],
        [KeyboardButton("🎛️ Стиль"), KeyboardButton("🔄 Сброс лица")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def style_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton("✨ realistic"), KeyboardButton("🎬 cinematic")],
        [KeyboardButton("📰 editorial"), KeyboardButton("🌙 night")],
        [KeyboardButton("⬅️ Назад")],
    ]
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)


def _ensure_keys():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not FAL_KEY:
        # fal-client умеет брать из env FAL_KEY, но мы явно проверим, чтобы не было “тихих” ошибок
        raise RuntimeError("FAL_KEY is not set")


def _set_fal_key():
    # fal_client обычно использует env FAL_KEY; но на всякий — установим явно
    if FAL_KEY:
        try:
            fal_client.api_key = FAL_KEY
        except Exception:
            # если у версии SDK нет api_key — просто оставим env
            pass


# =========================
# PROMPT BUILDING (ВАЖНО)
# =========================

def _build_scene_prompt(user_short: str, style: str) -> str:
    """
    Делает из короткого запроса нормальный детальный промпт:
    - обязательно: человек в кадре
    - обязательно: окружение (Мальдивы/кофейня/город/горы)
    - фотореализм
    """
    t = _sanitize_text(user_short).lower()

    # Если человек пишет "я на мальдивах", "я в кофейне" — мы трактуем как СЦЕНУ,
    # а лицо берём из загруженного фото.
    # Чтобы модель не делала только лицо крупно — просим medium shot / half-body.
    base_photo = (
        "ultra realistic RAW photo, natural skin texture, DSLR, 35mm lens, "
        "medium shot, half-body portrait, single person in frame, sharp focus, high detail, "
        "真实照片, realistic lighting, no stylization"
    )

    style_add = {
        "realistic": "true-to-life colors, daylight, neutral grading",
        "cinematic": "cinematic lighting, shallow depth of field, film look, subtle grain",
        "editorial": "editorial portrait, magazine photo, clean composition, softbox lighting",
        "night": "night scene, neon or warm street lights, bokeh, high ISO but clean",
    }.get(style, "true-to-life colors, daylight")

    # Готовые сценарии (вариативность)
    maldives_variants = [
        "Maldives tropical beach, turquoise ocean, white sand, palm trees, sunny weather",
        "Maldives overwater villas, lagoon, bright blue water, sunny sky",
        "Maldives sunset on the beach, golden hour, calm ocean, soft warm light",
    ]
    cafe_variants = [
        "cozy cafe in Paris, street view, coffee cup on table, warm morning light",
        "modern cafe in Tokyo, minimalist интерьер, чашка кофе, city view through window",
        "small Italian cafe in Rome, espresso bar, warm light, натуральные цвета",
        "Scandinavian cafe in Oslo, soft daylight, уютный интерьер, кофе и ноутбук",
    ]
    city_variants = [
        "New York street, skyscrapers, daylight, city vibe, natural colors",
        "London street near historic buildings, cloudy but bright, realistic look",
        "Dubai marina, modern skyline, bright sun, crisp photo",
        "Singapore downtown, greenery + high-rises, clean modern look",
    ]
    mountains_variants = [
        "Swiss Alps viewpoint, mountains and lake, crisp daylight, realistic atmosphere",
        "Dolomites Italy, mountain cafe terrace, bright sky, scenic view",
        "Georgia mountains, scenic road, fresh air mood, daylight",
        "Norway fjords viewpoint, dramatic landscape, realistic light",
    ]

    # Определяем сцену
    if "мальдив" in t or "maldiv" in t or "🏝️" in t:
        scene = random.choice(maldives_variants)
        action = "person sitting relaxed, smiling naturally, travel photo"
    elif "коф" in t or "cafe" in t or "coffee" in t or "☕" in t:
        scene = random.choice(cafe_variants)
        action = "person sitting at a table, holding a coffee cup, natural relaxed pose"
    elif "гор" in t or "mount" in t or "alps" in t or "🏔️" in t:
        scene = random.choice(mountains_variants)
        action = "person standing at viewpoint, scenic background, travel photo"
    elif "город" in t or "city" in t or "🏙️" in t:
        scene = random.choice(city_variants)
        action = "person walking or standing, street photo, realistic"
    else:
        # Если свободный короткий текст — делаем универсальный “travel photo”,
        # но оставляем смысл пользователя как есть.
        scene = f"realistic scene: {user_short}"
        action = "single person in frame, realistic travel photo"

    # Сборка промпта: важные слова про "один человек", "в кадре", "окружение"
    prompt = (
        f"{base_photo}, {style_add}, {scene}, {action}. "
        f"IMPORTANT: include the person clearly in the scene (not only a close-up face), "
        f"show background/location, keep identity consistent with reference face."
    )
    return prompt


# =========================
# FAL CALL
# =========================

async def generate_with_fal(face_path: str, prompt: str) -> str:
    """
    Returns URL of generated image.
    """

    # Загружаем фото лица в fal
    face_url = fal_client.upload_file(face_path)

    args = {
        # ❗ ВАЖНО: ИМЕННО ЭТОТ ПАРАМЕТР
        "face_image_url": face_url,

        # Основной промпт (уже расширенный сценарием)
        "prompt": prompt,

        # Негативный промпт
        "negative_prompt": (
            "low quality, blurry, deformed face, extra fingers, "
            "bad anatomy, cartoon, anime, painting, unrealistic"
        ),

        # Параметры качества
        "guidance_scale": 7.5,
        "num_inference_steps": 40,
        "num_samples": 1,
        "width": 768,
        "height": 1024,
        "face_id_det_size": 640,
        "seed": 42,
    }

    # ВАЖНО: правильный вызов
    result = await fal_client.run(
        "fal-ai/ip-adapter-face-id",
        arguments=args,
    )

    # Забираем ссылку на изображение
    return result["images"][0]["url"]


    raise RuntimeError(f"Unexpected fal result format: {json.dumps(result)[:500]}")


# =========================
# TELEGRAM HANDLERS
# =========================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "Привет! Я генерирую **фотореалистичные** изображения с сохранением твоего лица.\n\n"
        "Как пользоваться:\n"
        "1) Пришли **фото лица** (селфи/портрет, лицо крупно).\n"
        "2) Нажми кнопку сценария (☕/🏝️/🏙️/🏔️) или напиши коротко: например «я на Мальдивах».\n\n"
        "Команды:\n"
        "/status — лимит\n"
        "/reset — сбросить лицо\n\n"
        "⚠️ Отправляй фото только с согласия человека."
    )
    await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="Markdown")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    used, lim = _get_usage(uid)
    await update.message.reply_text(f"Лимит на сегодня: {used}/{lim} генераций.", reply_markup=main_keyboard())


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    if uid in _user_face_path:
        try:
            os.remove(_user_face_path[uid])
        except Exception:
            pass
        _user_face_path.pop(uid, None)
    await update.message.reply_text("Лицо сброшено ✅ Пришли новое фото лица.", reply_markup=main_keyboard())


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]  # best quality
    tg_file = await photo.get_file()
    # Сохраняем в temp
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
    tmp_path = tmp.name
    tmp.close()

    await tg_file.download_to_drive(custom_path=tmp_path)
    _user_face_path[uid] = tmp_path

    if uid not in _user_style:
        _user_style[uid] = DEFAULT_STYLE

    await update.message.reply_text(
        "Лицо принято ✅\n"
        "Теперь нажми кнопку сценария (☕/🏝️/🏙️/🏔️) или напиши коротко (например: «я на Мальдивах»).",
        reply_markup=main_keyboard()
    )


async def _handle_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, user_text: str) -> None:
    uid = update.effective_user.id

    if uid not in _user_face_path:
        await update.message.reply_text("Сначала пришли фото лица 📸", reply_markup=main_keyboard())
        return

    if not _limit_ok(uid):
        used, lim = _get_usage(uid)
        await update.message.reply_text(f"Лимит на сегодня исчерпан: {used}/{lim}.", reply_markup=main_keyboard())
        return

    style = _user_style.get(uid, DEFAULT_STYLE)
    prompt = _build_scene_prompt(user_text, style)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Генерирую фотореалистичное фото с твоим лицом… ⏳", reply_markup=main_keyboard())

    try:
        img_url = await generate_with_fal(_user_face_path[uid], prompt)
        _inc_usage(uid)
        caption = f"Готово ✅\nМодель: {FAL_MODEL}\nСтиль: {style}"
        await update.message.reply_photo(photo=img_url, caption=caption, reply_markup=main_keyboard())

    except Exception as e:
        log.exception("Generation failed")
        await update.message.reply_text(
            "Ошибка генерации ❌\n"
            f"{type(e).__name__}: {e}\n\n"
            "Если это повторяется — пришли ещё раз фото лица и попробуй снова.",
            reply_markup=main_keyboard()
        )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = _sanitize_text(update.message.text)
    uid = update.effective_user.id

    if text in ("▶️ Запустить",):
        await cmd_start(update, context)
        return

    if text in ("📊 Лимит",):
        await cmd_status(update, context)
        return

    if text in ("🔄 Сброс лица",):
        await cmd_reset(update, context)
        return

    if text in ("🎛️ Стиль",):
        await update.message.reply_text("Выбери стиль:", reply_markup=style_keyboard())
        return

    if text in ("⬅️ Назад",):
        await update.message.reply_text("Ок.", reply_markup=main_keyboard())
        return

    if text.startswith("✨ ") or text.startswith("🎬 ") or text.startswith("📰 ") or text.startswith("🌙 "):
        # style selected
        style = text.split()[-1].strip()
        _user_style[uid] = style
        await update.message.reply_text(f"Стиль установлен: {style} ✅", reply_markup=main_keyboard())
        return

    # Scenario buttons
    if text in ("☕ Кофейня", "🏝️ Мальдивы", "🏙️ Город", "🏔️ Горы"):
        await _handle_generation(update, context, text)
        return

    # Free text (коротко)
    # Например: "я на мальдивах" / "в кофейне" / "ночью в городе" etc.
    await _handle_generation(update, context, text)


# =========================
# MAIN
# =========================

def main() -> None:
    _ensure_keys()
    _set_fal_key()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("reset", cmd_reset))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # ВАЖНО:
    # "Conflict: terminated by other getUpdates request" бывает только если
    # запущено 2 копии бота одновременно (например, старый процесс не умер или запущено локально и на Render).
    # drop_pending_updates помогает со “старыми” апдейтами, но не решает 2 процесса.
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
