import os
import re
import json
import uuid
import logging
import asyncio
import threading

import requests
import yt_dlp
import imageio_ffmpeg
from flask import Flask

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from mutagen.id3 import ID3, ID3NoHeaderError, TIT2, TPE1

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("musicbot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN muhit o'zgaruvchisi sozlanmagan.")

DOWNLOAD_DIR = "/tmp/musicbot_downloads"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
FFMPEG_PATH = imageio_ffmpeg.get_ffmpeg_exe()
MAX_UPLOAD_BYTES = 45 * 1024 * 1024  # Telegram bot API ~50MB limitidan biroz pastroq

# Render'da alohida "Secret File" o'rniga cookies.txt matnini to'g'ridan-to'g'ri
# COOKIES_CONTENT muhit o'zgaruvchisi orqali berish mumkin - shu holda uni
# ishga tushishda vaqtinchalik faylga yozib, COOKIES_FILE sifatida ishlatamiz.
COOKIES_FILE = os.getenv("COOKIES_FILE", "")
COOKIES_CONTENT = os.getenv("COOKIES_CONTENT", "")
if COOKIES_CONTENT and not COOKIES_FILE:
    COOKIES_FILE = os.path.join(DOWNLOAD_DIR, "cookies.txt")
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(COOKIES_CONTENT)

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

URL_RE = re.compile(r"https?://\S+")
PLATFORM_PATTERNS = {
    "Instagram": re.compile(r"instagram\.com"),
    "TikTok": re.compile(r"tiktok\.com"),
    "Pinterest": re.compile(r"pinterest\.[a-z.]+|pin\.it"),
}
# TikTok slayd-shou (rasm+musiqa) postlari /photo/ yo'lida bo'ladi. yt-dlp'ning
# TikTok extraktori bularni "Unsupported URL" deb butunlay rad etadi (URL'ni
# /video/ ga almashtirib yuborish ham yordam bermaydi - u post turini
# ID orqali aniqlab, baribir video sifatida ishlamasligini aytadi), shuning
# uchun bunday postlarni sahifa HTML'idan o'zimiz o'qib olamiz.
TIKTOK_PHOTO_RE = re.compile(r"tiktok\.com/@[\w.\-]+/photo/\d+")

pending_links: dict[str, str] = {}
pending_audio: dict[int, str] = {}


class TagStates(StatesGroup):
    waiting_title = State()
    waiting_artist = State()


def _cleanup(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception as e:
            logger.warning("Faylni o'chirishda xatolik: %s", e)


def detect_platform(url: str) -> str | None:
    for name, pattern in PLATFORM_PATTERNS.items():
        if pattern.search(url):
            return name
    return None


def _base_ydl_opts() -> dict:
    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "ffmpeg_location": FFMPEG_PATH,
    }
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def _ytdlp_extract(query_or_url: str, audio_only: bool):
    opts = _base_ydl_opts()
    opts["outtmpl"] = os.path.join(DOWNLOAD_DIR, "%(id)s_%(epoch)s.%(ext)s")
    opts["max_filesize"] = MAX_UPLOAD_BYTES
    # Parchalab (HLS/DASH) beriladigan fayllarni bir nechta ulanish bilan
    # parallel yuklab tezlashtiradi.
    opts["concurrent_fragment_downloads"] = 4
    if audio_only:
        # "*" - filesize/bitrate kabi ba'zi metama'lumotlari to'liq bo'lmagan
        # formatlarni ham qabul qiladi, aks holda yt-dlp ularni "mos emas" deb
        # rad etib "Requested format is not available" xatosini beradi.
        # mp3 formatini ustunlik bilan tanlaymiz va uni qayta kodlamaymiz -
        # SoundCloud odatda progressiv mp3 beradi, qayta encode qilish faqat
        # vaqt yo'qotadi (Telegram mp3/m4a'ni to'g'ridan-to'g'ri qabul qiladi).
        opts["format"] = "bestaudio[ext=mp3]/bestaudio*/best*"
    else:
        # Video va audio alohida oqim sifatida berilgan hollarda (masalan
        # Pinterest) ularni birlashtiramiz - aks holda faqat ovozsiz video
        # yuklanib qolishi mumkin edi.
        opts["format"] = "bestvideo*+bestaudio/best*"
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(query_or_url, download=True)
        if "entries" in info:
            info = info["entries"][0]
        filename = ydl.prepare_filename(info)
        return filename, info


async def search_music(query: str):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _ytdlp_extract, f"scsearch1:{query}", True)


