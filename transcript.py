import os
import re
import json
import time
import asyncio
import aiohttp
import requests
from datetime import datetime, timezone
from dotenv import load_dotenv
from web3 import Web3
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import MarketOrderArgs, OrderType
from py_clob_client.order_builder.constants import BUY, SELL
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

load_dotenv()

# ---------- Config ----------
PRIVATE_KEY = os.getenv("PRIVATE_KEY")
WALLET_ADDRESS = os.getenv("WALLET_ADDRESS")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
YT_TRANSCRIPT_TOKEN = os.getenv("YT_TRANSCRIPT_TOKEN")  # for youtube-transcript.io
MRBEAST_TRADE_AMOUNT = float(os.getenv("MRBEAST_TRADE_AMOUNT", "5.0"))  # USDC per qualifying Yes trade (0 to disable)
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = os.getenv("CONDITIONAL_TOKENS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")
MRBEAST_EVENT_SLUG = "what-will-mrbeast-say-during-his-next-youtube-video"  # fixed event

# YouTube monitoring (existing)
YT_API_KEYS = [k.strip() for k in os.getenv("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
POLL_INTERVAL = float(os.getenv("YT_POLL_INTERVAL", "60"))
TELEGRAM_HEARTBEAT = float(os.getenv("YT_HEARTBEAT", "10"))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN in environment")

# ---------- Web3 + ClobClient ----------
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = None
if PRIVATE_KEY and WALLET_ADDRESS:
    try:
        client = ClobClient(
            host=CLOB_API,
            key=PRIVATE_KEY,
            chain_id=137,
            signature_type=1,
            funder=WALLET_ADDRESS
        )
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
        print("ClobClient initialized")
    except Exception as e:
        print("ClobClient init failed:", e)

ERC1155_ABI = [...]  # (keep your existing ABI)

# ---------- Word groups for MrBeast transcript counting ----------
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

# ---------- Helpers (existing + new) ----------
# (keep your existing helpers: fetch_active_markets, normalize_outcomes_and_token_ids, get_mid_price, get_balance_shares, place_market_order)

def extract_video_id(user_input: str):
    patterns = [
        r'(?:v=|\/embed\/|\/shorts\/|\/watch\?v=|youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, user_input)
        if match:
            return match.group(1)
    return None

def extract_transcript_text(data):
    text_parts = []
    def collect(obj):
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, str) and len(v.split()) > 5:
                    text_parts.append(v)
                elif isinstance(v, list):
                    for item in v:
                        if isinstance(item, dict) and 'text' in item:
                            text_parts.append(item['text'])
                        collect(item)
                collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(text_parts)

def count_words(text_lower: str):
    counts = {}
    for category, pattern in word_groups.items():
        counts[category] = len(re.findall(pattern, text_lower))
    return counts

def format_count_table(counts: dict):
    sorted_counts = dict(sorted(counts.items()))
    total = sum(sorted_counts.values())
    lines = [
        "<b>MrBeast Word Counts</b>",
        "<pre>",
        f"{'Category':<30} {'Count':>8}",
        "-" * 40,
    ]
    for cat, cnt in sorted_counts.items():
        lines.append(f"{cat:<30} {cnt:>8}")
    lines.extend(["-" * 40, f"{'TOTAL':<30} {total:>8}", "</pre>"])
    return "\n".join(lines)

def get_category_and_threshold(market: dict):
    q = market.get("question", "").lower()
    if "dollar" in q and "10" in q:
        return "Dollar", 10
    if ("thousand" in q or "million" in q) and "10" in q:
        return "Thousand/Million", 10
    if "challenge" in q:
        return "Challenge", 1
    if "eliminated" in q:
        return "Eliminated", 1
    if "trap" in q:
        return "Trap", 1
    if "car" in q or "supercar" in q:
        return "Car/Supercar", 1
    if "tesla" in q or "lamborghini" in q:
        return "Tesla/Lamborghini", 1
    if "helicopter" in q or "jet" in q:
        return "helicopter/Jet", 1
    if "island" in q:
        return "Island", 1
    if "mystery box" in q:
        return "Mystery Box", 1
    if "massive" in q:
        return "Massive", 1
    if "world's biggest" in q or "worlds largest" in q:
        return "World's Biggest/Worlds Largest", 1
    if "beast games" in q:
        return "Beast Games", 1
    if "feastables" in q:
        return "Feastables", 1
    if "mrbeast" in q or "mr.beast" in q:
        return "MrBeast", 1
    if "insane" in q:
        return "Insane", 1
    if "subscribe" in q:
        return "Subscribe", 1
    return None, None

# ---------- New: YouTube link handler ----------
async def handle_youtube_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video_id = extract_video_id(update.message.text)
    if not video_id:
        await update.message.reply_text("Couldn't extract video ID. Send a valid YouTube link.")
        return

    await update.message.reply_text(f"🔗 Detected MrBeast video: {video_id}\nFetching transcript...")

    if not YT_TRANSCRIPT_TOKEN:
        await update.message.reply_text("No YT_TRANSCRIPT_TOKEN set – can't auto-fetch. Paste transcript manually.")
        return

    try:
        url = "https://www.youtube-transcript.io/api/transcripts"
        headers = {"Authorization": f"Basic {YT_TRANSCRIPT_TOKEN}", "Content-Type": "application/json"}
        payload = {"ids": [video_id]}
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        transcript = extract_transcript_text(data).lower()
        if not transcript.strip():
            await update.message.reply_text("Transcript fetched but empty (no captions?). Paste manually.")
            return
    except Exception as e:
        await update.message.reply_text(f"Transcript fetch failed: {str(e)[:200]}\nPaste manually if needed.")
        return

    await update.message.reply_text("Transcript fetched! Counting words...")

    counts = count_words(transcript)
    await update.message.reply_text(format_count_table(counts), parse_mode="HTML")

    await update.message.reply_text("Checking Polymarket markets for auto-trades...")

    markets = fetch_active_markets(MRBEAST_EVENT_SLUG)
    if not markets:
        await update.message.reply_text("No active markets found for the MrBeast event.")
        return

    trade_log = []
    amount = MRBEAST_TRADE_AMOUNT

    for market in markets:
        cat, thresh = get_category_and_threshold(market)
        if not cat:
            continue
        count = counts.get(cat, 0)
        if count < thresh:
            continue

        outcomes, token_ids = normalize_outcomes_and_token_ids(market)
        yes_idx = next((i for i, o in enumerate(outcomes) if isinstance(o, str) and "yes" in o.lower()), None)
        if yes_idx is None or not token_ids:
            continue

        token_yes = token_ids[yes_idx]
        mid = get_mid_price(token_yes)
        price_str = f"{mid:.4f}" if mid else "N/A"

        if amount <= 0:
            trade_log.append(f"✅ {cat} qualifies (count {count} >= {thresh}, ~{price_str}) – trade disabled (amount=0)")
            continue

        resp = await asyncio.to_thread(place_market_order, token_yes, amount, BUY)
        status = resp.get("status", "error") if isinstance(resp, dict) else "error"
        trade_log.append(f"✅ {cat} Yes (count {count} >= {thresh}, ~{price_str}) → Bought ${amount} | {status}")

    if trade_log:
        await update.message.reply_text("<b>Auto-Trades Executed</b>\n\n" + "\n".join(trade_log), parse_mode="HTML")
    else:
        await update.message.reply_text("No qualifying markets for auto-trade.")

# ---------- Rest of your existing code (monitoring, conversation, etc.) ----------
# (keep everything else unchanged)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Existing handlers
    # ... (your conv handler, stop, status, check_latest_subs, etc.)

    # New: YouTube link handler (high priority – non-command text with youtube link)
    app.add_handler(MessageHandler(filters.Regex(r'(https?://)?(www\.)?(youtube|youtu)\.(com|be)'), handle_youtube_link))

    # (keep your existing app.bot_data setup)

    print("Bot running – send a MrBeast YouTube link for auto transcript + trade!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
