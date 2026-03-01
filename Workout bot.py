#!/usr/bin/env python3
"""
Bigsis 100-Day Workout Challenge Bot
- Daily morning reminders (skips Tuesday & Friday)
- Progress tracker with visual map
- Journal entries
- Streak rewards & encouragement
"""

import os
import json
import logging
import asyncio
import ssl
import re
from datetime import datetime, date, timedelta
from pathlib import Path
from threading import Lock, Thread
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs
from urllib.request import urlopen, Request
from urllib.error import URLError
from io import BytesIO
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ContextTypes, filters
)


def load_env_file(path: str = ".env"):
    env_path = Path(path)
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


load_env_file()


def escape_markdown(text: str) -> str:
    """Escape basic Markdown meta characters for user-supplied strings."""
    for ch in ("_", "*", "[", "]", "(", ")", "`"):
        text = text.replace(ch, f"\\{ch}")
    return text

# ── Config ────────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
DATA_FILE  = Path("workout_data.json")
LOG_FILE   = Path("bot.log")
WEB_SYNC_HOST = os.environ.get("WEB_SYNC_HOST", "127.0.0.1")
WEB_SYNC_PORT = int(os.environ.get("WEB_SYNC_PORT", "8765"))

# Days OFF (0=Mon … 6=Sun)
REST_DAYS = {1, 4}   # Tuesday=1, Friday=4

# Morning reminder time (24h, local server time)
REMINDER_HOUR   = 7
REMINDER_MINUTE = 0

# Playlist — first 50 videos from the Bigsis 100-day program
# (Day numbers; URLs follow the pattern below — update as needed)
PLAYLIST_ID = "PLkVCuKZG0QB5RUvjcBY3lYnftB37Li3Y2"
PLAYLIST_URL = f"https://www.youtube.com/playlist?list={PLAYLIST_ID}"

# Day 1..50 video IDs in playlist order
VIDEO_IDS = [
    "OXf2EMtFYbI", "KkTuRMHsQMc", "oE9P3G1v77Y", "oIbE7Vf5HBQ", "fV3R0Cg5i4c",
    "JtH0WSTE7tI", "3Rn5PxMmQeU", "Iy6oflpGbmI", "8S8UzCn_4dQ", "BpwHj5_zT2Q",
    "q6wQ4P4JQFQ", "m3cBt1W4sKo", "Q2nf8lSx4bM", "aDRv5DJNlCE", "z8n4R0WGWSE",
    "kVjn8mmO1qI", "ZhKY8GFBT8E", "vAFkBOoUNS4", "yXJOyO0t2OU", "a5TPWV7FROU",
    "Fp5kvQCqT5k", "w2T4_gBmkxg", "tS0fOjNROMc", "U8vkbHdWMmA", "rj4XzTFPz6k",
    "iPJkbXBBmrQ", "BXMV_K3oYfs", "4MIH1WUZTJE", "hkTIjPmrQxQ", "GFbNHpQPMOc",
    "jEsAkwVhsrc", "EaBe1A5dJ3I", "cHmvDgBxVzo", "iPtOvNmWm-I", "MxMsrBBjnmI",
    "xAFXtJHbWS0", "dxIdTKKuViY", "cMPMXaxSfmE", "HQRzA0MGEOA", "nqA6aTCRNjU",
    "9GkBKzjJMtM", "PwP5GsPOzNg", "qFNMSG1TZJA", "D4g3GOYDg6E", "Aa6-sTG-KAQ",
    "NqpB3rZ0r4A", "xyDrUwnVgfA", "MiULFGMmFpA", "4kbBmhWHbIs", "w_tLTaY3lxc",
]

