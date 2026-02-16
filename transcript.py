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
TRADE_AMOUNT = float(os.environ.get("TRADE_AMOUNT", "10"))  # Default $10
MIN_TRADE_AMOUNT = float(os.environ.get("MIN_TRADE_AMOUNT", "1"))  # Min $1 (configurable)
POLYMARKET_SLUG = os.environ.get("POLYMARKET_SLUG", "what-will-mrbeast-say-during-his-next-youtube-video").strip()

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)
if not POLYMARKET_SLUG:
    print("ERROR: POLYMARKET_SLUG not set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Derive address
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

# Transcript extraction
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

# Word groups & thresholds
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

thresholds = {
    "Dollar": 10,
    "Thousand/Million": 10,
    **{cat: 1 for cat in word_groups if cat not in ["Dollar", "Thousand/Million"]}
}

# UPDATED: More flexible market mapping to catch variations
market_mapping = {
    # Primary keywords (lowercase)
    "dollar": "Dollar",
    "thousand": "Thousand/Million",
    "million": "Thousand/Million",
    "challenge": "Challenge",
    "eliminated": "Eliminated",
    "trap": "Trap",
    "car": "Car/Supercar",
    "supercar": "Car/Supercar",
    "tesla": "Tesla/Lamborghini",
    "lamborghini": "Tesla/Lamborghini",
    "helicopter": "Helicopter/Jet",
    "jet": "Helicopter/Jet",
    "island": "Island",
    "mystery box": "Mystery Box",
    "massive": "Massive",
    "biggest": "World's Biggest/Largest",
    "largest": "World's Biggest/Largest",
    "beast games": "Beast Games",
    "feastables": "Feastables",
    "mrbeast": "MrBeast",
    "insane": "Insane",
    "subscribe": "Subscribe"
}

def match_market_to_category(question_lower):
    """Match Polymarket question to bot category using flexible matching"""
    
    # Direct keyword matching
    for keyword, category in market_mapping.items():
        if keyword in question_lower:
            return category
    
    # Special handling for compound terms
    if "world's biggest" in question_lower or "world's largest" in question_lower:
        return "World's Biggest/Largest"
    
    if ("tesla" in question_lower or "lamborghini" in question_lower):
        return "Tesla/Lamborghini"
    
    if ("car" in question_lower or "supercar" in question_lower):
        return "Car/Supercar"
    
    if ("helicopter" in question_lower or "jet" in question_lower):
        return "Helicopter/Jet"
    
    if ("thousand" in question_lower or "million" in question_lower):
        return "Thousand/Million"
    
    return None

# Polymarket fetch - IMPROVED with better matching
def get_polymarket_data():
    try:
        url = f"https://gamma-api.polymarket.com/events/slug/{POLYMARKET_SLUG}"
        print(f"\n🔍 Fetching from: {url}")
        response = requests.get(url, timeout=15)
        response.raise_for_status()
        event = response.json()
        markets = event.get("markets", [])
        
        if not markets:
            print("⚠️  No markets found in event!")
            return None, None
        
        print(f"✅ Found {len(markets)} markets")
        
        prices = {}
        token_ids = {}
        
        for market in markets:
            question = market.get("question", "")
            question_lower = question.lower()
            
            # Match to category
            matched_cat = match_market_to_category(question_lower)
            
            if not matched_cat:
                print(f"⚠️  No match for: {question}")
                continue
            
            print(f"✓ Matched: {question[:60]}... → {matched_cat}")
            
            # Get price
            outcome_prices = market.get("outcome_prices") or market.get("outcomePrices", [])
            if isinstance(outcome_prices, str):
                try:
                    outcome_prices = json.loads(outcome_prices)
                except:
                    outcome_prices = []
            
            if isinstance(outcome_prices, list) and len(outcome_prices) > 0:
                yes_price = float(outcome_prices[0])
                prices[matched_cat] = yes_price
                print(f"  Price: {yes_price:.4f}")
            
            # Get token ID - Multiple methods
            tokens = market.get("tokens", [])
            if tokens:
                for token in tokens:
                    if token.get("outcome", "").lower() == "yes":
                        token_id = token.get("token_id")
                        if token_id:
                            token_ids[matched_cat] = token_id
                            print(f"  Token: {token_id[:20]}...")
                            break
            
            # Fallback: clobTokenIds
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
                
                for idx, outcome in enumerate(outcomes):
                    if str(outcome).lower() == "yes":
                        if idx < len(clob_ids):
                            token_ids[matched_cat] = clob_ids[idx]
                            print(f"  Token (from clobTokenIds): {clob_ids[idx][:20]}...")
                        break
            
            # Last resort: condition_id
            if matched_cat not in token_ids:
                condition_id = market.get("condition_id")
                if condition_id:
                    token_ids[matched_cat] = condition_id
                    print(f"  Token (from condition_id): {condition_id[:20]}...")
        
        print(f"\n📊 Results: {len(prices)} markets with prices, {len(token_ids)} with token_ids")
        return prices, token_ids
        
    except Exception as e:
        print(f"❌ Polymarket fetch error: {e}")
        import traceback
        traceback.print_exc()
        return None, None

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
        
        meets_threshold = 0
        missing_price = []
        missing_token = []
        
        for cat, count in sorted_counts.items():
            thresh = thresholds.get(cat, 1)
            yes_p = prices.get(cat)
            token_id = token_ids.get(cat)
            status = ""
            
            # Check if meets threshold
            if count >= thresh:
                meets_threshold += 1
                
                if yes_p is None:
                    status = "⚠️ NO PRICE DATA"
                    missing_price.append(cat)
                elif token_id is None:
                    status = "⚠️ NO TOKEN_ID"
                    missing_token.append(cat)
                elif yes_p >= 0.95:
                    status = f"Too high (≥95¢)"
                elif yes_p < 0.95:
                    edge = (1.0 - yes_p) / yes_p * 100
                    status = f"SNIPABLE (~{edge:.0f}% edge)"
                    opportunities.append((cat, token_id, yes_p))
            
            yes_str = f"{yes_p:.2f}" if yes_p is not None else "N/A"
            poly_section += f"{cat:<30} {count:>6} {f'≥{thresh}':>9} {yes_str:>8} {status:>20}\n"
        
        poly_section += "-" * 80 + "\n"
        poly_section += f"\n<b>📊 SUMMARY:</b>"
        poly_section += f"\n  Categories meeting threshold: {meets_threshold}"
        poly_section += f"\n  Tradable opportunities: {len(opportunities)}"
        
        if missing_price:
            poly_section += f"\n  ⚠️ Missing price data: {len(missing_price)}"
            poly_section += f"\n     ({', '.join(missing_price[:4])}{'...' if len(missing_price) > 4 else ''})"
        
        if missing_token:
            poly_section += f"\n  ⚠️ Missing token_id: {len(missing_token)}"
            poly_section += f"\n     ({', '.join(missing_token[:4])}{'...' if len(missing_token) > 4 else ''})"

        if opportunities:
            poly_section += f"\n\n<b>🚨 {len(opportunities)} READY TO TRADE!</b>"
        elif meets_threshold > 0 and (missing_price or missing_token):
            poly_section += f"\n\n<b>⚠️ {meets_threshold} opportunities but {len(missing_price) + len(missing_token)} missing data!</b>"
        elif meets_threshold > 0:
            poly_section += f"\n\n<b>⚠️ {meets_threshold} opportunities but prices ≥95¢</b>"
        else:
            poly_section += "\n\n<b>No opportunities (counts below threshold).</b>"
        poly_section += "</pre>"
    else:
        poly_section += "\n<i>⚠️ Failed to fetch market data.</i>"

    # Auto-trading - IMPROVED
    if AUTO_TRADE and PRIVATE_KEY and opportunities:
        actual_trade_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
        trade_section += f"\n<b>🤖 AUTO_TRADING ACTIVE (${actual_trade_amt} per opp)</b>"
        
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
            trade_section += f"\nTrading from {address[:8]}...{address[-6:]}"
            
            # Check balance
            try:
                balance_resp = client.get_balance()
                usdc_balance = float(balance_resp.get("balance", 0)) / 1e6
                trade_section += f"\nUSDC Balance: ${usdc_balance:.2f}"
                
                if usdc_balance < actual_trade_amt * len(opportunities):
                    trade_section += f"\n⚠️ Insufficient balance for all {len(opportunities)} trades!"
            except Exception as e:
                trade_section += f"\n⚠️ Couldn't check balance: {str(e)[:50]}"
            
            for cat, token_id, yes_p in opportunities:
                try:
                    trade_section += f"\n\n📊 {cat}:"
                    trade_section += f"\n  Token: {token_id[:16]}..."
                    trade_section += f"\n  Price: {yes_p:.4f} (${yes_p:.2f})"
                    
                    # Try GTC order with slippage
                    try:
                        # 2% slippage tolerance
                        limit_price = min(yes_p * 1.02, 0.99)
                        shares = actual_trade_amt / limit_price
                        
                        args = OrderArgs(
                            token_id=token_id,
                            price=limit_price,
                            size=shares,
                            side=BUY,
                        )
                        signed = client.create_order(args)
                        resp = client.post_order(signed, OrderType.GTC)
                        
                        order_id = resp.get("order_id") or resp.get("orderID")
                        if order_id:
                            trade_section += f"\n  ✅ GTC order: {order_id[:12]}..."
                            time.sleep(1)
                            
                            # Check status
                            try:
                                order_status = client.get_order(order_id)
                                status = order_status.get("status", "unknown")
                                trade_section += f"\n  Status: {status}"
                            except:
                                pass
                        else:
                            error_msg = resp.get('error') or resp.get('message', 'Unknown error')
                            trade_section += f"\n  ⚠️ Order rejected: {error_msg[:60]}"
                    
                    except Exception as gtc_error:
                        error_str = str(gtc_error)[:80]
                        trade_section += f"\n  ❌ GTC failed: {error_str}"
                        
                        # Fallback: FOK with higher amount
                        try:
                            trade_section += f"\n  Trying FOK..."
                            args = MarketOrderArgs(
                                token_id=token_id,
                                amount=actual_trade_amt * 2,
                                side=BUY,
                                order_type=OrderType.FOK
                            )
                            signed = client.create_market_order(args)
                            resp = client.post_order(signed, OrderType.FOK)
                            
                            if "order_id" in resp or resp.get("status") in ["open", "matched"]:
                                trade_section += f"\n  ✅ FOK filled"
                            else:
                                error_msg = resp.get('error') or resp.get('message', 'No fill')
                                trade_section += f"\n  ❌ FOK: {error_msg[:60]}"
                        except Exception as fok_error:
                            trade_section += f"\n  ❌ FOK: {str(fok_error)[:60]}"
                
                except Exception as e:
                    trade_section += f"\n❌ {cat} failed: {str(e)[:100]}"
        
        except Exception as e:
            trade_section += f"\n❌ Trading setup failed: {str(e)[:200]}"
    
    elif AUTO_TRADE and not opportunities:
        trade_section += "\n<i>AUTO_TRADE=true but no opportunities detected.</i>"
    elif AUTO_TRADE and not PRIVATE_KEY:
        trade_section += "\n<i>AUTO_TRADE=true but PRIVATE_KEY not set.</i>"

    return f"<b>MrBeast Word Count + Sniper 🚀</b>\n\n{msg}{poly_section}{trade_section}"

# Handlers
@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    actual_trade_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
    welcome_text = (
        "<b>MrBeast Word Counter + Polymarket Sniper Bot! 👋</b>\n\n"
        "Send YouTube URL/ID, transcript text, or .txt file.\n\n"
        f"Market: {POLYMARKET_SLUG}\n"
        "• Fixed thresholds (Dollar & Thousand/Million: 10+, others: 1+)\n"
        "• Live Yes prices from Polymarket\n"
        f"• Trade amount: ${actual_trade_amt} per opp (set via TRADE_AMOUNT)\n"
        f"• Min trade: ${MIN_TRADE_AMOUNT} (set via MIN_TRADE_AMOUNT)\n"
        f"• Wallet: {WALLET_ADDRESS[:10]}...{WALLET_ADDRESS[-6:] if WALLET_ADDRESS else 'Not set'}\n"
        f"• AutoTrade: {'✅ ENABLED' if AUTO_TRADE else '❌ DISABLED'}"
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

print(f"Bot starting...")
print(f"  Slug: {POLYMARKET_SLUG}")
print(f"  AUTO_TRADE: {AUTO_TRADE}")
print(f"  TRADE_AMOUNT: ${TRADE_AMOUNT}")
print(f"  MIN_TRADE_AMOUNT: ${MIN_TRADE_AMOUNT}")
print(f"  Actual trade amount: ${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)}")
print(f"  Wallet: {WALLET_ADDRESS[:10]}...{WALLET_ADDRESS[-6:] if WALLET_ADDRESS else 'Not set'}")
bot.infinity_polling()
