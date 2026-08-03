"""
بوت تيليجرام: يقرا الأغاني اللي تتنزل في قناة، يجيب الكلمات من lyrics.ovh،
ويكتبها كـ"تعليق" (رد) تحت البوست في مجموعة النقاش المربوطة بالقناة.

الإعداد المطلوب قبل التشغيل:
1) اعمل بوت جديد عبر @BotFather وخذ الـ TOKEN.
2) رانج البوت أدمن (أو عضو عادي كافي) في:
   - القناة (اختياري، مش ضروري يكون أدمن فيها)
   - مجموعة النقاش (Discussion Group) المربوطة بالقناة -- هوني لازم يكون البوت
     عضو حتى يقدر يقرا الماسجات ويرد عليها.
3) تأكد إن القناة مربوطة فعليا بمجموعة نقاش (من إعدادات القناة > Discussion).
4) حط الـ TOKEN تحت في المتغير BOT_TOKEN أو كـ environment variable.
5) (اختياري بس ينصح بيه) اعمل مفتاح Gemini API مجاني من:
   https://aistudio.google.com/apikey
   وحطو في environment variable باسم GEMINI_API_KEY. يخدم باش ينظف اسم
   الفنان والعنوان من الميتاداتا الفوضوية (VEVO، Official Video، إلخ)
   قبل ما نبعثهم لـ lyrics.ovh. إذا ماحطيتوش، البوت يخدم بـ regex بسيط بديل.

تنصيب المكتبات:
    pip install python-telegram-bot==21.* httpx

تشغيل:
    export BOT_TOKEN="التوكن_متاعك"
    export GEMINI_API_KEY="مفتاح_Gemini_متاعك"   # اختياري
    python lyrics_bot.py
"""

import os
import re
import json
import asyncio
import logging
import httpx
from threading import Thread
from flask import Flask
from telegram import Update
from telegram.ext import Application, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# التوكن والمفتاح لازم يتحطو كـ Environment Variables في Render (Settings > Environment)
# ماتكتبهمش صريح هوني أبدا.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # سريع ورخيص، كافي لهالمهمة البسيطة

# ---------- سيرفر صغير باش Render يشوف البوت "حي" (Web Service) ----------
web_app = Flask(__name__)


@web_app.route("/")
def home():
    return "البوت شغال ✅"


def run_web():
    port = int(os.environ.get("PORT", 8080))
    web_app.run(host="0.0.0.0", port=port)


def keep_alive():
    Thread(target=run_web).start()