WORKOUT_SCHEDULE = [
    {"day": 1,  "title": "Full Body 50 Min",      "focus": "Full Body"},
    {"day": 2,  "title": "Lower Body + Abs 50 Min","focus": "Lower Body"},
    {"day": 3,  "title": "Upper Body + Abs 53 Min","focus": "Upper Body"},
    {"day": 4,  "title": "Full Body + Abs 55 Min", "focus": "Full Body"},
    {"day": 5,  "title": "Lower Body 57 Min",      "focus": "Lower Body"},
    {"day": 6,  "title": "Full Body 55 Min",       "focus": "Full Body"},
    {"day": 7,  "title": "Lower Body + Abs",       "focus": "Lower Body"},
    {"day": 8,  "title": "Upper Body + Abs",       "focus": "Upper Body"},
    {"day": 9,  "title": "Full Body",              "focus": "Full Body"},
    {"day": 10, "title": "Lower Body + Abs",       "focus": "Lower Body"},
    # Days 11-50 follow the same rotation pattern
] + [{"day": d, "title": f"Day {d} Workout", "focus": ["Full Body","Lower Body","Upper Body"][(d-1)%3]} for d in range(11, 51)]

EMOJIS = {
    "Full Body":   "💪",
    "Lower Body":  "🦵",
    "Upper Body":  "🙌",
}

MILESTONE_MSGS = {
    5:  "🔥 5 workouts done! You're on fire!",
    10: "⚡ 10 workouts! Double digits — legend!",
    15: "🌟 15 workouts! You're unstoppable!",
    20: "🏅 20 workouts! Halfway to 40 — keep going!",
    25: "🎉 25 workouts! Quarter century club!",
    30: "🥇 30 workouts! You're seriously impressive!",
    40: "🚀 40 workouts! Almost there!",
    50: "🏆 50 workouts COMPLETE! You did it!! INCREDIBLE! 🎊🎊🎊",
}

DATA_LOCK = Lock()

def _read_url_bytes(url: str) -> bytes:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.youtube.com/",
    })
    try:
        with urlopen(req, timeout=10) as resp:
            return resp.read()
    except URLError as e:
        msg = str(e).lower()
        if "certificate_verify_failed" not in msg:
            raise
        insecure_ctx = ssl.create_default_context()
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        with urlopen(req, timeout=10, context=insecure_ctx) as resp:
            return resp.read()

def sync_video_ids_from_playlist() -> bool:
    """Refresh VIDEO_IDS from playlist page text mirror if available."""
    global VIDEO_IDS
    try:
        txt = _read_url_bytes(f"https://r.jina.ai/http://www.youtube.com/playlist?list={PLAYLIST_ID}").decode("utf-8", errors="ignore")
        ids = []
        seen = set()
        for m in re.finditer(r"watch\?v=([A-Za-z0-9_-]{11})", txt):
            vid = m.group(1)
            if vid not in seen:
                seen.add(vid)
                ids.append(vid)
        if len(ids) < 45:
            logging.warning("Playlist sync found too few IDs (%s); keeping hardcoded VIDEO_IDS", len(ids))
            return False
        # Playlist often includes an intro at first position.
        mapped = ids[1:51] if len(ids) >= 51 else ids[:50]
        if len(mapped) < 45:
            logging.warning("Playlist sync mapping too short (%s); keeping hardcoded VIDEO_IDS", len(mapped))
            return False
        VIDEO_IDS = mapped + VIDEO_IDS[len(mapped):]
        logging.info("Playlist sync loaded %s IDs. Day1 video id now: %s", len(mapped), VIDEO_IDS[0] if VIDEO_IDS else "N/A")
        return True
    except Exception as e:
        logging.warning("Playlist sync failed; using hardcoded VIDEO_IDS: %s", e)
        return False

# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data() -> dict:
    with DATA_LOCK:
        if DATA_FILE.exists():
            with open(DATA_FILE) as f:
                return json.load(f)
        return {}

def save_data(data: dict):
    with DATA_LOCK:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)

def get_user(data: dict, uid: int) -> dict:
    key = str(uid)
    if key not in data:
        data[key] = {
            "started": date.today().isoformat(),
            "completed_days": [],   # list of workout day numbers done
            "journal": {},          # {"2026-02-23": "felt great!"}
            "streak": 0,
            "last_workout_date": None,
        }
    return data[key]

