import asyncio
import html
import io
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ================= CẤU HÌNH =================
TOKEN = os.environ.get("BOT_TOKEN")

UA = {"User-Agent": "Mozilla/5.0"}
MAX_VIDEO = 50 * 1024 * 1024   # giới hạn Bot API
MAX_AUDIO = 50 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024

CACHE_TTL = 30 * 60            # link dùng lại được trong 30 phút
CACHE = {}                     # token -> {data, ts}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# ---------- health check cho Render ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *a):
        pass


def _run_health():
    port = int(os.environ.get("PORT", 8080))
    HTTPServer(("0.0.0.0", port), _Health).serve_forever()


# ---------- tiện ích ----------
def retry(fn, times=5, delay=3):
    for i in range(times):
        try:
            return fn()
        except Exception:
            if i == times - 1:
                raise
            time.sleep(delay)


def _fix(u):
    return "https://www.tikwm.com" + u if u.startswith("/") else u


def _resolve(url):
    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        u = r.url.split("?")[0]
        if "tiktok.com" in u and ("/video/" in u or "/photo/" in u):
            return u
    except Exception:
        pass
    return url


def _tikwm_once(url):
    r = requests.post(
        "https://www.tikwm.com/api/",
        data={"url": url, "hd": 1},
        headers=UA,
        timeout=30,
    )
    j = r.json()
    if j.get("code") != 0:
        raise RuntimeError(j.get("msg", "tikwm error"))
    return j["data"]


def tikwm(url):
    last = "tikwm error"
    for u in dict.fromkeys([_resolve(url), url]):
        try:
            return _tikwm_once(u)
        except Exception as e:
            last = str(e)
    raise RuntimeError(last)


def get_bytes(u):
    r = requests.get(_fix(u), headers=UA, timeout=90)
    r.raise_for_status()
    return r.content


# ---------- phân tích dữ liệu tikwm ----------
def video_url(d):
    return d.get("hdplay") or d.get("play")


def music_url(d):
    mi = d.get("music_info") or {}
    return d.get("music") or mi.get("play") or mi.get("url")


def collect_items(d):
    """('p', url) ảnh tĩnh, ('v', url) live photo."""
    imgs = d.get("images") or []
    live = d.get("live_images") or []
    items = []
    for i, img in enumerate(imgs):
        lv = live[i] if i < len(live) and isinstance(live[i], str) and live[i] else None
        if lv:
            items.append(("v", lv))
        elif img:
            items.append(("p", img))
    if not items and live:
        items = [("v", x) for x in live if isinstance(x, str) and x]
    return items


def options_for(d):
    opts = []
    if music_url(d):
        opts.append("music")
    if collect_items(d):
        opts.append("photo")
    if video_url(d):
        opts.append("video")
    return opts


# ---------- tải & gửi từng loại ----------
def _download_part(part):
    out = []
    for kind, url in part:
        out.append((kind, retry(lambda u=url: get_bytes(u))))
    return out


def _media_for(kind, data):
    bio = io.BytesIO(data)
    if kind == "v":
        bio.name = "live.mp4"
        return InputMediaVideo(media=bio, supports_streaming=True)
    bio.name = "photo.jpg"
    return InputMediaPhoto(media=bio)


def _author_name(d):
    mi = d.get("music_info") or {}
    if mi.get("author"):
        return str(mi["author"])
    a = d.get("author")
    if isinstance(a, dict):
        return str(a.get("nickname") or "TikTok")
    return str(a or "TikTok")


async def send_music(msg, d):
    u = music_url(d)
    if not u:
        raise RuntimeError("Không tìm thấy link nhạc.")

    b = await asyncio.to_thread(get_bytes, u)
    if len(b) > MAX_AUDIO:
        raise RuntimeError("File nhạc lớn hơn 50MB.")

    mi = d.get("music_info") or {}
    title = (mi.get("title") or d.get("title") or "Nhạc TikTok")[:60]
    bio = io.BytesIO(b)
    bio.name = "music.mp3"
    await msg.reply_audio(
        audio=bio,
        title=title,
        performer=_author_name(d)[:60],
        caption="🎵 Nhạc của bạn đây!",
    )


async def send_photos(msg, d):
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có ảnh / live photo nào.")

    for i in range(0, len(items), 10):
        part = items[i:i + 10]
        blobs = await asyncio.to_thread(_download_part, part)

        for k, b in blobs:
            limit = MAX_VIDEO if k == "v" else MAX_PHOTO
            if len(b) > limit:
                raise RuntimeError("Có file vượt giới hạn Telegram (ảnh 10MB / video 50MB).")

        if len(blobs) == 1:
            k, b = blobs[0]
            if k == "v":
                bio = io.BytesIO(b)
                bio.name = "live.mp4"
                await msg.reply_video(
                    video=bio, supports_streaming=True, caption="Live photo 📸"
                )
            else:
                await msg.reply_photo(photo=io.BytesIO(b), caption="Ảnh của bạn đây! 📸")
        else:
            await msg.reply_media_group(media=[_media_for(k, b) for k, b in blobs])


