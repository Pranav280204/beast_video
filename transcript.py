import os
import re
import requests
import telebot
import hashlib
from ecdsa import SigningKey, SECP256k1

# Polymarket trading imports
if os.environ.get("AUTO_TRADE", "false").lower() == "true":
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType

# Environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_TOKEN = os.environ.get("API_TOKEN")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS")
MARKET_SLUG = os.environ.get("MARKET_SLUG", "what-will-mrbeast-say-during-his-next-youtube-video")
AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT", "20"))

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Derive address
def derive_address(private_key: str) -> str:
    pk = private_key[2:] if private_key.startswith("0x") else private_key
    priv_key_bytes = bytes.fromhex(pk)
    sk = SigningKey.from_string(priv_key_bytes, curve=SECP256k1)
    vk = sk.verifying_key
    uncompressed_pub = b'\x04' + vk.to_string()
    keccak = hashlib.sha3_256(uncompressed_pub).digest()
    return "0x" + keccak[-20:].hex()

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

# Transcript extraction
def extract_transcript_text(data):
    text_parts = []
    def collect(obj):
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            if "text" in obj and isinstance(obj["text"], str):
                text_parts.append(obj["text"])
            else:
                for v in obj.values():
                    collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(text_parts)

# Word groups & keywords
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

polymarket_keywords = {
    "Dollar": "dollar",
    "Thousand/Million": "thousand|million|billion",
    "Challenge": "challenge",
    "Eliminated": "eliminated",
    "Trap": "trap",
    "Car/Supercar": "car|supercar",
    "Tesla/Lamborghini": "tesla|lamborghini",
    "Helicopter/Jet": "helicopter|jet",
    "Island": "island",
    "Mystery Box": "mystery box",
    "Massive": "massive",
    "World's Biggest/Largest": "world.?s (biggest|largest)",
    "Beast Games": "beast games",
    "Feastables": "feastables",
    "MrBeast": r"mr\.?\s*beast|mrbeast",
    "Insane": "insane",
    "Subscribe": "subscribe"
}

# Fixed thresholds
thresholds = {
    "Dollar": 10,
    "Thousand/Million": 10,
    **{cat: 1 for cat in word_groups if cat not in ["Dollar", "Thousand/Million"]}
}

# Robust Polymarket fetch
def get_polymarket_data():
    if not MARKET_SLUG:
        return None, None

    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{MARKET_SLUG}"
        response = requests.get(url, timeout=20)
        response.raise_for_status()
        data = response.json()

        # Robust event extraction
        event = data.get("event") or data.get("data") or data
        markets = event.get("markets", [])

        # Filter active markets (exactly like reference code)
        active_markets = [
            m for m in markets
            if m.get("active", False) and not m.get("closed", True)
        ]

        prices = {}
        token_ids = {}

        for market in active_markets:
            question_lower = market.get("question", "").lower()

            matched_cat = None
            for cat, keyword in polymarket_keywords.items():
                if re.search(keyword, question_lower, re.IGNORECASE):
                    matched_cat = cat
                    break

            if not matched_cat:
                continue

            # Prices (try multiple field names)
            outcome_prices = (
                market.get("outcome_prices") or
                market.get("outcomePrices") or
                market.get("outcome_prices", [])
            )
            if outcome_prices and len(outcome_prices) >= 2:
                yes_price = float(outcome_prices[0])
                prices[matched_cat] = yes_price

            # Token IDs - prefer new "tokens" array
            yes_token = None
            tokens = market.get("tokens", [])
            if tokens:
                for token in tokens:
                    if token.get("outcome", "").lower() == "yes":
                        yes_token = token.get("token_id")
                        break
            # Fallback to old clobTokenIds
            if not yes_token:
                clob_ids = (
                    market.get("clob_token_ids") or
                    market.get("clobTokenIds") or
                    market.get("clob_token_ids", [])
                )
                if clob_ids:
                    yes_token = clob_ids[0]  # Yes is first

            if yes_token:
                token_ids[matched_cat] = yes_token

        return prices, token_ids

    except Exception as e:
        print(f"Polymarket fetch error: {e}")
        return None, None

# Rest of the code (format_results, handlers, etc.) remains the same as previous version
# Only the get_polymarket_data() function is updated above for better robustness

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
        poly_section += "\n<b>📈 Polymarket MrBeast Markets (Live Yes Prices)</b>\n<pre>"
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
            poly_section += f"\n<b>🚨 {len(opportunities)} OPPORTUNITIES DETECTED!</b>"
        else:
            poly_section += "\nNo strong edges right now."
        poly_section += "</pre>"
    else:
        poly_section += "\n<i>⚠️ No active markets found. Check MARKET_SLUG env var (current: {MARKET_SLUG}).\nCorrect slug from URL: copy the part after /event/ before the next /</i>"

    if AUTO_TRADE and PRIVATE_KEY and opportunities and prices:
        try:
            pk = PRIVATE_KEY[2:] if PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
            client = ClobClient(host="https://clob.polymarket.com", key=pk, chain_id=137)
            creds = client.create_or_derive_api_creds()
            client.api_key = creds["api_key"]
            client.api_secret = creds["api_secret"]
            client.api_passphrase = creds["api_passphrase"]

            address = client.get_address() or WALLET_ADDRESS or "unknown"
            trade_section += f"\n<b>🤖 Auto-trading from {address[:8]}... (${TRADE_AMOUNT} per opp)</b>"

            for cat, token_id, yes_p in opportunities:
                if not token_id:
                    trade_section += f"\n⚠️ {cat}: Missing token_id"
                    continue

                mo = MarketOrderArgs(
                    token_id=token_id,
                    amount=TRADE_AMOUNT,
                    side="BUY",
                    order_type=OrderType.FOK
                )
                try:
                    signed = client.create_market_order(mo)
                    resp = client.post_order(signed, OrderType.FOK)
                    if resp.get("status") == "SUCCESS" or resp.get("order_id"):
                        trade_section += f"\n✅ Bought {cat} Yes (~${TRADE_AMOUNT})"
                    else:
                        trade_section += f"\n⚠️ {cat}: {resp.get('message', 'Failed')}"
                except Exception as e:
                    trade_section += f"\n❌ {cat}: {str(e)[:80]}"
        except Exception as e:
            trade_section += f"\n❌ Trading failed: {str(e)[:150]}"

    return f"<b>MrBeast Word Count + Sniper 🚀</b>\n\n{msg}{poly_section}{trade_section}"

# Handlers remain unchanged...
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "<b>MrBeast Sniper Bot 👋</b>\n\n"
        f"Slug: {MARKET_SLUG}\n"
        f"Trade amount: ${TRADE_AMOUNT}\n"
        f"Wallet: {WALLET_ADDRESS or 'Not set'}\n\n"
        "Send video link/transcript!"
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')

# ... (text and document handlers same as before)

print("Bot running...")
bot.infinity_polling()
