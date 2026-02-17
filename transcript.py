import os
import re
import requests
import telebot
import hashlib
import json
from ecdsa import SigningKey, SECP256k1
import time

# Polymarket trading (only if AUTO_TRADE enabled)
if os.environ.get("AUTO_TRADE", "false").lower() == "true":
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderArgs
    from py_clob_client.order_builder.constants import BUY
    from py_clob_client.clob_types import OrderType

# Environment variables
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_TOKEN = os.environ.get("API_TOKEN")
PRIVATE_KEY = os.environ.get("PRIVATE_KEY")
WALLET_ADDRESS = os.environ.get("WALLET_ADDRESS")
AUTO_TRADE = os.environ.get("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT", "10"))
MIN_TRADE_AMOUNT = float(os.environ.get("MIN_TRADE_AMOUNT", "1"))

# Market slugs from env
POLYMARKET_SLUG_1 = os.environ.get("POLYMARKET_SLUG", "what-will-mrbeast-say-during-his-next-youtube-video").strip()
POLYMARKET_SLUG_2 = os.environ.get("POLYMARKET_SLUG_2", "what-will-be-said-on-the-first-joe-rogan-experience-episode-of-the-week-february-22").strip()

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Per-user state: stores selected market key
user_market_selection = {}

# ─────────────────────────────────────────────
# MARKET CONFIGS
# ─────────────────────────────────────────────

MARKET_CONFIGS = {
    "mrbeast": {
        "slug": POLYMARKET_SLUG_1,
        "label": "🎬 MrBeast YouTube",
        "word_groups": {
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
            "Subscribe": r"\bsubscrib(e|ed|ing|er|s)?\b",
        },
        "thresholds": {
            "Dollar": 10,
            "Thousand/Million": 10,
        },
        "default_threshold": 1,
        "match_market": "mrbeast",
    },
    "joerogan": {
        "slug": POLYMARKET_SLUG_2,
        "label": "🎙️ Joe Rogan Experience",
        "word_groups": {
            "People": r"\bpeople\b",
            "Fuck/Fucking": r"\bf+u+c+k+(ing)?\b",
            "Really": r"\breally\b",
            "Interesting": r"\binteresting\b",
            "Jamie": r"\bjamie\b",
            "Dow Jones": r"\bdow\s+jones\b",
            "Pam/Bondi": r"\b(pam|bondi)\b",
            "Trump/MAGA": r"\b(trump|maga)\b",
            "Epstein": r"\bepstein\b",
            "DHS": r"\bdhs\b",
            "Congress": r"\bcongress\b",
            "Shutdown": r"\bshut\s*down\b",
            "Shooting": r"\bshooting\b",
            "War": r"\bwar\b",
            "Cocaine": r"\bcocaine\b",
            "Fentanyl": r"\bfentanyl\b",
            "Terrorist/Terrorism": r"\b(terrorist|terrorism)\b",
            "Super Bowl/Big Game": r"\b(super\s+bowl|big\s+game)\b",
            "Olympic/Olympics": r"\bolympic(s)?\b",
            "Valentine": r"\bvalentine'?s?\b",
        },
        "thresholds": {
            "People": 100,
            "Fuck/Fucking": 20,
            "Really": 10,
            "Interesting": 5,
            "Jamie": 5,
        },
        "default_threshold": 1,
        "match_market": "joerogan",
    },
}


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

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

def get_token_id_for_outcome(market, target_outcome):
    target = target_outcome.lower()
    tokens = market.get("tokens", [])
    for token in tokens:
        if token.get("outcome", "").lower() == target:
            tid = token.get("token_id")
            if tid is not None:
                return str(tid)
    outcomes_raw = market.get("outcomes", [])
    if isinstance(outcomes_raw, str):
        try:
            outcomes = json.loads(outcomes_raw)
        except:
            outcomes = []
    else:
        outcomes = outcomes_raw or []
    clob_ids_raw = market.get("clobTokenIds", []) or market.get("clob_token_ids", [])
    if isinstance(clob_ids_raw, str):
        try:
            clob_ids = json.loads(clob_ids_raw)
        except:
            clob_ids = []
    else:
        clob_ids = clob_ids_raw or []
    for idx, outcome in enumerate(outcomes):
        if str(outcome).lower() == target:
            if idx < len(clob_ids):
                tid = clob_ids[idx]
                if tid is not None:
                    return str(tid)
    return None


