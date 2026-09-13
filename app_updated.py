import os
import io
import json
import time
import math
import hmac
import sqlite3
import hashlib
import secrets
from datetime import datetime, timezone

import streamlit as st
from PIL import Image, UnidentifiedImageError
import pandas as pd
import plotly.express as px
import folium
from streamlit_folium import st_folium
import requests

try:
    from streamlit_js_eval import get_geolocation
except ImportError:
    get_geolocation = None

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

# ============================================================
# WasteWise AI Portal - production-oriented single-file version
# ============================================================
# Required packages:
# streamlit
# pillow
# pandas
# plotly
# folium
# streamlit-folium
# streamlit-js-eval
# requests
# google-genai
#
# Run:
#   streamlit run app.py
#
# Optional .streamlit/secrets.toml:
#   GEMINI_API_KEY = "your_key"
#
# Optional environment variable:
#   GEMINI_API_KEY=your_key
#
# Notes:
# - SQLite gives persistent local storage.
# - Passwords are stored as salted PBKDF2 hashes, never plaintext.
# - Uploaded profile images are stored as files under data/profile_pics.
# - AI results are validated before being accepted.
# - No fake AI result is shown when AI is unavailable.
# - GPS failure does not silently replace the user's location with a fake one.
# ============================================================

APP_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(APP_DIR, "data")
PROFILE_DIR = os.path.join(DATA_DIR, "profile_pics")
DB_PATH = os.path.join(DATA_DIR, "wastewise.db")

os.makedirs(PROFILE_DIR, exist_ok=True)

st.set_page_config(
    page_title="WasteWise AI Portal",
    page_icon="♻️",
    layout="wide",
)