def next_workout_day(user: dict) -> int:
    done = set(user["completed_days"])
    for d in range(1, 51):
        if d not in done:
            return d
    return None  # all done!

def is_rest_day(dt: date = None) -> bool:
    if dt is None:
        dt = date.today()
    return dt.weekday() in REST_DAYS

def workout_for_day(n: int) -> dict:
    if 1 <= n <= len(WORKOUT_SCHEDULE):
        return WORKOUT_SCHEDULE[n - 1]
    return {"day": n, "title": f"Day {n}", "focus": "Full Body"}

def video_id_for_day(n: int) -> str | None:
    if 1 <= n <= len(VIDEO_IDS):
        return VIDEO_IDS[n - 1]
    return None

def video_url_for_day(n: int) -> str:
    vid = video_id_for_day(n)
    if vid:
        # +1 because playlist index 1 is usually intro, day 1 starts after it.
        return f"https://www.youtube.com/watch?v={vid}&list={PLAYLIST_ID}&index={n+1}"
    return PLAYLIST_URL

def thumbnail_url_for_day(n: int) -> str | None:
    vid = video_id_for_day(n)
    if not vid:
        return None
    return f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"

def thumbnail_candidates_for_day(n: int) -> list[str]:
    vid = video_id_for_day(n)
    if not vid:
        return []
    return [
        f"https://i.ytimg.com/vi/{vid}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/sddefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/maxresdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/hqdefault.jpg",
        f"https://img.youtube.com/vi/{vid}/mqdefault.jpg",
        f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg",
    ]

def download_thumbnail_bytes(url: str) -> bytes:
    req = Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": "https://www.youtube.com/",
    })

    def _read(context=None) -> bytes:
        with urlopen(req, timeout=8, context=context) as resp:
            data = resp.read()
            # Reject tiny placeholder/error images.
            if len(data) < 1500:
                raise ValueError("thumbnail payload too small")
            return data

    try:
        return _read()
    except URLError as e:
        msg = str(e).lower()
        if "certificate_verify_failed" not in msg:
            raise
        # Local Python SSL trust store is broken/missing. Retry insecure for thumbnails only.
        logging.warning("SSL verify failed for thumbnail URL, retrying insecure: %s", url)
        insecure_ctx = ssl.create_default_context()
        insecure_ctx.check_hostname = False
        insecure_ctx.verify_mode = ssl.CERT_NONE
        return _read(context=insecure_ctx)

async def fetch_thumbnail_file(n: int):
    for url in thumbnail_candidates_for_day(n):
        try:
            data = await asyncio.to_thread(download_thumbnail_bytes, url)
            buf = BytesIO(data)
            buf.seek(0)
            return InputFile(buf, filename=f"day-{n}.jpg")
        except Exception as e:
            logging.info("Thumbnail fetch failed for day %s from %s: %s", n, url, e)
            continue
    logging.warning("All thumbnail sources failed for day %s", n)
    return None

async def send_workout_preview(chat_id: int, bot, user: dict, n: int, preface: str = ""):
    w = workout_for_day(n)
    emoji = EMOJIS.get(w["focus"], "💪")
    rest_line = " _(rest day today — workout tomorrow!)_" if is_rest_day() else ""
    video_url = video_url_for_day(n)
    thumb_file = await fetch_thumbnail_file(n)
    caption = (
        f"{preface}"
        f"{emoji} *Day {n}: {w['title']}*\n"
        f"Focus: {w['focus']}\n"
        f"[▶️ Open Day {n} Video]({video_url})\n\n"
        f"🔥 Streak: *{user['streak']}* | Done: *{len(user['completed_days'])}/50*\n"
        "When you're done, hit /done ✅"
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"▶️ Watch Day {n}", url=video_url)]
    ])
    if thumb_file:
        try:
            await bot.send_photo(
                chat_id=chat_id,
                photo=thumb_file,
                caption=caption,
                parse_mode="Markdown",
                reply_markup=keyboard,
            )
            return
        except Exception as e:
            logging.warning("send_photo failed for day %s: %s", n, e)
    await bot.send_message(
        chat_id=chat_id,
        text=caption,
        parse_mode="Markdown",
        disable_web_page_preview=False,
        reply_markup=keyboard,
    )

