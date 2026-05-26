import os
import io
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Mail
import json
import time
import base64
import sqlite3
import threading
from datetime import datetime, timedelta
from collections import Counter
import streamlit as st
import cv2
import hashlib
from deepface import DeepFace
from PIL import Image, ImageDraw, ImageFont

# ---------------- Session State ----------------

if "user" not in st.session_state:
    st.session_state.user = None

if "otp" not in st.session_state:
    st.session_state.otp = None

if "otp_verified" not in st.session_state:
    st.session_state.otp_verified = False

# Preload emotion model
try:
    DeepFace.build_model("Emotion")
except Exception:
    pass

# ---------------- Optional / Safe imports ----------------
try:
    import requests
    REQUESTS_AVAILABLE = True
except Exception:
    REQUESTS_AVAILABLE = False

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except Exception:
    OPENAI_AVAILABLE = False

# TTS & voice stop 
TTS_AVAILABLE = False
VOICE_LISTEN_AVAILABLE = False
try:
    import pyttsx3
    TTS_AVAILABLE = True
except Exception:
    TTS_AVAILABLE = False
try:
    import speech_recognition as sr
    VOICE_LISTEN_AVAILABLE = True
except Exception:
    VOICE_LISTEN_AVAILABLE = False

# ---------------- USER DATABASE ----------------