# ─────────────────────────────────────────────
# MARKET MATCHING FUNCTIONS
# ─────────────────────────────────────────────

def match_market_mrbeast(question_lower):
    if "beast games" in question_lower:
        return "Beast Games"
    if "mystery box" in question_lower:
        return "Mystery Box"
    if "world's biggest" in question_lower or "world's largest" in question_lower:
        return "World's Biggest/Largest"
    if "tesla" in question_lower and "lamborghini" in question_lower:
        return "Tesla/Lamborghini"
    if "helicopter" in question_lower and "jet" in question_lower:
        return "Helicopter/Jet"
    if "car" in question_lower and "supercar" in question_lower:
        return "Car/Supercar"
    if ("thousand" in question_lower or "million" in question_lower) and "10+" in question_lower:
        return "Thousand/Million"
    if "dollar" in question_lower and "10+" in question_lower:
        return "Dollar"
    if "subscribe" in question_lower:
        return "Subscribe"
    if "insane" in question_lower:
        return "Insane"
    if "feastables" in question_lower:
        return "Feastables"
    if "mrbeast" in question_lower or "mr beast" in question_lower:
        return "MrBeast"
    if "eliminated" in question_lower:
        return "Eliminated"
    if "challenge" in question_lower:
        return "Challenge"
    if "massive" in question_lower:
        return "Massive"
    if "island" in question_lower:
        return "Island"
    if "trap" in question_lower:
        return "Trap"
    return None

def match_market_joerogan(question_lower):
    if "valentine" in question_lower:
        return "Valentine"
    if "people" in question_lower and "100+" in question_lower:
        return "People"
    if ("fuck" in question_lower or "fucking" in question_lower) and "20+" in question_lower:
        return "Fuck/Fucking"
    if "really" in question_lower and "10+" in question_lower:
        return "Really"
    if "interesting" in question_lower and "5+" in question_lower:
        return "Interesting"
    if "jamie" in question_lower and "5+" in question_lower:
        return "Jamie"
    if "dow jones" in question_lower or ("dow" in question_lower and "jones" in question_lower):
        return "Dow Jones"
    if "pam" in question_lower or "bondi" in question_lower:
        return "Pam/Bondi"
    if "trump" in question_lower or "maga" in question_lower:
        return "Trump/MAGA"
    if "epstein" in question_lower:
        return "Epstein"
    if "dhs" in question_lower:
        return "DHS"
    if "congress" in question_lower:
        return "Congress"
    if "shutdown" in question_lower or "shut down" in question_lower:
        return "Shutdown"
    if "shooting" in question_lower:
        return "Shooting"
    if "war" in question_lower:
        return "War"
    if "cocaine" in question_lower:
        return "Cocaine"
    if "fentanyl" in question_lower:
        return "Fentanyl"
    if "terrorist" in question_lower or "terrorism" in question_lower:
        return "Terrorist/Terrorism"
    if "super bowl" in question_lower or "big game" in question_lower:
        return "Super Bowl/Big Game"
    if "olympic" in question_lower:
        return "Olympic/Olympics"
    return None

MARKET_MATCHERS = {
    "mrbeast": match_market_mrbeast,
    "joerogan": match_market_joerogan,
}


# ─────────────────────────────────────────────
# POLYMARKET DATA FETCH
# ─────────────────────────────────────────────

