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

# UPDATED: More specific market mapping to avoid duplicates
market_mapping = {
    # Order matters - more specific matches first
    "subscribe": "Subscribe",
    "insane": "Insane",
    "beast games": "Beast Games",
    "feastables": "Feastables",
    "mrbeast": "MrBeast",
    "mr beast": "MrBeast",
    "mystery box": "Mystery Box",
    "world's biggest": "World's Biggest/Largest",
    "world's largest": "World's Biggest/Largest",
    "tesla": "Tesla/Lamborghini",
    "lamborghini": "Tesla/Lamborghini",
    "supercar": "Car/Supercar",
    "car": "Car/Supercar",
    "helicopter": "Helicopter/Jet",
    "jet": "Helicopter/Jet",
    "thousand": "Thousand/Million",
    "million": "Thousand/Million",
    "eliminated": "Eliminated",
    "challenge": "Challenge",
    "massive": "Massive",
    "island": "Island",
    "dollar": "Dollar",
    "trap": "Trap",
}

def match_market_to_category(question_lower):
    """Match Polymarket question to bot category by finding the quoted word/phrase"""
    
    # Strategy: Extract what word is being asked about (usually in quotes)
    # Example: 'Will MrBeast say "Dollar" 10+ times' -> look for "dollar"
    
    # Multi-word phrases (check these first as they're most specific)
    if '"beast games"' in question_lower or "'beast games'" in question_lower or "beast games" in question_lower:
        return "Beast Games"
    
    if '"mystery box"' in question_lower or "'mystery box'" in question_lower or "mystery box" in question_lower:
        return "Mystery Box"
    
    if ('"world\'s biggest"' in question_lower or '"world\'s largest"' in question_lower or 
        "world's biggest" in question_lower or "world's largest" in question_lower):
        return "World's Biggest/Largest"
    
    # Two-word vehicle phrases
    if '"tesla"' in question_lower or '"lamborghini"' in question_lower or ("tesla" in question_lower and "lamborghini" in question_lower):
        return "Tesla/Lamborghini"
    
    if '"helicopter"' in question_lower or '"jet"' in question_lower or ("helicopter" in question_lower and "jet" in question_lower):
        return "Helicopter/Jet"
    
    if '"car"' in question_lower or '"supercar"' in question_lower or ("car" in question_lower and "supercar" in question_lower):
        return "Car/Supercar"
    
    # Number words with 10+ context
    if ('"thousand"' in question_lower or '"million"' in question_lower) and "10+" in question_lower:
        return "Thousand/Million"
    
    # Dollar with 10+ context
    if '"dollar"' in question_lower and "10+" in question_lower:
        return "Dollar"
    
    # Single word checks - look for the word in quotes
    if '"subscribe"' in question_lower:
        return "Subscribe"
    
    if '"insane"' in question_lower:
        return "Insane"
    
    if '"feastables"' in question_lower:
        return "Feastables"
    
    if '"mrbeast"' in question_lower or '"mr beast"' in question_lower:
        return "MrBeast"
    
    if '"eliminated"' in question_lower:
        return "Eliminated"
    
    if '"challenge"' in question_lower:
        return "Challenge"
    
    if '"massive"' in question_lower:
        return "Massive"
    
    if '"island"' in question_lower:
        return "Island"
    
    if '"trap"' in question_lower:
        return "Trap"
    
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
        
        print(f"✅ Found {len(markets)} markets\n")
        
        prices = {}
        token_ids = {}
        matched_categories = set()  # Track which categories we've seen
        
        for market in markets:
            question = market.get("question", "")
            question_lower = question.lower()
            
            # Match to category
            matched_cat = match_market_to_category(question_lower)
            
            if not matched_cat:
                print(f"❌ No match: {question}")
                continue
            
            # Check for duplicates
            if matched_cat in matched_categories:
                print(f"⚠️  DUPLICATE MATCH for {matched_cat}: {question[:60]}...")
                print(f"   Skipping duplicate - keeping first match")
                continue
            
            matched_categories.add(matched_cat)
            print(f"✅ {matched_cat:<25} ← {question[:50]}...")
            
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
                print(f"   Price: {yes_price:.4f} ({yes_price*100:.1f}¢)")
            else:
                print(f"   ⚠️  NO PRICE DATA")
            
            # Get token ID - Multiple methods
            token_id = None
            
            # Method 1: tokens array
            tokens = market.get("tokens", [])
            if tokens:
                for token in tokens:
                    if token.get("outcome", "").lower() == "yes":
                        token_id = token.get("token_id")
                        if token_id:
                            token_id = str(token_id)
                            print(f"   Token: {token_id}")
                            break
            
            # Method 2: clobTokenIds
            if not token_id:
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
                            token_id = str(clob_ids[idx])
                            print(f"   Token: {token_id}")
                        break
            
            # Method 3: condition_id
            if not token_id:
                condition_id = market.get("condition_id")
                if condition_id:
                    token_id = str(condition_id)
                    print(f"   Token: {token_id}")
            
            # Store data
            if token_id:
                token_ids[matched_cat] = token_id
            else:
                print(f"   ⚠️  NO TOKEN_ID")
            
            print()  # Blank line between markets
        
        print(f"📊 Summary: {len(prices)} with prices, {len(token_ids)} with token_ids\n")
        
        # Debug: Show what's missing
        all_categories = set(word_groups.keys())
        found_categories = set(prices.keys()) | set(token_ids.keys())
        missing_categories = all_categories - found_categories
        
        if missing_categories:
            print(f"⚠️  Categories NOT found in Polymarket:")
            for cat in sorted(missing_categories):
                print(f"   - {cat}")
            print()
        
        # Show data issues
        for cat in found_categories:
            if cat not in prices:
                print(f"⚠️  {cat}: Has token_id but NO PRICE")
            elif cat not in token_ids:
                print(f"⚠️  {cat}: Has price but NO TOKEN_ID")
        
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
    
    # Compact counts (only show non-zero or meeting threshold)
    msg = "<b>📊 Word Counts</b>\n<pre>"
    for category, count in sorted_counts.items():
        thresh = thresholds.get(category, 1)
        if count >= thresh:
            msg += f"{category:<20} {count:>3} ✅\n"
        elif count > 0:
            msg += f"{category:<20} {count:>3}\n"
    msg += f"{'─'*25}\nTOTAL: {total}\n</pre>"

    prices, token_ids = get_polymarket_data()
    opportunities = []
    
    # Build opportunities list
    if prices:
        meets_threshold = 0
        missing_data = []
        
        for cat, count in sorted_counts.items():
            thresh = thresholds.get(cat, 1)
            yes_p = prices.get(cat)
            token_id = token_ids.get(cat)
            
            if count >= thresh:
                meets_threshold += 1
                
                if yes_p is None or token_id is None:
                    missing_data.append(cat)
                elif yes_p < 0.95:
                    opportunities.append((cat, token_id, yes_p))
        
        # Compact opportunities display
        poly_section = f"\n<b>🎯 Opportunities: {len(opportunities)}/{meets_threshold}</b>"
        
        if opportunities:
            poly_section += "\n<pre>"
            for cat, token_id, yes_p in opportunities:
                edge = int((1.0 - yes_p) / yes_p * 100)
                poly_section += f"{cat:<20} {yes_p:.2f} ~{edge}%\n"
            poly_section += "</pre>"
        
        if missing_data:
            poly_section += f"\n<i>⚠️ {len(missing_data)} missing data: {', '.join(missing_data[:3])}</i>"
    else:
        poly_section = "\n<i>⚠️ Failed to fetch market data.</i>"
        opportunities = []

    # Trading section (compact)
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
            
            # Log client info
            address = client.get_address()
            print(f"\n🔑 Trading wallet: {address}")
            
            # Check balance
            try:
                balance_resp = client.get_balance()
                usdc_balance = float(balance_resp.get("balance", 0)) / 1e6
                print(f"💰 USDC Balance: ${usdc_balance:.2f}")
                
                if usdc_balance < actual_trade_amt * len(opportunities):
                    print(f"⚠️  Insufficient balance! Need ${actual_trade_amt * len(opportunities):.2f}, have ${usdc_balance:.2f}")
                    trade_results.append(f"⚠️ Low balance: ${usdc_balance:.2f}")
            except Exception as e:
                print(f"⚠️  Balance check failed: {e}")
            
            for cat, token_id, yes_p in opportunities:
                try:
                    print(f"\n📊 Trading {cat}:")
                    print(f"   Token: {token_id}")
                    print(f"   Price: {yes_p:.4f}")
                    print(f"   Amount: ${actual_trade_amt}")
                    
                    # Use market order with USD amount (simple!)
                    args = MarketOrderArgs(
                        token_id=token_id,
                        amount=actual_trade_amt,  # USD amount
                        side=BUY,
                    )
                    
                    print(f"   Creating market order...")
                    signed = client.create_market_order(args)
                    
                    print(f"   Posting order...")
                    resp = client.post_order(signed, OrderType.FOK)  # FOK = Fill or Kill (immediate)
                    
                    print(f"   Response: {resp}")
                    
                    order_id = resp.get("order_id") or resp.get("orderID")
                    success = resp.get("success", False)
                    status = resp.get("status", "")
                    
                    if order_id or success or status in ["matched", "live", "open"]:
                        print(f"   ✅ Success! Status: {status}")
                        trade_results.append(f"✅ {cat[:15]} ${actual_trade_amt}")
                        time.sleep(0.5)  # Rate limit pause
                    else:
                        error = resp.get('error') or resp.get('errorMsg') or resp.get('message', 'No fill')
                        print(f"   ⚠️  Order failed: {error}")
                        trade_results.append(f"⚠️ {cat[:15]} No fill")
                
                except Exception as e:
                    error_str = str(e)
                    print(f"   ❌ Error: {error_str}")
                    
                    # Parse common errors
                    if "status_code=400" in error_str:
                        if "insufficient" in error_str.lower():
                            trade_results.append(f"❌ {cat[:15]} Low balance")
                        else:
                            trade_results.append(f"❌ {cat[:15]} API error")
                    else:
                        trade_results.append(f"❌ {cat[:15]} Error")
                    
                    time.sleep(0.5)
        
        except Exception as e:
            error_msg = str(e)
            print(f"\n❌ Trading setup failed: {error_msg}")
            trade_results.append(f"❌ Setup: {error_msg[:30]}")
    
    # Combine results
    result = f"<b>MrBeast Sniper 🚀</b>\n\n{msg}{poly_section}"
    
    if trade_results:
        result += f"\n\n<b>🤖 Trades (${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)})</b>\n"
        result += "\n".join(trade_results[:10])  # Limit to 10 trades shown
    elif AUTO_TRADE and opportunities:
        result += "\n\n<i>AUTO_TRADE enabled but no trades executed</i>"
    
    return result

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