async def send_video(msg, d):
    url = video_url(d)
    if not url:
        raise RuntimeError("Không tìm thấy link tải video.")

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "v.mp4")

        def _dl():
            with open(path, "wb") as f:
                f.write(retry(lambda: get_bytes(url)))

        await asyncio.to_thread(_dl)
        if os.path.getsize(path) > MAX_VIDEO:
            raise RuntimeError("Video lớn hơn 50MB, bot không gửi được.")

        with open(path, "rb") as f:
            await msg.reply_video(
                video=f,
                width=720,
                height=1280,
                supports_streaming=True,
                caption="Video của bạn đây! 🎬",
            )


async def run_choice(msg, d, what, errors):
    if what in ("music", "all"):
        try:
            await send_music(msg, d)
        except Exception as e:
            log.exception("music failed")
            errors.append("nhạc: " + str(e)[:80])

    if what in ("photo", "all") and collect_items(d):
        try:
            await send_photos(msg, d)
        except Exception as e:
            log.exception("photo failed")
            errors.append("ảnh: " + str(e)[:80])

    if what in ("video", "all") and video_url(d):
        try:
            await send_video(msg, d)
        except Exception as e:
            log.exception("video failed")
            errors.append("video: " + str(e)[:80])


# ---------- bàn phím lựa chọn ----------
LABEL = {"music": "🎵 Nhạc", "photo": "🖼 Ảnh", "video": "🎬 Video"}


def build_keyboard(token, opts):
    rows = [[InlineKeyboardButton(LABEL[o], callback_data=f"dl:{token}:{o}")] for o in opts]
    if len(opts) > 1:
        rows.append([InlineKeyboardButton("📦 Tải tất cả", callback_data=f"dl:{token}:all")])
    return InlineKeyboardMarkup(rows)


def _clean_cache():
    now = time.time()
    for k in [k for k, v in CACHE.items() if now - v["ts"] > CACHE_TTL]:
        CACHE.pop(k, None)


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Gửi link TikTok, mình sẽ cho bạn chọn tải: 🎵 nhạc, 🖼 ảnh / live photo, 🎬 video."
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("Vui lòng gửi link hợp lệ.")
        return

    link = m.group(0)
    if "tiktok.com" not in link:
        await update.message.reply_text("Mình chỉ xử lý link TikTok.")
        return

    status = await update.message.reply_text("Đang lấy dữ liệu... ⏳")

    try:
        data = await asyncio.to_thread(tikwm, link)
        opts = options_for(data)
        if not opts:
            raise RuntimeError("Bài đăng này không có nhạc/ảnh/video để tải.")

        _clean_cache()
        token = uuid.uuid4().hex[:10]
        CACHE[token] = {"data": data, "ts": time.time()}

        title = html.escape((data.get("title") or "").strip())[:200]
        head = f"<b>{title}</b>\n\n" if title else ""
        await status.edit_text(
            head + "Chọn thứ bạn muốn tải:",
            parse_mode="HTML",
            reply_markup=build_keyboard(token, opts),
        )
    except Exception as e:
        log.exception("prepare failed")
        try:
            await status.edit_text("Lỗi: " + str(e)[:200])
        except Exception:
            await update.message.reply_text("Lỗi: " + str(e)[:200])


async def handle_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not q or not q.message:
        return
    await q.answer()

    try:
        _, token, what = (q.data or "").split(":", 2)
    except ValueError:
        await q.edit_message_text("Nút không hợp lệ.")
        return

    entry = CACHE.get(token)
    if not entry:
        await q.edit_message_text("Phiên đã hết hạn, hãy gửi lại link TikTok.")
        return

    data = entry["data"]
    name = {"music": "nhạc 🎵", "photo": "ảnh 🖼", "video": "video 🎬"}.get(what, "tất cả 📦")

    status = await q.message.reply_text(f"Đang tải {name}... ⏳")
    t0 = time.time()
    errors = []

    await run_choice(q.message, data, what, errors)

    try:
        if errors:
            await status.edit_text("Lỗi: " + " | ".join(errors)[:300])
        else:
            await status.delete()
            await q.message.reply_text("Xong trong %.1f giây." % (time.time() - t0))
    except Exception:
        log.exception("edit status failed")


if __name__ == "__main__":
    threading.Thread(target=_run_health, daemon=True).start()

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot đang chạy...")
    app.run_polling()
