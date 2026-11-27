import asyncio, io, logging, os, re, shutil, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
import requests
from telegram import InputMediaPhoto, InputMediaVideo, Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

TOKEN = os.environ["BOT_TOKEN"]
ADMIN = int(os.environ.get("ADMIN_CHAT_ID", "0"))
UA = {"User-Agent": "Mozilla/5.0"}
MAXV, MAXP = 50 * 1024 * 1024, 10 * 1024 * 1024
W, H, FPS, STATIC = 720, 1280, 30, 3.0
MODE = os.environ.get("MODE", "merged").lower()
FILL = os.environ.get("FILL", "1") == "1"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("bot")
SEM = None

def sem():
    global SEM
    if SEM is None:
        SEM = asyncio.Semaphore(3)
    return SEM

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
    def log_message(self, *a): pass

def _health():
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", 8080))), _Health).serve_forever()

FF = None
def ff():
    global FF
    if FF: return FF
    FF = os.environ.get("FFMPEG_BIN") or "ffmpeg"
    try:
        subprocess.run([FF, "-version"], capture_output=True, check=True); return FF
    except Exception:
        import imageio_ffmpeg
        FF = imageio_ffmpeg.get_ffmpeg_exe(); log.info("ffmpeg: %s", FF); return FF

def ex(cmd):
    p = subprocess.run(cmd, capture_output=True)
    if p.returncode != 0:
        raise RuntimeError("ffmpeg: " + p.stderr.decode("utf-8", "ignore")[-400:])

def dur(path):
    p = subprocess.run([ff(), "-i", path], capture_output=True)
    m = re.search(rb"Duration: (\d+):(\d+):(\d+\.\d+)", p.stderr or b"")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else 0.0

def fix(u): return "https://www.tikwm.com" + u if u.startswith("/") else u

def resolve(url):
    try:
        r = requests.get(url, headers=UA, timeout=20, allow_redirects=True)
        u = r.url.split("?")[0]
        if "tiktok.com" in u and ("/video/" in u or "/photo/" in u):
            return u
    except Exception:
        pass
    return url

def tikwm(url):
    last = "tikwm error"
    for u in dict.fromkeys([resolve(url), url]):
        try:
            r = requests.post("https://www.tikwm.com/api/", data={"url": u, "hd": 1}, headers=UA, timeout=30)
            j = r.json()
            if j.get("code") != 0:
                raise RuntimeError(j.get("msg", "tikwm error"))
            return j["data"]
        except Exception as e:
            last = str(e)
    raise RuntimeError(last)

def getb(u):
    r = requests.get(fix(u), headers=UA, timeout=90); r.raise_for_status(); return r.content

def save(u, path):
    with open(path, "wb") as f: f.write(getb(u))

def retry(fn, n=4, d=2):
    for i in range(n):
        try:
            return fn()
        except Exception:
            if i == n - 1: raise
            time.sleep(d)

def kind_of(d):
    if d.get("images") or d.get("live_images"): return "photo"
    if d.get("hdplay") or d.get("play"): return "video"
    raise RuntimeError("TikWM không trả ảnh/live photo/video cho link này.")

def items(d):
    imgs, live = d.get("images") or [], d.get("live_images") or []
    out = []
    for i, img in enumerate(imgs):
        lv = live[i] if i < len(live) and isinstance(live[i], str) and live[i] else None
        if lv: out.append(("v", lv))
        elif img: out.append(("p", img))
    if not out and live:
        out = [("v", x) for x in live if isinstance(x, str) and x]
    return out

def fit(src, out, loop=False, t=STATIC):
    vf = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
          f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,fps={FPS}")
    cmd = [ff(), "-y"] + (["-loop", "1", "-t", f"{t:.3f}"] if loop else [])
    ex(cmd + ["-i", src, "-vf", vf, "-an", "-c:v", "libx264",
              "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", out])

