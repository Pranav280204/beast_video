import os
import re
import time
import asyncio
import aiohttp
from datetime import datetime, timezone
import requests
from youtube_transcript_api import YouTubeTranscriptApi, NoTranscriptFound, TranscriptsDisabled
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------- Config ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
TRADE_AMOUNT_USDC = float(os.getenv("TRADE_AMOUNT_USDC", "5.0"))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"

# YouTube
YT_API_KEYS = [k.strip() for k in os.getenv("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCX6OQ3DkcsbYNE6H8uQQuVA")  # MrBeast
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))  # configurable via env, default 5 min
HEARTBEAT = float(os.getenv("HEARTBEAT", "300"))  # status every 5 min

# Event
EVENT_SLUG = "what-will-mrbeast-say-during-his-next-youtube-video"

# Market → (phrase_key, threshold)
MARKET_CONFIG = {
    "Dollar 10+": ("Dollar", 10),
    "Thousand / Million 10+": ("Thousand/Million", 10),
    "Challenge": ("Challenge", 1),
    "Eliminated": ("Eliminated", 1),
    "Trap": ("Trap", 1),
    "Car / Supercar": ("Car/Supercar", 1),
    "Tesla / Lamborghini": ("Tesla/Lamborghini", 1),
    "helicopter / Jet": ("helicopter/Jet", 1),  # lowercase for matching
    "Helicopter / Jet": ("helicopter/Jet", 1),
    "Island": ("Island", 1),
    "Mystery Box": ("Mystery Box", 1),
    "Massive": ("Massive", 1),
    "World's Biggest": ("World's Biggest/Worlds Largest", 1),
    "World's Largest": ("World's Biggest/Worlds Largest", 1),
    "Beast Games": ("Beast Games", 1),
    "Feastables": ("Feastables", 1),
    "MrBeast": ("MrBeast", 1),
    "Insane": ("Insane", 1),
    "Subscribe": ("Subscribe", 1),
}

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN")

# ---------- Web3 ----------
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = None
if PRIVATE_KEY and WALLET_ADDRESS:
    try:
        client = ClobClient(host=CLOB_API, key=PRIVATE_KEY, chain_id=137, signature_type=1, funder=WALLET_ADDRESS)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        print("ClobClient init error:", e)

# ---------- Word groups ----------
word_groups = {
    "Dollar": r"\bdollar(s)?\b",
    "Thousand/Million": r"\b(thousand|million)(s)?\b",
    "Challenge": r"\bchallenge(s)?\b",
    "Eliminated": r"\beliminated?\b",
    "Trap": r"\btrap(s)?\b",
    "Car/Supercar": r"\b(car|supercar)(s)?\b",
    "Tesla/Lamborghini": r"\b(tesla|lamborghini)(s)?\b",
    "helicopter/Jet": r"\b(helicopter|jet)(s)?\b",
    "Island": r"\bisland(s)?\b",
    "Mystery Box": r"\bmystery box(es)?\b",
    "Massive": r"\bmassive\b",
    "World's Biggest/Worlds Largest": r"\bworld'?s?\s+(biggest|largest)\b",
    "Beast Games": r"\bbeast games\b",
    "Feastables": r"\bfeastables\b",
    "MrBeast": r"\bmr\.?\s*beast\b",
    "Insane": r"\binsane\b",
    "Subscribe": r"\bsubscrib(e|ed|ing|er|s)?\b"
}

# ---------- Helpers ----------
def extract_video_id(input_str: str) -> str | None:
    patterns = [
        r'(?:v=|\/embed\/|\/shorts\/|\/watch\?v=|youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    for pat in patterns:
        m = re.search(pat, input_str)
        if m:
            return m.group(1)
    return None

def fetch_active_markets(slug: str):
    try:
        r = requests.get(f"{GAMMA_API}/events/slug/{slug}", timeout=15)
        r.raise_for_status()
        data = r.json()
        return [m for m in data.get("markets", []) if m.get("active") and not m.get("closed")]
    except Exception as e:
        print("fetch markets error:", e)
        return []

def normalize_outcomes_and_token_ids(market: dict):
    outcomes = market.get("outcomes", [])
    token_ids = market.get("clobTokenIds", [])
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes) if outcomes else []
    if isinstance(token_ids, str):
        token_ids = json.loads(token_ids) if token_ids else []
    return outcomes, token_ids

def place_buy_order(token_id: str, amount_usdc: float):
    if DRY_RUN or not client:
        return {"status": "dry_run", "amount": amount_usdc}
    try:
        args = MarketOrderArgs(token_id=token_id, amount=amount_usdc, side=BUY, order_type=OrderType.FOK)
        signed = client.create_market_order(args)
        resp = client.post_order(signed, OrderType.FOK)
        return resp
    except Exception as e:
        return {"error": str(e)}

