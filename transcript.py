import os
import re
import threading
import time
import json
import hashlib
import requests
import telebot
from telebot import types
from ecdsa import SigningKey, SECP256k1

# ─────────────────────────────────────────────
# OPTIONAL AUTO-TRADE IMPORTS
# ─────────────────────────────────────────────
if os.environ.get("AUTO_TRADE", "false").lower() == "true":
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import MarketOrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

# ─────────────────────────────────────────────
# ENVIRONMENT VARIABLES
# ─────────────────────────────────────────────
BOT_TOKEN          = os.environ.get("BOT_TOKEN")
API_TOKEN          = os.environ.get("API_TOKEN")           # youtube-transcript.io Basic token
PRIVATE_KEY        = os.environ.get("PRIVATE_KEY")
WALLET_ADDRESS     = os.environ.get("WALLET_ADDRESS")
AUTO_TRADE         = os.environ.get("AUTO_TRADE", "false").lower() == "true"
TRADE_AMOUNT       = float(os.environ.get("TRADE_AMOUNT", "10"))
MIN_TRADE_AMOUNT   = float(os.environ.get("MIN_TRADE_AMOUNT", "1"))
POLL_INTERVAL      = int(os.environ.get("POLL_INTERVAL", "2"))   # seconds between checks

# ─────────────────────────────────────────────
# YOUTUBE API KEY ROTATOR
# ─────────────────────────────────────────────

