import os
import re
import requests
import telebot

# Get tokens from environment variables (set these in Railway)
BOT_TOKEN = os.environ.get("BOT_TOKEN")
API_TOKEN = os.environ.get("API_TOKEN")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set in environment variables!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# Robust video ID extraction
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

# Robust transcript text extraction
def extract_transcript_text(data):
    text_parts = []
    
    def collect_text(obj):
        if isinstance(obj, str):
            text_parts.append(obj)
        elif isinstance(obj, dict):
            for value in obj.values():
                if isinstance(value, str) and len(value.split()) > 5:
                    text_parts.append(value)
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict) and 'text' in item:
                            text_parts.append(item['text'])
                        elif isinstance(item, str):
                            text_parts.append(item)
                collect_text(value)
        elif isinstance(obj, list):
            for item in obj:
                collect_text(item)
    
    collect_text(data)
    return " ".join(text_parts)

# Word/phrase groups
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

def format_results(text_lower):
    counts = {}
    for category, pattern in word_groups.items():
        counts[category] = len(re.findall(pattern, text_lower))
    
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
    
    return f"<b>MrBeast Word Count Results</b>\n\n{msg}"

@bot.message_handler(commands=['start', 'help'])
def send_welcome(message):
    welcome_text = (
        "<b>Welcome to the MrBeast Word Counter Bot! 👋</b>\n\n"
        "Send me one of the following:\n"
        "• A <b>YouTube video URL</b> (or just the video ID) → I'll auto-fetch the transcript and count the buzzwords (requires API_TOKEN configured).\n"
        "• The <b>transcript text directly</b> (good for short transcripts).\n"
        "• A <b>.txt file</b> containing the full transcript (best for long MrBeast videos).\n\n"
        "The bot counts classic MrBeast words like Dollar, Challenge, Insane, Subscribe, etc."
    )
    bot.reply_to(message, welcome_text, parse_mode='HTML')

@bot.message_handler(content_types=['text'])
def handle_text(message):
    user_text = message.text.strip()
    if not user_text:
        bot.reply_to(message, "Please send a YouTube URL, transcript text, or a .txt file.")
        return
    
    video_id = extract_video_id(user_text)
    
    if video_id and API_TOKEN:
        bot.reply_to(message, "🔄 Fetching transcript from YouTube...")
        try:
            url = "https://www.youtube-transcript.io/api/transcripts"
            headers = {
                "Authorization": f"Basic {API_TOKEN}",
                "Content-Type": "application/json"
            }
            payload = {"ids": [video_id]}
            
            response = requests.post(url, headers=headers, json=payload, timeout=30)
            response.raise_for_status()
            data = response.json()
            
            raw_text = extract_transcript_text(data)
            if raw_text.strip():
                result_msg = format_results(raw_text.lower())
                bot.send_message(message.chat.id, result_msg, parse_mode='HTML')
                return
            else:
                bot.reply_to(message, "No transcript found (video may not have captions). Please paste it manually or send as .txt file.")
                return
        except Exception as e:
            bot.reply_to(message, f"❌ Fetch failed: {str(e)[:200]}\nPlease paste the transcript manually or send as .txt file.")
            return
    else:
        # Treat as manual transcript text
        if not API_TOKEN and video_id:
            bot.reply_to(message, "API_TOKEN not configured → can't auto-fetch. Treating your message as manual transcript.")
        result_msg = format_results(user_text.lower())
        bot.send_message(message.chat.id, result_msg, parse_mode='HTML')

@bot.message_handler(content_types=['document'])
def handle_document(message):
    doc = message.document
    if doc.mime_type != 'text/plain' and not doc.file_name.lower().endswith('.txt'):
        bot.reply_to(message, "Please send a plain text (.txt) file for the transcript.")
        return
    
    bot.reply_to(message, "📄 Processing your transcript file...")
    try:
        file_info = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        transcript = downloaded.decode('utf-8', errors='replace')
        
        result_msg = format_results(transcript.lower())
        bot.send_message(message.chat.id, result_msg, parse_mode='HTML')
    except Exception as e:
        bot.reply_to(message, f"❌ Error processing file: {str(e)}")

print("Bot is running...")
bot.infinity_polling()
