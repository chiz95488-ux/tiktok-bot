import asyncio
import html
import io
import logging
import os
import re
import shutil
import subprocess
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

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    imageio_ffmpeg = None
    FFMPEG = "ffmpeg"

# ================= CẤU HÌNH =================
TOKEN = os.environ.get("BOT_TOKEN")

UA = {"User-Agent": "Mozilla/5.0"}
MAX_VIDEO = 50 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024

W, H, FPS = 540, 960, 30      # nhỏ cho nhẹ CPU của Render Free

CACHE_TTL = 30 * 60
CACHE = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# ---------- health check ----------
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
        r = requests.get(url, headers=UA, timeout=25, allow_redirects=True)
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
        timeout=40,
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
    r = requests.get(_fix(u), headers=UA, timeout=120)
    r.raise_for_status()
    return r.content


# ---------- phân tích dữ liệu ----------
def video_url(d):
    return d.get("hdplay") or d.get("play")


def music_url(d):
    """Chỉ dùng làm TIẾNG cho video ghép."""
    mi = d.get("music_info") or {}
    return d.get("music") or mi.get("play") or mi.get("url")


def image_urls(d):
    return [u for u in (d.get("images") or []) if isinstance(u, str) and u]


def live_urls(d):
    return [u for u in (d.get("live_images") or []) if isinstance(u, str) and u]


def collect_items(d):
    imgs = image_urls(d)
    live = live_urls(d)
    items = []
    for i, img in enumerate(imgs):
        lv = live[i] if i < len(live) else None
        if lv:
            items.append(("v", lv))
        elif img:
            items.append(("p", img))
    if not items and live:
        items = [("v", x) for x in live]
    return items


def options_for(d):
    opts = []
    if image_urls(d):
        opts.append("image")
    if live_urls(d):
        opts.append("live")
    if video_url(d) or collect_items(d):
        opts.append("video")
    return opts


# ---------- tải & gửi ----------
def _download_urls(urls):
    return [retry(lambda u=u: get_bytes(u)) for u in urls]


async def send_images(msg, d):
    urls = image_urls(d)
    if not urls:
        raise RuntimeError("Bài đăng không có ảnh tĩnh.")

    for i in range(0, len(urls), 10):
        part = urls[i:i + 10]
        blobs = await asyncio.to_thread(_download_urls, part)

        small = [b for b in blobs if len(b) <= MAX_PHOTO]
        big = [b for b in blobs if len(b) > MAX_PHOTO]

        if len(small) == 1:
            await msg.reply_photo(photo=io.BytesIO(small[0]), caption="Ảnh 🖼")
        elif small:
            media = []
            for b in small:
                bio = io.BytesIO(b)
                bio.name = "photo.jpg"
                media.append(InputMediaPhoto(media=bio))
            await msg.reply_media_group(media=media)

        for b in big:
            bio = io.BytesIO(b)
            bio.name = "photo.jpg"
            await msg.reply_document(document=bio)


async def send_live(msg, d):
    urls = live_urls(d)
    if not urls:
        raise RuntimeError("Bài đăng không có live photo.")

    for i in range(0, len(urls), 10):
        part = urls[i:i + 10]
        blobs = await asyncio.to_thread(_download_urls, part)

        for b in blobs:
            if len(b) > MAX_VIDEO:
                raise RuntimeError("Live photo lớn hơn 50MB.")

        if len(blobs) == 1:
            bio = io.BytesIO(blobs[0])
            bio.name = "live.mp4"
            await msg.reply_video(
                video=bio, supports_streaming=True, caption="Live photo 📸"
            )
        else:
            media = []
            for b in blobs:
                bio = io.BytesIO(b)
                bio.name = "live.mp4"
                media.append(InputMediaVideo(media=bio, supports_streaming=True))
            await msg.reply_media_group(media=media)


async def send_original_video(msg, d):
    url = video_url(d)
    if not url:
        raise RuntimeError("Không tìm thấy link video.")

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
                caption="Video 🎬",
            )


# ---------- FFMPEG ----------
def _ff(cmd, step):
    r = subprocess.run(cmd, capture_output=True, timeout=1500)
    if r.returncode != 0:
        err = r.stderr.decode(errors="ignore").strip()[-400:]
        raise RuntimeError(f"{step}: {err or 'ffmpeg lỗi'}")


def _verify(path):
    if (not os.path.exists(path)) or os.path.getsize(path) < 2048:
        raise RuntimeError("File video ghép bị rỗng.")
    if imageio_ffmpeg is not None:
        try:
            n, _ = imageio_ffmpeg.count_frames_and_secs(path)
            if n <= 0:
                raise RuntimeError("Video ghép không có khung hình.")
        except RuntimeError:
            raise
        except Exception:
            pass


