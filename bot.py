import asyncio
import io
import logging
import os
import re
import shutil
import subprocess
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

# ==========================================
ADMIN_CHAT_ID = 5614209455
# ==========================================

UA = {"User-Agent": "Mozilla/5.0"}
MAX_VIDEO = 50 * 1024 * 1024
MAX_PHOTO = 10 * 1024 * 1024

W, H = 720, 1280
FPS = 30
STATIC_DEFAULT = 3.0        # giây cho ảnh tĩnh khi không có nhạc

# "merged" = gộp cả bài thành 1 mp4 (mặc định)
# "album"  = gửi từng slide
MODE = os.environ.get("MODE", "merged").lower()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("bot")


# ---------- semaphore lazy (tránh bind sai event loop) ----------
SEM = None


def sem():
    global SEM
    if SEM is None:
        SEM = asyncio.Semaphore(3)
    return SEM


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


# ---------- ffmpeg ----------
FFMPEG = None


def ffmpeg():
    global FFMPEG
    if FFMPEG:
        return FFMPEG
    FFMPEG = os.environ.get("FFMPEG_BIN") or "ffmpeg"
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
        return FFMPEG
    except Exception:
        pass
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        log.info("dùng ffmpeg bundled: %s", FFMPEG)
        return FFMPEG
    except Exception:
        raise RuntimeError("Không có ffmpeg. Thêm 'imageio-ffmpeg' vào requirements.txt")


def _exec(cmd):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.decode("utf-8", "ignore")[-400:])


def _duration(path):
    p = subprocess.run([ffmpeg(), "-i", path], capture_output=True)
    m = re.search(rb"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr or b"")
    if not m:
        return 0.0
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))


# ---------- tikwm ----------
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


def save(u, path):
    with open(path, "wb") as f:
        f.write(get_bytes(u))


def classify(d):
    if d.get("images") or d.get("live_images"):
        return "photo"
    if d.get("hdplay") or d.get("play"):
        return "video"
    raise RuntimeError("TikWM không trả về ảnh, live photo hay video cho link này.")


def collect_items(d):
    """('p', url) ảnh tĩnh · ('v', url) clip live photo."""
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


# ---------- gộp thành một video ----------
def _fit(src, out, loop_img=False, dur=STATIC_DEFAULT):
    vf = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS}"
    )
    cmd = [ffmpeg(), "-y"]
    if loop_img:
        cmd += ["-loop", "1", "-t", f"{dur:.3f}"]
    cmd += [
        "-i", src, "-vf", vf, "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
        "-pix_fmt", "yuv420p", out,
    ]
    _exec(cmd)


def build_full_video(d, workdir, out_path):
    """Ghép mọi slide + nhạc thành một mp4 liền mạch."""
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có ảnh hay live photo nào.")

    # tải nhạc trước để tính thời lượng mỗi ảnh tĩnh
    audio = None
    music = (d.get("music_info") or {}).get("play")
    if music:
        p = os.path.join(workdir, "audio.mp3")
        try:
            save(music, p)
            audio = p
        except Exception as e:
            log.warning("không tải được nhạc: %s", e)

    total = _duration(audio) if audio else 0.0
    per = total / len(items) if total else STATIC_DEFAULT
    per = max(1.2, min(per, 8.0))

    clips = []
    for i, (kind, url) in enumerate(items):
        src = os.path.join(workdir, f"src_{i:02d}." + ("mp4" if kind == "v" else "jpg"))
        retry(lambda u=url, s=src: save(u, s))
        out = os.path.join(workdir, f"clip_{i:02d}.mp4")
        _fit(src, out, loop_img=(kind == "p"), dur=per)
        clips.append(out)

    listfile = os.path.join(workdir, "list.txt")
    with open(listfile, "w", encoding="utf-8") as f:
        for c in clips:
            f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))

    joined = os.path.join(workdir, "joined.mp4")
    _exec([ffmpeg(), "-y", "-f", "concat", "-safe", "0", "-i", listfile, "-c", "copy", joined])

    if audio:
        # apad: nhạc ngắn hơn video thì bù im lặng, -shortest dừng theo video
        _exec([
            ffmpeg(), "-y", "-i", joined, "-i", audio,
            "-af", "apad",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest", "-movflags", "+faststart", out_path,
        ])
    else:
        shutil.move(joined, out_path)

    # quá 50MB thì nén lại
    if os.path.getsize(out_path) > MAX_VIDEO:
        log.info("vượt 50MB, nén lại")
        small = os.path.join(workdir, "small.mp4")
        _exec([
            ffmpeg(), "-y", "-i", out_path,
            "-vf", "scale=540:960:force_original_aspect_ratio=decrease,"
                   "pad=540:960:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k", small,
        ])
        shutil.move(small, out_path)

    return out_path, len(items), _duration(out_path)


# ---------- chế độ album ----------
def prepare_photo(d):
    items = collect_items(d)
    if not items:
        raise RuntimeError("Bài đăng không có ảnh / live photo nào.")
    chunks = []
    for i in range(0, len(items), 10):
        part = items[i:i + 10]
        chunks.append([(k, retry(lambda u=u: get_bytes(u))) for k, u in part])
    return chunks