async def clean_with_gemini(raw_text: str) -> tuple[str, str] | None:
    """يستعمل Gemini API باش يستخرج اسم الفنان الحقيقي وعنوان الأغنية
    من نص فوضوي (ميتاداتا/كابشن فيهم اسم قناة، VEVO، Official Video، إلخ)."""
    if not GEMINI_API_KEY:
        return None

    url = (
        f"https://generativelanguage.googleapis.com/v1beta/models/"
        f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    )
    prompt = (
        "من النص التالي (ميتاداتا أو كابشن أغنية غير منظم، فيه ربما اسم قناة "
        "يوتيوب أو كلمات كيف VEVO أو Official Video)، استخرج اسم الفنان الحقيقي "
        "وعنوان الأغنية فقط، بلا أي كلمات زايدة.\n\n"
        f'النص: "{raw_text}"\n\n'
        "رد فقط بصيغة JSON بالضبط بهالشكل، بلا أي نص إضافي ولا Markdown:\n"
        '{"artist": "...", "title": "..."}'
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "response_mime_type": "application/json"},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                logger.warning(f"Gemini رجع خطأ: {resp.status_code} - {resp.text[:200]}")
                return None
            data = resp.json()
            text = data["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            artist, title = parsed.get("artist"), parsed.get("title")
            if artist and title:
                return artist.strip(), title.strip()
    except Exception as e:
        logger.error(f"خطأ في الاتصال بـ Gemini: {e}")
    return None


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """يجيب الكلمات من lyrics.ovh API (مجاني، بلا مفتاح)."""
    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("lyrics")
    except Exception as e:
        logger.error(f"خطأ في جلب الكلمات: {e}")
    return None


# كلمات زايدة نشيلوها من العنوان (Official Video، Lyrics، إلخ)
JUNK_PATTERN = re.compile(
    r"""[\(\[]\s*(official\s*)?(music\s*)?(video|audio|lyrics?|visualizer|hd|4k|mv)\s*.*?[\)\]]"""
    r"""|[\(\[].*?[\)\]]\s*$"""
    r"""|\bofficial\s*(music\s*)?video\b"""
    r"""|\blyrics?\s*video\b"""
    r"""|\bofficial\s*audio\b""",
    re.IGNORECASE,
)


def clean_text(text: str) -> str:
    """يشيل الكلمات الزايدة كيف (Official Video)، [Lyrics]، VEVO، إلخ."""
    text = JUNK_PATTERN.sub("", text)
    text = re.sub(r"\bVEVO\b", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" -_.\t")


def get_raw_text(message) -> str:
    """يجمع كل النصوص المتوفرة (ميتاداتا + كابشن) في نص وحيد نبعثوه لـ Gemini."""
    audio = message.audio
    bits = []
    if audio and audio.performer:
        bits.append(audio.performer)
    if audio and audio.title:
        bits.append(audio.title)
    if message.caption:
        bits.append(message.caption)
    if audio and audio.file_name:
        bits.append(audio.file_name)
    return " | ".join(bits)


def extract_artist_title_fallback(message):
    """طريقة احتياطية (regex) تستعمل إذا Gemini ماخدمش أو ماعندوش مفتاح."""
    audio = message.audio
    if audio and audio.performer and audio.title:
        return clean_text(audio.performer), clean_text(audio.title)

    caption = message.caption or ""
    if " - " in caption:
        parts = [p.strip() for p in caption.split(" - ")]
        # إذا فيه 3 أجزاء وأكثر (كيف: القناة - المغني - العنوان)، نرمي أول جزء
        # لأنه غالبا اسم قناة/بوت وماشي مغني حقيقي
        if len(parts) >= 3:
            artist, title = parts[1], " - ".join(parts[2:])
        else:
            artist, title = parts[0], " - ".join(parts[1:])
        return clean_text(artist), clean_text(title)

    return None, None


async def get_artist_title(message):
    """يجرب Gemini أول (أدق مع النصوص الفوضوية)، وإذا فشل يرجع لـ regex."""
    raw = get_raw_text(message)
    if raw:
        result = await clean_with_gemini(raw)
        if result:
            return result

    return extract_artist_title_fallback(message)


def split_text(text, max_length=4000):
    """يقسم النص لأجزاء ما تتجاوزش max_length، ويحاول يقسم عالسطر باش ما يقطعش كلمة نص نص."""
    parts = []
    while len(text) > max_length:
        split_at = text.rfind('\n', 0, max_length)
        if split_at == -1:  # مفماش سطر جديد قبل الحد، اقسم عادي
            split_at = max_length
        parts.append(text[:split_at])
        text = text[split_at:].lstrip('\n')
    if text:
        parts.append(text)
    return parts


async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يشتغل على الماسجات الجايين في مجموعة النقاش."""
    message = update.effective_message
    if not message:
        return

    # نتأكد إن الماسج هو forward تلقائي من القناة (يعني بوست جديد تنزل)
    if not message.is_automatic_forward:
        return

    if not message.audio:
        return  # مش أغنية، تجاهل

    artist, title = await get_artist_title(message)
    if not artist or not title:
        logger.info("ماعرفتش نستخرج اسم المغني/العنوان، نتخطى.")
        return

    lyrics = await fetch_lyrics(artist, title)

    if lyrics:
        header = f"🎵 كلمات: {artist} - {title}\n\n"
        full_text = header + lyrics
        chunks = split_text(full_text, max_length=4000)

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                chunk = f"{chunk}\n\n[{i+1}/{len(chunks)}]"
            await message.reply_text(chunk)
    else:
        reply_text = f"⚠️ ماعرفتش نلقى كلمات لـ {artist} - {title}"
        await message.reply_text(reply_text)


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "لازم تحط BOT_TOKEN كـ Environment Variable في Render (Settings > Environment)."
        )

    # باتش لبايثون 3.14: asyncio ماعادش يعمل event loop تلقائي في MainThread
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    # نسمع لكل الماسجات الجايين في المجموعات (فيهم الفوروارد التلقائي من القناة)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.AUDIO, handle_group_message)
    )

    logger.info("البوت خدام...")
    app.run_polling()


if __name__ == "__main__":
    keep_alive()
    main()
    