class YouTubeKeyRotator:
    def __init__(self, raw_env: str | None):
        self._keys  = [k.strip() for k in (raw_env or "").split(",") if k.strip()]
        self._index = 0
        self._lock  = threading.Lock()
        self._exhausted: set[int] = set()

    @property
    def available(self) -> bool:
        return bool(self._keys) and len(self._exhausted) < len(self._keys)

    @property
    def count(self) -> int:
        return len(self._keys)

    def next_key(self) -> str | None:
        with self._lock:
            if not self._keys:
                return None
            start = self._index
            while True:
                if self._index not in self._exhausted:
                    key = self._keys[self._index]
                    self._index = (self._index + 1) % len(self._keys)
                    return key
                self._index = (self._index + 1) % len(self._keys)
                if self._index == start:
                    return None  # all keys exhausted

    def mark_exhausted(self, key: str, chat_id: int | None = None):
        """Mark a key as quota-exceeded and optionally notify via Telegram."""
        with self._lock:
            try:
                idx = self._keys.index(key)
                self._exhausted.add(idx)
                remaining = len(self._keys) - len(self._exhausted)
                msg = (f"⚠️ YouTube key #{idx+1} quota exceeded. "
                       f"{remaining}/{len(self._keys)} keys remaining.")
                print(msg, flush=True)
                # Notify on Telegram if we have a chat_id
                if chat_id and bot:
                    try:
                        bot.send_message(chat_id, msg)
                    except Exception:
                        pass
                # If ALL keys now exhausted, send a louder warning
                if remaining == 0 and chat_id and bot:
                    try:
                        bot.send_message(
                            chat_id,
                            "🚨 <b>ALL YouTube API keys exhausted!</b>\n"
                            "Monitoring cannot continue until quota resets at midnight UTC.\n"
                            "Use /stop to clean up.",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except ValueError:
                pass

    def reset_exhausted(self):
        with self._lock:
            self._exhausted.clear()
            print("🔄 YouTube API key quotas reset.", flush=True)

    def status(self) -> str:
        with self._lock:
            total  = len(self._keys)
            active = total - len(self._exhausted)
            return f"{active}/{total} keys active"


YT_KEYS = YouTubeKeyRotator(os.environ.get("YOUTUBE_API_KEY"))

POLYMARKET_SLUG_1  = os.environ.get("POLYMARKET_SLUG",  "what-will-mrbeast-say-during-his-next-youtube-video").strip()
POLYMARKET_SLUG_2  = os.environ.get("POLYMARKET_SLUG_2","what-will-be-said-on-the-first-joe-rogan-experience-episode-of-the-week-february-22").strip()

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN not set!")
    exit(1)

bot = telebot.TeleBot(BOT_TOKEN)

# ─────────────────────────────────────────────
# CHANNEL METADATA
# ─────────────────────────────────────────────
CHANNELS = {
    "mrbeast":  {
        "channel_id":  "UCX6OQ3DkcsbYNE6H8uQQuVA",
        "handle":      "@MrBeast",
        "label":       "🎬 MrBeast YouTube",
    },
    "joerogan": {
        "channel_id":  "UCzQUP1qoWDoEbmsQxvdjxgQ",
        "handle":      "@joerogan",
        "label":       "🎙️ Joe Rogan Experience",
    },
    "souravjoshi": {
        "channel_id":  "UCjvgGbPPn-FgYeguc5nxG4A",
        "handle":      "@SouravJoshiVlogs",
        "label":       "🇮🇳 Sourav Joshi Vlogs (Testing)",
        "testing":     True,
    },
}

# ─────────────────────────────────────────────
# USER STATE
# ─────────────────────────────────────────────
user_state: dict[int, dict] = {}


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

def log(msg: str):
    import datetime
    ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


# ─────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────

def derive_address(private_key: str) -> str:
    pk = private_key[2:] if private_key.startswith("0x") else private_key
    sk  = SigningKey.from_string(bytes.fromhex(pk), curve=SECP256k1)
    vk  = sk.verifying_key
    pub = b"\x04" + vk.to_string()
    keccak = hashlib.sha3_256(pub).digest()
    return "0x" + keccak[-20:].hex()

if PRIVATE_KEY and not WALLET_ADDRESS:
    WALLET_ADDRESS = derive_address(PRIVATE_KEY)


def extract_video_id(user_input: str) -> str | None:
    patterns = [
        r"(?:v=|\/embed\/|\/shorts\/|\/watch\?v=|youtu\.be\/)([0-9A-Za-z_-]{11})",
        r"^([0-9A-Za-z_-]{11})$",
    ]
    for p in patterns:
        m = re.search(p, user_input)
        if m:
            return m.group(1)
    return None


def extract_transcript_text(data) -> str:
    parts = []
    def collect(obj):
        if isinstance(obj, str):
            parts.append(obj)
        elif isinstance(obj, dict):
            if "text" in obj and isinstance(obj["text"], str):
                parts.append(obj["text"])
            else:
                for v in obj.values():
                    collect(v)
        elif isinstance(obj, list):
            for item in obj:
                collect(item)
    collect(data)
    return " ".join(parts)


def fetch_transcript(video_id: str) -> str | None:
    if not API_TOKEN:
        return None
    try:
        url     = "https://www.youtube-transcript.io/api/transcripts"
        headers = {"Authorization": f"Basic {API_TOKEN}", "Content-Type": "application/json"}
        r       = requests.post(url, headers=headers, json={"ids": [video_id]}, timeout=30)
        r.raise_for_status()
        text = extract_transcript_text(r.json())
        return text if text.strip() else None
    except Exception as e:
        print(f"❌ Transcript fetch error: {e}")
        return None


def get_token_id_for_outcome(market, target_outcome: str) -> str | None:
    target = target_outcome.lower()
    for token in market.get("tokens", []):
        if token.get("outcome", "").lower() == target:
            tid = token.get("token_id")
            if tid is not None:
                return str(tid)
    outcomes_raw = market.get("outcomes", [])
    if isinstance(outcomes_raw, str):
        try:    outcomes = json.loads(outcomes_raw)
        except: outcomes = []
    else:
        outcomes = outcomes_raw or []
    clob_ids_raw = market.get("clobTokenIds", []) or market.get("clob_token_ids", [])
    if isinstance(clob_ids_raw, str):
        try:    clob_ids = json.loads(clob_ids_raw)
        except: clob_ids = []
    else:
        clob_ids = clob_ids_raw or []
    for idx, outcome in enumerate(outcomes):
        if str(outcome).lower() == target and idx < len(clob_ids):
            return str(clob_ids[idx])
    return None


# ─────────────────────────────────────────────
# YOUTUBE DATA API
# ─────────────────────────────────────────────

def _yt_get(url: str, params: dict, chat_id: int | None = None) -> "requests.Response | None":
    """
    YouTube Data API GET with key rotation.
    Retries with next key on 403 (quota exceeded).
    Sends Telegram alert on quota hit if chat_id provided.
    """
    if not YT_KEYS.available:
        log("⚠️  All YouTube API keys exhausted.")
        return None

    base_params = {k: v for k, v in params.items() if k != "key"}

    tried = 0
    while tried < YT_KEYS.count:
        key = YT_KEYS.next_key()
        if key is None:
            log("⚠️  No YouTube API keys available.")
            return None
        request_params = {**base_params, "key": key}
        try:
            r = requests.get(url, params=request_params, timeout=15)
            if r.status_code == 403:
                log(f"[YT] ⚠️  403 quota hit. Rotating key…")
                YT_KEYS.mark_exhausted(key, chat_id=chat_id)
                tried += 1
                continue
            if r.status_code == 400:
                log(f"[YT] ❌ 400 Bad Request — {r.text[:300]}")
                return None
            r.raise_for_status()
            return r
        except requests.exceptions.HTTPError as e:
            log(f"[YT] ❌ HTTP error: {e}")
            tried += 1
        except Exception as e:
            log(f"[YT] ❌ Request exception: {e}")
            return None
    return None


def _uploads_playlist_id(channel_id: str) -> str:
    """UC… → UU… (YouTube convention, zero API cost)"""
    return "UU" + channel_id[2:]


def parse_iso8601_duration(duration: str) -> int:
    """Convert ISO 8601 duration string to total seconds. Returns -1 for PT0S (unpopulated)."""
    if not duration or duration in ("PT0S", "P0D", ""):
        return -1   # ← KEY FIX: -1 means "unknown / not yet populated"
    m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", duration)
    if not m:
        return -1
    h, mi, s = (int(x or 0) for x in m.groups())
    total = h * 3600 + mi * 60 + s
    return total if total > 0 else -1


def get_video_count(channel_id: str, chat_id: int | None = None) -> int | None:
    """
    Stage 1 tripwire — returns total video count for a channel.
    Costs 1 quota unit. Updates instantly on new upload.
    """
    if not YT_KEYS.available:
        return None
    try:
        log(f"[YT] channels.statistics → {channel_id}")
        r = _yt_get(
            "https://www.googleapis.com/youtube/v3/channels",
            {"id": channel_id, "part": "statistics"},
            chat_id=chat_id,
        )
        if r is None:
            return None
        items = r.json().get("items", [])
        if not items:
            return None
        count = int(items[0]["statistics"]["videoCount"])
        log(f"[YT] videoCount = {count}")
        return count
    except Exception as e:
        import traceback
        log(f"[YT] ❌ get_video_count error: {e}\n{traceback.format_exc()}")
        return None


def get_latest_video(channel_id: str, chat_id: int | None = None) -> dict | None:
    """
    Stage 2 fetch — called ONLY when videoCount increases.
    Returns latest non-Shorts video dict {video_id, title} or None.
    Costs 2 quota units (playlistItems + videos batch).

    FIX: If duration == -1 (PT0S / not yet populated by YouTube),
    we wait 20 seconds and retry ONCE. If still -1, we assume it is
    NOT a Short and return it — a 10-minute video should never be dropped
    just because the API hasn't finished processing its metadata.
    """
    if not YT_KEYS.available:
        return None

    def _fetch_candidates():
        playlist_id = _uploads_playlist_id(channel_id)
        log(f"[YT] playlistItems.list → {playlist_id}")
        r = _yt_get(
            "https://www.googleapis.com/youtube/v3/playlistItems",
            {"playlistId": playlist_id, "part": "snippet", "maxResults": 8},
            chat_id=chat_id,
        )
        if r is None:
            return []
        items = r.json().get("items", [])
        log(f"[YT] playlistItems OK — {len(items)} items")
        candidates = []
        for item in items:
            snippet = item.get("snippet", {})
            rid     = snippet.get("resourceId", {})
            vid_id  = rid.get("videoId")
            title   = snippet.get("title", "")
            if vid_id:
                candidates.append((vid_id, title))
                log(f"[YT]   candidate: {vid_id} | {title}")
        return candidates

    def _fetch_durations(candidates):
        if not candidates:
            return {}
        vid_ids_str = ",".join(v for v, _ in candidates)
        r2 = _yt_get(
            "https://www.googleapis.com/youtube/v3/videos",
            {"id": vid_ids_str, "part": "contentDetails"},
            chat_id=chat_id,
        )
        durations: dict[str, int] = {}
        if r2:
            for v_item in r2.json().get("items", []):
                vid  = v_item["id"]
                dur  = v_item["contentDetails"]["duration"]
                secs = parse_iso8601_duration(dur)
                durations[vid] = secs
                log(f"[YT]   duration: {vid} → {dur} ({secs}s)")
        else:
            log("[YT] ⚠️  videos.list failed — will treat all as non-Shorts")
        return durations

    try:
        candidates = _fetch_candidates()
        if not candidates:
            log("[YT] No candidates from playlist.")
            return None

        durations = _fetch_durations(candidates)

        # ── Check if any duration came back as -1 (PT0S / unpopulated) ──
        # This is the core bug fix: YouTube takes 10-40s to populate
        # contentDetails.duration after an upload. If we see -1, wait and retry.
        unpopulated = [vid for vid, _ in candidates if durations.get(vid, -1) == -1]
        if unpopulated:
            log(f"[YT] ⚠️  {len(unpopulated)} video(s) have PT0S duration (metadata not ready). "
                f"Waiting 20s then retrying…")
            time.sleep(20)
            durations = _fetch_durations(candidates)   # retry once

        # ── Return first non-Short ──────────────────────────────────────
        for vid_id, title in candidates:
            secs = durations.get(vid_id, -1)

            if secs == -1:
                # Still unpopulated after retry → ASSUME NOT A SHORT.
                # A real Short would have finished processing by now.
                # A long video that just got uploaded may still lag.
                # Better to analyse a Short by mistake than to miss a long video.
                log(f"[YT]   {vid_id}: duration still unknown → treating as NON-Short ✅")
                return {"video_id": vid_id, "title": title}

            is_sh = secs <= 60
            log(f"[YT]   {vid_id}: {secs}s → {'SHORT ❌' if is_sh else 'VIDEO ✅'}")
            if not is_sh:
                log(f"[YT] ✅ Selected: {vid_id} | {title}")
                return {"video_id": vid_id, "title": title}

        log(f"[YT] All {len(candidates)} candidates are confirmed Shorts (duration ≤ 60s).")
        return None

    except Exception as e:
        import traceback
        log(f"[YT] ❌ get_latest_video error: {e}\n{traceback.format_exc()}")
        return None


# ─────────────────────────────────────────────
# COUNTING ENGINE
# ─────────────────────────────────────────────

def count_matches(text_lower: str, category_spec: tuple) -> int:
    if category_spec[0] == "simple":
        _, pattern = category_spec
        return len(re.findall(pattern, text_lower, re.IGNORECASE))
    elif category_spec[0] == "fullname":
        _, full_pat, fallback_pat = category_spec
        full_matches = re.findall(full_pat, text_lower, re.IGNORECASE)
        scrubbed = re.sub(full_pat, "XXFULLNAMEXX", text_lower, flags=re.IGNORECASE)
        leftover = re.findall(fallback_pat, scrubbed, re.IGNORECASE)
        return len(full_matches) + len(leftover)
    return 0


# ─────────────────────────────────────────────
# MARKET CONFIGS
# ─────────────────────────────────────────────

MARKET_CONFIGS = {
    "mrbeast": {
        "slug":  POLYMARKET_SLUG_1,
        "label": "🎬 MrBeast YouTube",
        "channel_key": "mrbeast",
        "testing": False,
        "word_groups": {
            "Dollar":                   ("simple",
                r"\bdollar'?s?\b"
                r"|\$\s*[\d,]+(?:\.\d+)?"
                r"|\$\s*(?:one|two|three|four|five|six|seven|eight|nine|ten|"
                r"twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|"
                r"hundred|thousand|million|billion|trillion)"
            ),
            "Thousand/Million":         ("simple", r"\b(thousand|million|billion)'?s?\b"),
            "Challenge":                ("simple", r"\bchallenge'?s?\b"),
            "Eliminated":               ("simple", r"\beliminated'?s?\b"),
            "Trap":                     ("simple",
                r"\btrap'?s?\b"
                r"|\btrapdoor'?s?\b"
                r"|\b(?:death|fire|fly|rat|mouse|man|speed|tourist|poverty|"
                r"sun|net|steam|wind|cold|heat|love|mind|speed)trap'?s?\b"
                r"|\bbooby[\s\-]trap'?s?\b"
            ),
            "Car/Supercar":             ("simple", r"\b\w*car'?s?\b"),
            "Tesla/Lamborghini":        ("simple", r"\b(tesla|lamborghini)'?s?\b"),
            "Helicopter/Jet":           ("simple", r"\bhelicopter'?s?\b|\bjet\w*'?s?\b"),
            "Island":                   ("simple", r"\bisland'?s?\b"),
            "Mystery Box":              ("simple", r"\bmystery\s+box(?:es|'?s)?\b"),
            "Massive":                  ("simple", r"\bmassive'?s?\b"),
            "World's Biggest/Largest":  ("simple", r"\bworld'?s?\s+(biggest|largest)\b"),
            "Beast Games":              ("simple", r"\bbeast\s+games?\b"),
            "Feastables":               ("simple", r"\bfeastables?'?s?\b"),
            "MrBeast":                  ("simple", r"\bmr\.?\s*beast'?s?\b"),
            "Insane":                   ("simple", r"\binsane'?s?\b"),
            "Subscribe":                ("simple", r"\bsubscribe'?s?\b"),
            "Cocoa":                    ("simple", r"\bcocoa'?s?\b"),
            "Chocolate":                ("simple", r"\bchocolate'?s?\b"),
        },
        "thresholds": {"Dollar": 10, "Thousand/Million": 10, "Cocoa": 3, "Chocolate": 3},
        "default_threshold": 1,
        "match_market": "mrbeast",
    },

    "joerogan": {
        "slug":  POLYMARKET_SLUG_2,
        "label": "🎙️ Joe Rogan Experience",
        "channel_key": "joerogan",
        "testing": False,
        "word_groups": {
            "People":               ("simple", r"\bpeople'?s?\b"),
            "Fuck/Fucking":         ("simple",
                r"\bf+u+c+k(?:s|'?s|ing|ed|er'?s?|wit'?s?|head'?s?)?\b"
                r"|\[\s*__\s*\]"
                r"|mother\[\s*__\s*\](?:ing|er'?s?)?"),
            "Really":               ("simple", r"\breally\b"),
            "Interesting":          ("simple", r"\binteresting\b"),
            "Jamie":                ("simple", r"\bjamie'?s?\b"),
            "Dow Jones":            ("simple", r"\bdow\s+jones'?\b"),
            "Pam/Bondi":            ("fullname", r"\bpam\s+bondi'?s?\b", r"\b(?:pam|bondi)'?s?\b"),
            "Trump/MAGA":           ("simple", r"\btrump(?:'?s|ism|ist|ian)?\b|\bmaga\b"),
            "Epstein":              ("simple", r"\bepstein'?s?\b"),
            "DHS":                  ("simple", r"\bdhs'?s?\b"),
            "Congress":             ("simple", r"\bcongress(?:'?s|ional|man|woman|person|people)?\b"),
            "Shutdown":             ("simple", r"\bshutdowns?'?s?\b|\bshut\s+down\b"),
            "Shooting":             ("simple", r"\bshooting'?s?\b"),
            "War":                  ("simple", r"\bwars?'?s?\b|\bwar(?:fare|time|zone|lord|head|monger|torn|path|ring)'?s?\b"),
            "Cocaine":              ("simple", r"\bcocaine'?s?\b"),
            "Fentanyl":             ("simple", r"\bfentanyl'?s?\b"),
            "Terrorist/Terrorism":  ("simple", r"\bterrorists?'?s?\b|\bterrorism'?s?\b|\b(?:counter|anti)[\s\-]terror(?:ist'?s?|ism'?s?)?\b"),
            "Super Bowl/Big Game":  ("simple", r"\bsuper\s+bowl'?s?\b|\bbig\s+game'?s?\b"),
            "Olympic/Olympics":     ("simple", r"\bolympic'?s?\b"),
            "Valentine":            ("simple", r"\bvalentine'?s?\b"),
        },
        "thresholds": {
            "People":      100,
            "Fuck/Fucking": 20,
            "Really":       10,
            "Interesting":   5,
            "Jamie":         5,
        },
        "default_threshold": 1,
        "match_market": "joerogan",
    },

    "souravjoshi": {
        "slug":  None,
        "label": "🇮🇳 Sourav Joshi Vlogs (Testing)",
        "channel_key": "souravjoshi",
        "testing": True,
        "word_groups": {
            "अवंतिका": ("simple", r"अवंतिका"),
        },
        "thresholds": {},
        "default_threshold": 1,
        "match_market": "souravjoshi",
    },
}


# ─────────────────────────────────────────────
# MARKET MATCHING FUNCTIONS
# ─────────────────────────────────────────────

def match_market_mrbeast(q: str) -> str | None:
    ql = q.lower()
    m = re.search(r"\bsay\s+(.+?)(?:\s+\d+\+\s+times?|\s+during\b)", ql)
    term = m.group(1).strip() if m else ql

    if "beast games"                      in term: return "Beast Games"
    if "mystery box"                      in term: return "Mystery Box"
    if "world" in term and ("biggest" in term or "largest" in term):
                                                   return "World's Biggest/Largest"
    if "tesla"        in term:                     return "Tesla/Lamborghini"
    if "lamborghini"  in term:                     return "Tesla/Lamborghini"
    if "helicopter"   in term:                     return "Helicopter/Jet"
    if "jet"          in term:                     return "Helicopter/Jet"
    if "thousand"     in term or "million" in term or "billion" in term:
                                                   return "Thousand/Million"
    if "dollar"       in term:                     return "Dollar"
    if "subscribe"    in term:                     return "Subscribe"
    if "insane"       in term:                     return "Insane"
    if "feastables"   in term:                     return "Feastables"
    if "cocoa"        in term:                     return "Cocoa"
    if "chocolate"    in term:                     return "Chocolate"
    if "mr" in term and "beast" in term:           return "MrBeast"
    if "mrbeast"      in term:                     return "MrBeast"
    if "eliminated"   in term:                     return "Eliminated"
    if "challenge"    in term:                     return "Challenge"
    if "massive"      in term:                     return "Massive"
    if "island"       in term:                     return "Island"
    if "trap"         in term:                     return "Trap"
    if "car"          in term:                     return "Car/Supercar"
    return None


def match_market_joerogan(q):
    ql = q.lower()
    if "valentine" in ql:                                   return "Valentine"
    if "people" in ql and "100+" in ql:                     return "People"
    if ("fuck" in ql or "fucking" in ql) and "20+" in ql:  return "Fuck/Fucking"
    if "really" in ql and "10+" in ql:                      return "Really"
    if "interesting" in ql and "5+" in ql:                  return "Interesting"
    if "jamie" in ql and "5+" in ql:                        return "Jamie"
    if "dow jones" in ql or ("dow" in ql and "jones" in ql): return "Dow Jones"
    if "pam" in ql or "bondi" in ql:                        return "Pam/Bondi"
    if "trump" in ql or "maga" in ql:                       return "Trump/MAGA"
    if "epstein" in ql:                                      return "Epstein"
    if "dhs"     in ql:                                      return "DHS"
    if "congress" in ql:                                     return "Congress"
    if "shutdown" in ql or "shut down" in ql:               return "Shutdown"
    if "shooting" in ql:                                     return "Shooting"
    if "war"      in ql:                                     return "War"
    if "cocaine"  in ql:                                     return "Cocaine"
    if "fentanyl" in ql:                                     return "Fentanyl"
    if "terrorist" in ql or "terrorism" in ql:              return "Terrorist/Terrorism"
    if "super bowl" in ql or "big game" in ql:              return "Super Bowl/Big Game"
    if "olympic"  in ql:                                     return "Olympic/Olympics"
    return None


def match_market_souravjoshi(q):
    return None


MARKET_MATCHERS = {
    "mrbeast":      match_market_mrbeast,
    "joerogan":     match_market_joerogan,
    "souravjoshi":  match_market_souravjoshi,
}


# ─────────────────────────────────────────────
# POLYMARKET DATA FETCH
# ─────────────────────────────────────────────

def get_polymarket_data(slug, match_fn, word_groups):
    if not slug:
        return None, None
    try:
        url  = f"https://gamma-api.polymarket.com/events/slug/{slug}"
        print(f"\n🔍 Fetching: {url}")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        markets = resp.json().get("markets", [])
        if not markets:
            return None, None

        prices, token_ids, matched_cats = {}, {}, set()
        for market in markets:
            question = market.get("question", "")
            cat = match_fn(question)
            if not cat or cat in matched_cats:
                continue
            matched_cats.add(cat)
            op = market.get("outcome_prices") or market.get("outcomePrices", [])
            if isinstance(op, str):
                try: op = json.loads(op)
                except: op = []
            if isinstance(op, list) and op:
                prices[cat] = float(op[0])
            yes_tok = get_token_id_for_outcome(market, "yes")
            no_tok  = get_token_id_for_outcome(market, "no")
            token_ids[cat] = {"yes": yes_tok, "no": no_tok}
        return prices, token_ids
    except Exception as e:
        print(f"❌ Polymarket error: {e}")
        return None, None


# ─────────────────────────────────────────────
# FORMAT RESULTS
# ─────────────────────────────────────────────

def format_results(text: str, market_key: str) -> str:
    config      = MARKET_CONFIGS[market_key]
    word_groups = config["word_groups"]
    thresh_map  = config.get("thresholds", {})
    default_th  = config.get("default_threshold", 1)
    slug        = config["slug"]
    match_fn    = MARKET_MATCHERS[config["match_market"]]
    is_testing  = config.get("testing", False)

    thresholds = {cat: thresh_map.get(cat, default_th) for cat in word_groups}
    text_lower = text.lower()
    counts     = {cat: count_matches(text_lower, spec) for cat, spec in word_groups.items()}
    sorted_cnt = dict(sorted(counts.items()))
    total      = sum(sorted_cnt.values())

    msg = f"<b>📊 Word Counts — {config['label']}</b>\n<pre>"
    for cat, count in sorted_cnt.items():
        thresh = thresholds.get(cat, 1)
        if count >= thresh:
            msg += f"{cat:<28} {count:>4} ✅\n"
        elif count > 0:
            msg += f"{cat:<28} {count:>4} ❌\n"
        else:
            msg += f"{cat:<28} {count:>4} ➖\n"
    msg += f"{'─'*34}\nTOTAL: {total}\n</pre>"

    if is_testing:
        return f"<b>🧪 TEST MODE — {config['label']}</b>\n\n{msg}\n<i>No Polymarket trades (testing only).</i>"

    prices, token_ids = get_polymarket_data(slug, match_fn, word_groups)

    tradeable, no_token, no_market = [], [], []

    for cat, count in sorted_cnt.items():
        thresh  = thresholds.get(cat, 1)
        yes_p   = prices.get(cat) if prices else None

        if yes_p is None:
            no_market.append(cat)
            continue

        no_p    = 1.0 - yes_p
        tokens  = token_ids.get(cat, {})
        yes_tok = tokens.get("yes")
        no_tok  = tokens.get("no")

        if count >= thresh:
            side, p, tok = "Yes", yes_p, yes_tok
        else:
            side, p, tok = "No",  no_p,  no_tok

        if p < 0.95:
            edge = int((1.0 - p) / p * 100) if p > 0 else 999
            if tok:
                tradeable.append((cat, side, tok, p, edge))
            else:
                no_token.append((cat, side, p, edge))
        else:
            no_token.append((cat, side, p, 0))

    total_shown = len(tradeable) + len(no_token) + len(no_market)
    poly_section = f"\n<b>🎯 All {total_shown} outcomes ({len(tradeable)} tradeable)</b>"

    if tradeable:
        poly_section += "\n<pre>"
        for cat, side, _, price, edge in tradeable:
            poly_section += f"{cat:<28} {side:<4} {price:.2f}  ~{edge}%\n"
        poly_section += "</pre>"

    if no_token:
        poly_section += "\n<b>⚠️ No token (price known):</b>\n<pre>"
        for cat, side, price, edge in no_token:
            poly_section += f"{cat:<28} {side:<4} {price:.2f}  ~{edge}%\n"
        poly_section += "</pre>"

    if no_market:
        poly_section += f"\n<b>❓ No market data:</b> {', '.join(no_market)}"

    opportunities = tradeable

    trade_results = []
    if AUTO_TRADE and PRIVATE_KEY and opportunities:
        import datetime
        def _ist() -> str:
            utc = datetime.datetime.utcnow()
            ist = utc + datetime.timedelta(hours=5, minutes=30)
            return ist.strftime("%H:%M:%S IST")

        actual_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
        t_trades_start = _ist()
        try:
            pk     = PRIVATE_KEY[2:] if PRIVATE_KEY.startswith("0x") else PRIVATE_KEY
            client = ClobClient(
                host="https://clob.polymarket.com",
                chain_id=137,
                key=pk,
                signature_type=1,
                funder=WALLET_ADDRESS or None,
            )
            client.set_api_creds(client.create_or_derive_api_creds())
            for cat, side, tok, price, edge in opportunities:
                try:
                    t_before = datetime.datetime.utcnow()
                    args     = MarketOrderArgs(token_id=tok, amount=actual_amt, side=BUY)
                    signed   = client.create_market_order(args)
                    resp     = client.post_order(signed, OrderType.FOK)
                    t_after  = datetime.datetime.utcnow()
                    elapsed  = (t_after - t_before).total_seconds()
                    trade_ts = _ist()
                    status   = resp.get("status", "")
                    if resp.get("order_id") or resp.get("success") or status in ("matched","live","open"):
                        trade_results.append(f"✅ {cat[:16]:<16} {side}  ${actual_amt}  @{trade_ts}  ({elapsed:.2f}s)")
                    else:
                        trade_results.append(f"⚠️ {cat[:16]:<16} {side}  No fill  @{trade_ts}  ({elapsed:.2f}s)")
                    time.sleep(0.5)
                except Exception as ex:
                    trade_results.append(f"❌ {cat[:16]:<16} {side}  Error: {str(ex)[:40]}  @{_ist()}")
                    time.sleep(0.5)
        except Exception as e:
            trade_results.append(f"❌ Setup failed: {str(e)[:60]}")

    result = f"<b>Polymarket Sniper 🚀</b>\n\n{msg}{poly_section}"
    if trade_results:
        result += f"\n\n<b>🤖 Trades (${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)}) — started {t_trades_start}</b>\n<pre>"
        result += "\n".join(trade_results[:10])
        result += "</pre>"
    return result


# ─────────────────────────────────────────────
# AUTO-MONITOR THREAD
#
# CHANGES vs original:
#  • No heartbeat messages — silence until a new video is found
#  • Polling STOPS immediately when videoCount increases (before transcript fetch)
#  • Duration=PT0S → waits 20s and retries → defaults to NON-Short if still unknown
#  • Quota errors send Telegram alerts
#  • 2-second poll interval recommended
# ─────────────────────────────────────────────

def monitor_channel(chat_id: int, market_key: str, stop_event: threading.Event):
    import datetime
    import traceback

    def ist_now() -> str:
        utc = datetime.datetime.utcnow()
        ist = utc + datetime.timedelta(hours=5, minutes=30)
        return ist.strftime("%d %b %Y  %H:%M:%S IST")

    try:
        config     = MARKET_CONFIGS[market_key]
        chan_key   = config["channel_key"]
        channel_id = CHANNELS[chan_key]["channel_id"]
        chan_label = config["label"]

        log(f"[Monitor] Thread started — market={market_key} channel={channel_id} chat={chat_id}")

        if not YT_KEYS.available:
            msg = "❌ No YouTube API keys available. Cannot monitor."
            log(f"[Monitor] {msg}")
            bot.send_message(chat_id, msg)
            return

        # ── Seed: record current videoCount as baseline ──────────────────
        log(f"[Monitor] Seeding videoCount…")
        last_count = get_video_count(channel_id, chat_id=chat_id)

        log(f"[Monitor] Seeding latest video ID…")
        seed_vid = get_latest_video(channel_id, chat_id=chat_id)
        last_vid_id = seed_vid["video_id"] if seed_vid else None

        log(f"[Monitor] Seed — videoCount={last_count}  latest={last_vid_id}")

        bot.send_message(
            chat_id,
            f"👁 <b>Monitoring started</b> — {chan_label}\n"
            f"🕐 <b>Started:</b> <code>{ist_now()}</code>\n"
            f"🔑 Keys: <code>{YT_KEYS.status()}</code>\n"
            f"⏱ Polling every <b>{POLL_INTERVAL}s</b>\n"
            f"📊 Seeded count: <code>{last_count}</code>\n"
            f"📌 Seeded video: <code>{last_vid_id or 'none'}</code>\n\n"
            f"🔕 <i>No further messages until a new video is detected.</i>\n"
            f"Use /stop to cancel.",
            parse_mode="HTML",
        )

        poll_count = 0

        # ── Main poll loop ──────────────────────────────────────────────
        while not stop_event.is_set():
            stop_event.wait(POLL_INTERVAL)
            if stop_event.is_set():
                log("[Monitor] Stop event received — exiting.")
                break

            poll_count += 1

            try:
                # ── Stage 1: cheap videoCount check ──────────────────────
                new_count = get_video_count(channel_id, chat_id=chat_id)

                if new_count is None:
                    # API failed — quota hit notification is sent inside mark_exhausted
                    log(f"[Monitor] Poll #{poll_count} — videoCount API failed")
                    if not YT_KEYS.available:
                        log("[Monitor] All keys exhausted — stopping monitor.")
                        stop_event.set()
                        break
                    continue

                log(f"[Monitor] Poll #{poll_count} — count={new_count} (was {last_count})")

                if last_count is not None and new_count <= last_count:
                    # No change — silent (no heartbeat)
                    continue

                # ── Count increased → NEW VIDEO! ──────────────────────────
                # STOP POLLING IMMEDIATELY so we don't burn quota during fetch
                stop_event.set()

                t_detected = ist_now()
                diff = (new_count - last_count) if last_count else 1
                log(f"[Monitor] 🆕 videoCount {last_count}→{new_count} (+{diff}) at {t_detected}")

                bot.send_message(
                    chat_id,
                    f"🔔 <b>New upload detected!</b>\n"
                    f"🕐 <code>{t_detected}</code>\n"
                    f"📊 videoCount: <code>{last_count} → {new_count}</code>\n"
                    f"⏳ Fetching video details…",
                    parse_mode="HTML",
                )

                # ── Stage 2: identify the new video ──────────────────────
                latest = get_latest_video(channel_id, chat_id=chat_id)

                if latest is None:
                    log(f"[Monitor] ⚠️  get_latest_video returned None")
                    bot.send_message(
                        chat_id,
                        f"⚠️ Count increased but couldn't identify the new non-Short video.\n"
                        f"🕐 <code>{ist_now()}</code>\n"
                        f"Use /market to restart monitoring.",
                        parse_mode="HTML",
                    )
                    break

                vid_id = latest["video_id"]
                title  = latest["title"]

                if vid_id == last_vid_id:
                    log(f"[Monitor] ⚠️  Same vid as before ({vid_id}) — likely a Short was uploaded")
                    bot.send_message(
                        chat_id,
                        f"⚠️ Count +1 but latest non-Short is unchanged: <code>{vid_id}</code>\n"
                        f"Likely a Short was uploaded — monitoring stopped.\n"
                        f"Use /market to restart.",
                        parse_mode="HTML",
                    )
                    break

                t_video_detected = ist_now()
                log(f"[Monitor] ✅ New video confirmed: {vid_id} | {title}")

                bot.send_message(
                    chat_id,
                    f"🆕 <b>New video confirmed!</b>\n"
                    f"🕐 <b>Confirmed:</b> <code>{t_video_detected}</code>\n"
                    f"🎬 <a href='https://youtu.be/{vid_id}'>{title}</a>\n"
                    f"⏳ Fetching transcript…",
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )

                # ── Transcript ────────────────────────────────────────────
                t_tr_start = datetime.datetime.utcnow()
                transcript = fetch_transcript(vid_id)
                t_tr_end   = datetime.datetime.utcnow()
                tr_secs    = (t_tr_end - t_tr_start).total_seconds()

                if not transcript:
                    log(f"[Monitor] ⚠️  Transcript not ready for {vid_id}")
                    bot.send_message(
                        chat_id,
                        f"⚠️ <b>Transcript not ready yet</b>\n"
                        f"🕐 <code>{ist_now()}</code>\n"
                        f"Video: <a href='https://youtu.be/{vid_id}'>{title}</a>\n\n"
                        f"Try again manually with /market → paste the URL once transcript is available.",
                        parse_mode="HTML",
                        disable_web_page_preview=True,
                    )
                    break

                t_tr_done = ist_now()
                log(f"[Monitor] ✅ Transcript fetched in {tr_secs:.1f}s ({len(transcript):,} chars)")

                # ── Analysis + trades ─────────────────────────────────────
                t_an_start = datetime.datetime.utcnow()
                result     = format_results(transcript, market_key)
                t_an_end   = datetime.datetime.utcnow()
                an_secs    = (t_an_end - t_an_start).total_seconds()
                total_secs = (t_an_end - t_tr_start).total_seconds()

                timing_footer = (
                    f"\n\n<b>⏱ Pipeline timing</b>\n<pre>"
                    f"Detected     : {t_detected}\n"
                    f"Video ID     : {t_video_detected}\n"
                    f"Transcript   : {t_tr_done}\n"
                    f"Analysis done: {ist_now()}\n"
                    f"{'─'*34}\n"
                    f"Transcript : {tr_secs:.1f}s\n"
                    f"Analysis   : {an_secs:.1f}s\n"
                    f"Total      : {total_secs:.1f}s\n"
                    f"</pre>"
                )

                bot.send_message(chat_id, result + timing_footer, parse_mode="HTML")
                log(f"[Monitor] ✅ Done. Pipeline: {total_secs:.1f}s")

                bot.send_message(
                    chat_id,
                    f"✅ <b>Done!</b> Pipeline complete — transcript analysed & trades placed.\n"
                    f"Use /market to monitor the next video.",
                    parse_mode="HTML",
                )
                break   # already stopped via stop_event.set() above

            except Exception as e:
                tb = traceback.format_exc()
                log(f"[Monitor] ❌ Exception in poll #{poll_count}: {e}\n{tb}")
                try:
                    bot.send_message(
                        chat_id,
                        f"❌ <b>Error in poll #{poll_count}</b>\n"
                        f"🕐 <code>{ist_now()}</code>\n"
                        f"<code>{str(e)[:300]}</code>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        # Only send "stopped" if manually stopped (not auto-stopped after detection)
        state = user_state.get(chat_id, {})
        if state.get("mode") == "monitoring":
            bot.send_message(
                chat_id,
                f"⛔ <b>Monitoring stopped</b>\n🕐 <code>{ist_now()}</code>",
                parse_mode="HTML",
            )
        state["mode"] = "awaiting_link"
        log(f"[Monitor] Thread exited for chat {chat_id}.")

    except Exception as fatal:
        tb = traceback.format_exc()
        log(f"[Monitor] 💀 FATAL crash: {fatal}\n{tb}")
        try:
            import datetime
            utc = datetime.datetime.utcnow()
            ist = utc + datetime.timedelta(hours=5, minutes=30)
            ist_str = ist.strftime("%d %b %Y  %H:%M:%S IST")
            bot.send_message(
                chat_id,
                f"💀 <b>Monitor thread crashed</b>\n"
                f"🕐 <code>{ist_str}</code>\n"
                f"<code>{str(fatal)[:300]}</code>\n\nUse /market to restart.",
                parse_mode="HTML",
            )
        except Exception:
            pass


def start_monitoring(chat_id: int, market_key: str):
    stop_monitoring(chat_id)
    stop_event = threading.Event()
    t = threading.Thread(
        target=monitor_channel,
        args=(chat_id, market_key, stop_event),
        daemon=True,
    )
    user_state[chat_id]["stop_event"]     = stop_event
    user_state[chat_id]["monitor_thread"] = t
    user_state[chat_id]["mode"]           = "monitoring"
    t.start()


def stop_monitoring(chat_id: int):
    state = user_state.get(chat_id, {})
    ev = state.get("stop_event")
    if ev:
        ev.set()
    state.pop("stop_event",     None)
    state.pop("monitor_thread", None)


# ─────────────────────────────────────────────
# INLINE KEYBOARD HELPERS
# ─────────────────────────────────────────────

def market_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("🎬 MrBeast YouTube",         callback_data="market_mrbeast"),
        types.InlineKeyboardButton("🎙️ Joe Rogan Experience",    callback_data="market_joerogan"),
        types.InlineKeyboardButton("🇮🇳 Sourav Joshi (Testing)", callback_data="market_souravjoshi"),
    )
    return kb

def yesno_keyboard(yes_data: str, no_data: str):
    kb = types.InlineKeyboardMarkup(row_width=2)
    kb.add(
        types.InlineKeyboardButton("✅ Yes", callback_data=yes_data),
        types.InlineKeyboardButton("❌ No",  callback_data=no_data),
    )
    return kb


# ─────────────────────────────────────────────
# BOT COMMAND HANDLERS
# ─────────────────────────────────────────────

@bot.message_handler(commands=["start", "help"])
def send_welcome(message):
    chat_id = message.chat.id
    actual_amt = max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)
    wallet_disp = (f"{WALLET_ADDRESS[:10]}…{WALLET_ADDRESS[-6:]}"
                   if WALLET_ADDRESS else "Not set")
    bot.send_message(
        chat_id,
        "<b>🎯 Polymarket Word Sniper Bot</b>\n\n"
        "Step 1 — pick your market below.\n"
        "Step 2 — choose auto-monitor or paste a video link.\n\n"
        f"Settings: trade ${actual_amt} | AutoTrade {'✅' if AUTO_TRADE else '❌'} | "
        f"Wallet {wallet_disp}",
        parse_mode="HTML",
        reply_markup=market_keyboard(),
    )


