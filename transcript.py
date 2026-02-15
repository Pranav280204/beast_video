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
DRY_RUN = os.getenv("DRY_RUN", "True").lower() == "true"
GAMMA_API = os.getenv("GAMMA_API", "https://gamma-api.polymarket.com")
CLOB_API = os.getenv("CLOB_API", "https://clob.polymarket.com")
POLYGON_RPC = os.getenv("POLYGON_RPC", "https://polygon-rpc.com/")
CONDITIONAL_TOKENS = os.getenv("CONDITIONAL_TOKENS", "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045")

# YouTube transcript fetch
YT_TRANSCRIPT_API_TOKEN = os.getenv("YT_TRANSCRIPT_API_TOKEN")  # youtube-transcript.io token

# Auto-trade config for MrBeast video resolution
MRBEAST_EVENT_SLUG = "what-will-mrbeast-say-during-his-next-youtube-video"
AUTO_BUY_USDC_PER_MARKET = float(os.getenv("AUTO_BUY_USDC_PER_MARKET", "0"))  # 0 = disabled
AUTO_MAX_YES_PRICE = float(os.getenv("AUTO_MAX_YES_PRICE", "0.95"))  # only buy if mid price < this

# YouTube monitoring (existing)
YT_API_KEYS = [k.strip() for k in os.getenv("YOUTUBE_API_KEYS", "").split(",") if k.strip()]
YOUTUBE_CHANNEL_ID = os.getenv("YOUTUBE_CHANNEL_ID")
POLL_INTERVAL = float(os.getenv("YT_POLL_INTERVAL", "60"))
TELEGRAM_HEARTBEAT = float(os.getenv("YT_HEARTBEAT", "10"))

if not TELEGRAM_BOT_TOKEN:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN")

# ---------- Web3 + Clob ----------
w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
client = None
if PRIVATE_KEY and WALLET_ADDRESS:
    try:
        client = ClobClient(host=CLOB_API, key=PRIVATE_KEY, chain_id=137, signature_type=1, funder=WALLET_ADDRESS)
        creds = client.create_or_derive_api_creds()
        client.set_api_creds(creds)
    except Exception as e:
        print("ClobClient init error:", e)

# ---------- Word groups for MrBeast buzzwords ----------
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

# Mapping from market question keywords → (category, threshold)
MARKET_MAPPING = {
    "dollar": ("Dollar", 10),
    "thousand": ("Thousand/Million", 10),
    "million": ("Thousand/Million", 10),
    "challenge": ("Challenge", 1),
    "eliminated": ("Eliminated", 1),
    "trap": ("Trap", 1),
    "car": ("Car/Supercar", 1),
    "supercar": ("Car/Supercar", 1),
    "tesla": ("Tesla/Lamborghini", 1),
    "lamborghini": ("Tesla/Lamborghini", 1),
    "helicopter": ("helicopter/Jet", 1),
    "jet": ("helicopter/Jet", 1),
    "island": ("Island", 1),
    "mystery box": ("Mystery Box", 1),
    "massive": ("Massive", 1),
    "world's biggest": ("World's Biggest/Worlds Largest", 1),
    "world's largest": ("World's Biggest/Worlds Largest", 1),
    "beast games": ("Beast Games", 1),
    "feastables": ("Feastables", 1),
    "mrbeast": ("MrBeast", 1),
    "insane": ("Insane", 1),
    "subscribe": ("Subscribe", 1),
}

# ---------- Helpers (existing + new) ----------
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
                collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(text_parts)

# ... (keep your existing helpers: fetch_active_markets, normalize_outcomes_and_token_ids, get_mid_price, get_balance_shares, place_market_order)

