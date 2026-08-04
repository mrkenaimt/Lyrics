import os
import re
import json
import asyncio
import logging
import httpx
from threading import Thread
from flask import Flask
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    ChatMemberHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# التوكن والمفتاح لازم يتحطو كـ Environment Variables في Render (Settings > Environment)
# ماتكتبهمش صريح هوني أبدا.
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = "gemini-2.5-flash"  # سريع ورخيص، كافي لهالمهمة البسيطة

# آيدي الشات متاعك (تحصل عليه من بوت كيف @userinfobot). البوت يبعثلك فيه طلبات
# الموافقة وإشعارات الفشل، وما يخدمش في أي شات ما توافقش عليه.
try:
    OWNER_CHAT_ID = int(os.environ.get("OWNER_CHAT_ID", "0"))
except ValueError:
    OWNER_CHAT_ID = 0

# اختياري: توكن Genius (https://genius.com/api-clients) باش نستعملو كمصدر ثالث
# احتياطي للكلمات إذا المصدرين الأولين ما لقاوش شيء.
GENIUS_ACCESS_TOKEN = os.environ.get("GENIUS_ACCESS_TOKEN", "")

# نخزنو لستة الشاتات المفعّلة/المعلّقة في Upstash Redis (REST API) باش
# التخزين يبقى ثابت حتى لو Render عاود شغّل السيرفيس (القرص المحلي مش دائم).
# https://console.upstash.com -> Create Database -> انسخ REST URL و REST TOKEN
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")

REDIS_APPROVED_KEY = "lyrics_bot:approved_chats"  # Redis Set
REDIS_PENDING_KEY = "lyrics_bot:pending_chats"  # Redis Hash: chat_id -> json({"title","type"})
# ملاحظة: إذا UPSTASH_REDIS_REST_URL/TOKEN ماهومش محطوطين، البوت يخدم بس ما
# يحتفظش بالموافقات بين الـ restarts (كل تشغيلة جديدة، ما فماش شات مفعّلة).

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


# ============================================================
# تخزين الشاتات (موافق عليها / معلّقة) - Upstash Redis REST API
# ============================================================
async def redis_cmd(*args) -> object:
    """يبعث أمر Redis وحيد لـ Upstash REST API ويرجع نتيجته (result field)."""
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        logger.warning("UPSTASH_REDIS_REST_URL / TOKEN ماهومش محطوطين، ما نجمش نخزن.")
        return None
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                UPSTASH_URL,
                headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
                json=list(args),
            )
            if resp.status_code == 200:
                return resp.json().get("result")
            logger.error(f"Upstash رجع خطأ: {resp.status_code} - {resp.text[:200]}")
    except Exception as e:
        logger.error(f"خطأ في الاتصال بـ Upstash: {e}")
    return None


async def is_approved(chat_id: int) -> bool:
    result = await redis_cmd("SISMEMBER", REDIS_APPROVED_KEY, str(chat_id))
    return result == 1


async def is_pending(chat_id: int) -> bool:
    result = await redis_cmd("HEXISTS", REDIS_PENDING_KEY, str(chat_id))
    return result == 1


async def add_pending(chat_id: int, title: str, chat_type: str) -> None:
    await redis_cmd(
        "HSET", REDIS_PENDING_KEY, str(chat_id), json.dumps({"title": title, "type": chat_type})
    )


async def pop_pending(chat_id: int) -> dict | None:
    raw = await redis_cmd("HGET", REDIS_PENDING_KEY, str(chat_id))
    await redis_cmd("HDEL", REDIS_PENDING_KEY, str(chat_id))
    if raw:
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


async def approve_chat(chat_id: int) -> None:
    await redis_cmd("SADD", REDIS_APPROVED_KEY, str(chat_id))


async def remove_chat(chat_id: int) -> None:
    await redis_cmd("SREM", REDIS_APPROVED_KEY, str(chat_id))
    await redis_cmd("HDEL", REDIS_PENDING_KEY, str(chat_id))


async def get_all_approved() -> list:
    result = await redis_cmd("SMEMBERS", REDIS_APPROVED_KEY)
    return result or []


async def get_all_pending() -> dict:
    """يرجع dict {chat_id: {"title":..., "type":...}} من الـ Redis Hash."""
    flat = await redis_cmd("HGETALL", REDIS_PENDING_KEY)
    result = {}
    if flat:
        for i in range(0, len(flat) - 1, 2):
            key, raw_val = flat[i], flat[i + 1]
            try:
                result[key] = json.loads(raw_val)
            except Exception:
                result[key] = {"title": raw_val, "type": "?"}
    return result