async def cmd_test_thumb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    day = 1
    if ctx.args:
        try:
            day = max(1, min(50, int(ctx.args[0])))
        except ValueError:
            pass
    for url in thumbnail_candidates_for_day(day):
        try:
            data = await asyncio.to_thread(download_thumbnail_bytes, url)
            await update.message.reply_text(f"✅ Day {day}\n{url}\nSize: {len(data)} bytes")
        except Exception as e:
            await update.message.reply_text(f"❌ Day {day}\n{url}\n{e}")

def build_done_message(user: dict, n: int) -> tuple[str, InlineKeyboardMarkup | None]:
    done_count = len(user["completed_days"])
    milestone_msg = MILESTONE_MSGS.get(done_count, "")
    w = workout_for_day(n)
    emoji = EMOJIS.get(w["focus"], "💪")
    congrats = (
        f"{emoji} *Day {n} complete!* Great work!\n"
        f"🔥 Streak: *{user['streak']}* days\n"
        f"📊 Total: *{done_count}/50* done\n\n"
    )
    if milestone_msg:
        congrats += f"{milestone_msg}\n\n"
    congrats += "Don't forget to journal how you felt! /journal"
    return congrats, None

def apply_done_for_user(uid: int) -> tuple[bool, str, InlineKeyboardMarkup | None]:
    data = load_data()
    user = get_user(data, uid)
    n = next_workout_day(user)
    if n is None:
        return False, "🏆 You've finished all 50 workouts! AMAZING!", None
    user["completed_days"].append(n)
    today_str = date.today().isoformat()
    if user["last_workout_date"]:
        last = date.fromisoformat(user["last_workout_date"])
        delta = (date.today() - last).days
        if delta == 1 or (delta == 2 and is_rest_day(date.today() - timedelta(1))):
            user["streak"] += 1
        elif delta == 0:
            pass
        else:
            user["streak"] = 1
    else:
        user["streak"] = 1
    user["last_workout_date"] = today_str
    save_data(data)
    msg, kb = build_done_message(user, n)
    return True, msg, kb

def choose_uid(data: dict, requested_uid: str = None) -> str | None:
    if requested_uid:
        return str(requested_uid)
    if not data:
        return None
    if len(data) == 1:
        return next(iter(data.keys()))
    # Stable fallback when multiple users exist.
    return sorted(data.keys())[0]

def sanitize_completed_days(value) -> list[int]:
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for n in value:
        try:
            x = int(n)
        except (TypeError, ValueError):
            continue
        if 1 <= x <= 50 and x not in seen:
            seen.add(x)
            out.append(x)
    out.sort()
    return out


