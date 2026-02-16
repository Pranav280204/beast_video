import os
import re
import requests
import telebot
import hashlib
import json  # Added for safe parsing
from ecdsa import SigningKey, SECP256k1

# Polymarket trading (only if AUTO_TRADE enabled)
if os.environ.get("AUTO_TRADE", "false").lower() == "true":
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import OrderArgs
    from py_clob_client.constants import BUY

# Environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_TOKEN = os.environ.get("API_TOKEN")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")  # Revealed magic key (with or without 0x)
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS")  # Optional, auto-derived if missing
AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT", "20"))  # USD per opportunity
POLYMARKET_SLUG = os.environ.get("POLYMARKET_SLUG", "").strip()  # REQUIRED

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)

if not POLYMARKET_SLUG:
    print("ERROR: POLYMARKET_SLUG not set in environment variables!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Derive address if not provided
def derive_address(private_key: str) -> str:
    pk = private_key[2:] if private_key.startswith('0x') else private_key
    priv_key_bytes = bytes.fromhex(pk)
    sk = SigningKey.from_string(priv_key_bytes, curve=SECP256k1)
    vk = sk.verifying_key
    uncompressed_pub_key = b'\x04' + vk.to_string()
    keccak = hashlib.sha3_256(uncompressed_pub_key).digest()
    return '0x' + keccak[-20:].hex()

if PRIVATE_KEY and not WALLET_ADDRESS:
    WALLET_ADDRESS = derive_address(PRIVATE_KEY)

# Video ID extraction
def extract_video_id(user_input):
    patterns = [
        r'(?:v=|\/embed\/|\/shorts\/|\/watch\?v=|youtu\.be\/)([0-9A-Za-z_-]{11})',
        r'^([0-9A-Za-z_-]{11})$'
    ]
    for pattern in patterns:
        match = re.search(pattern, user_input)
        if match:
            return match.group(1)
    return None

# Clean transcript extraction
def extract_transcript_text(data):
    text_parts = []
    def collect(obj):
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            if 'text' in obj and isinstance(obj['text'], str):
                text_parts.append(obj['text'])
            else:
                for v in obj.values():
                    collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(text_parts)

# Fixed word groups
word_groups = {
    "Dollar": r"\bdollar(s)?\b",
    "Thousand/Million": r"\b(thousand|million|billion)(s)?\b",
    "Challenge": r"\bchallenge(s)?\b",
    "Eliminated": r"\beliminated?\b",
    "Trap": r"\btrap(s)?\b",
    "Car/Supercar": r"\b(car|supercar)(s)?\b",
    "Tesla/Lamborghini": r"\b(tesla|lamborghini)(s)?\b",
    "Helicopter/Jet": r"\b(helicopter|jet)(s)?\b",
    "Island": r"\bisland(s)?\b",
    "Mystery Box": r"\bmystery\s+box(es)?\b",
    "Massive": r"\bmassive\b",
    "World's Biggest/Largest": r"\bworld'?s?\s+(biggest|largest)\b",
    "Beast Games": r"\bbeast\s+games\b",
    "Feastables": r"\bfeastables\b",
    "MrBeast": r"\bmr\.?\s*beast\b",
    "Insane": r"\binsane\b",
    "Subscribe": r"\bsubscrib(e|ed|ing|er|s)?\b"
}

# Fixed thresholds
thresholds = {
    "Dollar": 10,
    "Thousand/Million": 10,
    **{cat: 1 for cat in word_groups if cat not in ["Dollar", "Thousand/Million"]}
}

# Keyword mapping
market_mapping = {
    "dollar 10+ times": "Dollar",
    "thousand / million 10+ times": "Thousand/Million",
    "challenge": "Challenge",
    "eliminated": "Eliminated",
    "trap": "Trap",
    "car / supercar": "Car/Supercar",
    "tesla / lamborghini": "Tesla/Lamborghini",
    "helicopter / jet": "Helicopter/Jet",
    "island": "Island",
    "mystery box": "Mystery Box",
    "massive": "Massive",
    "world's biggest / world's largest": "World's Biggest/Largest",
    "beast games": "Beast Games",
    "feastables": "Feastables",
    "mrbeast": "MrBeast",
    "insane": "Insane",
    "subscribe": "Subscribe"
}

# Fetch Polymarket data with robust JSON string handling
def get_polymarket_data():
    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{POLYMARKET_SLUG}"
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        event = response.json()

        markets = event.get("markets", [])
        if not markets:
            return None, None

        prices = {}
        token_ids = {}

        for market in markets:
            question_lower = market.get("question", "").lower()
            matched_cat = None
            for keyword, cat in market_mapping.items():
                if keyword in question_lower:
                    matched_cat = cat
                    break

            if matched_cat:
                # Robust outcome_prices handling (can be list or JSON string)
                outcome_prices = market.get("outcome_prices") or market.get("outcomePrices", [])
                if isinstance(outcome_prices, str):
                    try:
                        outcome_prices = json.loads(outcome_prices)
                    except:
                        outcome_prices = []
                if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                    yes_price = float(outcome_prices[0])
                    prices[matched_cat] = yes_price

                # Token handling
                tokens = market.get("tokens", [])
                if tokens:
                    for token in tokens:
                        if token.get("outcome", "").lower() == "yes":
                            token_ids[matched_cat] = token.get("token_id")
                            break

                # Fallback to clobTokenIds/outcomes if needed
                if matched_cat not in token_ids:
                    outcomes = market.get("outcomes", [])
                    clob_ids = market.get("clobTokenIds", []) or market.get("clob_token_ids", [])
                    if isinstance(outcomes, str):
                        try:
                            outcomes = json.loads(outcomes)
                        except:
                            outcomes = []
                    if isinstance(clob_ids, str):
                        try:
                            clob_ids = json.loads(clob_ids)
                        except:
                            clob_ids = []
                    if "yes" in [str(o).lower() for o in outcomes]:
                        idx = [str(o).lower() for o in outcomes].index("yes")
                        if idx < len(clob_ids):
                            token_ids[matched_cat] = clob_ids[idx]

        return prices, token_ids

    except Exception as e:
        print(f"Polymarket fetch error: {e}")
        return None, None

# Rest of the code unchanged (format_results, handlers, etc.)
def format_results(text_lower):
    counts = {cat: len(re.findall(pattern, text_lower)) for cat, pattern in word_groups.items()}
    sorted_counts = dict(sorted(counts.items()))
    total = sum(sorted_counts.values())

    msg = "<pre>"
    msg += f"{'Category':<30} {'Count':>8}\n"
    msg += "-" * 40 + "\n"
    for category, count in sorted_counts.items():
        msg += f"{category:<30} {count:>8}\n"
    msg += "-" * 40 + "\n"
    msg += f"{'TOTAL':<30} {total:>8}\n"
    msg += "</pre>"

    prices, token_ids = get_polymarket_data()
    poly_section = ""
    opportunities = []
    trade_section = ""

    if prices:
        poly_section += "\n<b>📈 Polymarket MrBeast Next Video Markets</b>\n<pre>"
        poly_section += f"{'Category':<30} {'Count':>6} {'≥Thresh':>9} {'Yes ¢':>8} {'Status':>20}\n"
        poly_section += "-" * 80 + "\n"

        for cat, count in sorted_counts.items():
            thresh = thresholds.get(cat, 1)
            yes_p = prices.get(cat)
            status = ""
            if count >= thresh and yes_p is not None and yes_p < 0.95:
                edge = (1.0 - yes_p) / yes_p * 100
                status = f"SNIPABLE (~{edge:.0f}% edge)"
                opportunities.append((cat, token_ids.get(cat), yes_p))

            yes_str = f"{yes_p:.2f}" if yes_p is not None else "N/A"
            poly_section += f"{cat:<30} {count:>6} {f'≥{thresh}':>9} {yes_str:>8} {status:>20}\n"

        poly_section += "-" * 80 + "\n"
        if opportunities:
            poly_section += f"\n<b>🚨 {len(opportunities)} OPPORTUNITIES!</b>"
        else:
            poly_section += "\nNo strong edges."
        poly_section += "</pre>"
    else:
        poly_section += "\n<i>⚠️ Failed to fetch market data (check POLYMARKET_SLUG or API).</i>"

    if AUTO_TRADE and PRIVATE_KEY and opportunities and prices:
        try:
            pk = PRIVATE_KEY[2:] if PRIVATE_KEY.startswith('0x') else PRIVATE_KEY
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                signature_type=1,
                funder=WALLET_ADDRESS
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)

            address = client.get_address()
            trade_section += f"\n<b>🤖 Auto-trading from {address[:8]}... (${TRADE_AMOUNT} per opp)</b>"

            for cat, token_id, yes_p in opportunities:
                if not token_id:
                    continue
                max_price = min(0.99, yes_p + 0.10)
                size = TRADE_AMOUNT / max_price

                order_args = OrderArgs(
                    token_id=token_id,
                    price=max_price,
                    size=round(size, 6),
                    side="BUY"
                )
                try:
                    order = client.create_order(order_args)
                    signed = client.sign_order(order)
                    resp = client.post_order(signed)
                    if resp.get("order_id"):
                        trade_section += f"\n✅ Bought {cat} Yes (~${TRADE_AMOUNT})"
                    else:
                        trade_section += f"\n⚠️ {cat}: {resp.get('message', 'Failed')}"
                except Exception as e:
                    trade_section += f"\n❌ {cat}: {str(e)[:80]}"
        except Exception as e:
            trade_section += f"\n❌ Trading failed: {str(e)[:150]}"

    return f"<b>MrBeast Word Count + Sniper 🚀</b>\n\n{msg}{poly_section}{trade_section}"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "<b>MrBeast Word Counter + Polymarket Sniper Bot! 👋</b>\n\n"
        "Send YouTube URL/ID, transcript text, or .txt file.\n\n"
        f"Market: {POLYMARKET_SLUG}\n"
        "• Fixed thresholds (Dollar & Thousand/Million: 10+, others: 1+)\n"
        "• Live Yes prices from Polymarket\n"
        f"• Auto-snipe underpriced Yes (${os.environ.get('TRADE_AMOUNT', '20')} per opp if AUTO_TRADE=true)\n\n"
        f"Wallet: {WALLET_ADDRESS or 'Not set'}"
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        return

    video_id = extract_video_id(user_text)

    if video_id and API_TOKEN:
        bot.reply_to(message, "🔄 Fetching transcript...")
        try:
            url = "https://www.youtube-transcript.io/api/transcripts"
            headers = {"Authorization": f"Basic {API_TOKEN}", "Content-Type": "application/json"}
            payload = {"ids": [video_id]}
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            raw_text = extract_transcript_text(response.json())
            if not raw_text.strip():
                bot.reply_to(message, "No transcript. Paste manually.")
                return
        except Exception as e:
            bot.reply_to(message, f"❌ Fetch error: {str(e)[:200]}")
            return
    else:
        raw_text = user_text

    result_msg = format_results(raw_text.lower())
    bot.send_message(message.chat.id, result_msg, parse_mode='HTML')

@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if not (doc.mime_type == 'text/plain' or doc.file_name.lower().endswith('.txt')):
        bot.reply_to(message, "Send .txt file only.")
        return

    bot.reply_to(message, "📄 Processing...")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        transcript = downloaded.decode('utf-8', errors='replace')
        result_msg = format_results(transcript.lower())
        bot.send_message(message.chat.id, result_msg, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")

print("Bot running...")
bot.infinity_polling()