def init_user_db():

    conn = sqlite3.connect("chefmoji.db")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        email TEXT UNIQUE,
        password TEXT,
        created_at TEXT
    )
    """)

    conn.commit()
    conn.close()


def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def create_user(username,email,password):

    conn = sqlite3.connect("chefmoji.db")
    c = conn.cursor()

    try:
        c.execute(
            "INSERT INTO users(username,email,password,created_at) VALUES(?,?,?,?)",
            (username,email,hash_password(password),str(datetime.now()))
        )

        conn.commit()
        return True

    except:
        return False

    finally:
        conn.close()


def login_user(email,password):

    conn = sqlite3.connect("chefmoji.db")
    c = conn.cursor()

    c.execute(
        "SELECT * FROM users WHERE email=? AND password=?",
        (email,hash_password(password))
    )

    user = c.fetchone()

    conn.close()

    return user

# ---------------- Configuration ----------------
OPENAI_API_KEY = st.secrets["OPENAI_API_KEY"]
OPENWEATHER_API_KEY = st.secrets["OPENWEATHER_API_KEY"]
GOOGLE_CAL_CREDS = os.environ.get("GOOGLE_CAL_CREDS")  # optional path to service account JSON

openai_client = None
if OPENAI_AVAILABLE and OPENAI_API_KEY:
    openai_client = OpenAI(api_key=OPENAI_API_KEY)

DB_PATH = "chefmoji_history_v2.db"

# ------------------ DB Initialization ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # ---------------- Recipes Table ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipes (
        recipe_id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        title TEXT,
        cuisine TEXT,
        calories TEXT,
        mood TEXT,
        recipe_json TEXT,
        created_at TEXT
    )
    """)

    # ---------------- Recipe Ingredients ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS recipe_ingredients (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER,
            ingredient TEXT,
            FOREIGN KEY(recipe_id) REFERENCES recipes(recipe_id)
        )
    """)

    # ---------------- Pantry Table (Enhanced) ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pantry (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ingredient TEXT UNIQUE,
            added_date TEXT
        )
    """)

    # ---------------- History Table (Normalized) ----------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS history (
            history_id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER,
            mood TEXT,
            user_id INTEGER,
            weather TEXT,
            time_of_day TEXT,
            occasion TEXT,
            timestamp TEXT,
            notes TEXT,
            rating INTEGER,
            FOREIGN KEY(recipe_id) REFERENCES recipes(recipe_id)
        )
    """)

    conn.commit()
    conn.close()

init_db()
init_user_db()


# ------------------ Pantry helpers ------------------
def add_pantry_item(item):
    if not item:
        return False
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("""
           INSERT OR IGNORE INTO pantry (ingredient, added_date)
           VALUES (?, ?)""", (item.strip().lower(), datetime.utcnow().isoformat()))

        conn.commit()
        conn.close()
        return True
    except Exception as e:
        st.error(f"Could not add pantry item: {e}")
        return False

def remove_pantry_item(item):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("DELETE FROM pantry WHERE ingredient = ?", (item.strip().lower(),))
        conn.commit()
        conn.close()
        return True
    except Exception as e:
        st.error(f"Could not remove pantry item: {e}")
        return False

def get_pantry_items():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT ingredient FROM pantry")
        rows = cur.fetchall()
        conn.close()
        return [r[0] for r in rows]
    except Exception:
        return []

# ------------------ Save History ------------------

def save_recipe(recipe, mood):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    user_id = st.session_state.user[0]

    title = recipe.get("title")
    cuisine = recipe.get("cuisine")
    calories = recipe.get("calories")
    created_at = datetime.utcnow().isoformat()

    user_id = st.session_state.user[0]

    cur.execute("""
    INSERT INTO recipes(
        user_id,
        title,
        cuisine,
        calories,
        mood,
        recipe_json,
        created_at
    )
    VALUES (?,?,?,?,?,?,?)
    """,
    (
        user_id,
        recipe.get("title"),
        recipe.get("cuisine"),
        recipe.get("calories"),
        mood,
        json.dumps(recipe),
        datetime.utcnow().isoformat()
    ))

    recipe_id = cur.lastrowid

    for ing in recipe.get("ingredients", []):
        cur.execute("""
            INSERT INTO recipe_ingredients (recipe_id, ingredient)
            VALUES (?, ?)
        """, (recipe_id, ing))

    conn.commit()
    conn.close()

    return recipe_id

def save_history_entry(mood, weather, time_of_day, occasion, recipe_obj, notes="", rating=None):

    try:
        # First save recipe
        recipe_id = save_recipe(recipe_obj, mood)

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
            INSERT INTO history 
            (recipe_id, mood, weather, time_of_day, occasion, timestamp, notes, rating)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            recipe_id,
            mood,
            json.dumps(weather),
            time_of_day,
            json.dumps(occasion) if occasion else None,
            datetime.utcnow().isoformat(),
            notes,
            rating
        ))

        conn.commit()
        conn.close()
        return True

    except Exception as e:
        st.error(f"Could not save history: {e}")
        return False


# ------------------ Avatar & Personality ------------------
MOOD_EMOJIS = {"happy":"😄","sad":"😢","angry":"😡","surprised":"😲","neutral":"😐","fearful":"😨","nostalgic":"🥲"}

PERSONALITY_TEMPLATES = {
    "angry": [
        "Whoa there — time for a tiny chef joke: Why do onions make you cry? Because they saw the salad dressing! Let's try a gentle soup.",
        "Take two slow breaths. I suggest a warm one-pot recipe with soothing spices — I'm right here with you."
    ],
    "happy": [
        "You're sparkling! Fancy a challenge? I dare you to try an exotic spice today — sumac, za'atar, or ras el hanout!",
        "Woo! Let's match this mood with a bold, bright dish. Want an exotic twist or extra garnish ideas?"
    ],
    "sad": [
        "I'm here — how about a warm bowl of comfort? Creamy soup or cinnamon-spiced oats could help.",
        "Comfort food time: slow-cooked, aromatic, and gentle. Want a candlelit vibe? I'll suggest ambiance tips."
    ],
    "neutral": [
        "Nice and steady — let's make something simple and satisfying. A fresh salad or herb-lemon pasta?",
        "Low-effort, high-satisfaction — I got you. Want to tweak for more protein or fewer carbs?"
    ],
    "surprised": [
        "Plot twist! You're in for a little culinary experiment — how about a fun topping or playful plating?",
    ],
    "fearful": [
        "Let's keep it small and safe — a 3-step recipe with easy swaps. I'll guide you through each step.",
    ],
    "nostalgic": [
        "Feeling nostalgic? We can recreate a memory. Tell me a favorite from your past or let me suggest a comforting classic.",
    ]
}

def pick_personality_line(mood):
    choices = PERSONALITY_TEMPLATES.get(mood, PERSONALITY_TEMPLATES.get("neutral"))
    if not choices:
        return "Let's cook something nice together."
    return choices[int(time.time()) % len(choices)]

def create_emoji_avatar(mood, size=240, teach_text=None):
    """
    Creates an avatar image with mood emoji.
    If teach_text is provided, it will appear as a 'speech bubble' teaching the recipe.
    """
    emoji = MOOD_EMOJIS.get(mood, "🙂")
    img = Image.new("RGBA", (size*3, size*2), (255,255,255,0))  # bigger canvas for text bubble
    draw = ImageDraw.Draw(img)

    try:
        big = ImageFont.truetype("arial.ttf", int(size*0.45))
        small = ImageFont.truetype("arial.ttf", int(size*0.12))
        teach_font = ImageFont.truetype("arial.ttf", int(size*0.10))
    except Exception:
        big = ImageFont.load_default()
        small = ImageFont.load_default()
        teach_font = ImageFont.load_default()

    # Emoji circle
    draw.rectangle([0,0,size,size], fill=(255,255,255,0))
    bbox = draw.textbbox((0, 0), emoji, font=big)
    w = bbox[2] - bbox[0]; h = bbox[3] - bbox[1]
    draw.text(((size-w)/2, (size-h)/2 - 10), emoji, font=big, fill=(0,0,0))
    draw.text((10, size- int(size*0.18)), "ChefMoji", font=small, fill=(30,30,30))

    # Teaching speech bubble
    if teach_text:
        bubble_x, bubble_y = size + 20, 20
        bubble_w, bubble_h = size*1.8, size*1.5
        draw.rectangle([bubble_x, bubble_y, bubble_x+bubble_w, bubble_y+bubble_h], 
                       fill=(255,255,220,255), outline=(0,0,0))
        # wrap text nicely
        import textwrap
        wrapped = textwrap.fill(teach_text, width=30)
        draw.multiline_text((bubble_x+10, bubble_y+10), wrapped, font=teach_font, fill=(0,0,0))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ------------------ Weather & Time ------------------
def get_time_of_day(now=None):
    now = now or datetime.now()
    h = now.hour
    if 5 <= h < 12:
        return "morning"
    if 12 <= h < 17:
        return "afternoon"
    if 17 <= h < 21:
        return "evening"
    return "night"

def fetch_weather_for_location(location_query=None):
    if not OPENWEATHER_API_KEY or not REQUESTS_AVAILABLE:
        return {"main": "Unknown", "desc": "No API / requests", "temp_c": None}
    if not location_query:
        location_query = input("Enter city name for weather lookup: ").strip()
    try:
        geocode_url = f"http://api.openweathermap.org/geo/1.0/direct?q={location_query}&limit=1&appid={OPENWEATHER_API_KEY}"
        g_resp = requests.get(geocode_url, timeout=6)
        if g_resp.status_code != 200: return {"main":"Unknown","desc":"Geocode error","temp_c":None}
        g = g_resp.json()
        if not g: return {"main":"Unknown","desc":"Location not found","temp_c":None}
        lat = g[0]["lat"]; lon = g[0]["lon"]
        url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&units=metric&appid={OPENWEATHER_API_KEY}"
        r_resp = requests.get(url, timeout=6)
        if r_resp.status_code != 200: return {"main":"Unknown","desc":"Weather API error","temp_c":None}
        r = r_resp.json()
        main = r.get("weather", [{}])[0].get("main", "Unknown")
        desc = r.get("weather", [{}])[0].get("description", "")
        temp = r.get("main", {}).get("temp")
        return {"main": main, "desc": desc, "temp_c": temp}
    except Exception as e:
        return {"main":"Unknown","desc":"Request failed","temp_c":None}

# ------------------ Calendar / Occasion detection ------------------
def get_upcoming_events(days=7):
    # Try Google Calendar if creds exist (optional)
    if GOOGLE_CAL_CREDS and os.path.exists(GOOGLE_CAL_CREDS):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
            creds = service_account.Credentials.from_service_account_file(GOOGLE_CAL_CREDS, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
            service = build('calendar', 'v3', credentials=creds)
            now = datetime.utcnow().isoformat() + 'Z'
            future = (datetime.utcnow() + timedelta(days=days)).isoformat() + 'Z'
            events_result = service.events().list(calendarId='primary', timeMin=now, timeMax=future, singleEvents=True, orderBy='startTime').execute()
            items = events_result.get('items', [])
            parsed = []
            for e in items:
                start = e.get('start', {}).get('dateTime', e.get('start', {}).get('date'))
                parsed.append({"summary": e.get('summary'), "start": start})
            return parsed
        except Exception:
            pass
    # fallback to events.json file
    path = "events.json"
    if os.path.exists(path):
        try:
            with open(path,'r',encoding='utf-8') as f:
                data = json.load(f)
                ep = []
                today = datetime.utcnow().date()
                for it in data:
                    try:
                        d = datetime.fromisoformat(it.get('date')).date()
                        if 0 <= (d - today).days <= days:
                            ep.append({"summary": it.get('summary'), "start": it.get('date')})
                    except Exception:
                        continue
                return ep
        except Exception:
            return []
    return []

# ------------------ Emotion detection ------------------
def detect_emotion_from_image_bytes(img_bytes):

    try:
        import numpy as np
        import cv2

        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img_np = np.array(img)

        # DeepFace emotion detection
        results = DeepFace.analyze(
            img_np,
            actions=['emotion'],
            enforce_detection=False
        )

        # DeepFace sometimes returns dict instead of list
        if not isinstance(results, list):
            results = [results]

        img_draw = img.copy()
        draw = ImageDraw.Draw(img_draw)

        largest_face = None
        largest_area = 0

        for i, face in enumerate(results):

            region = face["region"]
            x = region["x"]
            y = region["y"]
            w = region["w"]
            h = region["h"]

            area = w * h

            if area > largest_area:
                largest_area = area
                largest_face = face

            color = "green" if face == largest_face else "red"

            draw.rectangle(
                [(x, y), (x+w, y+h)],
                outline=color,
                width=5
            )

            draw.text(
                (x, y-15),
                f"Face {i+1}",
                fill="red"
            )

        if len(results) > 1:
            st.warning(f"⚠️ {len(results)} faces detected. Using largest face.")

        if largest_face is None:
            st.warning("No face detected clearly.")
            return None, img_draw

        emotion = largest_face["dominant_emotion"]

        mapping = {
            "happy": "happy",
            "sad": "sad",
            "angry": "angry",
            "surprise": "surprised",
            "neutral": "neutral",
            "fear": "fearful"
        }

        mood = mapping.get(emotion, emotion)

        return mood, img_draw

    except Exception as e:
        st.error(f"Emotion detection error: {e}")
        return None, None
    

# ------------------ Recipe generation ------------------
def generate_three_recipes(mood, diet_pref, weather, region="Global", prefer_quick=False):
    if not openai_client:
        # deterministic fallback
        builtins = {
            "happy":[{"title":"Bright Mango Smoothie","cuisine":"Global","description":"A bright, refreshing mango smoothie.","ingredients":["mango","yogurt","milk"],"instructions":["Blend everything."],"calories":"220","nutrition":{"protein":"8","carbs":"45","fats":"4"},"quick_version":["Use frozen mango"],"pairing":"Light cookies","seasonal_note":"Best in summer"}],
            "sad":[{"title":"Warm Mushroom Soup","cuisine":"Comfort","description":"Warm and creamy." ,"ingredients":["mushrooms","onion","stock"],"instructions":["Saute and simmer."],"calories":"300","nutrition":{"protein":"6","carbs":"15","fats":"20"},"quick_version":[],"pairing":"Bread","seasonal_note":"Cool days"}],
            "neutral":[{"title":"Herb Lemon Pasta","cuisine":"Italian","description":"Simple pasta." ,"ingredients":["pasta","lemon","olive oil"],"instructions":["Cook and toss."],"calories":"450","nutrition":{"protein":"12","carbs":"60","fats":"12"},"quick_version":[],"pairing":"Salad","seasonal_note":"All seasons"}]
        }
        return builtins.get(mood, builtins['neutral'])
    prompt = f"You are a friendly recipe assistant. User mood: {mood}. Diet: {diet_pref}. weather: {weather}. Region: {region}. Provide 3 recipe objects (title,cuisine,description,ingredients array,instructions array,calories,nutrition object,quick_version array,pairing,seasonal_note) as pure JSON array."
    try:
        resp = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":prompt}],
            max_tokens=1200,
            temperature=0.8
        )
        raw = ""
        if hasattr(resp,'choices') and len(resp.choices) > 0:
            raw = getattr(resp.choices[0].message,'content','') or resp.choices[0].message.content
        parsed = None
        try:
            parsed = json.loads(raw)
        except Exception:
            start = raw.find('[')
            end = raw.rfind(']')
            if start != -1 and end != -1:
                sub = raw[start:end+1]
                try:
                    parsed = json.loads(sub)
                except Exception:
                    parsed = None
        if parsed and isinstance(parsed, list):
            return parsed[:3]
    except Exception:
        pass
    # fallback to deterministic if parse fails
    return generate_three_recipes(mood, diet_pref, weather, region, prefer_quick=False)

# ------------------ Pantry-aware recipe ordering ------------------
def score_recipe_match(recipe, pantry_items):
    # Normalize everything to lowercase for matching
    recipe_ings = [str(i).lower() for i in recipe.get('ingredients', [])]
    matched = 0
    for p in pantry_items:
        p_lower = p.lower()
        # Count as match if pantry item appears anywhere in the ingredient string
        if any(p_lower in ing for ing in recipe_ings):
            matched += 1
    missing = max(0, len(recipe_ings) - matched)
    return matched, missing

def order_recipes_by_pantry(recipes, pantry_items):
    scored = []
    for recipe in recipes:
        matched, missing = score_recipe_match(recipe, pantry_items)
        scored.append({"recipe": recipe, "matched": matched, "missing": missing})

    # Sort: first by matched (desc), then by missing (asc)
    scored.sort(key=lambda x: (-x["matched"], x["missing"]))

    # Return only the recipes in sorted order
    return [item["recipe"] for item in scored]

# ------------------ TTS & Voice-stop manager ------------------
tts_engine = None
if TTS_AVAILABLE:
    try:
        tts_engine = pyttsx3.init()
    except Exception:
        tts_engine = None

# stop flag for cross-thread coordination
stop_flag = threading.Event()
voice_listener_thread = None

def _speak_blocking(text):
    """Blocking speak call used in a thread. Honors stop_flag by checking before each utterance chunk."""
    if not tts_engine:
        return
    # pyttsx3 does not provide a granular chunk callback easily; use one long utterance but honor stop_flag via stop()
    try:
        tts_engine.say(text)
        tts_engine.runAndWait()
    except Exception:
        pass

def speak_text_async(text):
    stop_flag.clear()
    if not tts_engine:
        st.warning("TTS engine not available.")
        return
    t = threading.Thread(target=_speak_blocking, args=(text,), daemon=True)
    t.start()

def stop_speaking():
    stop_flag.set()
    if tts_engine:
        try:
            tts_engine.stop()
        except Exception:
            pass

def _voice_listen_loop_ui_notification():
    """Background voice listener thread that listens for 'ok stop' and stops TTS.
       This runs server-side and requires an accessible microphone on the machine running the app."""
    if not VOICE_LISTEN_AVAILABLE:
        return
    recognizer = sr.Recognizer()
    try:
        mic = sr.Microphone()
    except Exception:
        return
    with mic as source:
        recognizer.adjust_for_ambient_noise(source, duration=1.0)
        while not stop_flag.is_set():
            try:
                audio = recognizer.listen(source, timeout=5, phrase_time_limit=4)
                cmd = recognizer.recognize_google(audio, show_all=False)
                if not cmd:
                    continue
                cmd_l = cmd.lower()
                if "ok stop" in cmd_l or "okay stop" in cmd_l or "stop" == cmd_l.strip():
                    stop_flag.set()
                    try:
                        if tts_engine:
                            tts_engine.stop()
                    except Exception:
                        pass
                    # Can't call Streamlit UI functions from background thread reliably,
                    # but we can set a session flag; the app will reflect it on next rerun.
                    try:
                        st.session_state['speech_stopped_by_voice'] = True
                    except Exception:
                        pass
                    break
            except sr.WaitTimeoutError:
                continue
            except Exception:
                # ignore other recognition errors and loop
                continue

def start_voice_listener_if_needed():
    global voice_listener_thread
    if not VOICE_LISTEN_AVAILABLE:
        return False
    if voice_listener_thread and voice_listener_thread.is_alive():
        return True
    voice_listener_thread = threading.Thread(target=_voice_listen_loop_ui_notification, daemon=True)
    voice_listener_thread.start()
    return True

def read_recipe_aloud(recipe):
    # Compose readable text
    parts = []
    title = recipe.get('title') or "Recipe"
    parts.append(f"{title}.")
    ings = recipe.get('ingredients', [])
    if ings:
        parts.append("Ingredients:")
        parts.append(", ".join(ings) + ".")
    instr = recipe.get('instructions', [])
    if instr:
        parts.append("Instructions:")
        # join steps with short pauses (periods)
        parts.append(" ".join(instr))
    text = " ".join(parts)
    speak_text_async(text)
    # start voice listener in background to detect "ok stop" (best-effort)
    start_voice_listener_if_needed()

import random
import smtplib
from email.mime.text import MIMEText

def generate_otp():
    return str(random.randint(100000, 999999))

os.environ["SENDGRID_API_KEY"] = st.secrets["SENDGRID_API_KEY"]

def send_email_otp(receiver_email, otp):
    message = Mail(
        from_email='garimajain7014@gmail.com',  # your SendGrid verified email
        to_emails=receiver_email,
        subject='ChefMoji OTP Verification',
        plain_text_content=f'Your OTP is: {otp}'
    )

    try:
        sg = SendGridAPIClient(os.getenv("SENDGRID_API_KEY"))
        sg.send(message)
        return True
    except Exception as e:
        st.error(f"Email sending failed: {e}")
        return False

# ---------------- Login / Signup Page ----------------
st.set_page_config(page_title="ChefMoji — Emotional Chef Friend (Pantry + TTS)", layout="centered")

st.markdown("""
<style>
            