class WebSyncHandler(BaseHTTPRequestHandler):
    server_version = "WorkoutBotSync/1.0"

    def _send_json(self, code: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client closed connection before we wrote the response; safe to ignore.
            pass

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self._send_json(200, {"ok": True, "service": "workout-bot-sync"})
            return
        if parsed.path != "/api/state":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return

        qs = parse_qs(parsed.query)
        requested_uid = qs.get("uid", [None])[0]
        data = load_data()
        uid = choose_uid(data, requested_uid)
        if uid is None:
            self._send_json(404, {"ok": False, "error": "No user data yet. Use /start in Telegram first."})
            return

        user = get_user(data, int(uid))
        save_data(data)
        self._send_json(200, {
            "ok": True,
            "uid": uid,
            "state": {
                "completed_days": user.get("completed_days", []),
                "journal": user.get("journal", {}),
                "streak": user.get("streak", 0),
                "last_workout_date": user.get("last_workout_date"),
                "started": user.get("started"),
            }
        })

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/state":
            self._send_json(404, {"ok": False, "error": "Not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            self._send_json(400, {"ok": False, "error": "Invalid JSON"})
            return

        qs = parse_qs(parsed.query)
        requested_uid = body.get("uid") or qs.get("uid", [None])[0]
        data = load_data()
        uid = choose_uid(data, requested_uid)
        if uid is None:
            self._send_json(400, {"ok": False, "error": "uid is required when there is no existing user data"})
            return

        user = get_user(data, int(uid))
        if "completed_days" in body:
            user["completed_days"] = sanitize_completed_days(body.get("completed_days"))
        if "streak" in body:
            try:
                user["streak"] = max(0, int(body.get("streak", 0)))
            except (TypeError, ValueError):
                pass
        if "last_workout_date" in body:
            lw = body.get("last_workout_date")
            user["last_workout_date"] = lw if isinstance(lw, str) or lw is None else user.get("last_workout_date")
        if "journal" in body and isinstance(body.get("journal"), dict):
            clean = {}
            for k, v in body["journal"].items():
                if isinstance(k, str) and isinstance(v, str):
                    clean[k] = v
            user["journal"] = clean
        save_data(data)
        self._send_json(200, {"ok": True, "uid": uid})

    def log_message(self, fmt, *args):
        logging.debug("web-sync: " + fmt, *args)


def start_web_sync_server():
    server = ThreadingHTTPServer((WEB_SYNC_HOST, WEB_SYNC_PORT), WebSyncHandler)
    t = Thread(target=server.serve_forever, daemon=True)
    t.start()
    logging.info("Web sync API running on http://%s:%s", WEB_SYNC_HOST, WEB_SYNC_PORT)

# ── Visual progress map ───────────────────────────────────────────────────────

def build_progress_map(user: dict) -> str:
    done = set(user["completed_days"])
    total = 50
    cols = 10
    lines = ["📍 *Your 50-Day Progress Map*\n"]
    lines.append("✅ = done   ⬜ = upcoming   🎯 = next up")
    lines.append("")
    next_day = next_workout_day(user)
    for row in range(total // cols):
        row_str = ""
        for col in range(cols):
            n = row * cols + col + 1
            if n in done:
                row_str += "✅"
            elif n == next_day:
                row_str += "🎯"
            else:
                row_str += "⬜"
        start = row * cols + 1
        end   = row * cols + cols
        lines.append(f"`W{start:02d}-{end:02d}` {row_str}")
    done_count = len(done)
    pct = int(done_count / total * 100)
    bar_filled = pct // 5
    bar = "█" * bar_filled + "░" * (20 - bar_filled)
    lines.append(f"\n`[{bar}]` {pct}%")
    lines.append(f"\n🏋️ *{done_count}/50* workouts complete  |  🔥 Streak: *{user['streak']}*")
    return "\n".join(lines)

# ── Handlers ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    uid  = update.effective_user.id
    user = get_user(data, uid)
    save_data(data)
    raw_name = update.effective_user.first_name or "Champion"
    name = escape_markdown(raw_name)
    msg = (
        f"👋 Hey *{name}*! Welcome to your Bigsis 50-Day Workout Challenge Bot! 🎽\n\n"
        "I'll keep you accountable with:\n"
        "• 🌅 Morning reminders (Mon/Wed/Thu/Sat/Sun)\n"
        "• ✅ Workout check-ins\n"
        "• 📓 Daily journaling\n"
        "• 🗺 Visual progress map\n"
        "• 🏆 Milestone rewards\n\n"
        "Commands:\n"
        "/today — Today's workout + preview\n"
        "/done — Mark today's workout complete\n"
        "/next — See your next workout\n"
        "/map — View your progress map\n"
        "/journal — Write or read your journal\n"
        "/journal_read — View recent journal entries\n"
        "/stats — Your stats & streak\n"
        "/skip — Log a skipped day\n\n"
        "Let's go! 💪 Type /next to see your first workout!"
    )
    await update.message.reply_text(msg)

async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    save_data(data)
    n = next_workout_day(user)
    if n is None:
        await update.message.reply_text("🏆 You've completed ALL 50 workouts! INCREDIBLE!!")
        return
    await send_workout_preview(update.effective_chat.id, ctx.bot, user, n)

async def cmd_today(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    save_data(data)
    n = next_workout_day(user)
    if n is None:
        await update.message.reply_text("🏆 You've completed ALL 50 workouts! INCREDIBLE!!")
        return
    await send_workout_preview(
        update.effective_chat.id,
        ctx.bot,
        user,
        n,
        preface="📅 *Today's workout*\n\n"
    )

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    _, msg, _ = apply_done_for_user(update.effective_user.id)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💤 Skipped logged. Rest up and come back stronger tomorrow! 💪\n"
        "Your streak is on pause for today."
    )

async def cmd_map(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    save_data(data)
    msg = build_progress_map(user)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    save_data(data)
    done  = len(user["completed_days"])
    left  = 50 - done
    jcount= len(user["journal"])
    streak= user["streak"]
    started = user.get("started", "unknown")
    msg = (
        f"📊 *Your Stats*\n\n"
        f"🏋️ Workouts done: *{done}/50*\n"
        f"📅 Workouts left: *{left}*\n"
        f"🔥 Current streak: *{streak}*\n"
        f"📓 Journal entries: *{jcount}*\n"
        f"🗓 Challenge started: {started}\n\n"
        "Keep going! 💪"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")


async def cmd_reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Wipe this user's stored progress and journal."""
    data = load_data()
    uid_str = str(update.effective_user.id)
    if uid_str in data:
        data.pop(uid_str, None)
        save_data(data)
        await update.message.reply_text("Your workout data has been cleared. Send /start to begin fresh.")
    else:
        await update.message.reply_text("No stored data found. Send /start to begin.")

async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data  = load_data()
    uid   = update.effective_user.id
    user  = get_user(data, uid)
    save_data(data)
    today = date.today().isoformat()
    existing = user["journal"].get(today)
    if existing:
        await update.message.reply_text(
            f"📓 *Today's entry:*\n_{existing}_\n\n"
            "Send a new message now to overwrite today's entry, or type /journal_read to view recent entries.",
            parse_mode="Markdown"
        )
        ctx.user_data["awaiting_journal"] = True
    else:
        await update.message.reply_text(
            "📓 *Journal*\nNo entry yet for today. Send your entry now, or type /journal_read to view recent entries.",
            parse_mode="Markdown"
        )
        ctx.user_data["awaiting_journal"] = True

async def cmd_journal_read(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    user = get_user(data, update.effective_user.id)
    save_data(data)
    j = user["journal"]
    if not j:
        await update.message.reply_text("No journal entries yet. Work out and write something! 💪")
        return
    recent = sorted(j.keys(), reverse=True)[:5]
    lines = ["📖 *Recent Journal Entries*\n"]
    for d in recent:
        lines.append(f"*{d}:*\n_{j[d]}_\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data_key = query.data

    if data_key == "act_done":
        _, msg, _ = apply_done_for_user(query.from_user.id)
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data_key == "act_next":
        data = load_data()
        user = get_user(data, query.from_user.id)
        save_data(data)
        n = next_workout_day(user)
        if n is None:
            await query.message.reply_text("🏆 You've completed ALL 50 workouts! INCREDIBLE!!")
        else:
            await send_workout_preview(query.message.chat_id, ctx.bot, user, n)

    elif data_key == "act_stats":
        data = load_data()
        user = get_user(data, query.from_user.id)
        save_data(data)
        done = len(user["completed_days"])
        left = 50 - done
        jcount = len(user["journal"])
        streak = user["streak"]
        started = user.get("started", "unknown")
        msg = (
            f"📊 *Your Stats*\n\n"
            f"🏋️ Workouts done: *{done}/50*\n"
            f"📅 Workouts left: *{left}*\n"
            f"🔥 Current streak: *{streak}*\n"
            f"📓 Journal entries: *{jcount}*\n"
            f"🗓 Challenge started: {started}\n\n"
            "Keep going! 💪"
        )
        await query.message.reply_text(msg, parse_mode="Markdown")

    elif data_key == "act_map":
        data = load_data()
        user = get_user(data, query.from_user.id)
        save_data(data)
        await query.message.reply_text(build_progress_map(user), parse_mode="Markdown")

    elif data_key == "act_journal":
        ctx.user_data["awaiting_journal"] = True
        await query.message.reply_text(
            "📓 Write your journal entry below (just send a message):\n"
            "_How did you feel? What was hard? What did you enjoy?_",
            parse_mode="Markdown"
        )

    elif data_key in ("journal_write", ) or data_key.startswith("journal_prompt_"):
        ctx.user_data["awaiting_journal"] = True
        await query.edit_message_text(
            "📓 Write your journal entry below (just send a message):\n"
            "_How did you feel? What was hard? What did you enjoy?_",
            parse_mode="Markdown"
        )

    elif data_key == "journal_read":
        data  = load_data()
        user  = get_user(data, query.from_user.id)
        save_data(data)
        j = user["journal"]
        if not j:
            await query.edit_message_text("No journal entries yet. Work out and write something! 💪")
            return
        recent = sorted(j.keys(), reverse=True)[:5]
        lines  = ["📖 *Recent Journal Entries*\n"]
        for d in recent:
            lines.append(f"*{d}:*\n_{j[d]}_\n")
        await query.edit_message_text("\n".join(lines), parse_mode="Markdown")

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Catch-all: handle journal entries sent as plain text."""
    if ctx.user_data.get("awaiting_journal"):
        ctx.user_data["awaiting_journal"] = False
        data  = load_data()
        uid   = update.effective_user.id
        user  = get_user(data, uid)
        today = date.today().isoformat()
        user["journal"][today] = update.message.text
        save_data(data)
        await update.message.reply_text(
            f"📓 Journal saved for {today}! ✅\n\n"
            "Keep reflecting — it's part of the journey. 🌱"
        )
    else:
        await update.message.reply_text(
            "Not sure what you mean! Try:\n"
            "/next — next workout\n"
            "/done — log a workout\n"
            "/map — progress map\n"
            "/journal — journal\n"
            "/stats — your stats"
        )

# ── Scheduled reminders ───────────────────────────────────────────────────────

async def send_morning_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Called daily by JobQueue. Sends reminders to all users on workout days."""
    if is_rest_day():
        return  # Tuesday or Friday — silent
    data = load_data()
    today = date.today()
    dow_name = today.strftime("%A")
    for uid_str, user in data.items():
        n = next_workout_day(user)
        if n is None:
            continue
        try:
            await send_workout_preview(
                chat_id=int(uid_str),
                bot=ctx.bot,
                user=user,
                n=n,
                preface=f"☀️ Good morning! It's *{dow_name}* — workout day!\n\n"
            )
        except Exception as e:
            logging.warning(f"Could not send reminder to {uid_str}: {e}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()]
    )
    sync_video_ids_from_playlist()

    app = Application.builder().token(BOT_TOKEN).build()
    start_web_sync_server()

    # Commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("today",   cmd_today))
    app.add_handler(CommandHandler("next",    cmd_next))
    app.add_handler(CommandHandler("done",    cmd_done))
    app.add_handler(CommandHandler("map",     cmd_map))
    app.add_handler(CommandHandler("stats",   cmd_stats))
    app.add_handler(CommandHandler("reset",   cmd_reset))
    app.add_handler(CommandHandler("journal", cmd_journal))
    app.add_handler(CommandHandler("journal_read", cmd_journal_read))
    app.add_handler(CommandHandler("skip",    cmd_skip))

    # Callbacks & messages
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # Daily morning reminder at 07:00
    app.job_queue.run_daily(
        send_morning_reminders,
        time=datetime.strptime(f"{REMINDER_HOUR:02d}:{REMINDER_MINUTE:02d}", "%H:%M").time(),
        name="morning_reminder"
    )

    print("🤖 Workout Bot is running...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