async def _fix_faststart(path: str) -> str:
    """MP4'ning meta-ma'lumotini (moov atom) fayl boshiga ko'chiradi - aks
    holda ba'zi pleyerlar (shu jumladan Telegram) videoni birinchi kadrdan
    keyin "muzlatib" qo'yishi mumkin. Faqat oqimni ko'chiradi (qayta encode
    qilmaydi), shuning uchun juda tez ishlaydi."""
    fixed_path = f"{os.path.splitext(path)[0]}_fs.mp4"
    cmd = [FFMPEG_PATH, "-y", "-i", path, "-c", "copy", "-movflags", "+faststart", fixed_path]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("faststart remuxida xatolik, asl fayl ishlatiladi: %s", stderr.decode(errors="ignore")[-300:])
        return path
    _cleanup(path)
    return fixed_path


async def download_media(url: str, audio_only: bool):
    loop = asyncio.get_running_loop()
    path, info = await loop.run_in_executor(None, _ytdlp_extract, url, audio_only)
    if not audio_only:
        path = await _fix_faststart(path)
    return path, info


def _save_image_bytes(image_url: str) -> str:
    resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.jpg")
    with open(path, "wb") as f:
        f.write(resp.content)
    return path


def _download_photo_sync(url: str) -> str:
    try:
        opts = _base_ydl_opts()
        opts["skip_download"] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            thumb = info.get("thumbnail")
            if not thumb and info.get("thumbnails"):
                thumb = info["thumbnails"][-1]["url"]
            if thumb:
                return _save_image_bytes(thumb)
    except Exception as e:
        logger.info("yt-dlp orqali rasm topilmadi, og:image'ga o'tilmoqda: %s", e)

    resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
    match = re.search(r'<meta property="og:image" content="([^"]+)"', resp.text)
    if not match:
        raise RuntimeError("Rasm topilmadi.")
    return _save_image_bytes(match.group(1))


async def download_photo(url: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _download_photo_sync, url)


TIKTOK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


def _scrape_tiktok_slideshow_sync(url: str) -> dict:
    """yt-dlp TikTok slayd-shou (rasm+musiqa) postlarini qo'llab-quvvatlamaydi,
    shuning uchun sahifa ichidagi __UNIVERSAL_DATA_FOR_REHYDRATION__ JSON
    blokini o'zimiz o'qib olamiz. TikTok sahifa tuzilishini o'zgartirsa, bu
    funksiya ham yangilanishga muhtoj bo'lib qolishi mumkin."""
    resp = requests.get(url, timeout=15, headers={"User-Agent": TIKTOK_UA})
    resp.raise_for_status()
    match = re.search(
        r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>',
        resp.text, re.DOTALL,
    )
    if not match:
        raise RuntimeError("TikTok sahifasidan ma'lumot topilmadi.")
    data = json.loads(match.group(1))
    scope = data.get("__DEFAULT_SCOPE__", {})
    item = None
    for key in ("webapp.video-detail", "webapp.photo-detail"):
        detail = (scope.get(key) or {}).get("itemInfo", {}).get("itemStruct")
        if detail:
            item = detail
            break
    if not item:
        raise RuntimeError("TikTok postining ma'lumotlari topilmadi.")

    images = []
    for img in (item.get("imagePost") or {}).get("images") or []:
        url_list = (img.get("imageURL") or {}).get("urlList") or []
        if url_list:
            images.append(url_list[0])

    music = item.get("music") or {}
    music_url_list = (music.get("playUrl") or {}).get("urlList") or []

    return {
        "images": images,
        "music_url": music_url_list[0] if music_url_list else None,
        "music_title": music.get("title"),
        "music_author": music.get("authorName"),
    }


async def scrape_tiktok_slideshow(url: str) -> dict:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _scrape_tiktok_slideshow_sync, url)


def _download_urls_sync(urls: list, ext: str) -> list:
    paths = []
    for u in urls:
        resp = requests.get(u, timeout=20, headers={"User-Agent": TIKTOK_UA})
        resp.raise_for_status()
        path = os.path.join(DOWNLOAD_DIR, f"{uuid.uuid4().hex}.{ext}")
        with open(path, "wb") as f:
            f.write(resp.content)
        paths.append(path)
    return paths


def _friendly_download_error(e: Exception) -> str:
    text = str(e)
    lower = text.lower()
    if "rate-limit" in lower or "login required" in lower or "redirected to the login page" in lower:
        return (
            "Bu havola login talab qiladi (platforma cheklovi, masalan Instagram). "
            "Administrator shu platforma uchun COOKIES_CONTENT'ni yangilashi kerak."
        )
    if "sign in to confirm" in lower or "requested format is not available" in lower:
        return "Manba hozir botlarni cheklamoqda / kerakli formatni bermayapti."
    return text