# ----------------------------
# Styling
# ----------------------------
st.markdown(
    """
    <style>
    .stApp { background-color: #090d16; color: #f1f5f9; }
    .card {
        background: #1e293b; padding: 18px; border-radius: 12px;
        margin-bottom: 12px; border-left: 4px solid #10b981;
    }
    .stat {
        background: #0f172a; padding: 12px; border-radius: 10px;
        text-align: center; border: 1px solid #1e293b;
    }
    .profile-card {
        background: #131d31; padding: 20px; border-radius: 14px;
        border: 1px solid #334155;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

# ----------------------------
# Configuration
# ----------------------------
try:
    api_key = st.secrets.get("GEMINI_API_KEY", "")
except Exception:
    api_key = ""

api_key = api_key or os.getenv("GEMINI_API_KEY", "")

MAX_IMAGE_BYTES = 8 * 1024 * 1024
ALLOWED_IMAGE_TYPES = {"jpg", "jpeg", "png", "webp"}

# Conservative, editable estimates.
# These are estimates, not scientific measurements.
CO2_BY_MATERIAL = {
    "plastic": 0.50,
    "pet": 0.50,
    "paper": 0.20,
    "cardboard": 0.20,
    "glass": 0.30,
    "metal": 0.70,
    "aluminum": 0.70,
    "steel": 0.60,
    "organic": 0.10,
    "e-waste": 1.00,
    "electronic": 1.00,
    "textile": 0.40,
    "mixed": 0.20,
    "other": 0.10,
}

POINTS_BY_HAZARD = {
    "low": 10,
    "medium": 20,
    "high": 30,
    "unknown": 10,
}


# ----------------------------
# Database
# ----------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with get_db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                profile_pic TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                object_name TEXT NOT NULL,
                material TEXT NOT NULL,
                value TEXT NOT NULL,
                hazard TEXT NOT NULL,
                prep_steps TEXT NOT NULL,
                upcycling TEXT NOT NULL,
                points INTEGER NOT NULL DEFAULT 0,
                co2_saved REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                latitude REAL NOT NULL,
                longitude REAL NOT NULL,
                address TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )


init_db()


# ----------------------------
# Security / password helpers
# ----------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 310_000
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, iterations
    )
    return f"pbkdf2_sha256${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_hex, digest_hex = stored.split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def validate_password(password: str) -> tuple[bool, str]:
    if len(password) < 8:
        return False, "Password must be at least 8 characters."
    if len(password) > 128:
        return False, "Password is too long."
    if password.strip() != password:
        return False, "Password must not start or end with spaces."
    return True, ""


def validate_username(username: str) -> tuple[bool, str]:
    username = username.strip()
    if not 3 <= len(username) <= 40:
        return False, "Username must be 3–40 characters."
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._- ")
    if any(ch not in allowed for ch in username):
        return False, "Username contains unsupported characters."
    return True, ""


# ----------------------------
# User functions
# ----------------------------
def create_user(username: str, password: str):
    username = username.strip()
    valid, msg = validate_username(username)
    if not valid:
        return False, msg

    valid, msg = validate_password(password)
    if not valid:
        return False, msg

    try:
        with get_db() as conn:
            cur = conn.execute(
                """
                INSERT INTO users (username, password_hash, created_at)
                VALUES (?, ?, ?)
                """,
                (username, hash_password(password), utc_now()),
            )
            return True, cur.lastrowid
    except sqlite3.IntegrityError:
        return False, "That username already exists."


def authenticate(username: str, password: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE",
            (username.strip(),),
        ).fetchone()

    if row and verify_password(password, row["password_hash"]):
        return row
    return None


def get_user(user_id: int):
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE id = ?", (user_id,)
        ).fetchone()


def update_username(user_id: int, username: str):
    username = username.strip()
    valid, msg = validate_username(username)
    if not valid:
        return False, msg
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE users SET username = ? WHERE id = ?",
                (username, user_id),
            )
        return True, ""
    except sqlite3.IntegrityError:
        return False, "That username is already in use."


def update_profile_pic(user_id: int, uploaded_file):
    if uploaded_file is None:
        return None, "No image selected."

    data = uploaded_file.getvalue()
    if len(data) > MAX_IMAGE_BYTES:
        return None, "Profile image is larger than 8 MB."

    try:
        img = Image.open(io.BytesIO(data))
        img.verify()

        # Re-open after verify so the image can be saved safely.
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return None, "Invalid image file."

    filename = f"user_{user_id}_{secrets.token_hex(8)}.jpg"
    path = os.path.join(PROFILE_DIR, filename)
    img.save(path, format="JPEG", quality=88, optimize=True)

    with get_db() as conn:
        old = conn.execute(
            "SELECT profile_pic FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        conn.execute(
            "UPDATE users SET profile_pic = ? WHERE id = ?",
            (path, user_id),
        )

    if old and old["profile_pic"] and old["profile_pic"] != path:
        try:
            if os.path.isfile(old["profile_pic"]):
                os.remove(old["profile_pic"])
        except OSError:
            pass

    return path, ""


# ----------------------------
# Scan / leaderboard functions
# ----------------------------
def save_scan(user_id, result):
    points = calculate_points(result.get("hazard"))
    co2 = calculate_co2(result.get("material"))

    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO scans (
                user_id, object_name, material, value, hazard,
                prep_steps, upcycling, points, co2_saved, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                safe_text(result.get("object"), "Waste Item"),
                safe_text(result.get("material"), "Other"),
                safe_text(result.get("value"), "Not available"),
                normalize_hazard(result.get("hazard")),
                json.dumps(normalize_steps(result.get("prep_steps"))),
                safe_text(result.get("upcycling"), "Recycle responsibly."),
                points,
                co2,
                utc_now(),
            ),
        )


def calculate_points(hazard):
    return POINTS_BY_HAZARD.get(normalize_hazard(hazard), 10)


def calculate_co2(material):
    text = str(material or "").lower()
    for key, value in CO2_BY_MATERIAL.items():
        if key in text:
            return value
    return CO2_BY_MATERIAL["other"]


def get_user_stats(user_id):
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS scans,
                COALESCE(SUM(points), 0) AS points,
                COALESCE(SUM(co2_saved), 0) AS co2
            FROM scans
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()
    return {
        "scans": int(row["scans"]),
        "points": int(row["points"]),
        "co2": float(row["co2"]),
    }


def get_leaderboard():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT
                u.username AS "User Name",
                COALESCE(SUM(s.points), 0) AS "Points",
                COUNT(s.id) AS "Scans",
                COALESCE(SUM(s.co2_saved), 0) AS "CO2 Saved (kg)"
            FROM users u
            LEFT JOIN scans s ON s.user_id = u.id
            GROUP BY u.id, u.username
            ORDER BY "Points" DESC, "Scans" DESC, u.username ASC
            """
        ).fetchall()
    return pd.DataFrame([dict(r) for r in rows])


def save_location(user_id, lat, lon, address):
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO locations (user_id, latitude, longitude, address, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, float(lat), float(lon), address or "", utc_now()),
        )


# ----------------------------
# General helpers
# ----------------------------
def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_text(value, fallback):
    if value is None:
        return fallback
    text = str(value).strip()
    return text if text else fallback


def normalize_hazard(value):
    text = str(value or "").strip().lower()
    if text in {"low", "medium", "high", "unknown"}:
        return text
    return "unknown"


def normalize_steps(value):
    if isinstance(value, list):
        return [safe_text(x, "Follow safe handling guidance.") for x in value][:10]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return ["Follow safe handling guidance."]