@bot.message_handler(commands=["market"])
def cmd_market(message):
    bot.send_message(
        message.chat.id,
        "Select a market:",
        parse_mode="HTML",
        reply_markup=market_keyboard(),
    )


@bot.message_handler(commands=["stop"])
def cmd_stop(message):
    chat_id = message.chat.id
    state   = user_state.get(chat_id, {})
    if state.get("mode") == "monitoring":
        stop_monitoring(chat_id)
        state["mode"] = "awaiting_link"
        bot.reply_to(message, "⛔ Monitoring stopped.")
    else:
        bot.reply_to(message, "ℹ️ No active monitor to stop.")


@bot.message_handler(commands=["status"])
def cmd_status(message):
    chat_id = message.chat.id
    state   = user_state.get(chat_id, {})
    mk      = state.get("market_key")
    mode    = state.get("mode", "—")
    label   = MARKET_CONFIGS[mk]["label"] if mk else "None"
    bot.reply_to(
        message,
        f"<b>Status</b>\nMarket: {label}\nMode: {mode}\n"
        f"YouTube API keys: {YT_KEYS.status()}",
        parse_mode="HTML",
    )


# ─────────────────────────────────────────────
# CALLBACK QUERY HANDLER
# ─────────────────────────────────────────────

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: types.CallbackQuery):
    chat_id = call.message.chat.id
    data    = call.data

    if data.startswith("market_"):
        mk = data[len("market_"):]
        if mk not in MARKET_CONFIGS:
            bot.answer_callback_query(call.id, "Unknown market.")
            return
        config = MARKET_CONFIGS[mk]
        user_state[chat_id] = {"market_key": mk, "mode": "ask_monitor"}
        bot.edit_message_text(
            f"✅ Market set: <b>{config['label']}</b>",
            chat_id, call.message.message_id,
            parse_mode="HTML",
        )

        if config.get("testing"):
            bot.send_message(
                chat_id,
                "🧪 <b>Sourav Joshi</b> is in <b>testing mode</b> (no real trades).\n\n"
                "Do you want to run the bot for the <b>next uploaded video</b>?",
                parse_mode="HTML",
                reply_markup=yesno_keyboard("monitor_yes", "monitor_no"),
            )
        else:
            bot.send_message(
                chat_id,
                f"Do you want to auto-monitor for the <b>next video</b> on "
                f"<b>{config['label']}</b>?",
                parse_mode="HTML",
                reply_markup=yesno_keyboard("monitor_yes", "monitor_no"),
            )
        bot.answer_callback_query(call.id)
        return

    if data == "monitor_yes":
        state = user_state.get(chat_id)
        if not state:
            bot.answer_callback_query(call.id, "Please select a market first.")
            return
        mk = state.get("market_key")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        if not YT_KEYS.available:
            bot.send_message(
                chat_id,
                "⚠️ <b>YOUTUBE_API_KEY</b> not set or all keys exhausted.\n"
                "Set comma-separated keys and restart.",
                parse_mode="HTML",
            )
            bot.answer_callback_query(call.id)
            return
        start_monitoring(chat_id, mk)
        bot.answer_callback_query(call.id, "Monitoring started!")
        return

    if data == "monitor_no":
        state = user_state.get(chat_id)
        if state:
            state["mode"] = "awaiting_link"
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=None)
        bot.send_message(
            chat_id,
            "📎 Send a <b>YouTube URL/ID</b> or paste <b>transcript text</b> directly.",
            parse_mode="HTML",
        )
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id)


