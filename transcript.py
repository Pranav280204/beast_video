import os
import re
import requests
from flask import Flask, request, render_template_string

app = Flask(__name__)

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

# Word/phrase groups (same as before)
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

# HTML template
HTML = """
<!doctype html>
<html>
<head>
    <title>MrBeast Word Counter</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; }
        textarea { width: 100%; font-family: monospace; }
        table { border-collapse: collapse; width: 100%; margin-top: 30px; }
        th, td { border: 1px solid #ccc; padding: 10px; text-align: left; }
        th { background: #f0f0f0; }
        .error { color: red; font-weight: bold; }
    </style>
</head>
<body>
    <h1>MrBeast Word Counter</h1>
    <form method="post">
        <h3>Option 1: Auto-fetch Transcript (requires API_TOKEN in env)</h3>
        <label>Enter YouTube Video URL or ID:</label><br>
        <input type="text" name="video_input" size="70" placeholder="e.g. https://www.youtube.com/watch?v=ZFoNBxpXen4 or just ZFoNBxpXen4"><br><br>
        
        <h3>Option 2: Manual Transcript Paste</h3>
        <label>Paste the full transcript here:</label><br>
        <textarea name="manual_transcript" rows="15" placeholder="Paste transcript and submit..."></textarea><br><br>
        
        <input type="submit" value="Analyze Transcript">
    </form>
    
    {% if error %}
        <p class="error">{{ error }}</p>
    {% endif %}
    
    {% if results %}
        <h2>Word/Phrase Count Results</h2>
        <table>
            <tr><th>Category</th><th>Count</th></tr>
            {% for category, count in results.items() %}
                <tr><td>{{ category }}</td><td>{{ count }}</td></tr>
            {% endfor %}
            <tr><td><strong>TOTAL</strong></td><td><strong>{{ total }}</strong></td></tr>
        </table>
    {% endif %}
</body>
</html>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    results = None
    total = 0
    error = None
    
    if request.method == "POST":
        manual_transcript = request.form.get("manual_transcript", "").strip()
        video_input = request.form.get("video_input", "").strip()
        
        text = ""
        
        # Prefer manual transcript if provided
        if manual_transcript:
            text = manual_transcript.lower()
        elif video_input:
            video_id = extract_video_id(video_input)
            if not video_id:
                error = "Could not extract a valid YouTube video ID. Check the URL/ID."
            else:
                api_token = os.environ.get("API_TOKEN")
                if not api_token:
                    error = "API_TOKEN is not set in environment variables. Use manual mode instead."
                else:
                    url = "https://www.youtube-transcript.io/api/transcripts"
                    headers = {
                        "Authorization": f"Basic {api_token}",
                        "Content-Type": "application/json"
                    }
                    payload = {"ids": [video_id]}
                    
                    try:
                        response = requests.post(url, headers=headers, json=payload, timeout=30)
                        response.raise_for_status()
                        data = response.json()
                        
                        raw_text = extract_transcript_text(data)
                        if raw_text.strip():
                            text = raw_text.lower()
                        else:
                            error = "Transcript fetched but no text found (possibly no captions available)."
                    except requests.exceptions.HTTPError as http_err:
                        status = response.status_code
                        if status == 429:
                            error = "Rate limited (429). Wait and try again later."
                        elif status == 400:
                            error = "Bad request (400) – check your API token or video ID."
                        else:
                            error = f"API error: {http_err} (status {status})"
                    except Exception as e:
                        error = f"Error fetching transcript: {str(e)}"
        else:
            error = "Please provide either a video URL/ID or paste a transcript."
        
        # If we have text, count the words
        if text and not error:
            counts = {}
            for category, pattern in word_groups.items():
                counts[category] = len(re.findall(pattern, text))
            results = dict(sorted(counts.items()))
            total = sum(results.values())
    
    return render_template_string(HTML, results=results, total=total, error=error)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)