def normalize_ai_result(data):
    if not isinstance(data, dict):
        raise ValueError("AI returned an invalid response.")

    required = ["object", "material", "value", "hazard", "prep_steps", "upcycling"]
    missing = [k for k in required if k not in data]
    if missing:
        raise ValueError("AI response is missing: " + ", ".join(missing))

    result = {
        "object": safe_text(data["object"], "Waste Item"),
        "material": safe_text(data["material"], "Other"),
        "value": safe_text(data["value"], "Not available"),
        "hazard": normalize_hazard(data["hazard"]),
        "prep_steps": normalize_steps(data["prep_steps"]),
        "upcycling": safe_text(data["upcycling"], "Recycle responsibly."),
    }

    # Keep model output reasonably sized for UI/database safety.
    result["object"] = result["object"][:200]
    result["material"] = result["material"][:200]
    result["value"] = result["value"][:200]
    result["upcycling"] = result["upcycling"][:500]
    return result


# ----------------------------
# Gemini AI
# ----------------------------
def analyze_waste(image: Image.Image):
    if not api_key:
        raise RuntimeError(
            "Gemini API key is not configured. Add GEMINI_API_KEY to "
            ".streamlit/secrets.toml or your environment."
        )

    if genai is None or types is None:
        raise RuntimeError(
            "The google-genai package is not installed. Run: pip install google-genai"
        )

    # Resize large images before sending to the model.
    img = image.convert("RGB")
    img.thumbnail((1600, 1600))

    client = genai.Client(api_key=api_key)

    prompt = """
You are a waste-sorting assistant.

Analyze the supplied waste-item image. If the image is unclear or does not
show a waste item, say so in the object field and use conservative values.

Return ONLY valid JSON, with exactly these keys:
{
  "object": "specific object name",
  "material": "material/category",
  "value": "estimated scrap value or 'Not available'",
  "hazard": "low|medium|high|unknown",
  "prep_steps": ["step 1", "step 2"],
  "upcycling": "one practical idea"
}

Do not invent exact prices. If a local scrap price cannot be reliably known,
write "Not available" rather than making up a price.
For hazardous materials, clearly identify the hazard and recommend
appropriate professional/local disposal.
"""

    last_error = None
    for model_name in ("gemini-2.5-flash", "gemini-2.0-flash"):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=[
                    types.Part.from_bytes(
                        data=image_to_jpeg_bytes(img),
                        mime_type="image/jpeg",
                    ),
                    prompt,
                ],
                config=types.GenerateContentConfig(
                    temperature=0.1,
                    response_mime_type="application/json",
                ),
            )
            raw = (response.text or "").strip()
            data = json.loads(raw)
            return normalize_ai_result(data)
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)

    raise RuntimeError(f"AI analysis failed: {last_error}")


def image_to_jpeg_bytes(image):
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# ----------------------------
# Reverse geocoding
# ----------------------------
def get_location_details(lat, lon):
    url = "https://nominatim.openstreetmap.org/reverse"
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lon,
        "zoom": 18,
        "addressdetails": 1,
    }
    headers = {
        "User-Agent": "WasteWise/1.0 (contact-app-owner-before-production-use)"
    }

    try:
        response = requests.get(
            url, params=params, headers=headers, timeout=8
        )
        response.raise_for_status()
        data = response.json()
        return data.get("display_name", "Address unavailable")
    except requests.RequestException:
        return "Address lookup unavailable"


