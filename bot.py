import asyncio
import html
import io
import logging
import os
import re
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

CACHE_TTL = 30 * 60
CACHE = {}

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")

# Chuẩn hoá khung hình + tạo timestamp chuẩn 30fps (sửa lỗi clip 0:00)
NORM = (
    "fps=30,"
    "scale=720:1280:force_original_aspect_ratio=decrease,"
    "pad=720:1280:(ow-iw)/2:(oh-ih)/2,"
    "setsar=1,"
    "format=yuv420p,"
    "setpts=N/30/TB"
)


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


def _run_ffmpeg(cmd):
    r = subprocess.run(cmd, capture_output=True, timeout=1200)
    if r.returncode != 0:
        err = r.stderr.decode(errors="ignore").strip()
        log.error("ffmpeg lỗi: %s", err[-500:])
        raise RuntimeError("ffmpeg: " + err[-200:])


def _probe(path):
    """Trả về (số_frame, số_giây)."""
    if imageio_ffmpeg is None:
        return (1, 0)
    try:
        frames, secs = imageio_ffmpeg.count_frames_and_secs(path)
        return (int(frames), int(secs))
    except Exception:
        return (0, 0)


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


# ---------- phân tích dữ liệu ----------
def video_url(d):
    return d.get("hdplay") or d.get("play")


def music_url(d):
    """Chỉ dùng làm tiếng cho video ghép – KHÔNG có nút tải nhạc."""
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


# ---------- ffmpeg: chuyển 1 clip/ảnh thành mp4 chuẩn ----------
def convert_clip(src, dst, kind):
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    if kind == "p":
        cmd += ["-loop", "1", "-t", "3"]        # ảnh tĩnh -> 3 giây
    else:
        cmd += ["-fflags", "+genpts"]           # tạo lại timestamp cho clip lỗi
    cmd += [
        "-i", src,
        "-vf", NORM,
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "26",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        dst,
    ]
    _run_ffmpeg(cmd)
    frames, _ = _probe(dst)
    if frames == 0:
        raise RuntimeError("Clip nguồn không đọc được.")
    return dst


# ---------- gửi ----------
async def send_images(msg, d):
    urls = image_urls(d)
    if not urls:
        raise RuntimeError("Bài đăng không có ảnh.")

    for i, u in enumerate(urls, 1):
        b = await asyncio.to_thread(retry, lambda uu=u: get_bytes(uu))
        if len(b) <= MAX_PHOTO:
            try:
                await msg.reply_photo(
                    photo=io.BytesIO(b), caption=f"Ảnh {i}/{len(urls)} 🖼"
                )
                continue
            except Exception:
                log.warning("Gửi photo lỗi, chuyển sang document", exc_info=True)
        bio = io.BytesIO(b)
        bio.name = f"photo{i}.jpg"
        await msg.reply_document(document=bio)
        await asyncio.sleep(0.2)


async def send_live(msg, d):
    urls = live_urls(d)
    if not urls:
        raise RuntimeError("Bài đăng không có live photo.")

    with tempfile.TemporaryDirectory() as tmp:
        for i, u in enumerate(urls, 1):
            src = os.path.join(tmp, f"src{i}.mp4")
            with open(src, "wb") as f:
                f.write(await asyncio.to_thread(retry, lambda uu=u: get_bytes(uu)))

            dst = os.path.join(tmp, f"live{i}.mp4")
            await asyncio.to_thread(convert_clip, src, dst, "v")

            if os.path.getsize(dst) > MAX_VIDEO:
                raise RuntimeError("Live photo lớn hơn 50MB.")

            with open(dst, "rb") as f:
                await msg.reply_video(
                    video=f,
                    width=720,
                    height=1280,
                    supports_streaming=True,
                    caption=f"Live photo {i}/{len(urls)} 📸",
                )
            await asyncio.sleep(0.3)


