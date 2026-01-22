import os
import re
import json
import time
import logging
import tempfile
import random
from dataclasses import dataclass
from typing import Dict, Any, Optional, List

from telegram import Update, ReplyKeyboardMarkup, KeyboardButton
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
# CONFIG (Render env vars)
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
FAL_KEY = os.getenv("FAL_KEY", "").strip()

# FAL model repo
FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/ip-adapter-face-id").strip()

# daily free limit per user (local limiter; resets on restart)
FREE_LIMIT = int(os.getenv("FREE_LIMIT", "10").strip())

# Quality tuning
DEFAULT_GUIDANCE = float(os.getenv("GUIDANCE_SCALE", "7.5"))
DEFAULT_STEPS = int(os.getenv("STEPS", "40"))
DEFAULT_NUM_SAMPLES = int(os.getenv("NUM_SAMPLES", "1"))
DEFAULT_WIDTH = int(os.getenv("WIDTH", "768"))
DEFAULT_HEIGHT = int(os.getenv("HEIGHT", "1024"))
DEFAULT_FACE_DET = int(os.getenv("FACE_ID_DET_SIZE", "640"))

# Optional (some models ignore these)
DEFAULT_SEED = int(os.getenv("SEED", "-1"))

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
# USAGE LIMIT (in-memory)
# ---------------------------
@dataclass
class Usage:
    day: str
    count: int

_usage: Dict[int, Usage] = {}


def _today_key() -> str:
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
# STATE per user
# ---------------------------
# user_id -> {"face_path": str, "style": str}
_state: Dict[int, Dict[str, Any]] = {}


# ---------------------------
# UI (Buttons)
# ---------------------------
BTN_START = "▶️ Запустить"
BTN_STATUS = "📊 Лимит"
BTN_RESET = "♻️ Сбросить лицо"

BTN_CAFE = "☕️ Кофейня"
BTN_BEACH = "🏝 Мальдивы"
BTN_CITY = "🏙 Город"
BTN_MOUNTAINS = "🏔 Горы"
BTN_OFFICE = "💼 Офис"

def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(BTN_START), KeyboardButton(BTN_STATUS)],
            [KeyboardButton(BTN_CAFE), KeyboardButton(BTN_BEACH)],
            [KeyboardButton(BTN_CITY), KeyboardButton(BTN_MOUNTAINS)],
            [KeyboardButton(BTN_OFFICE), KeyboardButton(BTN_RESET)],
        ],
        resize_keyboard=True,
        one_time_keyboard=False,
        input_field_placeholder="Пришли фото лица или выбери сценарий",
    )