def get_polymarket_data(slug, match_fn, word_groups):
    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        print(f"\n🔍 Fetching from: {url}")
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        event = response.json()
        markets = event.get("markets", [])

        if not markets:
            print("⚠️  No markets found in event!")
            return None, None

        print(f"✅ Found {len(markets)} markets\n")
        prices = {}
        token_ids = {}
        matched_categories = set()

        for market in markets:
            question = market.get("question", "")
            question_lower = question.lower()
            matched_cat = match_fn(question_lower)

            if not matched_cat:
                print(f"❌ No match: {question}")
                continue
            if matched_cat in matched_categories:
                print(f"⚠️  DUPLICATE MATCH for {matched_cat}: {question[:60]}...")
                continue

            matched_categories.add(matched_cat)
            print(f"✅ {matched_cat:<25} ← {question[:50]}...")

            outcome_prices = market.get("outcome_prices") or market.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except:
                    outcome_prices = []

            yes_price = None
            if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                yes_price = float(outcome_prices[0])
                print(f"   Yes Price: {yes_price:.4f} ({yes_price*100:.1f}¢)")
            else:
                print(f"   ⚠️  NO PRICE DATA")

            yes_token = get_token_id_for_outcome(market, "yes")
            no_token = get_token_id_for_outcome(market, "no")

            if yes_price is not None:
                prices[matched_cat] = yes_price
            token_ids[matched_cat] = {"yes": yes_token, "no": no_token}
            print()

        print(f"📊 Summary: {len(prices)} with prices, {len(token_ids)} categories with tokens\n")
        all_categories = set(word_groups.keys())
        missing_categories = all_categories - set(prices.keys())
        if missing_categories:
            print(f"⚠️  Categories NOT found in Polymarket:")
            for cat in sorted(missing_categories):
                print(f"   - {cat}")

        return prices, token_ids

    except Exception as e:
        print(f"❌ Polymarket fetch error: {e}")
        import traceback
        traceback.print_exc()
        return None, None


# ─────────────────────────────────────────────
# FORMAT RESULTS
# ─────────────────────────────────────────────

