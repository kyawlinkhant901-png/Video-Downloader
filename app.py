import os
import re
import logging
import threading
import subprocess
import glob

import telebot
from flask import Flask
import yt_dlp

# ─── Logging ──────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ─── Config ───────────────────────────────────────────────────────
BOT_TOKEN   = os.environ.get("BOT_TOKEN", "")
PORT        = int(os.environ.get("PORT", 8080))
DOWNLOAD_DIR = "/tmp/vbot"
MAX_BYTES   = 49 * 1024 * 1024   # 49 MB

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# ─── Flask keep-alive ─────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def index():
    return "✅ Bot is alive!", 200

@app.route("/health")
def health():
    return {"status": "ok"}, 200

# ─── URL regex ────────────────────────────────────────────────────
URL_RE = re.compile(
    r"https?://(www\.)?"
    r"(youtube\.com|youtu\.be|tiktok\.com|vm\.tiktok\.com"
    r"|instagram\.com|facebook\.com|fb\.watch)"
    r"[^\s]*",
    re.IGNORECASE
)

def find_url(text: str):
    m = URL_RE.search(text)
    return m.group(0) if m else None

# ─── yt-dlp options ───────────────────────────────────────────────
def ydl_opts(out_tmpl: str) -> dict:
    return {
        "outtmpl": out_tmpl,
        "format": (
            "bestvideo[ext=mp4][height<=720]+bestaudio[ext=m4a]"
            "/best[ext=mp4][height<=720]/best"
        ),
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 5,
        "fragment_retries": 5,
        "concurrent_fragment_downloads": 4,
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
        "postprocessors": [{
            "key": "FFmpegVideoConvertor",
            "preferedformat": "mp4",
        }],
    }

# ─── ffprobe helper ───────────────────────────────────────────────
def get_duration(path: str) -> float:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration", "-of",
             "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30
        )
        return float(r.stdout.strip() or 0)
    except Exception:
        return 0.0

# ─── Compress ─────────────────────────────────────────────────────
def compress(src: str, dst: str) -> bool:
    duration = get_duration(src)
    if duration <= 0:
        duration = 300
    target_kbps = max(200, int((49 * 8 * 1024) / duration * 0.93) - 128)
    cmd = [
        "ffmpeg", "-y", "-i", src,
        "-c:v", "libx264", "-b:v", f"{target_kbps}k",
        "-c:a", "aac", "-b:a", "128k",
        "-vf", "scale='min(1280,iw)':'min(720,ih)'"
              ":force_original_aspect_ratio=decrease",
        "-movflags", "+faststart",
        dst
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=360)
        return r.returncode == 0
    except Exception as e:
        logger.error(f"compress error: {e}")
        return False

# ─── Split ────────────────────────────────────────────────────────
def split_parts(src: str, prefix: str) -> list:
    duration = get_duration(src)
    size     = os.path.getsize(src)
    if duration <= 0:
        return []
    n     = max(2, int(size / MAX_BYTES) + 1)
    chunk = duration / n
    parts = []
    for i in range(n):
        out = f"{prefix}_part{i+1}.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(i * chunk),
            "-i", src,
            "-t", str(chunk),
            "-c", "copy", out
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=180)
        if r.returncode == 0 and os.path.exists(out):
            parts.append(out)
    return parts

# ─── Cleanup ──────────────────────────────────────────────────────
def cleanup(chat_id):
    for f in glob.glob(os.path.join(DOWNLOAD_DIR, f"{chat_id}_*")):
        try:
            os.remove(f)
        except Exception:
            pass