def chat_label(chat) -> str:
    return chat.title or chat.username or str(chat.id)


async def request_approval(context: ContextTypes.DEFAULT_TYPE, chat) -> None:
    """يبعث طلب موافقة للأونر (مرة وحدة لكل شات، ما يسبامش)."""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID ماهوش محطوط، ما نجمش نبعث طلب موافقة.")
        return

    if await is_approved(chat.id):
        return
    if await is_pending(chat.id):
        return  # طلب مبعوث already

    await add_pending(chat.id, chat_label(chat), chat.type)

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ موافقة", callback_data=f"approve_{chat.id}"),
                InlineKeyboardButton("❌ رفض", callback_data=f"reject_{chat.id}"),
            ]
        ]
    )
    text = (
        "🔔 طلب تفعيل جديد للبوت\n\n"
        f"الشات: {chat_label(chat)}\n"
        f"ID: {chat.id}\n"
        f"النوع: {chat.type}"
    )
    try:
        await context.bot.send_message(OWNER_CHAT_ID, text, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"خطأ في بعث طلب الموافقة: {e}")


async def notify_owner(context: ContextTypes.DEFAULT_TYPE, chat, text: str) -> None:
    """يبعث إشعار (فشل، خطأ...) للأونر بدل الشات نفسها."""
    if not OWNER_CHAT_ID:
        logger.warning("OWNER_CHAT_ID ماهوش محطوط، ما نجمش نبعث إشعار.")
        return
    try:
        await context.bot.send_message(OWNER_CHAT_ID, f"⚠️ {chat_label(chat)}\n{text}")
    except Exception as e:
        logger.error(f"خطأ في بعث إشعار للأونر: {e}")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يتفعل كل مرة يتزاد فيها البوت / يتحيّد / تتبدل صلاحياته في شات."""
    cmu = update.my_chat_member
    if not cmu:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status
    chat = cmu.chat

    joined = {"member", "administrator"}
    left = {"left", "kicked"}

    if new_status in joined and old_status in left:
        await request_approval(context, chat)
    elif new_status in left:
        await remove_chat(chat.id)


async def on_approval_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_CHAT_ID:
        await query.answer("غير مصرح لك تستعمل هالزر.", show_alert=True)
        return

    await query.answer()
    action, chat_id_str = query.data.split("_", 1)
    chat_id = int(chat_id_str)

    info = await pop_pending(chat_id)
    label = info.get("title") if info else str(chat_id)

    if action == "approve":
        await approve_chat(chat_id)
        await query.edit_message_text(f"✅ تم تفعيل البوت في: {label}")
    else:
        await query.edit_message_text(f"❌ تم رفض: {label}")
        try:
            await context.bot.leave_chat(chat_id)
        except Exception as e:
            logger.warning(f"ماقدرتش نخرج من الشات {chat_id}: {e}")


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """أمر /list باش الأونر يشوف الشاتات المفعّلة والمعلّقة."""
    if update.effective_user is None or update.effective_user.id != OWNER_CHAT_ID:
        return
    approved = await get_all_approved() or ["لا شيء"]
    pending_data = await get_all_pending()
    pending = [f"{v['title']} ({k})" for k, v in pending_data.items()] or ["لا شيء"]
    text = (
        "✅ مفعّلة:\n" + "\n".join(str(a) for a in approved) +
        "\n\n⏳ معلّقة:\n" + "\n".join(pending)
    )
    await update.effective_message.reply_text(text)


# ============================================================
# استخراج اسم الفنان والعنوان
# ============================================================
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


# ============================================================
# مصادر الكلمات (lyrics) - نجربو بالترتيب لين نلقاو نتيجة
# ============================================================
async def fetch_lyrics_lrclib(artist: str, title: str) -> str | None:
    """lrclib.net - مجاني، بلا مفتاح، عندو /get للمطابقة الدقيقة و /search للمطابقة التقريبية."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                "https://lrclib.net/api/get",
                params={"artist_name": artist, "track_name": title},
            )
            if resp.status_code == 200:
                data = resp.json()
                lyrics = data.get("plainLyrics") or data.get("syncedLyrics")
                if lyrics:
                    return lyrics

            resp = await client.get(
                "https://lrclib.net/api/search",
                params={"artist_name": artist, "track_name": title},
            )
            if resp.status_code == 200:
                results = resp.json()
                if results:
                    lyrics = results[0].get("plainLyrics") or results[0].get("syncedLyrics")
                    if lyrics:
                        return lyrics
    except Exception as e:
        logger.error(f"خطأ في lrclib: {e}")
    return None