def format_results(text_lower, market_key):
    config = MARKET_CONFIGS[market_key]
    word_groups = config["word_groups"]
    thresholds_map = config.get("thresholds", {})
    default_thresh = config.get("default_threshold", 1)
    slug = config["slug"]
    match_fn = MARKET_MATCHERS[config["match_market"]]

    thresholds = {cat: thresholds_map.get(cat, default_thresh) for cat in word_groups}
    counts = {cat: len(re.findall(pattern, text_lower)) for cat, pattern in word_groups.items()}
    sorted_counts = dict(sorted(counts.items()))
    total = sum(sorted_counts.values())

    msg = f"<b>📊 Word Counts — {config['label']}</b>\n<pre>"
    for category, count in sorted_counts.items():
        thresh = thresholds.get(category, 1)
        if count >= thresh:
            msg += f"{category:<24} {count:>4} ✅\n"
        elif count > 0:
            msg += f"{category:<24} {count:>4} ❌\n"
    msg += f"{'─'*30}\nTOTAL: {total}\n</pre>"

    prices, token_ids = get_polymarket_data(slug, match_fn, word_groups)

    opportunities = []
    missing_data = []

    if prices:
        for cat, count in sorted_counts.items():
            thresh = thresholds.get(cat, 1)
            yes_p = prices.get(cat)
            if yes_p is None:
                continue
            no_p = 1.0 - yes_p
            tokens = token_ids.get(cat, {})
            yes_token = tokens.get("yes")
            no_token = tokens.get("no")

            if count >= thresh:
                if yes_p < 0.95 and yes_token:
                    edge = int((1.0 - yes_p) / yes_p * 100) if yes_p > 0 else 999
                    opportunities.append((cat, "Yes", yes_token, yes_p, edge))
                elif yes_p < 0.95 and not yes_token:
                    missing_data.append(f"{cat} (Yes)")
            else:
                if count == 0 or count < thresh:
                    if no_p < 0.95 and no_token:
                        edge = int((1.0 - no_p) / no_p * 100) if no_p > 0 else 999
                        opportunities.append((cat, "No", no_token, no_p, edge))
                    elif no_p < 0.95 and not no_token:
                        missing_data.append(f"{cat} (No)")

        poly_section = f"\n<b>🎯 Opportunities: {len(opportunities)}</b>"
        if opportunities:
            poly_section += "\n<pre>"
            for cat, side, _, price, edge in opportunities:
                poly_section += f"{cat:<24} {side} {price:.2f} ~{edge}%\n"
            poly_section += "</pre>"
        if missing_data:
            poly_section += f"\n<i>⚠️ Missing token for: {', '.join(missing_data[:5])}</i>"
    else:
        poly_section = "\n<i>⚠️ Failed to fetch market data.</i>"
        opportunities = []

    trade_results = []
    if AUTO_TRADE and PRIVATE_KEY and opportunities:
        actual_trade_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
        try:
            pk = PRIVATE_KEY[2:] if PRIVATE_KEY.startswith('0x') else PRIVATE_KEY
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                signature_type=1,
                funder=WALLET_ADDRESS or None
            )
            creds = client.create_or_derive_api_creds()
            client.set_api_creds(creds)
            address = client.get_address()
            print(f"\n🔑 Trading wallet: {address}")

            try:
                balance_resp = client.get_balance()
                usdc_balance = float(balance_resp.get("balance", 0)) / 1e6
                print(f"💰 USDC Balance: ${usdc_balance:.2f}")
                if usdc_balance < actual_trade_amt * len(opportunities):
                    trade_results.append(f"⚠️ Low balance: ${usdc_balance:.2f}")
            except Exception as e:
                print(f"⚠️  Balance check failed: {e}")

            for cat, side, token_id, price, edge in opportunities:
                try:
                    print(f"\n📊 Trading {cat} {side}: Token={token_id} Price={price:.4f} Amount=${actual_trade_amt}")
                    args = MarketOrderArgs(token_id=token_id, amount=actual_trade_amt, side=BUY)
                    signed = client.create_market_order(args)
                    resp = client.post_order(signed, OrderType.FOK)
                    print(f"   Response: {resp}")
                    order_id = resp.get("order_id") or resp.get("orderID")
                    success = resp.get("success", False)
                    status = resp.get("status", "")
                    if order_id or success or status in ["matched", "live", "open"]:
                        trade_results.append(f"✅ {cat[:14]} {side} ${actual_trade_amt}")
                        time.sleep(0.5)
                    else:
                        error = resp.get('error') or resp.get('errorMsg') or resp.get('message', 'No fill')
                        trade_results.append(f"⚠️ {cat[:14]} {side} No fill")
                except Exception as e:
                    trade_results.append(f"❌ {cat[:14]} {side} Error")
                    time.sleep(0.5)
        except Exception as e:
            print(f"\n❌ Trading setup failed: {e}")
            trade_results.append(f"❌ Setup failed")

    result = f"<b>Polymarket Sniper 🚀</b>\n\n{msg}{poly_section}"
    if trade_results:
        result += f"\n\n<b>🤖 Trades (${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)})</b>\n"
        result += "\n".join(trade_results[:10])
    elif AUTO_TRADE and opportunities:
        result += "\n\n<i>AUTO_TRADE enabled but no trades executed</i>"

    return result