def format_count_table(counts: dict):
    sorted_c = dict(sorted(counts.items()))
    total = sum(sorted_c.values())
    lines = [
        "<pre>",
        f"{'Category':<30} {'Count':>8}",
        "-" * 40,
    ]
    for cat, cnt in sorted_c.items():
        lines.append(f"{cat:<30} {cnt:>8}")
    lines += ["-" * 40, f"{'TOTAL':<30} {total:>8}", "</pre>"]
    return "\n".join(lines)

class YTKeyRotator:
    def __init__(self, keys):
        self.keys = keys or []
        self.idx = 0
    def get_key(self):
        if not self.keys: return None
        k = self.keys[self.idx % len(self.keys)]
        self.idx += 1
        return k

async def fetch_latest_video(session, api_key, channel_id):
    url = "https://www.googleapis.com/youtube/v3/channels"
    params = {"part": "contentDetails", "id": channel_id, "key": api_key}
    async with session.get(url, params=params, timeout=10) as resp:
        if resp.status != 200: return {"error": "channel fetch failed"}
        data = await resp.json()
        items = data.get("items", [])
        if not items: return {"error": "no channel"}
        uploads_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]

    url = "https://www.googleapis.com/youtube/v3/playlistItems"
    params = {"part": "snippet,contentDetails", "playlistId": uploads_id, "maxResults": 1, "key": api_key}
    async with session.get(url, params=params, timeout=10) as resp:
        if resp.status != 200: return {"error": "playlist fetch failed"}
        data = await resp.json()
        items = data.get("items", [])
        if not items: return {"error": "no videos"}
        item = items[0]
        video_id = item["contentDetails"]["videoId"]
        title = item["snippet"]["title"]
        return {"video_id": video_id, "title": title}

# ---------- Core processing function ----------
async def process_video(video_id: str, chat_id: int, application: Application):
    url = f"https://www.youtube.com/watch?v={video_id}"
    await application.bot.send_message(chat_id, f"🔍 Processing video: {url}\nFetching transcript...")

    # Fetch transcript
    transcript_text = None
    try:
        transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id, languages=['en'])
        transcript_text = " ".join([t['text'] for t in transcript_list]).lower()
    except (NoTranscriptFound, TranscriptsDisabled):
        try:
            transcript_list = await asyncio.to_thread(YouTubeTranscriptApi.get_transcript, video_id)
            transcript_text = " ".join([t['text'] for t in transcript_list]).lower()
        except Exception as e2:
            await application.bot.send_message(chat_id, f"❌ No transcript available: {str(e2)}")
            return
    except Exception as e:
        await application.bot.send_message(chat_id, f"❌ Transcript error: {str(e)}")
        return

    if not transcript_text.strip():
        await application.bot.send_message(chat_id, "❌ Empty transcript.")
        return

    # Setup markets if not done
    if not application.bot_data.get("monitored_markets"):
        markets = fetch_active_markets(EVENT_SLUG)
        if not markets:
            await application.bot.send_message(chat_id, "❌ Failed to fetch Polymarket markets.")
            return
        monitored = []
        for m in markets:
            question = m.get("question", "").lower()
            matched = None
            for key_lower, (phrase, thresh) in [(k.lower(), v) for k, v in MARKET_CONFIG.items()]:
                if key_lower in question:
                    matched = (phrase, thresh)
                    break
            if matched:
                outcomes, token_ids = normalize_outcomes_and_token_ids(m)
                try:
                    yes_idx = [o.lower() for o in outcomes].index("yes")
                    token_yes = token_ids[yes_idx]
                    monitored.append({
                        "question": m.get("question", ""),
                        "phrase": matched[0],
                        "threshold": matched[1],
                        "token_yes": token_yes
                    })
                except:
                    continue
        application.bot_data["monitored_markets"] = monitored
        await application.bot.send_message(chat_id, f"📊 Loaded {len(monitored)} markets.")

    # Count words
    counts = {cat: len(re.findall(pat, transcript_text)) for cat, pat in word_groups.items()}
    table = format_count_table(counts)

    # Trades
    monitored = application.bot_data["monitored_markets"]
    trade_log = []
    for m in monitored:
        cnt = counts.get(m["phrase"], 0)
        if cnt >= m["threshold"]:
            resp = await asyncio.to_thread(place_buy_order, m["token_yes"], TRADE_AMOUNT_USDC)
            status = resp.get("status", resp.get("error", "error"))
            trade_log.append(f"🟢 BUY YES ${TRADE_AMOUNT_USDC} on \"{m['question']}\" (count={cnt} ≥ {m['threshold']}) → {status}")
        else:
            trade_log.append(f"⚪ No trade: \"{m['question']}\" (count={cnt} < {m['threshold']})")

    msg = f"<b>Analysis Complete</b>\n\n{table}\n\n<b>Trades:</b>\n" + "\n".join(trade_log)
    await application.bot.send_message(chat_id, msg, parse_mode="HTML")