async def fetch_lyrics_ovh(artist: str, title: str) -> str | None:
    """lyrics.ovh API (مجاني، بلا مفتاح)."""
    url = f"https://api.lyrics.ovh/v1/{artist}/{title}"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("lyrics")
    except Exception as e:
        logger.error(f"خطأ في lyrics.ovh: {e}")
    return None


async def fetch_lyrics_genius(artist: str, title: str) -> str | None:
    """احتياط أخير: نبحثو في Genius API على الأغنية، ونجيبو الكلمات من صفحتها.
    يحتاج GENIUS_ACCESS_TOKEN. إذا مش محطوط أو bs4 ماهوش مثبت، يترجع None."""
    if not GENIUS_ACCESS_TOKEN:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 ماهوش مثبت، ماقدرش نستعمل Genius. زيدها في requirements.txt")
        return None

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            search_resp = await client.get(
                "https://api.genius.com/search",
                params={"q": f"{artist} {title}"},
                headers={"Authorization": f"Bearer {GENIUS_ACCESS_TOKEN}"},
            )
            if search_resp.status_code != 200:
                return None
            hits = search_resp.json().get("response", {}).get("hits", [])
            if not hits:
                return None
            song_url = hits[0]["result"]["url"]

            page_resp = await client.get(song_url)
            if page_resp.status_code != 200:
                return None
            soup = BeautifulSoup(page_resp.text, "html.parser")
            containers = soup.find_all("div", attrs={"data-lyrics-container": "true"})
            if not containers:
                return None
            lines = []
            for c in containers:
                lines.append(c.get_text(separator="\n"))
            lyrics = "\n".join(lines).strip()
            return lyrics or None
    except Exception as e:
        logger.error(f"خطأ في Genius: {e}")
    return None


async def fetch_lyrics(artist: str, title: str) -> str | None:
    """يجرب عدة مصادر بالترتيب لين يلقى الكلمات، باش يكون شبه مضمون يلقاها."""
    sources = (fetch_lyrics_lrclib, fetch_lyrics_ovh, fetch_lyrics_genius)
    for source in sources:
        try:
            lyrics = await source(artist, title)
        except Exception as e:
            logger.error(f"خطأ غير متوقع في {source.__name__}: {e}")
            lyrics = None
        if lyrics and lyrics.strip():
            return lyrics.strip()
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

    chat = message.chat

    # الشات لازم تكون موافق عليها من الأونر، وإلا نبعثو طلب موافقة (مرة وحدة) ونوقفو هوني.
    if not await is_approved(chat.id):
        await request_approval(context, chat)
        return

    # نتأكد إن الماسج هو forward تلقائي من القناة (يعني بوست جديد تنزل)
    if not message.is_automatic_forward:
        return

    if not message.audio:
        return  # مش أغنية، تجاهل

    artist, title = await get_artist_title(message)
    if not artist or not title:
        logger.info("i can't find the artist or the name for this song.")
        await notify_owner(context, chat, "ما قدرتش نلقى اسم الفنان أو عنوان الأغنية لهاد البوست.")
        return

    lyrics = await fetch_lyrics(artist, title)

    if lyrics:
        header = f"🎵 LYRICS: {artist} - {title}\n\n"
        full_text = header + lyrics
        chunks = split_text(full_text, max_length=4000)

        for i, chunk in enumerate(chunks):
            if len(chunks) > 1:
                chunk = f"{chunk}\n\n[{i+1}/{len(chunks)}]"
            await message.reply_text(chunk)
    else:
        await notify_owner(context, chat, f"ما لقيتش الكلمات لـ: {artist} - {title}")


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "لازم تحط BOT_TOKEN كـ Environment Variable في Render (Settings > Environment)."
        )
    if not OWNER_CHAT_ID:
        logger.warning(
            "OWNER_CHAT_ID ماهوش محطوط. البوت ما ينجمش يبعثلك طلبات موافقة ولا إشعارات الفشل."
        )

    # باتش لبايثون 3.14: asyncio ماعادش يعمل event loop تلقائي في MainThread
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    # موافقة على الشاتات الجداد (لمّا البوت يتزاد كعضو/أدمين في قروب أو قناة)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    # أزرار الموافقة/الرفض
    app.add_handler(CallbackQueryHandler(on_approval_callback, pattern=r"^(approve|reject)_"))
    # أمر /list للأونر باش يشوف الشاتات المفعّلة/المعلّقة
    app.add_handler(CommandHandler("list", cmd_list))
    # نسمع لكل الماسجات الجايين في المجموعات (فيهم الفوروارد التلقائي من القناة)
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.AUDIO, handle_group_message)
    )

    logger.info("البوت خدام...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    keep_alive()
    main()