# ─────────────────────────────────────────────
# BOT HANDLERS
# ─────────────────────────────────────────────

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    chat_id = message.chat.id
    current = user_market_selection.get(chat_id, None)
    current_label = MARKET_CONFIGS[current]['label'] if current else "None selected"
    actual_trade_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
    wallet_display = f"{WALLET_ADDRESS[:10]}...{WALLET_ADDRESS[-6:]}" if WALLET_ADDRESS else "Not set"

    welcome_text = (
        "<b>🎯 Polymarket Word Sniper Bot</b>\n\n"
        "Send a YouTube URL/ID, transcript text, or .txt file to analyze.\n\n"
        "<b>📌 Select your market first:</b>\n"
        "• /market1 — 🎬 MrBeast YouTube\n"
        "• /market2 — 🎙️ Joe Rogan Experience\n\n"
        f"<b>Current market:</b> {current_label}\n\n"
        f"<b>Settings:</b>\n"
        f"• Trade amount: ${actual_trade_amt} per opportunity\n"
        f"• Min trade: ${MIN_TRADE_AMOUNT}\n"
        f"• Wallet: {wallet_display}\n"
        f"• AutoTrade: {'✅ ENABLED' if AUTO_TRADE else '❌ DISABLED'}"
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')


@bot.message_handler(commands=['market1'])
def select_market1(message):
    chat_id = message.chat.id
    user_market_selection[chat_id] = "mrbeast"
    config = MARKET_CONFIGS["mrbeast"]
    bot.reply_to(
        message,
        f"✅ Market set to: <b>{config['label']}</b>\n"
        f"<code>{config['slug']}</code>\n\n"
        f"Now send a YouTube URL, transcript text, or .txt file.",
        parse_mode='HTML'
    )


@bot.message_handler(commands=['market2'])
def select_market2(message):
    chat_id = message.chat.id
    user_market_selection[chat_id] = "joerogan"
    config = MARKET_CONFIGS["joerogan"]
    bot.reply_to(
        message,
        f"✅ Market set to: <b>{config['label']}</b>\n"
        f"<code>{config['slug']}</code>\n\n"
        f"Now send a YouTube URL, transcript text, or .txt file.",
        parse_mode='HTML'
    )


@bot.message_handler(commands=['market'])
def show_market_menu(message):
    chat_id = message.chat.id
    current = user_market_selection.get(chat_id, None)
    current_label = MARKET_CONFIGS[current]['label'] if current else "None selected"
    text = (
        f"<b>🔀 Market Selection</b>\n\n"
        f"Current: <b>{current_label}</b>\n\n"
        f"Switch to:\n"
        f"• /market1 → 🎬 MrBeast YouTube\n"
        f"• /market2 → 🎙️ Joe Rogan Experience"
    )
    bot.reply_to(message, text, parse_mode='HTML')


def prompt_market_selection(message):
    bot.reply_to(
        message,
        "👋 Please select which market to trade on first:\n\n"
        "• /market1 → 🎬 MrBeast YouTube\n"
        "• /market2 → 🎙️ Joe Rogan Experience\n\n"
        "After selecting, resend your input.",
        parse_mode='HTML'
    )


@bot.message_handler(content_types=['text'])
def handle_text(message):
    chat_id = message.chat.id
    user_text = message.text.strip()
    if not user_text:
        return

    if chat_id not in user_market_selection:
        prompt_market_selection(message)
        return

    market_key = user_market_selection[chat_id]
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
                bot.reply_to(message, "No transcript found. Please paste the text manually.")
                return
        except Exception as e:
            bot.reply_to(message, f"❌ Fetch error: {str(e)[:200]}")
            return
    else:
        raw_text = user_text

    result_msg = format_results(raw_text.lower(), market_key)
    bot.send_message(chat_id, result_msg, parse_mode='HTML')


@bot.message_handler(content_types=['document'])
def handle_document(message):
    chat_id = message.chat.id
    doc = message.document

    if not (doc.mime_type == 'text/plain' or doc.file_name.lower().endswith('.txt')):
        bot.reply_to(message, "Please send a .txt file only.")
        return

    if chat_id not in user_market_selection:
        prompt_market_selection(message)
        return

    bot.reply_to(message, "📄 Processing...")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        transcript = downloaded.decode('utf-8', errors='replace')
        result_msg = format_results(transcript.lower(), user_market_selection[chat_id])
        bot.send_message(chat_id, result_msg, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

print(f"Bot starting...")
print(f"  Market 1 (MrBeast):   {POLYMARKET_SLUG_1}")
print(f"  Market 2 (Joe Rogan): {POLYMARKET_SLUG_2}")
print(f"  AUTO_TRADE: {AUTO_TRADE}")
print(f"  TRADE_AMOUNT: ${TRADE_AMOUNT}")
print(f"  MIN_TRADE_AMOUNT: ${MIN_TRADE_AMOUNT}")
print(f"  Actual trade amount: ${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)}")
print(f"  Wallet: {WALLET_ADDRESS[:10] if WALLET_ADDRESS else 'Not set'}...")
bot.infinity_polling()