def build(d, wd, out):
    it = items(d)
    if not it: raise RuntimeError("Không có ảnh/live photo.")
    audio, m = None, (d.get("music_info") or {}).get("play")
    if m:
        p = os.path.join(wd, "a.mp3")
        try: save(m, p); audio = p
        except Exception as e: log.warning("nhạc lỗi: %s", e)
    total = dur(audio) if audio else 0.0
    per = max(1.2, min(total / len(it), 8.0)) if total else STATIC
    clips = []
    for i, (k, u) in enumerate(it):
        src = os.path.join(wd, f"s{i:02d}." + ("mp4" if k == "v" else "jpg"))
        retry(lambda u=u, s=src: save(u, s))
        o = os.path.join(wd, f"c{i:02d}.mp4")
        fit(src, o, loop=(k == "p"), t=per)
        clips.append(o)

    if FILL and audio and total > 0:
        have = sum(dur(c) for c in clips)
        if have > 0.1 and have < total - 0.3:
            reps = min(int(total / have) + 1, 40)
            base = list(clips)
            for r in range(1, reps):
                for c in base:
                    n = os.path.join(wd, "r%d_%s" % (r, os.path.basename(c)))
                    shutil.copy(c, n)
                    clips.append(n)
            log.info("lặp %dx → %.1fs", reps, have * reps)

    lst = os.path.join(wd, "l.txt")
    with open(lst, "w", encoding="utf-8") as f:
        for c in clips: f.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
    joined = os.path.join(wd, "j.mp4")
    ex([ff(), "-y", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
    if audio:
        ex([ff(), "-y", "-i", joined, "-i", audio, "-af", "apad", "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", out])
    else:
        shutil.move(joined, out)
    if os.path.getsize(out) > MAXV:
        small = os.path.join(wd, "sm.mp4")
        ex([ff(), "-y", "-i", out, "-vf",
            "scale=540:960:force_original_aspect_ratio=decrease,"
            "pad=540:960:(ow-iw)/2:(oh-ih)/2:color=black",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k", small])
        shutil.move(small, out)
    return out, len(clips), dur(out)

def media(k, b):
    bio = io.BytesIO(b); bio.name = "live.mp4" if k == "v" else "photo.jpg"
    return InputMediaVideo(media=bio, supports_streaming=True) if k == "v" else InputMediaPhoto(media=bio)

async def album(msg, d):
    it = items(d)
    if not it: raise RuntimeError("Không có ảnh/live photo.")
    for i in range(0, len(it), 10):
        part = it[i:i + 10]
        loaded = [(k, retry(lambda u=u: getb(u))) for k, u in part]
        for k, b in loaded:
            if len(b) > (MAXV if k == "v" else MAXP):
                await msg.reply_text("File vượt giới hạn Telegram."); return
        try:
            if len(loaded) == 1:
                k, b = loaded[0]
                await (msg.reply_video(video=io.BytesIO(b), supports_streaming=True) if k == "v"
                       else msg.reply_photo(photo=io.BytesIO(b)))
            else:
                await msg.reply_media_group(media=[media(k, b) for k, b in loaded])
        except Exception as e:
            log.warning("album lỗi (%s), gửi lẻ", e)
            for k, b in loaded:
                await (msg.reply_video(video=io.BytesIO(b), supports_streaming=True) if k == "v"
                       else msg.reply_photo(photo=io.BytesIO(b)))

async def notify(ctx, u):
    if not u or not ADMIN: return
    try:
        await ctx.bot.send_message(ADMIN,
            f"👤 Tương tác:\nTên: {u.full_name}\n"
            f"Username: @{u.username or 'Không có'}\nID: <code>{u.id}</code>",
            parse_mode="HTML")
    except Exception as e:
        log.error("admin: %s", e)

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await notify(ctx, update.effective_user)
    await update.message.reply_text(f"Gửi link TikTok.\nChế độ: {MODE} · FILL: {FILL}")

async def mode_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    global MODE, FILL
    a = (ctx.args[0].lower() if ctx.args else "")
    if a in ("album", "merged"):
        MODE = a
    elif a in ("fill", "nofill"):
        FILL = (a == "fill")
    else:
        await update.message.reply_text("Dùng: /mode merged | album | fill | nofill")
        return
    await update.message.reply_text(f"MODE={MODE} FILL={FILL}")

async def raw_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = re.search(r"https?://\S+", update.message.text or "")
    if not m:
        await update.message.reply_text("Dùng: /raw <link>")
        return
    try:
        d = await asyncio.to_thread(tikwm, m.group(0))
    except Exception as e:
        await update.message.reply_text("Lỗi: " + str(e)[:150])
        return
    lines = []
    for k, v in d.items():
        if isinstance(v, list):
            lines.append(f"{k}: list[{len(v)}]")
        elif isinstance(v, (int, float, str)):
            lines.append(f"{k}: {v}")
        else:
            lines.append(f"{k}: {type(v).__name__}")
    await update.message.reply_text(("RAW\n" + "\n".join(lines))[:3900])

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await notify(ctx, update.effective_user)
    m = re.search(r"https?://\S+", update.message.text or "")
    if not m: await update.message.reply_text("Gửi link hợp lệ."); return
    link = m.group(0)
    if "tiktok.com" not in link: await update.message.reply_text("Chỉ xử lý link TikTok."); return

    async with sem():
        st = await update.message.reply_text("Đang tải... ⏳")
        t0 = time.time()
        try:
            d = await asyncio.to_thread(tikwm, link)
            k = kind_of(d)
            if k == "video":
                wd = tempfile.mkdtemp()
                try:
                    url = d.get("hdplay") or d.get("play")
                    p = os.path.join(wd, "v.mp4")
                    await asyncio.to_thread(lambda: save(url, p))
                    if os.path.getsize(p) > MAXV and d.get("play") and d["play"] != url:
                        await st.edit_text("HD quá 50MB, lấy bản SD...")
                        await asyncio.to_thread(lambda: save(d["play"], p))
                    if os.path.getsize(p) > MAXV:
                        await st.edit_text("Video > 50MB."); return
                    await st.edit_text("Đang gửi...")
                    with open(p, "rb") as f:
                        await update.message.reply_video(video=f, supports_streaming=True,
                                                         caption="Video của bạn đây! 🎬")
                finally:
                    shutil.rmtree(wd, ignore_errors=True)
            elif MODE == "album":
                await album(update.message, d)
            else:
                wd = tempfile.mkdtemp()
                try:
                    out = os.path.join(wd, "full.mp4")
                    await st.edit_text("Đang ghép slide... 🎞")
                    out, n, du = await asyncio.to_thread(build, d, wd, out)
                    if os.path.getsize(out) > MAXV:
                        await st.edit_text("Bản ghép > 50MB."); return
                    await st.edit_text("Đang gửi...")
                    title = (d.get("title") or "Live photo")[:180]
                    with open(out, "rb") as f:
                        await update.message.reply_video(
                            video=f, width=W, height=H, duration=max(1, int(round(du))),
                            supports_streaming=True,
                            caption=f"{title}\n{n} clip · {du:.1f}s 🎞")
                finally:
                    shutil.rmtree(wd, ignore_errors=True)
            await st.delete()
            await update.message.reply_text("Xong trong %.1f giây." % (time.time() - t0))
        except Exception as e:
            log.exception("fail")
            try: await st.edit_text("Lỗi: " + str(e)[:200])
            except Exception: await update.message.reply_text("Lỗi: " + str(e)[:200])

if __name__ == "__main__":
    threading.Thread(target=_health, daemon=True).start()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("raw", raw_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    log.info("chạy — mode=%s fill=%s", MODE, FILL)
    app.run_polling()