async def convert_to_round(src_path: str, out_path: str):
    cmd = [
        FFMPEG_PATH, "-y", "-i", src_path,
        "-t", "60",
        "-vf", "crop='min(iw,ih)':'min(iw,ih)',scale=480:480",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-threads", "0",
        "-c:a", "aac", "-b:a", "128k",
        out_path,
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-500:])


def set_audio_tags(path: str, title: str, artist: str):
    try:
        tags = ID3(path)
    except ID3NoHeaderError:
        tags = ID3()
    tags["TIT2"] = TIT2(encoding=3, text=title)
    tags["TPE1"] = TPE1(encoding=3, text=artist)
    tags.save(path)


@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "Salom! 👋\n\n"
        "🎵 Musiqa nomini yozing (shaxsiy chatda) - men uni topib beraman.\n"
        "🔗 Instagram, TikTok yoki Pinterest havolasini tashlang - "
        "video, rasm yoki musiqa qilib beraman (tanlaysiz).\n"
        "🎥 Video yuboring - aylana video (video note) qilib qaytaraman.\n"
        "🎧 Musiqa fayl yuboring - nomi va ijrochisini o'zgartirib beraman.\n\n"
        "Guruhda ishlashim uchun meni <b>admin</b> qiling (aks holda ba'zi "
        "xabarlarni ko'ra olmayman)."
    )


@dp.message(F.text.regexp(r"^\.id$"))
async def cmd_id(message: Message):
    if message.chat.type == "private":
        return await message.reply("Bu buyruq faqat guruhda, foydalanuvchiga reply qilib ishlaydi.")
    if not message.reply_to_message or not message.reply_to_message.from_user:
        return await message.reply("Foydalanuvchiga reply qilib .id deb yozing.")
    user = message.reply_to_message.from_user
    username = f"@{user.username}" if user.username else "(username yo'q)"
    await message.reply(
        f"👤 {user.full_name}\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"Username: {username}"
    )


@dp.message(F.video)
async def handle_video(message: Message):
    status = await message.reply("🔄 Aylana videoga aylantirilmoqda...")
    src_path = os.path.join(DOWNLOAD_DIR, f"{message.video.file_unique_id}_src.mp4")
    out_path = os.path.join(DOWNLOAD_DIR, f"{message.video.file_unique_id}_round.mp4")
    try:
        file = await bot.get_file(message.video.file_id)
        await bot.download_file(file.file_path, destination=src_path)
        await convert_to_round(src_path, out_path)
        await message.reply_video_note(FSInputFile(out_path))
        await status.delete()
    except Exception as e:
        logger.exception("Video note konvertatsiyasida xatolik")
        await status.edit_text(f"❌ Xatolik: {e}")
    finally:
        _cleanup(src_path, out_path)


@dp.message(F.audio | (F.document & F.document.mime_type.startswith("audio/")))
async def handle_audio(message: Message, state: FSMContext):
    audio = message.audio or message.document
    file = await bot.get_file(audio.file_id)
    path = os.path.join(DOWNLOAD_DIR, f"{audio.file_unique_id}.mp3")
    await bot.download_file(file.file_path, destination=path)
    pending_audio[message.from_user.id] = path
    await state.set_state(TagStates.waiting_title)
    await message.reply("🎼 Yangi nom (sarlavha)ni yuboring:")


@dp.message(TagStates.waiting_title)
async def receive_title(message: Message, state: FSMContext):
    if not message.text:
        return await message.reply("Iltimos, matn ko'rinishida nom yuboring.")
    await state.update_data(new_title=message.text.strip())
    await state.set_state(TagStates.waiting_artist)
    await message.reply("🎤 Endi ijrochi (muallif) nomini yuboring:")


@dp.message(TagStates.waiting_artist)
async def receive_artist(message: Message, state: FSMContext):
    if not message.text:
        return await message.reply("Iltimos, matn ko'rinishida ijrochi nomini yuboring.")
    data = await state.get_data()
    title = data.get("new_title", "")
    artist = message.text.strip()
    path = pending_audio.pop(message.from_user.id, None)
    await state.clear()
    if not path or not os.path.exists(path):
        return await message.reply("Xatolik: fayl topilmadi, musiqani qaytadan yuboring.")
    try:
        set_audio_tags(path, title, artist)
        await message.reply_audio(FSInputFile(path), title=title, performer=artist)
    except Exception as e:
        logger.exception("Teglarni o'zgartirishda xatolik")
        await message.reply(
            f"❌ Teglarni o'zgartirib bo'lmadi (faqat MP3 qo'llab-quvvatlanadi): {e}"
        )
    finally:
        _cleanup(path)


