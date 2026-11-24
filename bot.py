import logging
import os
import yt_dlp
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler, filters
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

# Lấy Token từ biến môi trường (bảo mật)
TOKEN = os.environ.get("BOT_TOKEN")

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

# --- Tạo Web Server giả để Render không báo lỗi ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is running!")

def run_health_check():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()
# --------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Chào bạn! Hãy gửi link video TikTok, mình sẽ tải về cho bạn (không dính logo).")

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if "tiktok.com" not in text:
        await update.message.reply_text("Vui lòng gửi link TikTok hợp lệ nhé.")
        return

    msg = await update.message.reply_text("Đang xử lý video, vui lòng chờ chút... ⏳")
    
    # Thư mục lưu tạm trên Render
    ydl_opts = {
        'outtmpl': '/tmp/%(id)s.%(ext)s', 
        'format': 'mp4',
        'quiet': True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(text, download=True)
            filename = ydl.prepare_filename(info)

        await update.message.reply_video(video=open(filename, 'rb'), caption="Video của bạn đây! 🎬")
        await msg.delete()
        os.remove(filename) # Xóa file sau khi gửi để nhẹ máy chủ
    except Exception as e:
        print(f"LỖI: {e}")
        await msg.edit_text("Lỗi không tải được video này. Hãy thử lại sau!")

if __name__ == '__main__':
    # Chạy web server giả ở chế độ nền
    threading.Thread(target=run_health_check, daemon=True).start()
    
    # Chạy bot
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    
    print("Bot đang chạy thành công!")
    app.run_polling()