# ---------- Auto monitoring ----------
async def monitor_new_video(application: Application):
    print("Auto monitoring started")
    rotator = YTKeyRotator(application.bot_data.get("yt_api_keys", YT_API_KEYS))
    chat_id = application.bot_data["chat_id"]

    if not rotator.keys:
        await application.bot.send_message(chat_id, "⚠️ No YouTube API keys configured. Cannot poll for new videos.")
        application.bot_data["video_monitoring"] = False
        return

    await application.bot.send_message(chat_id, "🔄 Starting auto monitoring for new MrBeast video...")

    last_video_id = None
    last_heartbeat = 0

    async with aiohttp.ClientSession() as session:
        # Get initial latest
        for _ in range(3):
            key = rotator.get_key()
            if not key: break
            res = await fetch_latest_video(session, key, YOUTUBE_CHANNEL_ID)
            if "video_id" in res:
                last_video_id = res["video_id"]
                title = res.get("title", "Unknown")
                await application.bot.send_message(chat_id, f"Current latest: {title} ({last_video_id})")
                break

        if not last_video_id:
            await application.bot.send_message(chat_id, "❌ Failed to get initial latest video.")
            application.bot_data["video_monitoring"] = False
            return

        while application.bot_data.get("video_monitoring", False):
            loop_start = time.time()
            key = rotator.get_key()
            if not key:
                await asyncio.sleep(POLL_INTERVAL)
                continue

            res = await fetch_latest_video(session, key, YOUTUBE_CHANNEL_ID)
            if "video_id" in res and res["video_id"] != last_video_id:
                await application.bot.send_message(chat_id, f"🆕 NEW VIDEO DETECTED!\n{res.get('title', 'Unknown')}")
                await process_video(res["video_id"], chat_id, application)
                application.bot_data["video_monitoring"] = False
                await application.bot.send_message(chat_id, "✅ Processed new video. Auto monitoring stopped.")
                return

            # Heartbeat
            if time.time() - last_heartbeat >= HEARTBEAT:
                await application.bot.send_message(chat_id, "⏳ Monitoring... No new video yet.")
                last_heartbeat = time.time()

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(0.1, POLL_INTERVAL - elapsed))

# ---------- Commands ----------
async def start_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.application.bot_data.get("video_monitoring"):
        await update.message.reply_text("Already monitoring.")
        return

    context.application.bot_data["chat_id"] = update.effective_chat.id
    context.application.bot_data["yt_api_keys"] = YT_API_KEYS
    context.application.bot_data["video_monitoring"] = True

    context.application.create_task(monitor_new_video(context.application))

    await update.message.reply_text(
        f"🚀 Auto monitoring started!\n"
        f"Polling interval: {POLL_INTERVAL}s (configurable via POLL_INTERVAL env)\n"
        f"DRY_RUN: {DRY_RUN}\n"
        f"Trade amount: ${TRADE_AMOUNT_USDC}"
    )

async def analyze_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /analyze <YouTube video URL or ID>")
        return

    video_input = " ".join(context.args)
    video_id = extract_video_id(video_input)
    if not video_id:
        await update.message.reply_text("❌ Invalid YouTube URL or video ID.")
        return

    chat_id = update.effective_chat.id
    await process_video(video_id, chat_id, context.application)

async def stop_monitor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.application.bot_data["video_monitoring"] = False
    await update.message.reply_text("🛑 Auto monitoring stopped.")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    monitoring = context.application.bot_data.get("video_monitoring", False)
    markets = len(context.application.bot_data.get("monitored_markets", []))
    await update.message.reply_text(
        f"Auto monitoring: {monitoring}\n"
        f"Markets loaded: {markets}\n"
        f"DRY_RUN: {DRY_RUN}\n"
        f"Trade amount: ${TRADE_AMOUNT_USDC}\n"
        f"Poll interval: {POLL_INTERVAL}s"
    )

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start_monitor", start_monitor))
    app.add_handler(CommandHandler("analyze", analyze_video))
    app.add_handler(CommandHandler("stop", stop_monitor))
    app.add_handler(CommandHandler("status", status))

    print("MrBeast Trading Bot ready.\nCommands:\n/start_monitor - auto poll for new video\n/analyze <url/id> - manual analyze\n/stop - stop auto\n/status")
    app.run_polling()

if __name__ == "__main__":
    main()