def make_merged(d, tmp):
    """
    1) Chuẩn hoá từng khung (ảnh tĩnh 3s / live photo nguyên tốc độ) về cùng 540x960 30fps
    2) Nối bằng concat demuxer (copy, không re-encode)
    3) Ghép nhạc nền (lặp cho đủ dài, dừng khi video hết)
    """
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có nội dung để ghép video.")

    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,"
        f"setsar=1,fps={FPS},format=yuv420p"
    )

    seg_paths = []
    for idx, (kind, url) in enumerate(items):
        raw = os.path.join(tmp, f"raw{idx}")
        with open(raw, "wb") as f:
            f.write(retry(lambda uu=url: get_bytes(uu)))

        out = os.path.join(tmp, f"norm{idx}.mp4")
        cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        if kind == "p":
            cmd += ["-loop", "1", "-t", "3", "-i", raw]
        else:
            cmd += ["-i", raw]
        cmd += [
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "26",
            "-an",
            "-movflags", "+faststart",
            out,
        ]
        _ff(cmd, f"chuẩn hoá khung {idx + 1}")
        seg_paths.append(out)

    list_file = os.path.join(tmp, "list.txt")
    with open(list_file, "w", encoding="utf-8") as f:
        for p in seg_paths:
            f.write("file '%s'\n" % p.replace("'", "'\\''"))

    joined = os.path.join(tmp, "joined.mp4")
    _ff(
        [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
         "-f", "concat", "-safe", "0", "-i", list_file,
         "-c", "copy", "-movflags", "+faststart", joined],
        "nối clip",
    )

    final = os.path.join(tmp, "merged.mp4")
    music = os.path.join(tmp, "music.mp3")
    has_music = False
    mu = music_url(d)
    if mu:
        try:
            with open(music, "wb") as f:
                f.write(retry(lambda: get_bytes(mu)))
            has_music = os.path.getsize(music) > 2048
        except Exception:
            log.warning("Không tải được nhạc nền", exc_info=True)

    if has_music:
        _ff(
            [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
             "-i", joined,
             "-stream_loop", "-1", "-i", music,
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "copy",
             "-c:a", "aac", "-b:a", "128k",
             "-shortest",
             "-movflags", "+faststart", final],
            "ghép nhạc",
        )
    else:
        shutil.copyfile(joined, final)

    _verify(final)
    return final


async def send_merged(msg, d):
    with tempfile.TemporaryDirectory() as tmp:
        path = await asyncio.to_thread(make_merged, d, tmp)
        if os.path.getsize(path) > MAX_VIDEO:
            raise RuntimeError("Video ghép lớn hơn 50MB.")

        with open(path, "rb") as f:
            await msg.reply_video(
                video=f,
                width=W,
                height=H,
                supports_streaming=True,
                caption="Video 🎬",
            )


async def send_video(msg, d):
    if video_url(d):
        await send_original_video(msg, d)
        return

    if collect_items(d):
        try:
            await send_merged(msg, d)
        except Exception as e:
            log.exception("Ghép video lỗi, gửi từng phần")
            await msg.reply_text("Ghép video lỗi (" + str(e)[:120] + "), gửi từng phần:")
            if live_urls(d):
                await send_live(msg, d)
            else:
                await send_images(msg, d)
        return

    raise RuntimeError("Bài đăng không có video.")


async def run_choice(msg, d, what, errors):
    if what in ("image", "all") and image_urls(d):
        try:
            await send_images(msg, d)
        except Exception as e:
            log.exception("image failed")
            errors.append("ảnh: " + str(e)[:80])

    if what in ("live", "all") and live_urls(d):
        try:
            await send_live(msg, d)
        except Exception as e:
            log.exception("live failed")
            errors.append("live photo: " + str(e)[:80])

    if what in ("video", "all") and (video_url(d) or collect_items(d)):
        try:
            await send_video(msg, d)
        except Exception as e:
            log.exception("video failed")
            errors.append("video: " + str(e)[:80])


# ---------- bàn phím ----------
LABEL = {
    "image": "🖼 Ảnh",
    "live": "📸 Live photo",
    "video": "🎬 Video",
}


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
        "Gửi link TikTok, bạn chọn:\n🖼 Ảnh\n📸 Live photo\n🎬 Video"
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
            raise RuntimeError("Bài đăng này không có ảnh/live photo/video.")

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
    name = {"image": "ảnh 🖼", "live": "live photo 📸", "video": "video 🎬"}.get(
        what, "tất cả 📦"
    )

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


# ---------- khởi động ----------
def _ensure_event_loop():
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
    except Exception:
        asyncio.set_event_loop(asyncio.new_event_loop())


def _build_app():
    builder = ApplicationBuilder().token(TOKEN)

    # tăng timeout để upload nhiều file không bị "Time out"
    try:
        from telegram.request import HTTPXRequest

        req = HTTPXRequest(
            connection_pool_size=8,
            connect_timeout=30.0,
            read_timeout=180.0,
            write_timeout=600.0,
            pool_timeout=60.0,
        )
        return builder.request(req).get_updates_request(req).build()
    except Exception:
        log.warning("Không đặt được timeout tuỳ chỉnh, dùng mặc định", exc_info=True)
        return builder.build()


if __name__ == "__main__":
    threading.Thread(target=_run_health, daemon=True).start()

    _ensure_event_loop()

    app = _build_app()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)