/* 🔥 Animated Gradient Background */
/* 🍔 FOOD THEME BACKGROUND */
.stApp {
    background-image: url("https://www.transparenttextures.com/patterns/food.png");
    background-color: #f8fafc;
}
            
/* Animation */
@keyframes gradientBG {
    0% {background-position: 0% 50%;}
    50% {background-position: 100% 50%;}
    100% {background-position: 0% 50%;}
}

/* Center card */
.block-container {
    max-width: 1400px;
    width: 95%;
    margin: auto;
    padding-top: 60px;
    border-radius: 20px;
    padding: 40px;

    background: rgba(255, 255, 255, 0.15);
    backdrop-filter: blur(15px);

    box-shadow: 0px 8px 30px rgba(0,0,0,0.2);
}

/* Title */
.auth-title {
    text-align: center;
    font-size: 32px;
    font-weight: bold;
    margin-bottom: 20px;
}

/* Input fields */
input {
    border-radius: 10px !important;
    padding: 10px !important;
}

/* Buttons */
.stButton > button {
    width: 100%;
    border-radius: 10px;
    background-color: #6366f1;
    color: white;
    font-weight: bold;
    height: 45px;
}

.subtitle {
    text-align: center;
    color: #475569;
    margin-bottom: 20px;
    font-size: 14px;
}
.stButton > button:hover {
    background-color: #4f46e5;
}

