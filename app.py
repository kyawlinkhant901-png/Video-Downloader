import os
import re
import logging
import threading
import subprocess
import glob
from pathlib import Path

import telebot
from flask import Flask
import yt_dlp

# ─── Logging Setup ────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
PORT = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = "/tmp/downloads"
MAX_SIZE_BYTES = 49 * 1024 * 1024  # 49MB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ─── Flask (Keep-Alive Web Server) ────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "✅ Bot is running!", 200

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

# ─── URL Detection ────────────────────────────────────────────────
URL_PATTERN = re.compile(
    r"https?://(www\.)?(youtube\.com|youtu\.be|tiktok\.com|"
    r"instagram\.com|facebook\.com|fb\.watch|vm\.tiktok\.com)"
    r"[^\s]*"
)

def extract_url(text: str) -> str | None:
    match = URL_PATTERN.search(text)
    return match.group(0) if match else None

# ─── yt-dlp Options ───────────────────────────────────────────────
def build_ydl_opts(output_path: str) -> dict:
    return {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]/best[ext=mp4][height<=720]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        },
        "extractor_args": {
            "youtube": {"player_client": ["android", "web"]},
        },
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

# ─── Compress Video ───────────────────────────────────────────────
def compress_video(input_path: str, output_path: str) -> bool:
    """ffmpeg နဲ့ video ကို 49MB အောက် compress လုပ်မယ်"""
    try:
        # Duration ရှာမယ်
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
             input_path],
            capture_output=True, text=True
        )
        duration = float(probe.stdout.strip() or "0")
        if duration <= 0:
            duration = 300  # default 5 min

        # Target bitrate တွက်မယ် (49MB = 49*8*1024 kbits)
        target_bitrate = int((49 * 8 * 1024) / duration * 0.95)
        video_bitrate = max(200, target_bitrate - 128)  # audio 128k ဖြုတ်ပြီး

        cmd = [
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264",
            "-b:v", f"{video_bitrate}k",
            "-c:a", "aac", "-b:a", "128k",
            "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease",
            "-movflags", "+faststart",
            output_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=300)
        return result.returncode == 0

    except Exception as e:
        logger.error(f"Compress error: {e}")
        return False

# ─── Split Video ──────────────────────────────────────────────────
def split_video(input_path: str, out_prefix: str) -> list[str]:
    """Video ကို 49MB chunk တွေအဖြစ် ခွဲမယ်"""
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of", "default=noprint_wrappers=1:nokey=1",
             input_path],
            capture_output=True, text=True
        )
        duration = float(probe.stdout.strip() or "0")
        file_size = os.path.getsize(input_path)
        
        if duration <= 0:
            return []

        # Chunk ရေ တွက်မယ်
        num_chunks = max(2, int(file_size / MAX_SIZE_BYTES) + 1)
        chunk_duration = duration / num_chunks

        parts = []
        for i in range(num_chunks):
            start = i * chunk_duration
            out_file = f"{out_prefix}_part{i+1}.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-i", input_path,
                "-t", str(chunk_duration),
                "-c", "copy",
                out_file
            ]
            result = subprocess.run(cmd, capture_output=True, timeout=180)
            if result.returncode == 0 and os.path.exists(out_file):
                parts.append(out_file)

        return parts

    except Exception as e:
        logger.error(f"Split error: {e}")
        return []

# ─── Cleanup ──────────────────────────────────────────────────────
def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