# ─── Core download+send ───────────────────────────────────────────
def process(chat_id: int, url: str, status_id: int):
    raw_tmpl = os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.%(ext)s")
    raw_mp4  = os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.mp4")
    comp_mp4 = os.path.join(DOWNLOAD_DIR, f"{chat_id}_comp.mp4")

    def edit(text):
        try:
            bot.edit_message_text(text, chat_id, status_id)
        except Exception:
            pass

    try:
        # ── Download ──────────────────────────────────────────────
        edit("📥 <b>Video ဒေါင်းလောဒ်နေပါသည်...</b>\nခဏစောင့်ပါ ⏳")

        with yt_dlp.YoutubeDL(ydl_opts(raw_tmpl)) as ydl:
            info  = ydl.extract_info(url, download=True)
            title = (info.get("title") or "Video")[:60]

        # downloaded file ရှာမယ်
        candidates = glob.glob(
            os.path.join(DOWNLOAD_DIR, f"{chat_id}_raw.*")
        )
        if not candidates:
            raise FileNotFoundError("Downloaded file မတွေ့ပါ")

        src = candidates[0]
        if not src.endswith(".mp4"):
            os.rename(src, raw_mp4)
            src = raw_mp4

        size = os.path.getsize(src)
        logger.info(f"Downloaded '{title}' — {size/1024/1024:.1f} MB")

        # ── Size check ────────────────────────────────────────────
        to_send = []

        if size <= MAX_BYTES:
            to_send = [src]

        else:
            edit(
                "⚙️ <b>Video ကြီးနေသည်၊ Compress လုပ်နေပါသည်...</b>\n"
                "ခဏစောင့်ပါ ⏳"
            )
            ok = compress(src, comp_mp4)

            if ok and os.path.exists(comp_mp4):
                if os.path.getsize(comp_mp4) <= MAX_BYTES:
                    to_send = [comp_mp4]
                else:
                    edit("✂️ <b>Parts ခွဲနေပါသည်...</b> ⏳")
                    pfx = os.path.join(DOWNLOAD_DIR, f"{chat_id}_split")
                    to_send = split_parts(comp_mp4, pfx)
            else:
                edit("✂️ <b>Parts ခွဲနေပါသည်...</b> ⏳")
                pfx = os.path.join(DOWNLOAD_DIR, f"{chat_id}_split")
                to_send = split_parts(src, pfx)

        if not to_send:
            raise RuntimeError("Video processing မအောင်မြင်ပါ")

        # ── Send ──────────────────────────────────────────────────
        total = len(to_send)
        for idx, fpath in enumerate(to_send, 1):
            if total > 1:
                edit(
                    f"📤 <b>Upload နေပါသည်...</b> "
                    f"Part {idx}/{total} ⏳"
                )
                cap = f"🎬 <b>{title}</b>\n📌 Part {idx}/{total}"
            else:
                edit("📤 <b>Telegram သို့ Upload နေပါသည်...</b> ⏳")
                cap = f"🎬 <b>{title}</b>"

            with open(fpath, "rb") as vf:
                bot.send_video(
                    chat_id, vf,
                    caption=cap,
                    supports_streaming=True,
                    timeout=180
                )

        edit(f"✅ <b>ပြီးဆုံးပါပြီ!</b>\n🎬 {title}")

    except yt_dlp.utils.DownloadError as e:
        edit(
            "❌ <b>Download မအောင်မြင်ပါ</b>\n\n"
            "ဖြစ်နိုင်သောအကြောင်းများ:\n"
            "• Private / Restricted video ဖြစ်နေသည်\n"
            "• Link မှားနေသည်\n"
            "• Platform ပိတ်ဆို့ထားသည်\n\n"
            f"<code>{str(e)[:200]}</code>"
        )
    except Exception as e:
        logger.error(f"Unexpected: {e}")
        edit(
            f"❌ <b>Error ဖြစ်ပေါ်ပါသည်</b>\n"
            f"<code>{str(e)[:200]}</code>"
        )
    finally:
        cleanup(chat_id)

# ─── Telegram handlers ────────────────────────────────────────────
@bot.message_handler(commands=["start", "help"])
def cmd_start(msg):
    bot.send_message(
        msg.chat.id,
        "👋 <b>Video Downloader Bot မှ ကြိုဆိုပါသည်!</b>\n\n"
        "📌 <b>ပံ့ပိုးသော Platform များ:</b>\n"
        "▶️ YouTube  •  🎵 TikTok\n"
        "📸 Instagram  •  👥 Facebook\n\n"
        "🔗 <b>အသုံးပြုနည်း:</b>\n"
        "Video link ကို ဒီ chat ထဲ paste ပြီး send လုပ်ပါ\n\n"
        "📦 49 MB ကျော်ပါက အလိုအလျောက် compress "
        "သို့မဟုတ် parts ခွဲပေးမည်"
    )

@bot.message_handler(func=lambda m: True, content_types=["text"])
def on_text(msg):
    url = find_url(msg.text.strip())
    if not url:
        bot.reply_to(
            msg,
            "⚠️ Valid video link မတွေ့ပါ\n"
            "YouTube / TikTok / Instagram / Facebook link ပို့ပါ"
        )
        return

    status = bot.send_message(msg.chat.id, "🔍 <b>Link စစ်ဆေးနေပါသည်...</b>")
    threading.Thread(
        target=process,
        args=(msg.chat.id, url, status.message_id),
        daemon=True
    ).start()

# ─── Entry point ──────────────────────────────────────────────────
def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable မသတ်မှတ်ရသေး!")

    logger.info("🚀 Starting Video Downloader Bot …")

    threading.Thread(target=run_flask, daemon=True).start()
    logger.info(f"🌐 Flask web server → port {PORT}")

    logger.info("🤖 Telegram polling started …")
    bot.infinity_polling(timeout=60, long_polling_timeout=30)