</style>
""", unsafe_allow_html=True)

st.markdown("""
<style>

/* 🌟 Random floating emojis */
.floating {
    position: fixed;
    font-size: 70px;
    opacity: 0.35;
    animation: floatRandom linear infinite;
}

/* Each emoji will behave differently */
@keyframes floatRandom {
    0% {
        transform: translate(0, 100vh) rotate(0deg);
    }
    25% {
        transform: translate(50px, 75vh) rotate(20deg);
    }
    50% {
        transform: translate(-30px, 50vh) rotate(-20deg);
    }
    75% {
        transform: translate(40px, 25vh) rotate(15deg);
    }
    100% {
        transform: translate(-20px, -10vh) rotate(0deg);
    }
}

</style>

<div class="floating" style="left:5%; animation-duration:14s ; font-size:80px;">🍕</div>
<div class="floating" style="left:20%; animation-duration:18s; font-size:70px;">🍔</div>
<div class="floating" style="left:35%; animation-duration:12s ; font-size:60px;">🍜</div>
<div class="floating" style="left:55%; animation-duration:20s ; font-size:75px;">🍩</div>
<div class="floating" style="left:70%; animation-duration:16s ; font-size:65px;">🍣</div>
<div class="floating" style="left:85%; animation-duration:22s ; font-size:75px;">🍰</div>