# ─────────────────────────────────────────────
# TEXT HANDLER
# ─────────────────────────────────────────────

@bot.message_handler(content_types=["text"])
def handle_text(message: types.Message):
    chat_id   = message.chat.id
    user_text = message.text.strip()
    if not user_text:
        return

    state = user_state.get(chat_id)
    if not state or "market_key" not in state:
        bot.reply_to(message, "👋 Please select a market first:", reply_markup=market_keyboard())
        return

    mode = state.get("mode")
    if mode == "monitoring":
        bot.reply_to(message, "ℹ️ Auto-monitor is active. Use /stop to cancel first.")
        return
    if mode == "ask_monitor":
        bot.reply_to(message, "Please answer the auto-monitor question above, or use /market to restart.")
        return

    market_key = state["market_key"]
    video_id   = extract_video_id(user_text)

    if video_id and API_TOKEN:
        bot.reply_to(message, "🔄 Fetching transcript…")
        transcript = fetch_transcript(video_id)
        if not transcript:
            bot.reply_to(message, "⚠️ Transcript not available. Try pasting text manually.")
            return
    elif video_id and not API_TOKEN:
        bot.reply_to(message, "⚠️ API_TOKEN not set — paste transcript text directly.")
        return
    else:
        transcript = user_text

    result = format_results(transcript, market_key)
    bot.send_message(chat_id, result, parse_mode="HTML")