def _media_for(kind, data):
    bio = io.BytesIO(data)
    if kind == "v":
        bio.name = "live.mp4"
        return InputMediaVideo(media=bio, supports_streaming=True)
    bio.name = "photo.jpg"
    return InputMediaPhoto(media=bio)


async def send_album(msg, chunks):
    for chunk in chunks:
        for k, b in chunk:
            limit = MAX_VIDEO if k == "v" else MAX_PHOTO
            if len(b) > limit:
                await msg.reply_text("Có file vượt giới hạn Telegram (ảnh 10MB / video 50MB).")
                return
        try:
            if len(chunk) == 1:
                k, b = chunk[0]
                if k == "v":
                    await msg.reply_video(video=io.BytesIO(b), supports_streaming=True)
                else:
                    await msg.reply_photo(photo=io.BytesIO(b))
            else:
                await msg.reply_media_group(media=[_media_for(k, b) for k, b in chunk])
        except Exception as e:
            # album trộn photo+video hay bị 400 → gửi lẻ
            log.warning("album lỗi (%s), gửi lẻ", e)
            for k, b in chunk:
                if k == "v":
                    await msg.reply_video(video=io.BytesIO(b), supports_streaming=True)
                else:
                    await msg.reply_photo(photo=io.BytesIO(b))


# ---------- notify ----------
async def notify_admin(ctx: ContextTypes.DEFAULT_TYPE, user):
    if not user or ADMIN_CHAT_ID == 123456789:
        return
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"👤 Có người dùng tương tác:\n"
                 f"Tên: {user.full_name}\n"
                 f"Username: @{user.username or 'Không có'}\n"
                 f"ID: <code>{user.id}</code>",
            parse_mode="HTML",
        )
    except Exception as e:
        log.error("Không gửi được tin nhắn cho admin: %s", e)


# ---------- handlers ----------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await notify_admin(ctx, update.effective_user)
    await update.message.reply_text(
        "Gửi link TikTok. Mình tải được: video thường, bài đăng ảnh, và live photo.\n"
        f"Chế độ hiện tại: {MODE}"
    )


async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MODE
    arg = (ctx.args[0].lower() if ctx.args else "")
    if arg in ("album", "merged"):
        MODE = arg
        await update.message.reply_text("Chế độ: " + MODE)
    else:
        await update.message.reply_text("Dùng: /mode merged | /mode album")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await notify_admin(ctx, update.effective_user)

    text = update.message.text or ""
    m = re.search(r"https?://\S+", text)
    if not m:
        await update.message.reply_text("Vui lòng gửi link hợp lệ.")
        return
    link = m.group(0)
    if "tiktok.com" not in link:
        await update.message.reply_text("Mình chỉ xử lý link TikTok.")
        return

    async with sem():
        status = await update.message.reply_text("Đang tải... ⏳")
        t0 = time.time()

        try:
            data = await asyncio.to_thread(tikwm, link)
            kind = classify(data)

            if kind == "video":
                tmpdir = tempfile.mkdtemp()
                try:
                    url = data.get("hdplay") or data.get("play")
                    path = os.path.join(tmpdir, "v.mp4")
                    await asyncio.to_thread(lambda: save(url, path))

                    if os.path.getsize(path) > MAX_VIDEO:
                        sd = data.get("play")
                        if sd and sd != url:
                            await status.edit_text("HD quá 50MB, lấy bản SD...")
                            await asyncio.to_thread(lambda: save(sd, path))

                    if os.path.getsize(path) > MAX_VIDEO:
                        await status.edit_text("Video lớn hơn 50MB, bot không gửi được.")
                        return

                    await status.edit_text("Đang gửi video...")
                    with open(path, "rb") as f:
                        await update.message.reply_video(
                            video=f,
                            supports_streaming=True,
                            caption="Video của bạn đây! 🎬",
                        )
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)

            elif MODE == "album":
                chunks = await asyncio.to_thread(prepare_photo, data)
                await send_album(update.message, chunks)

            else:  # merged
                tmpdir = tempfile.mkdtemp()
                try:
                    out = os.path.join(tmpdir, "full.mp4")
                    await status.edit_text("Đang ghép slide... 🎞")
                    out, n, dur = await asyncio.to_thread(build_full_video, data, tmpdir, out)

                    if os.path.getsize(out) > MAX_VIDEO:
                        await status.edit_text("Bản ghép vẫn quá 50MB, bot không gửi được.")
                        return

                    await status.edit_text("Đang gửi video...")
                    title = (data.get("title") or "Live photo")[:180]
                    with open(out, "rb") as f:
                        await update.message.reply_video(
                            video=f,
                            width=W,
                            height=H,
                            duration=max(1, int(round(dur))),
                            supports_streaming=True,
                            caption=f"{title}\n{n} slide · {dur:.1f}s 🎞",
                        )
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)

            await status.delete()