""", unsafe_allow_html=True)

if st.session_state.user is None:

    with st.container():

        st.markdown('<div class="auth-title">👨‍🍳 ChefMoji</div>', unsafe_allow_html=True)
        st.markdown('<div class="subtitle">Cook smarter with emotion 🍳</div>', unsafe_allow_html=True)
        st.markdown("###")
        tab1, tab2 = st.tabs(["🔑 Login", "📝 Signup"])

    with tab2:

        username = st.text_input("Username")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        confirm_password = st.text_input("Confirm Password", type="password")

        # 🔹 STEP 1: SEND OTP
        if st.button("Send OTP"):

           if not email:
                st.error("Please enter email first ❌")

           else:
                if st.session_state.otp is None:
                    otp = generate_otp()
                    st.session_state.otp = otp
                else:
                    otp = st.session_state.otp

                if send_email_otp(email, otp):
                    st.success("OTP sent to your email 📩")

        # 🔹 STEP 2: ENTER OTP
        user_otp = st.text_input("Enter OTP")

        # st.write("Stored OTP:", st.session_state.otp)
        # st.write("Entered OTP:", user_otp)

        if st.button("Verify OTP"):

            if user_otp.strip() == str(st.session_state.otp):
                st.session_state.otp_verified = True
                st.success("OTP Verified ✅")
            else:
                st.error("Invalid OTP ❌")

        if st.button("Create Account"):
            if not st.session_state.otp_verified:
               st.error("Please verify OTP first ❌")

            elif  password != confirm_password:
                st.error("Passwords do not match ❌")
            
            else:
                success = create_user(username, email, password)

                if success:
                    st.success("Account created successfully! Please login.")
                    st.session_state.otp = None
                    st.session_state.otp_verified = False
                else:
                    st.error("User already exists")

    with tab1:
        email = st.text_input("📧 Email")
        password = st.text_input("🔒 Password", type="password")

        if st.button("🚀 Login"):
            user = login_user(email,password)

            if user:
                st.session_state.user = user
                st.rerun()
            else:
                st.error("Invalid email or password")
    st.stop()

# ------------------ Streamlit UI ------------------
st.markdown(
    """
    <style>
    input:-webkit-autofill {
        display: none !important;
    }
    </style>
    """,
    unsafe_allow_html=True
)

st.title("ChefMoji — Emotional Chef Friend 🍳")

st.markdown("Mood-aware cooking assistant with permanent pantry storage, TTS reading, and voice stop (`ok stop`). Features degrade gracefully if optional libs/APIs are not available.")

# init_db()

# session_state defaults
ss = st.session_state
ss.setdefault('mood', None)
ss.setdefault('recipes', None)
ss.setdefault('avatar_buf', None)
ss.setdefault('last_saved', None)
ss.setdefault('events', None)
ss.setdefault('speech_stopped_by_voice', False)

# Sidebar: settings + pantry manager
with st.sidebar:
    if st.session_state.user:
        st.sidebar.write(f"👤 Logged in as {st.session_state.user[1]}")

        if st.sidebar.button("Logout"):

            # clear previous user session
            st.session_state.user = None
            st.session_state.recipes = None
            st.session_state.mood = None
            st.session_state.avatar_buf = None
            st.session_state.events = None

            st.rerun()
    st.header('Settings & Pantry')
    location = st.text_input('Weather location (city)', value='Global')
    diet_pref = st.selectbox('Dietary preference', ['No Preference','Vegan','Vegetarian','Gluten-Free','Keto'])
    prefer_quick = st.checkbox('Prefer quick recipes (≤15 min)', value=False)

    st.markdown('---')
    st.subheader('Pantry (persistent)')

    # Function just for adding + clearing pantry input
    def add_and_clear_pantry():
        ingredient = st.session_state.pantry_add.strip()
        if ingredient:
            add_pantry_item(ingredient)
            st.success(f"Added '{ingredient}' to pantry.")
        st.session_state.pantry_add = ""  # reset input field

    # Text input + Add button
    st.text_input("Add ingredient (e.g. 'tomato')", key="pantry_add")
    st.button("Add to pantry", on_click=add_and_clear_pantry)

    # Show pantry items
    pantry_items = get_pantry_items()
    if pantry_items:
        st.write("Your pantry:", ", ".join(pantry_items))
        remove_choice = st.selectbox('Remove ingredient', options=[""] + pantry_items, key='remove_choice')
        if st.button('Remove selected'):
            if remove_choice:
                remove_pantry_item(remove_choice)
                st.success(f'Removed "{remove_choice}"')
    else:
        st.info("Pantry is empty. Add staples you have at home.")

    st.markdown('---')
    st.subheader('Calendar / Occasion')
    st.markdown('Provide a local `events.json` file (optional) or set GOOGLE_CAL_CREDS env var to enable Google Calendar.')
    uploaded_events = st.file_uploader('Upload events.json (optional)', type=['json'])
    if uploaded_events:
        try:
            evdata = json.load(uploaded_events)
            with open('events.json','w',encoding='utf-8') as f:
                json.dump(evdata,f,ensure_ascii=False,indent=2)
            st.success('Saved events.json locally (used as fallback).')
        except Exception as e:
            st.error(f'Invalid JSON: {e}')

    st.markdown('---')
    st.write('Optional APIs:')
    st.write('- OpenWeatherMap: ' + ('configured' if OPENWEATHER_API_KEY else 'not configured'))
    st.write('- OpenAI: ' + ('configured' if OPENAI_API_KEY else 'not configured'))

    st.markdown('---')
    st.subheader('Speech Controls')
    if not TTS_AVAILABLE:
        st.warning('pyttsx3 not installed — TTS disabled.')
    if not VOICE_LISTEN_AVAILABLE:
        st.info('speech_recognition not installed or no microphone — voice stop disabled.')

# Main layout: left = capture, right = suggestions
col_left, col_right = st.columns([1,2])


with col_left:
    st.subheader('Capture or Upload')
    if st.button("Start Real-Time Emotion Detection"):
        st.info("Press ESC to stop camera")
    st.markdown('Use camera or upload a photo with your face for mood detection.')
    img_file = st.camera_input('Take a picture')
    uploaded = st.file_uploader('Or upload an image', type=['png','jpg','jpeg'])
    chosen_image = None
    if img_file is not None:
        chosen_image = img_file.getvalue()
    elif uploaded is not None:
        chosen_image = uploaded.read()

    if chosen_image:
        st.image(chosen_image, caption='Input image', use_column_width=True)

        if st.button('Detect Mood & Suggest Recipes'):
            with st.spinner('Detecting mood...'):
                mood, face_img = detect_emotion_from_image_bytes(chosen_image)
                if face_img:
                    st.image(face_img, caption="Detected Face", use_column_width=True)
                if mood is None:
                    st.warning('Could not reliably detect mood. Try a clearer selfie or upload a different image.')
                else:
                    ss.mood = mood
                    st.success(f'Detected mood: {mood} {MOOD_EMOJIS.get(mood,"") }')
                    # get weather & time
                    weather = fetch_weather_for_location(location)
                    tod = get_time_of_day()
                    # get events
                    events = get_upcoming_events(days=7)
                    ss.events = events
                    # occasion suggestion
                    occasion_suggestion = None
                    if events:
                        ev = events[0]
                        if mood == 'nostalgic':
                            occasion_suggestion = f"You're feeling nostalgic and {ev.get('summary')} is coming up ({ev.get('start')}). How about cooking a dish that reminds you of them?"
                        elif mood == 'happy':
                            occasion_suggestion = f"{ev.get('summary')} is coming up ({ev.get('start')}). Celebrate with an impressive version of a familiar dish!"
                        else:
                            occasion_suggestion = f"Upcoming event: {ev.get('summary')} on {ev.get('start')}."
                    # personality line & avatar
                    persona = pick_personality_line(mood)
                    avatar_buf = create_emoji_avatar(mood , size=240, teach_text=None)
                    ss.avatar_buf = avatar_buf
                    # generate recipes
                    with st.spinner('Generating recipes...'):
                        recipes = generate_three_recipes(mood, diet_pref, weather , region=location, prefer_quick=prefer_quick)
                        ss.recipes = recipes
                    # show ChefMoji response
                    st.markdown('### ChefMoji response')
                    st.image(avatar_buf, width=160)
                    st.info(persona)
                    st.write(f"**Time of day:** {tod} • **Weather:** {weather.get('main')} — {weather.get('desc')} • **Upcoming events (7d):** {len(events)}")
                    if occasion_suggestion:
                        st.success(occasion_suggestion)
                    st.markdown('---')

with col_right:
    st.subheader('Suggestions & Recipes')
    if ss.recipes:
        recipes = ss.recipes
        pantry_items = get_pantry_items()
        ordered = order_recipes_by_pantry(recipes, pantry_items)
        # contextual banner
        weather = fetch_weather_for_location(location)
        tod = get_time_of_day()
        st.markdown(f"**Context:** Mood: **{ss.mood}** {MOOD_EMOJIS.get(ss.mood,'')} • Weather: {weather.get('main')} • Time: {tod}")

        # radio to pick recipe
        chosen_idx = st.radio('Pick a recipe option', options=list(range(len(ordered))),
                              format_func=lambda i: ordered[i].get('title','Option '+str(i+1)))
        sel = ordered[chosen_idx]
        st.markdown(f"### {sel.get('title')}")
        st.write(sel.get('description',''))
        # ✅ Teaching Avatar Integration
        teach_text = " ".join(sel.get("instructions", [])[:3])  # first 3 steps only
        try:
            avatar_teach_buf = create_emoji_avatar(ss.mood, teach_text=f"Let's cook! {teach_text}")
            st.image(avatar_teach_buf, width=400, caption="ChefMoji teaching you this recipe")
        except Exception as e:
            st.warning(f"Could not create teaching avatar: {e}")
        st.markdown('**Ingredients**')
        recipe_ings = [str(i) for i in sel.get('ingredients',[])]
        # highlight matches / missing
        matched, missing = score_recipe_match(sel, pantry_items)
        if pantry_items:
            st.write(f"Pantry matches: **{matched}** • Missing ingredients: **{missing}**")
        for ing in recipe_ings:
            is_have = any(p in ing.lower() for p in pantry_items)
            if is_have:
                st.write(f"- ✅ {ing}")
            else:
                st.write(f"- ❌ {ing}")

        st.markdown('**Instructions**')
        for step in sel.get('instructions',[])[:50]:
            st.write(step)

        st.markdown(f"**Calories:** {sel.get('calories','N/A')} • **Pairing:** {sel.get('pairing','')}")
        st.markdown('---')

        # read aloud / stop controls
        col_t1, col_t2, col_t3 = st.columns([1,1,1])
        with col_t1:
            if st.button('Read Recipe Aloud'):
                if TTS_AVAILABLE and tts_engine:
                    read_recipe_aloud(sel)
                    st.success("Reading recipe aloud. Say 'ok stop' to stop (if voice listener available) or press Stop Speech.")
                else:
                    st.warning("TTS not available. Install pyttsx3 to enable offline TTS.")
        with col_t2:
            if st.button('Stop Speech'):
                stop_speaking()
                st.success("Stopped speech.")
        with col_t3:
            if st.button('Add missing ingredients to pantry (quick)'):
                # add missing to pantry
                for ing in recipe_ings:
                    if not any(p in ing.lower() for p in pantry_items):
                        add_pantry_item(ing)
                st.success("Added missing ingredients (as strings) to pantry. Reopen or refresh sidebar to view.")
        # save / note / download actions
        cols = st.columns(3)
        with cols[0]:
            rating = st.slider("Rate this recipe (1-5)", 1, 5, 4)
            if st.button('Save to history'):
                success = save_history_entry(
                    ss.mood,
                    weather,
                    tod,
                    ss.events[0] if ss.events else None,
                    sel,
                    notes='Saved by user',
                    rating=rating
                )
                if success:
                    st.success("Saved successfully with rating!")

        with cols[1]:
            if st.button('Generate short note for occasion'):
                if ss.events:
                    ev = ss.events[0]
                    note = None
                    if openai_client:
                        try:
                            prompt = f"Write a short friendly note (2-3 lines) to include with a home-cooked dish for {ev.get('summary')} on {ev.get('start')} — tone: warm and slightly nostalgic."
                            resp = openai_client.chat.completions.create(model='gpt-4o-mini', messages=[{'role':'system','content':prompt}], max_tokens=80, temperature=0.7)
                            raw = ''
                            if hasattr(resp,'choices') and len(resp.choices)>0:
                                raw = getattr(resp.choices[0].message,'content','') or resp.choices[0].message.content
                            note = raw.strip()
                        except Exception:
                            note = None
                    if not note:
                        note = f"Happy {ev.get('summary')}! I made this especially for you — hope it brings back good memories."
                    st.markdown('**Short note (you can copy/paste):**')
                    st.write(note)
                else:
                    st.warning('No upcoming event found in next 7 days.')
        with cols[2]:
            if st.button('Download Recipe Card (PNG)'):
                try:
                    card = Image.new('RGB',(900,600),color=(250,250,245))
                    d = ImageDraw.Draw(card)
                    try:
                        title_font = ImageFont.truetype('arial.ttf', 36)
                        body_font = ImageFont.truetype('arial.ttf', 18)
                    except Exception:
                        title_font = ImageFont.load_default()
                        body_font = ImageFont.load_default()
                    d.text((20,20), sel.get('title','Recipe'), font=title_font, fill=(20,20,20))
                    y = 80
                    d.text((20,y), 'Ingredients:', font=body_font, fill=(0,0,0))
                    y += 30
                    for ing in recipe_ings[:20]:
                        d.text((30,y), f'- {ing}', font=body_font, fill=(30,30,30))
                        y += 24
                    buf = io.BytesIO()
                    card.save(buf, format='PNG')
                    buf.seek(0)
                    st.download_button('Download PNG', data=buf, file_name=f"{sel.get('title','recipe')}.png", mime='image/png')
                except Exception as e:
                    st.error(f'Could not build recipe card: {e}')
    else:
        st.info('No suggestions yet — detect mood first using the left panel.')

# ------------------ History display ------------------
st.markdown('---')
st.subheader('History (recent)')
st.markdown("---")
st.subheader("📖 Your Saved Recipes")
try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        SELECT h.history_id,
               h.timestamp,
               h.mood,
               h.occasion,
               h.rating,
               r.title
        FROM history h
        JOIN recipes r ON h.recipe_id = r.recipe_id
        WHERE r.user_id = ?
        ORDER BY h.history_id DESC
        LIMIT 10
    """, (st.session_state.user[0],))


    rows = cur.fetchall()
    conn.close()
    if rows:
        for r in rows:
            st.write(
                f"🗓 {r[1][:19]} | 🍽 {r[5]} | Mood: {r[2]} | ⭐ Rating: {r[4]}"
            )


    else:
        st.write('No saved history yet.')