async def send_original_video(msg, d):
    url = video_url(d)
    if not url:
        raise RuntimeError("Không tìm thấy link video.")

    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src.mp4")
        with open(src, "wb") as f:
            f.write(await asyncio.to_thread(retry, lambda: get_bytes(url)))

        dst = os.path.join(tmp, "v.mp4")
        await asyncio.to_thread(convert_clip, src, dst, "v")

        if os.path.getsize(dst) > MAX_VIDEO:
            raise RuntimeError("Video lớn hơn 50MB.")

        with open(dst, "rb") as f:
            await msg.reply_video(
                video=f,
                width=720,
                height=1280,
                supports_streaming=True,
                caption="Video 🎬",
            )


# ---------- ghép live photo -> 1 video, giữ tốc độ gốc ----------
def make_merged(d, tmp):
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có nội dung để ghép.")

    paths, kinds = [], []
    for idx, (kind, url) in enumerate(items):
        ext = "mp4" if kind == "v" else "jpg"
        p = os.path.join(tmp, f"seg{idx}.{ext}")
        with open(p, "wb") as f:
            f.write(retry(lambda uu=url: get_bytes(uu)))
        paths.append(p)
        kinds.append(kind)

    n = len(paths)
    silent = os.path.join(tmp, "silent.mp4")

    # BƯỚC 1: nối video (không audio, không -shortest)
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
    for p, kind in zip(paths, kinds):
        if kind == "p":
            cmd += ["-loop", "1", "-t", "3", "-i", p]
        else:
            cmd += ["-fflags", "+genpts", "-i", p]

    parts = [f"[{i}:v]{NORM}[v{i}]" for i in range(n)]
    if n == 1:
        parts.append("[v0]null[v]")
    else:
        parts.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[v]")

    cmd += [
        "-filter_complex", ";".join(parts),
        "-map", "[v]",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "24",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        silent,
    ]
    _run_ffmpeg(cmd)

    frames, secs = _probe(silent)
    log.info("Video ghép: %s frame, %s giây", frames, secs)
    if frames == 0:
        raise RuntimeError("Không tạo được khung hình từ live photo.")

    # BƯỚC 2: lồng nhạc (lặp vô hạn, dừng đúng lúc video hết)
    final = silent
    mu = music_url(d)
    if mu:
        music = os.path.join(tmp, "music.mp3")
        try:
            with open(music, "wb") as f:
                f.write(retry(lambda: get_bytes(mu)))
        except Exception:
            log.warning("Không tải được nhạc nền", exc_info=True)
            music = None

        if music and os.path.getsize(music) > 2048:
            final = os.path.join(tmp, "merged.mp4")
            _run_ffmpeg([
                FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
                "-i", silent,
                "-stream_loop", "-1", "-i", music,
                "-map", "0:v:0", "-map", "1:a:0",
                "-c:v", "copy",
                "-c:a", "aac", "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                final,
            ])
    return final


async def send_merged(msg, d):
    with tempfile.TemporaryDirectory() as tmp:
        path = await asyncio.to_thread(make_merged, d, tmp)
        if os.path.getsize(path) > MAX_VIDEO:
            raise RuntimeError("Video ghép lớn hơn 50MB.")

        with open(path, "rb") as f:
            await msg.reply_video(
                video=f,
                width=720,
                height=1280,
                supports_streaming=True,
                caption="Video 🎬",
            )


async def send_video(msg, d):
    if video_url(d):
        await send_original_video(msg, d)
    elif collect_items(d):
        await send_merged(msg, d)
    else:
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


# ---------- bàn phím: chỉ 3 nút ----------
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
        "Gửi link TikTok, bạn chọn:\n🖼 Ảnh\n📸 Live photo\n🎬 Video\n\n"
        "Muốn 1 video dài bằng bài gốc (nối các live photo) thì bấm 🎬 Video."
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

    status = await q.message.reply_text(f"Đang tải {name}, chờ chút... ⏳")
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


if __name__ == "__main__":
    threading.Thread(target=_run_health, daemon=True).start()

    _ensure_event_loop()

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .connect_timeout(30)
        .read_timeout(120)
        .write_timeout(300)     # upload file lớn không bị Time out
        .pool_timeout(60)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern=r"^dl:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot đang chạy...")
    app.run_polling(drop_pending_updates=True)