# ---------------------------
# PROMPT HELPERS
# ---------------------------
def _sanitize_text(text: str, limit: int = 1200) -> str:
    text = (text or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text[:limit]


def _has_face(uid: int) -> bool:
    st = _state.get(uid) or {}
    path = st.get("face_path")
    return bool(path and os.path.exists(path))


# База для фотореализма + чтобы модель не уходила в “странные лица”
REALISM_BOOST = (
    "photorealistic, ultra realistic, natural skin texture, realistic pores, "
    "sharp focus, high detail, high resolution, professional photography, "
    "cinematic lighting, natural color, 35mm photo, DSLR, RAW, "
    "correct anatomy, correct hands, correct eyes, no cartoon, no painting"
)

NEGATIVE = (
    "cartoon, anime, illustration, painting, CGI, 3d render, plastic skin, "
    "deformed face, ugly, lowres, blurry, bad anatomy, extra fingers, "
    "missing fingers, distorted hands, watermark, text, logo"
)

# Города/страны для разнообразия
CAFE_LOCATIONS = [
    "Paris cafe with Eiffel Tower in the background",
    "Rome cafe near Colosseum",
    "Istanbul cozy cafe with Bosphorus view",
    "Tokyo modern cafe with neon street outside the window",
    "New York coffee shop with city skyline view",
    "Barcelona cafe near Sagrada Familia",
]

CITY_SCENES = [
    "walking on a sunny street in Lisbon, colorful houses",
    "evening in Dubai downtown, skyscrapers and lights",
    "rainy London street, reflections, cinematic mood",
    "night in Seoul, neon signs, lively street",
    "sunset in San Francisco, Golden Gate bridge view",
]

MOUNTAIN_SCENES = [
    "Iceland mountains, dramatic landscape, cold wind, epic view",
    "Swiss Alps, bright sun, snow peaks, premium travel photo",
    "Norway fjords, panoramic view, natural light",
    "Patagonia mountains, adventure photo, realistic scenery",
]

OFFICE_SCENES = [
    "modern luxury office, big window, city view, business portrait photo",
    "creative studio workspace, laptop, coffee, natural daylight",
    "high-end coworking space, clean minimal интерьер, professional portrait",
]

MALDIVES_SCENES = [
    "Maldives, turquoise ocean, white sand, palm trees, overwater villas",
    "Maldives beach sunset, warm golden light, ocean behind",
    "Maldives resort pier, crystal clear water, luxury travel photo",
]

def build_scene_prompt(style: str, scene: str) -> str:
    style = _sanitize_text(style or "realistic")
    scene = _sanitize_text(scene)

    # Важно: просим, чтобы человек был в кадре (не просто “фон”)
    return (
        f"{style}. {REALISM_BOOST}. "
        f"One person in the scene, the same person as the reference face, "
        f"natural proportions, realistic face identity preserved, "
        f"half-body or full-body shot, "
        f"scene: {scene}. "
        f"NO text, NO watermark."
    )


def expand_short_request(user_text: str) -> str:
    """
    Превращает короткий запрос пользователя в более понятную сцену.
    """
    t = _sanitize_text(user_text, 200).lower()

    # ключевые слова → сценарии
    if "коф" in t or "каф" in t or "coffee" in t or "cafe" in t:
        loc = random.choice(CAFE_LOCATIONS)
        return f"{loc}, person sitting at a table, drinking coffee, natural candid photo, beautiful background"
    if "мальдив" in t or "maldives" in t or "пляж" in t or "океан" in t:
        base = random.choice(MALDIVES_SCENES)
        return f"{base}, person sitting near ocean, relaxed vacation, realistic travel photo"
    if "горы" in t or "mountain" in t or "альп" in t:
        base = random.choice(MOUNTAIN_SCENES)
        return f"{base}, person standing with scenic view, travel portrait, realistic"
    if "офис" in t or "работ" in t or "office" in t:
        base = random.choice(OFFICE_SCENES)
        return f"{base}, person looking confident, professional portrait, realistic"
    if "город" in t or "street" in t or "downtown" in t:
        base = random.choice(CITY_SCENES)
        return f"{base}, person in foreground, street photo, realistic, cinematic"

    # fallback — если пользователь написал просто “я на…”
    # мы делаем нормальную “travel photo” сцену
    return f"travel photo: {user_text}, person in the foreground, realistic environment, natural daylight"


def scenario_by_button(btn: str) -> Optional[str]:
    if btn == BTN_CAFE:
        loc = random.choice(CAFE_LOCATIONS)
        return f"{loc}, person sitting at a cafe table, drinking coffee, beautiful view outside the window, candid photo"
    if btn == BTN_BEACH:
        base = random.choice(MALDIVES_SCENES)
        return f"{base}, person in the foreground, luxury travel photo, realistic"
    if btn == BTN_CITY:
        base = random.choice(CITY_SCENES)
        return f"{base}, person in the foreground, street photo, realistic"
    if btn == BTN_MOUNTAINS:
        base = random.choice(MOUNTAIN_SCENES)
        return f"{base}, person in the foreground, travel portrait, realistic"
    if btn == BTN_OFFICE:
        base = random.choice(OFFICE_SCENES)
        return f"{base}, person in the foreground, professional portrait, realistic"
    return None


# ---------------------------
# FAL CALL
# ---------------------------
async def generate_with_fal(face_path: str, prompt: str) -> str:
    """
    Returns URL of generated image.
    """
    face_url = fal_client.upload_file(face_path)

    args = {
        "prompt": prompt,
        "image_url": face_url,
        "negative_prompt": NEGATIVE,

        # common params (model may ignore some)
        "guidance_scale": DEFAULT_GUIDANCE,
        "num_inference_steps": DEFAULT_STEPS,
        "num_samples": DEFAULT_NUM_SAMPLES,
        "width": DEFAULT_WIDTH,
        "height": DEFAULT_HEIGHT,
        "face_id_det_size": DEFAULT_FACE_DET,
        "seed": DEFAULT_SEED,
    }

    handler = fal_client.submit(FAL_MODEL, arguments=args)
    result = handler.get()

    # Robust URL extraction
    if isinstance(result, dict):
        if "images" in result and isinstance(result["images"], list) and result["images"]:
            img0 = result["images"][0]
            if isinstance(img0, dict) and "url" in img0:
                return img0["url"]
            if isinstance(img0, str) and img0.startswith("http"):
                return img0

        if "image" in result:
            img = result["image"]
            if isinstance(img, dict) and "url" in img:
                return img["url"]
            if isinstance(img, str) and img.startswith("http"):
                return img

        for k in ("output", "url", "result_url"):
            if k in result and isinstance(result[k], str) and result[k].startswith("http"):
                return result[k]

    raise RuntimeError(f"Unexpected model response: {json.dumps(result)[:900]}")


# ---------------------------
# HANDLERS
# ---------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    log.info(f"start uid={uid}")

    msg = (
        "Привет! Я делаю **реалистичные фото с твоим лицом**.\n\n"
        "Как пользоваться (самый простой вариант):\n"
        "1) Пришли **фото лица** (селфи/портрет, лицо крупно).\n"
        "2) Нажми кнопку сценария (например **☕️ Кофейня**) ИЛИ напиши коротко: *«я на Мальдивах»*.\n\n"
        "Команды:\n"
        "/status — лимит\n"
        "/reset — сбросить лицо\n\n"
        "⚠️ Отправляй фото только с согласия человека."
    )

    await update.message.reply_text(msg, reply_markup=main_keyboard(), parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    day = _today_key()
    u = _usage.get(uid)
    used = 0 if not u or u.day != day else u.count
    await update.message.reply_text(
        f"Лимит на сегодня: {used}/{FREE_LIMIT} генераций.",
        reply_markup=main_keyboard(),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    st = _state.pop(uid, None)
    if st and st.get("face_path") and os.path.exists(st["face_path"]):
        try:
            os.remove(st["face_path"])
        except Exception:
            pass
    await update.message.reply_text("Ок, лицо сброшено. Пришли новое фото лица.", reply_markup=main_keyboard())


async def handle_face_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id

    if not can_use(uid):
        await update.message.reply_text(f"Лимит на сегодня исчерпан: {FREE_LIMIT}/{FREE_LIMIT}. Попробуй завтра.")
        return

    if not update.message.photo:
        await update.message.reply_text("Пришли фото как изображение (не документом).", reply_markup=main_keyboard())
        return

    await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)

    tmp_dir = tempfile.gettempdir()
    face_path = os.path.join(tmp_dir, f"face_{uid}_{int(time.time())}.jpg")
    await file.download_to_drive(face_path)

    prev = _state.get(uid, {}).get("face_path")
    if prev and os.path.exists(prev):
        try:
            os.remove(prev)
        except Exception:
            pass

    _state.setdefault(uid, {})["face_path"] = face_path
    _state.setdefault(uid, {})["style"] = _state.get(uid, {}).get("style", "realistic")

    await update.message.reply_text(
        "Лицо принято ✅\n"
        "Теперь нажми кнопку сценария (☕️/🏝/🏙/🏔/💼) или напиши коротко (например: «я на Мальдивах»).",
        reply_markup=main_keyboard(),
    )


async def _run_generation(update: Update, context: ContextTypes.DEFAULT_TYPE, scene: str) -> None:
    uid = update.effective_user.id

    if not _has_face(uid):
        await update.message.reply_text("Сначала пришли фото лица 🙂", reply_markup=main_keyboard())
        return

    if not can_use(uid):
        await update.message.reply_text(f"Лимит на сегодня исчерпан: {FREE_LIMIT}/{FREE_LIMIT}. Попробуй завтра.")
        return

    st = _state.get(uid, {})
    face_path = st["face_path"]
    style_txt = st.get("style", "realistic")

    prompt = build_scene_prompt(style_txt, scene)

    await update.message.chat.send_action(ChatAction.TYPING)
    await update.message.reply_text("Генерирую фотореалистичное фото с твоим лицом…")

    try:
        t0 = time.time()
        out_url = await generate_with_fal(face_path, prompt)
        dt = time.time() - t0

        inc_use(uid)

        await update.message.chat.send_action(ChatAction.UPLOAD_PHOTO)
        await update.message.reply_photo(
            photo=out_url,
            caption=f"Готово ✅ ({dt:.1f}s)\nСценарий: {scene}\nМодель: {FAL_MODEL}",
            reply_markup=main_keyboard(),
        )

    except Exception as e:
        log.exception("generation failed")
        await update.message.reply_text(
            "Ошибка генерации ❌\n"
            "Если это повторяется — обычно модель ждёт немного другие поля.\n\n"
            f"FAL_MODEL: {FAL_MODEL}\n"
            f"Ошибка: {str(e)[:900]}",
            reply_markup=main_keyboard(),
        )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id
    text = _sanitize_text(update.message.text or "", 200)

    # кнопка "Запустить" (без ручного /start)
    if text == BTN_START:
        return await start(update, context)

    if text == BTN_STATUS:
        return await status(update, context)

    if text == BTN_RESET:
        return await reset(update, context)

    # сценарий по кнопке
    scene = scenario_by_button(text)
    if scene:
        return await _run_generation(update, context, scene)

    # если пользователь пишет коротко: "я на мальдивах", "в кофейне", и т.п.
    scene = expand_short_request(text)
    return await _run_generation(update, context, scene)


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Не понял. Нажми ▶️ Запустить или пришли фото лица.", reply_markup=main_keyboard())


def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("reset", reset))

    # photo
    app.add_handler(MessageHandler(filters.PHOTO, handle_face_photo))

    # any text (including buttons)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    # IMPORTANT:
    # Polling requires ONLY ONE running instance.
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        close_loop=False,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
