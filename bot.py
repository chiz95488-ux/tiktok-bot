import asyncio
import io
import logging
import os
import re
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import requests
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN") 
UA = {"User-Agent": "Mozilla/5.0"}
MAX_VIDEO = 50 * 1024 * 1024  # Bot API upload limit
MAX_PHOTO = 10 * 1024 * 1024  # Bot API photo limit (sendPhoto)

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
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), _Health).serve_forever()
# ---------------------------------------------


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


def classify(d):
    if d.get("images") or d.get("live_images"):
        return "photo"
    if d.get("hdplay") or d.get("play"):
        return "video"
    raise RuntimeError("TikWM không trả về ảnh, live photo hay video cho link này.")


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


def prepare_photo(d):
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có ảnh / live photo nào.")
    chunks = []
    for i in range(0, len(items), 10):
        part = items[i : i + 10]
        loaded = [(kind, retry(lambda u=url: get_bytes(u))) for kind, url in part]
        chunks.append(loaded)
    return chunks


def prepare_video(d, tmp):
    url = d.get("hdplay") or d.get("play")
    if not url:
        raise RuntimeError("Không tìm thấy link tải video.")
    path = os.path.join(tmp, "v.mp4")
    retry(lambda: save(url, path))
    return path


def save(u, path):
    with open(path, "wb") as f:
        f.write(get_bytes(u))


def _media_for(kind, data):
    bio = io.BytesIO(data)
    if kind == "v":
        bio.name = "live.mp4"
        return InputMediaVideo(media=bio, supports_streaming=True)
    bio.name = "photo.jpg"
    return InputMediaPhoto(media=bio)


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Gửi link TikTok. Mình tải được: video thường, bài đăng ảnh, và live photo."
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

    status = await update.message.reply_text("Đang tải... ⏳")
    t0 = time.time()

    try:
        data = await asyncio.to_thread(tikwm, link)
        kind = classify(data)

        if kind == "photo":
            chunks = await asyncio.to_thread(prepare_photo, data)
            for chunk in chunks:
                for k, b in chunk:
                    limit = MAX_VIDEO if k == "v" else MAX_PHOTO
                    if len(b) > limit:
                        await status.edit_text(
                            "Có file vượt giới hạn Telegram (ảnh 10MB / video 50MB)."
                        )
                        return
                if len(chunk) == 1:
                    k, b = chunk[0]
                    if k == "v":
                        await update.message.reply_video(
                            video=io.BytesIO(b), supports_streaming=True
                        )
                    else:
                        await update.message.reply_photo(photo=io.BytesIO(b))
                else:
                    await update.message.reply_media_group(
                        media=[_media_for(k, b) for k, b in chunk]
                    )
        else:
            with tempfile.TemporaryDirectory() as tmp:
                path = await asyncio.to_thread(prepare_video, data, tmp)
                if os.path.getsize(path) > MAX_VIDEO:
                    await status.edit_text("Video lớn hơn 50MB, bot không gửi được.")
                    return
                await status.edit_text("Đang gửi video...")
                with open(path, "rb") as f:
                    await update.message.reply_video(
                        video=f,
                        width=720,
                        height=1280,
                        supports_streaming=True,
                        caption="Video của bạn đây! 🎬",
                    )

        await status.delete()
        await update.message.reply_text("Xong trong %.1f giây." % (time.time() - t0))
    except Exception as e:
        log.exception("download failed")
        try:
            await status.edit_text("Lỗi: " + str(e)[:200])
        except Exception:
            await update.message.reply_text("Lỗi: " + str(e)[:200])


if __name__ == "__main__":
    threading.Thread(target=_run_health, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    log.info("Bot đang chạy...")
    app.run_polling()