except Exception as e:
    st.error(f'Could not load history: {e}')

st.caption('ChefMoji — now with permanent pantry, pantry-aware recipes, TTS reading, and voice-stop (best-effort).')

st.markdown("---")
st.subheader("ChefMoji Insights 📊")

try:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Most frequent mood
    cur.execute("""
        SELECT mood, COUNT(*)
        FROM history
        WHERE user_id = ?
        GROUP BY mood
        ORDER BY COUNT(*) DESC
        """, (st.session_state.user[0],))
    moods = cur.fetchall()

    if moods:
        st.write("### Most Cooked Mood:")
        for m in moods:
            st.write(f"• {m[0]} → {m[1]} times")

    # Most used ingredient
    cur.execute("""
        SELECT ri.ingredient, COUNT(*)
        FROM recipe_ingredients ri
        JOIN recipes r ON ri.recipe_id = r.recipe_id
        WHERE r.user_id = ?
        GROUP BY ri.ingredient
        ORDER BY COUNT(*) DESC
        LIMIT 5
        """, (st.session_state.user[0],))
    ingredients = cur.fetchall()

    if ingredients:
        st.write("### Top Ingredients Used:")
        for ing in ingredients:
            st.write(f"• {ing[0]} → {ing[1]} times")

    conn.close()