# ---------- New: YouTube video handler for auto analysis + trade ----------
async def handle_youtube_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    video_id = extract_video_id(text)
    if not video_id:
        return  # not a YouTube link/ID

    if not YT_TRANSCRIPT_API_TOKEN:
        await update.message.reply_text("YT_TRANSCRIPT_API_TOKEN not configured – cannot auto-fetch transcript.")
        return

    await update.message.reply_text("🎥 YouTube video detected! Fetching transcript for MrBeast word analysis...")

    loop = asyncio.get_running_loop()
    try:
        response = await loop.run_in_executor(None, lambda: requests.post(
            "https://www.youtube-transcript.io/api/transcripts",
            headers={"Authorization": f"Basic {YT_TRANSCRIPT_API_TOKEN}", "Content-Type": "application/json"},
            json={"ids": [video_id]},
            timeout=30
        ))
        response.raise_for_status()
        data = response.json()
        transcript = extract_transcript_text(data).lower()

        if not transcript.strip():
            await update.message.reply_text("Transcript fetched but empty (no captions?).")
            return

        # Count words
        counts = {cat: len(re.findall(pat, transcript)) for cat, pat in word_groups.items()}
        sorted_counts = dict(sorted(counts.items()))
        total = sum(sorted_counts.values())

        table = "<pre>"
        table += f"{'Category':<30} {'Count':>8}\n"
        table += "-" * 40 + "\n"
        for cat, cnt in sorted_counts.items():
            table += f"{cat:<30} {cnt:>8}\n"
        table += "-" * 40 + "\n"
        table += f"{'TOTAL':<30} {total:>8}\n"
        table += "</pre>"

        await update.message.reply_text(f"<b>MrBeast Buzzword Counts</b>\n\n{table}", parse_mode="HTML")

        # Auto-trade if enabled
        if AUTO_BUY_USDC_PER_MARKET <= 0:
            await update.message.reply_text("Auto-trading disabled (set AUTO_BUY_USDC_PER_MARKET > 0).")
            return

        await update.message.reply_text("Checking markets for auto-trades...")

        markets = await loop.run_in_executor(None, fetch_active_markets, MRBEAST_EVENT_SLUG)
        if not markets:
            await update.message.reply_text("No active markets found for the MrBeast event.")
            return

        trade_log = []
        for market in markets:
            q_lower = market.get("question", "").lower()
            outcomes, token_ids = normalize_outcomes_and_token_ids(market)
            if len(outcomes) < 2 or not token_ids:
                continue

            yes_idx = next((i for i, o in enumerate(outcomes) if o.lower() == "yes"), None)
            if yes_idx is None:
                continue
            yes_token = token_ids[yes_idx]

            mid = await loop.run_in_executor(None, get_mid_price, yes_token)
            if mid is None or mid >= AUTO_MAX_YES_PRICE:
                continue

            # Match category & threshold
            matched = None
            for key, (cat, thresh) in MARKET_MAPPING.items():
                if key in q_lower:
                    matched = (cat, thresh)
                    break
            if not matched:
                continue

            cat, thresh = matched
            count = counts.get(cat, 0)
            if count < thresh:
                continue  # NO

            # YES → auto buy
            resp = await asyncio.to_thread(place_market_order, yes_token, AUTO_BUY_USDC_PER_MARKET, BUY)
            status = "DRY RUN" if DRY_RUN else "EXECUTED"
            trade_log.append(f"✅ {status} Buy ${AUTO_BUY_USDC_PER_MARKET} YES on \"{cat}\" (count: {count}, ~{mid:.2f}¢)")

        if trade_log:
            await update.message.reply_text("<b>Auto Trades Executed:</b>\n" + "\n".join(trade_log), parse_mode="HTML")
        else:
            await update.message.reply_text("No auto-trades triggered (priced in or below threshold).")

    except Exception as e:
        await update.message.reply_text(f"Error during analysis/trading: {str(e)}")

# ... (keep your existing handlers: monitor_youtube_and_trigger, check_latest_subs, conversation handlers, etc.)

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Priority: YouTube links first
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_youtube_video), group=0)

    # Existing conversation, commands, etc.
    # ... add your conv handler, stop, status, subs, etc.

    app.bot_data["yt_api_keys"] = YT_API_KEYS
    app.bot_data["YOUTUBE_CHANNEL_ID"] = YOUTUBE_CHANNEL_ID

    print("MrBeast Polymarket Bot started – send a YouTube link to auto-analyze & trade!")
    app.run_polling()

if __name__ == "__main__":
    main()