# ─── Main Download & Send Logic ───────────────────────────────────
def download_and_send(chat_id: int, url: str, status_msg):
    raw_path = os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.%(ext)s")
    final_mp4 = os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.mp4")
    compressed = os.path.join(DOWNLOAD_DIR, f"{chat_id}_compressed.mp4")

    try:
        # ── Step 1: Download ─────────────────────────────────────
        bot.edit_message_text(
            "📥 <b>Video ဒေါင်းလောဒ်နေပါသည်...</b>\nခဏစောင့်ပါ ⏳",
            chat_id, status_msg.message_id
        )

        ydl_opts = build_ydl_opts(raw_path)
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "Video")[:50]

        # Download ဖြစ်ပြီးသော file ရှာမယ်
        pattern = os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.*")
        files = glob.glob(pattern)
        if not files:
            raise FileNotFoundError("Downloaded file not found")

        downloaded = files[0]

        # mp4 မဟုတ်ရင် rename
        if not downloaded.endswith(".mp4"):
            os.rename(downloaded, final_mp4)
            downloaded = final_mp4

        file_size = os.path.getsize(downloaded)
        logger.info(f"Downloaded: {title}, Size: {file_size/1024/1024:.1f}MB")

        # ── Step 2: Size Check & Process ────────────────────────
        files_to_send = []

        if file_size <= MAX_SIZE_BYTES:
            # Size အဆင်ပြေတယ် - တိုက်ရိုက်ပို့မယ်
            files_to_send = [downloaded]

        else:
            # Compress ကြိုးစားမယ်
            bot.edit_message_text(
                "⚙️ <b>Video ကြီးတယ်၊ Compress လုပ်နေပါသည်...</b>\n"
                "ဒါဟာ အချိန်အနည်းငယ် ကြာနိုင်ပါသည် ⏳",
                chat_id, status_msg.message_id
            )

            success = compress_video(downloaded, compressed)

            if success and os.path.exists(compressed):
                comp_size = os.path.getsize(compressed)
                logger.info(f"Compressed size: {comp_size/1024/1024:.1f}MB")

                if comp_size <= MAX_SIZE_BYTES:
                    files_to_send = [compressed]
                else:
                    # Compress လုပ်ပေမယ့် ဆက်ကြီးနေသေးရင် ခွဲမယ်
                    bot.edit_message_text(
                        "✂️ <b>Video ကို Parts တွေ ခွဲနေပါသည်...</b> ⏳",
                        chat_id, status_msg.message_id
                    )
                    prefix = os.path.join(DOWNLOAD_DIR, f"{chat_id}_split")
                    files_to_send = split_video(compressed, prefix)
            else:
                # Compress မရရင် ခွဲမယ်
                bot.edit_message_text(
                    "✂️ <b>Video ကို Parts တွေ ခွဲနေပါသည်...</b> ⏳",
                    chat_id, status_msg.message_id
                )
                prefix = os.path.join(DOWNLOAD_DIR, f"{chat_id}_split")
                files_to_send = split_video(downloaded, prefix)

        if not files_to_send:
            raise Exception("Video processing failed")

        # ── Step 3: Send ─────────────────────────────────────────
        total = len(files_to_send)
        for idx, fpath in enumerate(files_to_send, 1):
            if total > 1:
                bot.edit_message_text(
                    f"📤 <b>Telegram သို့ Upload နေပါသည်...</b>\n"
                    f"Part {idx}/{total} ⏳",
                    chat_id, status_msg.message_id
                )
                caption = f"🎬 <b>{title}</b>\n📌 Part {idx}/{total}"
            else:
                bot.edit_message_text(
                    "📤 <b>Telegram သို့ Upload နေပါသည်...</b> ⏳",
                    chat_id, status_msg.message_id
                )
                caption = f"🎬 <b>{title}</b>"

            with open(fpath, "rb") as vf:
                bot.send_video(
                    chat_id,
                    vf,
                    caption=caption,
                    supports_streaming=True,
                    timeout=120
                )

        # Success message
        bot.edit_message_text(
            f"✅ <b>အောင်မြင်စွာ ပြီးဆုံးပါပြီ!</b>\n🎬 {title}",
            chat_id, status_msg.message_id
        )

    except yt_dlp.utils.DownloadError as e:
        err_msg = str(e)[:200]
        logger.error(f"yt-dlp error: {err_msg}")
        bot.edit_message_text(
            f"❌ <b>Download မအောင်မြင်ပါ</b>\n\n"
            f"ဖြစ်နိုင်သောအကြောင်းများ:\n"
            f"• Private/Restricted video ဖြစ်နေသည်\n"
            f"• Link မမှန်ကန်ပါ\n"
            f"• Platform က ပိတ်ဆို့ထားသည်\n\n"
            f"<code>{err_msg}</code>",
            chat_id, status_msg.message_id
        )

    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        bot.edit_message_text(
            f"❌ <b>Error ဖြစ်ပေါ်ပါသည်</b>\n<code>{str(e)[:200]}</code>",
            chat_id, status_msg.message_id
        )

    finally:
        # Cleanup temp files
        pattern = os.path.join(DOWNLOAD_DIR, f"{chat_id}_*")
        for f in glob.glob(pattern):
            cleanup(f)

# ─── Telegram Handlers ────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def start_handler(msg):
    bot.send_message(
        msg.chat.id,
        "👋 <b>Video Downloader Bot မှ ကြိုဆိုပါသည်!</b>\n\n"
        "📌 <b>ပံ့ပိုးသော Platform များ:</b>\n"
        "• ▶️ YouTube\n"
        "• 🎵 TikTok\n"
        "• 📸 Instagram\n"
        "• 👥 Facebook\n\n"
        "🔗 <b>အသုံးပြုနည်း:</b>\n"
        "Video link ကို ဒီ chat ထဲ paste လုပ်ပြီး send လုပ်ပါ\n\n"
        "⚡ Bot က အလိုအလျောက် download လုပ်ပေးမည်\n"
        "📦 49MB ကျော်သော video များကို အလိုအလျောက် compress "
        "သို့မဟုတ် parts ခွဲပေးမည်"
    )

@bot.message_handler(func=lambda m: True, content_types=["text"])
def url_handler(msg):
    url = extract_url(msg.text.strip())
    if not url:
        bot.send_message(
            msg.chat.id,
            "⚠️ Valid video link မတွေ့ပါ\n\n"
            "YouTube, TikTok, Instagram, Facebook link ပို့ပါ"
        )
        return

    status = bot.send_message(
        msg.chat.id,
        "🔍 <b>Link စစ်ဆေးနေပါသည်...</b>"
    )

    thread = threading.Thread(
        target=download_and_send,
        args=(msg.chat.id, url, status),
        daemon=True
    )
    thread.start()

# ─── Entry Point ──────────────────────────────────────────────────
def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT, debug=False)

if __name__ == "__main__":
    logger.info("🚀 Bot starting...")

    # Flask ကို background thread မှာ run မယ်
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info(f"🌐 Web server running on port {PORT}")

    # Bot ကို main thread မှာ polling run မယ်
    logger.info("🤖 Bot polling started...")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