except Exception as e:
    st.error(f"Analytics error: {e}")

try:

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    cur.execute("""
    SELECT recipe_id, title
    FROM recipes
    WHERE user_id = ?
    ORDER BY recipe_id DESC
    """,(st.session_state.user[0],))

    saved_recipes = cur.fetchall()

    conn.close()

    if saved_recipes:

        recipe_names = [r[1] for r in saved_recipes]

        selected_recipe = st.selectbox(
            "Open your saved recipe",
            recipe_names
        )

        if selected_recipe:

            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()

            cur.execute("""
            SELECT recipe_json
            FROM recipes
            WHERE title = ? AND user_id = ?
            """,(selected_recipe,st.session_state.user[0]))

            recipe_json = cur.fetchone()[0]

            conn.close()

            recipe = json.loads(recipe_json)

            st.markdown("## 🍽 Recipe")

            st.write("###", recipe["title"])

            if "description" in recipe:
                st.write(recipe["description"])

            st.markdown("### Ingredients")

            for ing in recipe.get("ingredients",[]):
                st.write("•", ing)

            st.markdown("### Instructions")

            for i,step in enumerate(recipe.get("instructions",[]),1):
                st.write(f"{i}. {step}")

    else:

        st.info("No saved recipes yet.")

except Exception as e:

    st.error(f"Error loading saved recipes: {e}")