# ----------------------------
# Session state / authentication
# ----------------------------
for key, default in {
    "authenticated": False,
    "user_id": None,
    "analysis": None,
    "analysis_image": None,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default


def logout():
    st.session_state.authenticated = False
    st.session_state.user_id = None
    st.session_state.analysis = None
    st.session_state.analysis_image = None
    st.rerun()


# ----------------------------
# Login / registration screen
# ----------------------------
if not st.session_state.authenticated:
    st.title("♻️ WasteWise AI Portal")
    st.caption("AI-assisted waste identification and recycling tracker.")

    login_tab, register_tab = st.tabs(["🔐 Login", "➕ Create Account"])

    with login_tab:
        with st.form("login_form"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button(
                "Login", type="primary", use_container_width=True
            )

        if submitted:
            if not username or not password:
                st.error("Please enter username and password.")
            else:
                user = authenticate(username, password)
                if user:
                    st.session_state.authenticated = True
                    st.session_state.user_id = int(user["id"])
                    st.session_state.analysis = None
                    st.rerun()
                else:
                    st.error("Invalid username or password.")

    with register_tab:
        with st.form("register_form"):
            new_username = st.text_input("Choose username")
            new_password = st.text_input("Choose password", type="password")
            confirm_password = st.text_input("Confirm password", type="password")
            submitted = st.form_submit_button(
                "Create Account", type="primary", use_container_width=True
            )

        if submitted:
            if new_password != confirm_password:
                st.error("Passwords do not match.")
            else:
                ok, result = create_user(new_username, new_password)
                if ok:
                    st.success("Account created. You can now log in.")
                else:
                    st.error(result)

    st.stop()


# ----------------------------
# Current user
# ----------------------------
current_user = get_user(st.session_state.user_id)

if current_user is None:
    st.session_state.authenticated = False
    st.session_state.user_id = None
    st.rerun()

stats = get_user_stats(st.session_state.user_id)

# ----------------------------
# Sidebar
# ----------------------------
st.sidebar.title("👤 Account")
st.sidebar.write(f"**{current_user['username']}**")
if st.sidebar.button("Log out", use_container_width=True):
    logout()

st.sidebar.markdown("---")
st.sidebar.caption(
    "Data is stored in the local SQLite database. "
    "For a multi-user/cloud deployment, use a hosted database and object storage."
)

# ----------------------------
# Header
# ----------------------------
head_col1, head_col2 = st.columns([1, 5])

with head_col1:
    pic = current_user["profile_pic"]
    if pic and os.path.isfile(pic):
        st.image(pic, width=80)
    else:
        st.markdown(
            "<h1 style='text-align:center;margin:0;'>👤</h1>",
            unsafe_allow_html=True,
        )

with head_col2:
    st.markdown("## ♻️ WasteWise Portal")
    st.markdown(f"Logged in as **{current_user['username']}**")

c1, c2, c3 = st.columns(3)
c1.markdown(
    f'<div class="stat">⚡ <b>Points:</b> {stats["points"]}</div>',
    unsafe_allow_html=True,
)
c2.markdown(
    f'<div class="stat">📸 <b>Total Scans:</b> {stats["scans"]}</div>',
    unsafe_allow_html=True,
)
c3.markdown(
    f'<div class="stat">🛡️ <b>Estimated CO₂ Saved:</b> {stats["co2"]:.1f} kg</div>',
    unsafe_allow_html=True,
)

st.markdown("---")

tab1, tab2, tab3, tab4 = st.tabs(
    ["📷 AI Scanner", "📍 GPS Map", "📊 Leaderboard", "👤 Profile Settings"]
)

# ============================================================
# TAB 1: AI SCANNER
# ============================================================
with tab1:
    col1, col2 = st.columns(2)

    with col1:
        source = st.radio(
            "Image Source:",
            ["Upload File", "Live Camera"],
            horizontal=True,
        )

        if source == "Upload File":
            img_file = st.file_uploader(
                "Upload waste image",
                type=["jpg", "jpeg", "png", "webp"],
                help="Maximum file size: 8 MB.",
            )
        else:
            img_file = st.camera_input("Take a photo")

        if img_file:
            raw_bytes = img_file.getvalue()

            if len(raw_bytes) > MAX_IMAGE_BYTES:
                st.error("Image is larger than 8 MB. Please choose a smaller image.")
            else:
                try:
                    img = Image.open(io.BytesIO(raw_bytes))
                    img.verify()
                    img = Image.open(io.BytesIO(raw_bytes)).convert("RGB")

                    st.image(img, use_container_width=True)

                    if st.button(
                        "🚀 Analyze Item",
                        type="primary",
                        use_container_width=True,
                    ):
                        with st.spinner("AI analyzing waste item..."):
                            try:
                                result = analyze_waste(img)
                                save_scan(st.session_state.user_id, result)
                                st.session_state.analysis = result
                                st.session_state.analysis_image = image_to_jpeg_bytes(img)
                                st.success("Analysis saved successfully.")
                                st.rerun()
                            except Exception as exc:
                                st.error(
                                    "The image could not be analyzed. "
                                    "Your scan was NOT added to points or statistics."
                                )
                                st.caption(f"Technical detail: {exc}")

                except (UnidentifiedImageError, OSError):
                    st.error("The uploaded file is not a valid image.")

    with col2:
        res = st.session_state.analysis
        if res:
            points = calculate_points(res.get("hazard"))
            co2 = calculate_co2(res.get("material"))

            st.markdown(
                f'<div class="card"><b>Detected:</b> {res["object"]}<br>'
                f'<b>Material:</b> {res["material"]}</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                f'<div class="card"><b>Estimated Scrap Value:</b> {res["value"]}<br>'
                f'<b>Points Earned:</b> +{points}<br>'
                f'<b>Estimated CO₂ Benefit:</b> +{co2:.2f} kg</div>',
                unsafe_allow_html=True,
            )

            st.markdown("**Preparation / Safety Steps:**")
            for step in res["prep_steps"]:
                st.write(f"• {step}")

            st.markdown(f"**Upcycling Idea:** {res['upcycling']}")
            st.caption(
                "AI identification and CO₂ figures are estimates. "
                "Verify local recycling and hazardous-waste rules before disposal."
            )
        else:
            st.info("Upload/capture a photo and click 'Analyze Item'.")

# ============================================================
# TAB 2: GPS MAP
# ============================================================
with tab2:
    st.subheader("📍 Live GPS Map")

    if get_geolocation is None:
        st.warning(
            "GPS component is not installed. Install streamlit-js-eval to enable browser geolocation."
        )
        loc = None
    else:
        try:
            loc = get_geolocation()
        except Exception:
            loc = None

    lat = None
    lon = None

    if isinstance(loc, dict) and isinstance(loc.get("coords"), dict):
        try:
            lat = float(loc["coords"]["latitude"])
            lon = float(loc["coords"]["longitude"])
        except (TypeError, ValueError):
            lat = lon = None

    if lat is not None and lon is not None:
        st.success(
            f"📍 GPS: Latitude `{lat:.5f}`, Longitude `{lon:.5f}`"
        )

        if st.button("🔎 Get Address", use_container_width=True):
            with st.spinner("Looking up address..."):
                address = get_location_details(lat, lon)
                st.session_state["gps_address"] = address
                try:
                    save_location(st.session_state.user_id, lat, lon, address)
                except sqlite3.Error as exc:
                    st.warning(f"Location could not be saved: {exc}")

        address = st.session_state.get("gps_address", "Address not requested.")
        st.info(f"**Address:** {address}")

        # OpenStreetMap tiles are used here to avoid embedding Google tile URLs.
        m = folium.Map(
            location=[lat, lon],
            zoom_start=17,
            control_scale=True,
            tiles="OpenStreetMap",
        )
        folium.Marker(
            [lat, lon],
            popup=f"User: {current_user['username']}",
            tooltip="Your location",
            icon=folium.Icon(color="red", icon="user", prefix="fa"),
        ).add_to(m)

        st_folium(m, width=None, height=500)
    else:
        st.warning(
            "Live GPS location is unavailable. Allow location permission in your browser/device "
            "and refresh the page. No fake fallback location is shown."
        )

# ============================================================
# TAB 3: LEADERBOARD
# ============================================================
with tab3:
    st.subheader("🏆 Users Ranking & Points Leaderboard")

    df_users = get_leaderboard()

    if df_users.empty:
        st.info("No users yet.")
    else:
        st.dataframe(df_users, use_container_width=True, hide_index=True)

        fig = px.bar(
            df_users,
            x="User Name",
            y="Points",
            title="User Eco-Points Comparison",
        )
        st.plotly_chart(fig, use_container_width=True)

# ============================================================
# TAB 4: PROFILE
# ============================================================
with tab4:
    st.subheader("👤 Account Settings")

    with st.container(border=True):
        prof_col1, prof_col2 = st.columns([1, 2])

        with prof_col1:
            pic = current_user["profile_pic"]
            if pic and os.path.isfile(pic):
                st.image(pic, caption="Profile Photo", width=140)
            else:
                st.info("No profile picture added.")

        with prof_col2:
            with st.form("profile_update_form"):
                edited_name = st.text_input(
                    "Change Username:",
                    value=current_user["username"],
                )
                new_prof_pic = st.file_uploader(
                    "Upload Profile Picture:",
                    type=["jpg", "jpeg", "png", "webp"],
                )
                save_btn = st.form_submit_button(
                    "Save Profile Settings",
                    type="primary",
                )

            if save_btn:
                changed = False

                if edited_name.strip() != current_user["username"]:
                    ok, msg = update_username(
                        st.session_state.user_id, edited_name
                    )
                    if not ok:
                        st.error(msg)
                    else:
                        changed = True

                if new_prof_pic is not None:
                    path, msg = update_profile_pic(
                        st.session_state.user_id, new_prof_pic
                    )
                    if path:
                        changed = True
                    else:
                        st.error(msg)

                if changed:
                    st.success("Profile updated successfully.")
                    st.rerun()

    st.markdown("---")
    st.markdown(
        f"""
        **Account ID:** `{st.session_state.user_id}`  
        **Earned Points:** `{stats["points"]}`  
        **Total Scans:** `{stats["scans"]}`  
        **Estimated CO₂ Benefit:** `{stats["co2"]:.2f} kg`
        """
    )