async def handle_link(message: Message, url: str):
    platform = detect_platform(url)
    if not platform:
        return await message.reply(
            "Bu havola qo'llab-quvvatlanmaydi. Instagram, TikTok yoki Pinterest "
            "havolasini yuboring."
        )
    token = uuid.uuid4().hex[:12]
    pending_links[token] = url
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎥 Video", callback_data=f"dl:video:{token}"),
        InlineKeyboardButton(text="🖼 Rasm", callback_data=f"dl:photo:{token}"),
        InlineKeyboardButton(text="🎵 Musiqa", callback_data=f"dl:audio:{token}"),
    ]])
    await message.reply(f"{platform} havolasi aniqlandi. Nimani yuklab beray?", reply_markup=kb)


@dp.callback_query(F.data.startswith("dl:"))
async def on_download_choice(callback: CallbackQuery):
    _, media_type, token = callback.data.split(":", 2)
    url = pending_links.pop(token, None)
    if not url:
        return await callback.answer("Havola muddati tugagan, qaytadan yuboring.", show_alert=True)

    await callback.answer()
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if TIKTOK_PHOTO_RE.search(url):
        return await _handle_tiktok_slideshow(callback, url, media_type)

    status = await callback.message.reply("⏳ Yuklab olinmoqda...")
    path = None
    try:
        if media_type == "audio":
            path, info = await download_media(url, audio_only=True)
            await callback.message.answer_audio(
                FSInputFile(path), title=info.get("title"), performer=info.get("uploader")
            )
        elif media_type == "video":
            path, info = await download_media(url, audio_only=False)
            await callback.message.answer_video(FSInputFile(path))
        else:
            path = await download_photo(url)
            await callback.message.answer_photo(FSInputFile(path))
        await status.delete()
    except Exception as e:
        logger.exception("Yuklab olishda xatolik")
        await status.edit_text(f"❌ Yuklab bo'lmadi: {_friendly_download_error(e)}")
    finally:
        _cleanup(path)


async def _handle_tiktok_slideshow(callback: CallbackQuery, url: str, media_type: str):
    status = await callback.message.reply("⏳ Yuklab olinmoqda...")
    loop = asyncio.get_running_loop()
    paths = []
    try:
        data = await scrape_tiktok_slideshow(url)
        if media_type == "video":
            await status.edit_text(
                "Bu post video emas, rasm+musiqa slayd-shou. \"🖼 Rasm\" yoki "
                "\"🎵 Musiqa\" tugmasidan foydalaning."
            )
            return
        if media_type == "photo":
            if not data["images"]:
                return await status.edit_text("❌ Rasmlar topilmadi.")
            paths = await loop.run_in_executor(None, _download_urls_sync, data["images"][:10], "jpg")
            if len(paths) == 1:
                await callback.message.answer_photo(FSInputFile(paths[0]))
            else:
                media = [InputMediaPhoto(media=FSInputFile(p)) for p in paths]
                await callback.message.answer_media_group(media)
        else:
            if not data["music_url"]:
                return await status.edit_text("❌ Musiqa topilmadi.")
            paths = await loop.run_in_executor(None, _download_urls_sync, [data["music_url"]], "mp3")
            await callback.message.answer_audio(
                FSInputFile(paths[0]), title=data.get("music_title"), performer=data.get("music_author")
            )
        await status.delete()
    except Exception as e:
        logger.exception("TikTok slayd-shouni yuklashda xatolik")
        await status.edit_text(f"❌ Yuklab bo'lmadi: {e}")
    finally:
        _cleanup(*paths)


@dp.message(F.text)
async def handle_text(message: Message):
    text = message.text.strip()
    if text.startswith("/") or text.startswith("."):
        return

    urls = URL_RE.findall(text)
    if urls:
        return await handle_link(message, urls[0])

    if message.chat.type != "private":
        return  # guruhda spam bo'lmasligi uchun erkin matn orqali qidiruv o'chirilgan

    status = await message.reply("🔎 Qidirilmoqda...")
    path = None
    try:
        path, info = await search_music(text)
        await message.reply_audio(
            FSInputFile(path), title=info.get("title"), performer=info.get("uploader")
        )
        await status.delete()
    except Exception as e:
        logger.exception("Musiqa qidirishda xatolik")
        await status.edit_text(f"❌ Topilmadi: {_friendly_download_error(e)}")
    finally:
        _cleanup(path)


fake_server = Flask(__name__)


@fake_server.route("/")
def home():
    return "Music bot ishlab turibdi."


def run_fake_server():
    port = int(os.getenv("PORT", 8080))
    fake_server.run(host="0.0.0.0", port=port)


async def main():
    threading.Thread(target=run_fake_server, daemon=True).start()
    logger.info("Music bot ishga tushdi.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