# ─────────────────────────────────────────────
# DOCUMENT HANDLER (.txt files)
# ─────────────────────────────────────────────

@bot.message_handler(content_types=["document"])
def handle_document(message: types.Message):
    chat_id = message.chat.id
    doc     = message.document

    if not (doc.mime_type == "text/plain" or doc.file_name.lower().endswith(".txt")):
        bot.reply_to(message, "Please send a .txt file only.")
        return

    state = user_state.get(chat_id)
    if not state or "market_key" not in state:
        bot.reply_to(message, "👋 Please select a market first:", reply_markup=market_keyboard())
        return

    bot.reply_to(message, "📄 Processing…")
    try:
        file_info  = bot.get_file(doc.file_id)
        downloaded = bot.download_file(file_info.file_path)
        transcript = downloaded.decode("utf-8", errors="replace")
        result     = format_results(transcript, state["market_key"])
        bot.send_message(chat_id, result, parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, f"❌ Error: {str(e)}")


# ─────────────────────────────────────────────
# DAILY QUOTA RESET at midnight UTC
# ─────────────────────────────────────────────

def _midnight_reset_loop():
    import datetime
    while True:
        now  = datetime.datetime.utcnow()
        nxt  = (now + datetime.timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
        secs = (nxt - now).total_seconds()
        time.sleep(secs)
        YT_KEYS.reset_exhausted()

threading.Thread(target=_midnight_reset_loop, daemon=True).start()


# ─────────────────────────────────────────────
# STARTUP
# ─────────────────────────────────────────────

print("Bot starting…")
print(f"  Markets: {', '.join(MARKET_CONFIGS.keys())}")
print(f"  AUTO_TRADE:    {AUTO_TRADE}")
print(f"  TRADE_AMOUNT:  ${max(TRADE_AMOUNT, MIN_TRADE_AMOUNT)}")
print(f"  POLL_INTERVAL: {POLL_INTERVAL}s")
print(f"  YouTube API:   {'✅ ' + YT_KEYS.status() if YT_KEYS.available else '❌ NOT SET'}")
print(f"  Transcript API:{'✅' if API_TOKEN else '❌ NOT SET'}")
print(f"  Wallet:        {(WALLET_ADDRESS[:10] + '…') if WALLET_ADDRESS else 'Not set'}")

bot.infinity_polling()
