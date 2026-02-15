import os
import re
import time
import asyncio
import aiohttp
import json
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
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
YT_TRANSCRIPT_IO_TOKEN = os.getenv("YT_TRANSCRIPT_IO_TOKEN")
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
TRADE_AMOUNT_USDC = float(os.getenv("TRADE_AMOUNT_USDC", "5.0"))
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID", "UCX6OQ3DkcsbYNE6H8uQQuVA")  # MrBeast
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "300"))
HEARTBEAT = float(os.getenv("HEARTBEAT", "300"))
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
    "helicopter / Jet": ("helicopter/Jet", 1),
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
if not YT_TRANSCRIPT_IO_TOKEN:
    raise ValueError("Missing YT_TRANSCRIPT_IO_TOKEN")

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

# ---------- RSS-based latest video fetch ----------
async def fetch_latest_video_rss(session):
    feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}"
    async with session.get(feed_url, timeout=10) as resp:
        if resp.status != 200:
            return {"error": f"RSS fetch failed: {resp.status}"}
        text = await resp.text()
        try:
            root = ET.fromstring(text)
            ns = {"atom": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015"}
            entries = root.findall("atom:entry", ns)
            if not entries:
                return {"error": "no videos in feed"}
            latest = entries[0]
            video_id = latest.find("yt:videoId", ns).text
            title = latest.find("atom:title", ns).text
            return {"video_id": video_id, "title": title}
        except ET.ParseError as e:
            return {"error": f"XML parse error: {str(e)}"}

# ---------- Core processing function ----------
async def process_video(video_id: str, chat_id: int, application: Application):
    url = f"https://www.youtube.com/watch?v={video_id}"
    await application.bot.send_message(chat_id, f"🔍 Processing video: {url}\nFetching transcript...")

    # Fetch transcript from youtube-transcript.io API
    transcript_text = None
    api_url = "https://www.youtube-transcript.io/api/transcripts"
    payload = {"ids": [video_id]}
    headers = {
        "Authorization": f"Basic {YT_TRANSCRIPT_IO_TOKEN}",
        "Content-Type": "application/json"
    }
    try:
        r = await asyncio.to_thread(requests.post, api_url, json=payload, headers=headers, timeout=30)
        r.raise_for_status()
        data = r.json()

        # Assuming response: { "video_id": [{"text": "...", ...}, ...] }
        # Adjust if actual format differs (e.g., data["transcripts"][0]["lines"])
        segments = data.get(video_id, [])
        if not isinstance(segments, list) or not segments:
            await application.bot.send_message(chat_id, f"❌ No transcript segments found in API response.")
            return

        transcript_text = " ".join([seg.get("text", "") for seg in segments if seg.get("text")]).lower()

    except requests.exceptions.HTTPError as e:
        if r.status_code == 429:
            await application.bot.send_message(chat_id, "❌ Rate limited by youtube-transcript.io (429).")
        else:
            await application.bot.send_message(chat_id, f"❌ API HTTP error: {r.status_code}")
        return
    except Exception as e:
        await application.bot.send_message(chat_id, f"❌ Transcript API error: {str(e)}")
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
            status = resp.get("status") or resp.get("error", "error")
            trade_log.append(f"🟢 BUY YES ${TRADE_AMOUNT_USDC} on \"{m['question']}\" (count={cnt} ≥ {m['threshold']}) → {status}")
        else:
            trade_log.append(f"⚪ No trade: \"{m['question']}\" (count={cnt} < {m['threshold']})")

    msg = f"<b>Analysis Complete</b>\n\n{table}\n\n<b>Trades:</b>\n" + "\n".join(trade_log)
    await application.bot.send_message(chat_id, msg, parse_mode="HTML")

# ---------- Auto monitoring ----------
async def monitor_new_video(application: Application):
    print("Auto monitoring started")
    chat_id = application.bot_data["chat_id"]
    await application.bot.send_message(chat_id, "🔄 Starting auto monitoring for new MrBeast video (using RSS feed)...")

    last_video_id = None
    last_heartbeat = 0
    async with aiohttp.ClientSession() as session:
        # Get initial latest
        res = await fetch_latest_video_rss(session)
        if "video_id" not in res:
            await application.bot.send_message(chat_id, f"❌ Failed to get initial latest video: {res.get('error')}")
            application.bot_data["video_monitoring"] = False
            return
        last_video_id = res["video_id"]
        await application.bot.send_message(chat_id, f"Current latest: {res.get('title', 'Unknown')} ({last_video_id})")

        while application.bot_data.get("video_monitoring", False):
            loop_start = time.time()
            res = await fetch_latest_video_rss(session)
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
    context.application.bot_data["video_monitoring"] = True
    context.application.create_task(monitor_new_video(context.application))
    await update.message.reply_text(
        f"🚀 Auto monitoring started!\n"
        f"Polling interval: {POLL_INTERVAL}s\n"
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
