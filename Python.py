#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Krishna_Zx_Store — Complete Clean Bot
Secrets are loaded ONLY from .env (see .env.example)

Run:    python3 ZxStore.py
"""

import asyncio
import email
import html
import imaplib
import logging
import os
import random
import re
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email import policy
from email.utils import parsedate_to_datetime
from io import BytesIO
from urllib.parse import urlencode

import qrcode
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, NetworkError, TelegramError, TimedOut
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters,
)

# ═══════════════════════════════════════════════════════════
# CONFIG — ALL FROM .env (no hardcoded secrets)
# ═══════════════════════════════════════════════════════════
load_dotenv()


def _require_env(key: str) -> str:
    val = os.getenv(key, "").strip()
    if not val:
        print(f"❌ FATAL: '{key}' missing in .env file!", flush=True)
        print(f"   → Set {key}=your_value in .env", flush=True)
        sys.exit(1)
    return val


def _optional_env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _optional_env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)).strip())
    except ValueError:
        return default


def _optional_env_bool(key: str, default: bool) -> bool:
    return os.getenv(key, str(default)).strip().lower() == "true"


BOT_TOKEN = _require_env("BOT_TOKEN")
ADMIN_ID = int(_require_env("ADMIN_ID"))
UPI_ID = _require_env("UPI_ID")
STORE_NAME = _optional_env("STORE_NAME", "Krishna_Zx_Store")
BOT_USERNAME = _optional_env("BOT_USERNAME", "@Krishna_Zx_Store_bot")
CURRENCY = _optional_env("CURRENCY", "INR")
DB_PATH = _optional_env("DB_PATH", "krishna_zx_store.db")

GMAIL_ADDRESS = _optional_env("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = _optional_env("GMAIL_APP_PASSWORD").replace(" ", "")
IMAP_HOST = _optional_env("IMAP_HOST", "imap.gmail.com")
PAYMENT_SENDERS = [
    s.strip().lower()
    for s in _optional_env("PAYMENT_SENDERS", "fampay,npci,yesbank,axisbank,icicibank,hdfcbank,sbi").split(",")
    if s.strip()
]
REQUIRE_DKIM = _optional_env_bool("REQUIRE_DKIM", True)
MAIL_LOOKBACK_DAYS = _optional_env_int("MAIL_LOOKBACK_DAYS", 2)
EMAIL_POLL_SECONDS = _optional_env_int("EMAIL_POLL_SECONDS", 30)
EMAIL_POLL_ROUNDS = _optional_env_int("EMAIL_POLL_ROUNDS", 20)
AUTO_APPROVE = _optional_env_bool("AUTO_APPROVE", True)

# Startup config log
print("=" * 60, flush=True)
print("🔍 CONFIG CHECK", flush=True)
print(f"   BOT_TOKEN:      {'✅ set' if BOT_TOKEN else '❌ missing'}", flush=True)
print(f"   ADMIN_ID:       {ADMIN_ID}", flush=True)
print(f"   UPI_ID:         {UPI_ID}", flush=True)
print(f"   STORE_NAME:     {STORE_NAME}", flush=True)
print(f"   GMAIL:          {'✅ set' if GMAIL_ADDRESS else '⚠️ not set (manual verify)'}", flush=True)
print(f"   AUTO_APPROVE:   {AUTO_APPROVE}", flush=True)
print(f"   REQUIRE_DKIM:   {REQUIRE_DKIM}", flush=True)
print(f"   DB_PATH:        {DB_PATH}", flush=True)
print("=" * 60, flush=True)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
log = logging.getLogger("krishna_zx_store")

# ═══════════════════════════════════════════════════════════
# THEME + STATES
# ═══════════════════════════════════════════════════════════
STRIPE = "🟥🟧🟨🟩🟦🟪🟥🟧🟨🟩🟦🟪"
COLOR_SEP = "▰▱▰▱▰▱▰▱▰▱▰"
SPARKLE = "✨🌟💫"
STATUS_ICON = {"AWAITING_PAYMENT": "🟡", "UNDER_REVIEW": "🔵", "PAID": "🟢", "DELIVERED": "✅", "FAILED": "🟤"}

ENTER_UTR = 1
ADD_NAME, ADD_AMOUNT, ADD_FILE, ADD_URLS = 2, 3, 4, 5
BROADCAST_TEXT = 10
QR_GEN_TEXT = 20
UTR_RE = re.compile(r"[A-Za-z0-9]{8,40}")
BACKGROUND_TASKS = set()


def admin_only_filter():
    return filters.User(user_id=ADMIN_ID)


def h(text) -> str:
    return html.escape(str(text))


def card(title, lines) -> str:
    body = "\n".join(lines) if isinstance(lines, (list, tuple)) else str(lines)
    return f"{STRIPE}\n<b>{SPARKLE} {h(title)} {SPARKLE}</b>\n{COLOR_SEP}\n\n{body}\n\n{COLOR_SEP}\n{STRIPE}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return user is not None and user.id == ADMIN_ID


def spawn(coro) -> None:
    task = asyncio.create_task(coro)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


# ═══════════════════════════════════════════════════════════
# DATABASE
# ═══════════════════════════════════════════════════════════
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, username TEXT, full_name TEXT, joined_at TEXT);
CREATE TABLE IF NOT EXISTS products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount INTEGER NOT NULL, file_id TEXT, file_type TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS product_files (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER REFERENCES products(id) ON DELETE CASCADE, file_id TEXT NOT NULL, file_type TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS product_urls (id INTEGER PRIMARY KEY AUTOINCREMENT, product_id INTEGER REFERENCES products(id) ON DELETE CASCADE, url TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS used_utrs (utr TEXT PRIMARY KEY, user_id INTEGER, verified_at TEXT);
CREATE TABLE IF NOT EXISTS orders (id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL REFERENCES products(id), amount INTEGER NOT NULL, utr TEXT UNIQUE, status TEXT NOT NULL, created_at TEXT NOT NULL, verified_at TEXT, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL REFERENCES orders(id), source TEXT NOT NULL, amount INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS verification_events (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, verdict TEXT NOT NULL, reason TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_logs (id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, user_id INTEGER NOT NULL, success INTEGER NOT NULL, details TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
"""


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate_db():
    needed = [
        ("products", "created_at", "TEXT"), ("used_utrs", "verified_at", "TEXT"),
        ("orders", "verified_at", "TEXT"), ("orders", "delivered_at", "TEXT"),
        ("payments", "amount", "INTEGER"), ("verification_events", "id", "INTEGER"),
        ("delivery_logs", "id", "INTEGER"),
    ]
    with db() as conn:
        for table, col, ctype in needed:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
            if col not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ctype}")


def init_db():
    with db() as conn:
        conn.executescript(SCHEMA)
    migrate_db()


def track_user(user):
    if not user:
        return
    with db() as conn:
        conn.execute(
            "INSERT INTO users (user_id, username, full_name, joined_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
            (user.id, user.username, user.full_name, now_iso()),
        )


# ═══════════════════════════════════════════════════════════
# FONTS + IMAGE HELPERS
# ═══════════════════════════════════════════════════════════
FONT_CANDIDATES = ["DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
FONT_CACHE = {}


def load_font(size):
    try:
        return FONT_CACHE[size]
    except KeyError:
        for path in FONT_CANDIDATES:
            try:
                f = ImageFont.truetype(path, size)
                FONT_CACHE[size] = f
                return f
            except OSError:
                continue
        try:
            f = ImageFont.load_default(size=size)
        except TypeError:
            f = ImageFont.load_default()
        FONT_CACHE[size] = f
        return f


W, H = 1000, 1500
RAINBOW_HEX = ["#FF4D6D", "#FF9F1C", "#FFD93D", "#2EC4B6", "#4D96FF", "#9B5DE5"]
BG_STOPS = [(255, 77, 109), (255, 159, 28), (155, 93, 229), (40, 90, 230)]
FRAME_STOPS = [(255, 77, 109), (255, 159, 28), (255, 217, 61), (46, 196, 182), (77, 150, 255), (155, 93, 229)]
QR_STOPS = [(55, 0, 120), (15, 50, 130), (120, 10, 80)]


def lerp_stops(stops, t):
    t = min(max(t, 0.0), 1.0)
    seg = t * (len(stops) - 1)
    i = min(int(seg), len(stops) - 2)
    f = seg - i
    a, b = stops[i], stops[i + 1]
    return tuple(round(a[k] + (b[k] - a[k]) * f) for k in range(3))


def diagonal_gradient(w, hgt, stops):
    img = Image.new("RGBA", (w, hgt))
    d = ImageDraw.Draw(img)
    total = w + hgt
    for k in range(total):
        d.line([(k, 0), (0, k)], fill=lerp_stops(stops, k / max(1, total - 1)) + (255,))
    return img


def rounded_mask(w, hgt, radius):
    mask = Image.new("L", (w, hgt), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, hgt - 1), radius=radius, fill=255)
    return mask


def gradient_box(w, hgt, radius, stops):
    box = diagonal_gradient(w, hgt, stops)
    box.putalpha(rounded_mask(w, hgt, radius))
    return box


def text_center(img, text, y, size, fill, shadow=True):
    d = ImageDraw.Draw(img)
    f = load_font(size)
    left, _, right, _ = d.textbbox((0, 0), text, font=f)
    x = (img.width - (right - left)) / 2 - left
    if shadow:
        d.text((x + 2, y + 3), text, font=f, fill=(30, 0, 60))
    d.text((x, y), text, font=f, fill=fill)


def is_finder_module(r, c, n):
    return (r < 7 and c < 7) or (r < 7 and c >= n - 7) or (r >= n - 7 and c < 7)


def build_qr(data, target, stops=None):
    if stops is None:
        stops = QR_STOPS
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_H, box_size=1, border=0)
    qr.add_data(data)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    px = max(4, target // n)
    img = Image.new("RGBA", (n * px, n * px), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for r, row in enumerate(matrix):
        for c, on in enumerate(row):
            if not on:
                continue
            x, y = c * px, r * px
            color = lerp_stops(stops, (c + r) / max(1, 2 * (n - 1))) + (255,)
            if is_finder_module(r, c, n):
                d.rectangle([x, y, x + px - 1, y + px - 1], fill=color)
            else:
                d.rounded_rectangle([x, y, x + px - 1, y + px - 1], radius=max(1, px // 4), fill=color)
    return img, n, px


def center_badge(img, n, px):
    d = ImageDraw.Draw(img)
    bs = max(76, int((n * px) * 0.16))
    cx, cy = img.width // 2, img.height // 2
    d.rounded_rectangle((cx - bs // 2, cy - bs // 2, cx + bs // 2, cy + bs // 2), radius=bs // 5, fill=(255, 255, 255, 255))
    d.rounded_rectangle((cx - bs // 2, cy - bs // 2, cx + bs // 2, cy + bs // 2), radius=bs // 5, outline=(123, 47, 247, 255), width=4)
    f = load_font(int(bs * 0.55))
    d.text((cx, cy), "K", font=f, fill=(255, 159, 28), anchor="mm")


def background():
    img = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp_stops(BG_STOPS, y / (H - 1)) + (255,))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    g.ellipse((-220, -240, 360, 340), fill=(255, 255, 255, 70))
    g.ellipse((680, 1000, 1240, 1560), fill=(255, 230, 102, 80))
    g.ellipse((-200, 1150, 320, 1670), fill=(0, 255, 200, 60))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(45)))
    d = ImageDraw.Draw(img)
    stripe = 18
    n = len(RAINBOW_HEX)
    for i, color in enumerate(RAINBOW_HEX):
        x0, x1 = i * W // n, (i + 1) * W // n
        d.rectangle([x0, 0, x1, stripe], fill=color)
        d.rectangle([x0, H - stripe, x1, H], fill=color)
    return img


def make_payment_card(product_name, amount, order_id):
    upi_link = "upi://pay?" + urlencode({"pa": UPI_ID, "pn": STORE_NAME, "am": f"{amount}.00", "cu": CURRENCY, "tn": order_id})
    img = background()
    text_center(img, STORE_NAME.upper(), 60, 58, (255, 255, 255))
    text_center(img, "SCAN & PAY", 128, 34, (255, 230, 102))
    FX, FY, FS = 130, 190, 740
    img.alpha_composite(gradient_box(FS, FS, 70, FRAME_STOPS), (FX, FY))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((FX, FY, FX + FS - 1, FY + FS - 1), radius=70, outline=(255, 255, 255, 255), width=6)
    PS = FS - 60
    panel = Image.new("RGBA", (PS, PS), (255, 255, 255, 255))
    panel.putalpha(rounded_mask(PS, PS, 50))
    img.alpha_composite(panel, (FX + (FS - PS) // 2, FY + (FS - PS) // 2))
    qr, nq, pq = build_qr(upi_link, 560)
    center_badge(qr, nq, pq)
    img.alpha_composite(qr, (FX + (FS - qr.width) // 2, FY + (FS - qr.height) // 2))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((300, 960, 700, 1060), radius=50, fill=(255, 230, 102, 255))
    text_center(img, f"Rs. {amount}", 978, 56, (43, 11, 82), shadow=False)
    text_center(img, product_name[:30], 1085, 40, (255, 255, 255))
    text_center(img, f"UPI: {UPI_ID}", 1145, 40, (255, 255, 255))
    d.rounded_rectangle((280, 1200, 720, 1265), radius=30, fill=(123, 47, 247, 255))
    text_center(img, f"Order: {order_id}", 1212, 30, (255, 255, 255), shadow=False)
    text_center(img, "Payment ke baad 12-digit UTR bot mein bhejein", 1310, 28, (255, 255, 255))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "payment.png"
    return buf


QR_COLOR_SCHEMES = {
    "rainbow": [(255, 77, 109), (255, 159, 28), (255, 217, 61), (46, 196, 182), (77, 150, 255), (155, 93, 229)],
    "ocean":   [(0, 119, 182), (0, 180, 216), (72, 202, 228), (144, 224, 239), (3, 4, 94)],
    "sunset":  [(255, 94, 98), (255, 153, 102), (255, 211, 105), (255, 105, 180), (199, 36, 100)],
    "neon":    [(255, 0, 128), (0, 255, 200), (255, 255, 0), (138, 43, 226), (0, 191, 255)],
    "forest":  [(34, 139, 34), (60, 179, 113), (152, 251, 152), (0, 100, 0), (85, 107, 47)],
}
SCHEME_EMOJI = {"rainbow": "🌈", "ocean": "🌊", "sunset": "🌅", "neon": "💜", "forest": "🌲"}


def make_colorful_qr_card(data, scheme="rainbow", title="MY QR CODE"):
    stops = QR_COLOR_SCHEMES.get(scheme, QR_COLOR_SCHEMES["rainbow"])
    Wq, Hq = 1000, 1300
    img = Image.new("RGBA", (Wq, Hq))
    d = ImageDraw.Draw(img)
    for y in range(Hq):
        d.line([(0, y), (Wq, y)], fill=lerp_stops(stops, y / (Hq - 1)) + (255,))
    glow = Image.new("RGBA", (Wq, Hq), (0, 0, 0, 0))
    g = ImageDraw.Draw(glow)
    rng = random.Random(hash((data, scheme)) % 1000000)
    for _ in range(140):
        x = rng.randint(0, Wq)
        y = rng.randint(0, Hq)
        r = rng.randint(3, 12)
        g.ellipse((x - r, y - r, x + r, y + r), fill=(255, 255, 255, rng.randint(50, 150)))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(2)))
    panel_w, panel_h = 840, 840
    px_panel = (Wq - panel_w) // 2
    py_panel = 200
    frame = gradient_box(panel_w + 24, panel_h + 24, 70, stops)
    img.alpha_composite(frame, (px_panel - 12, py_panel - 12))
    panel = Image.new("RGBA", (panel_w, panel_h), (255, 255, 255, 245))
    panel.putalpha(rounded_mask(panel_w, panel_h, 60))
    img.alpha_composite(panel, (px_panel, py_panel))
    qr, nq, pq = build_qr(data, 700, stops=stops)
    qr_x = px_panel + (panel_w - qr.width) // 2
    qr_y = py_panel + (panel_h - qr.height) // 2
    img.alpha_composite(qr, (qr_x, qr_y))
    d = ImageDraw.Draw(img)
    badge = 120
    cx, cy = px_panel + panel_w // 2, py_panel + panel_h // 2
    d.rounded_rectangle((cx - badge // 2, cy - badge // 2, cx + badge // 2, cy + badge // 2), radius=25, fill=(255, 255, 255, 255), outline=lerp_stops(stops, 0.5) + (255,), width=6)
    f = load_font(int(badge * 0.5))
    d.text((cx, cy), "K", font=f, fill=lerp_stops(stops, 0.4) + (255,), anchor="mm")
    text_center(img, title[:30], 50, 60, (255, 255, 255))
    text_center(img, f"{SCHEME_EMOJI.get(scheme, '🎨')} {scheme.title()} Theme {SCHEME_EMOJI.get(scheme, '🎨')}", 140, 34, (255, 255, 255))
    text_center(img, f"Data: {data[:42]}" + ("..." if len(data) > 42 else ""), 1090, 32, (255, 255, 255))
    text_center(img, f"🎨 Made by {STORE_NAME}", 1155, 32, (255, 255, 255))
    text_center(img, "Scan me with any QR reader ✨", 1215, 30, (255, 255, 255))
    buf = BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    buf.seek(0)
    buf.name = "colorful_qr.png"
    return buf


# ═══════════════════════════════════════════════════════════
# EMAIL VERIFICATION
# ═══════════════════════════════════════════════════════════
def mail_configured():
    return bool(GMAIL_ADDRESS and GMAIL_APP_PASSWORD)


def _text_of(msg):
    chunks = []
    for part in (msg.walk() if msg.is_multipart() else [msg]):
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True)
        if not raw:
            payload = part.get_payload()
            if isinstance(payload, str):
                chunks.append(payload)
            continue
        try:
            chunks.append(raw.decode(part.get_content_charset() or "utf-8", errors="ignore"))
        except LookupError:
            chunks.append(raw.decode("utf-8", errors="ignore"))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", " ".join(chunks))).strip()


VERDICT_AMOUNT_RE = re.compile(r"(?<![\d.,])(?:₹|Rs\.?|INR)?\s?(\d+(?:\.\d{1,2})?)(?![\d.,])", re.IGNORECASE)


def _candidate_score(msg, utr, amount):
    body = _text_of(msg)
    text = (body + " " + str(msg.get("Subject", ""))).strip()
    utr_re = re.compile(rf"(?<![A-Za-z0-9]){re.escape(utr)}(?![A-Za-z0-9])", re.IGNORECASE)
    if not utr_re.search(text):
        return (False, False, None, None)
    parsed = None
    for m in VERDICT_AMOUNT_RE.finditer(text):
        v = float(m.group(1))
        if v == float(amount):
            parsed = amount
            break
        elif parsed is None:
            parsed = v
    amt_found = (parsed == amount)
    receiver_ok = True
    body_lower = body.lower()
    for key in ("receiver:", "to ", "beneficiary ", "account "):
        idx = body_lower.find(key)
        if idx != -1:
            fragment = body_lower[idx + len(key): idx + len(key) + 50].strip()
            if fragment and UPI_ID.lower() not in fragment:
                receiver_ok = False
            break
    return (True, amt_found, receiver_ok, parsed)


def _verify_single(msg, utr, amount, created_at):
    from_addr = str(msg.get("From", ""))
    m = re.search(r"<([^>]+)>", from_addr)
    addr = m.group(1).lower() if m else from_addr.lower()
    domain = addr.split("@")[-1].strip() if "@" in addr else addr.strip()
    if not any(k in domain for k in PAYMENT_SENDERS):
        return ("REJECTED_UNTRUSTED", f"untrusted domain: {domain}")
    auth = str(msg.get("Authentication-Results", "")).lower()
    if REQUIRE_DKIM:
        if "dkim=pass" not in auth:
            return ("REJECTED_UNTRUSTED", "dkim=pass missing")
        dkim_domain = None
        for tok in auth.split():
            if tok.startswith("header.d=") or tok.startswith("dkim=s"):
                dkim_domain = tok.split("=", 1)[1].strip(";").lower()
                break
        if dkim_domain and dkim_domain not in domain and domain not in dkim_domain:
            return ("REJECTED_UNTRUSTED", f"dkim {dkim_domain} != {domain}")
    found, amt_found, receiver_ok, parsed = _candidate_score(msg, utr, amount)
    if not found:
        return ("NOT_FOUND", "UTR not found")
    if not amt_found:
        return ("NOT_FOUND", f"amount {parsed} != {amount}")
    if receiver_ok is False:
        return ("NOT_FOUND", "receiver mismatch")
    try:
        dt = parsedate_to_datetime(msg.get("Date"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        cutoff = datetime.fromisoformat(created_at).astimezone(timezone.utc) - timedelta(days=MAIL_LOOKBACK_DAYS)
        if dt < cutoff:
            return ("NOT_FOUND", "too old")
    except Exception:
        pass
    return ("VERIFIED", "all checks passed")


def _scan_inbox(utr, amount, created_at):
    results = []
    utr_re = re.compile(rf"(?<![A-Za-z0-9]){re.escape(utr)}(?![A-Za-z0-9])", re.IGNORECASE)
    since = (datetime.now(timezone.utc) - timedelta(days=MAIL_LOOKBACK_DAYS)).strftime("%d-%b-%Y")
    imap = imaplib.IMAP4_SSL(IMAP_HOST, timeout=30)
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        _, data = imap.search(None, "SINCE", since)
        for num in reversed((data[0] or b"").split()[-100:]):
            _, fetched = imap.fetch(num, "(RFC822)")
            if not fetched or not isinstance(fetched[0], tuple):
                continue
            msg = email.message_from_bytes(fetched[0][1], policy=policy.default)
            sender = str(msg.get("From", "")).lower()
            if not any(k in sender for k in PAYMENT_SENDERS):
                continue
            auth = str(msg.get("Authentication-Results", "")).lower()
            if REQUIRE_DKIM and "dkim=pass" not in auth:
                continue
            text = _text_of(msg) + " " + str(msg.get("Subject", ""))
            if utr_re.search(text):
                verdict, reason = _verify_single(msg, utr, amount, created_at)
                results.append((msg, verdict, reason))
        return results
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _write_verification_event(order_id, verdict, reason, source):
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO verification_events (order_id, verdict, reason, source, created_at) VALUES (?, ?, ?, ?, ?)",
                (order_id, verdict, reason, source, now_iso()),
            )
    except Exception as e:
        log.warning("verification_events write fail (%s): %s", order_id, e)


async def email_matches(order_id, utr, amount, created_at):
    try:
        candidates = await asyncio.to_thread(_scan_inbox, utr, amount, created_at)
    except Exception as e:
        log.warning("IMAP scan error: %s", e)
        candidates = []
    if not candidates:
        verdict, reason = "NOT_FOUND", "no candidate"
    elif any(c[1] == "VERIFIED" for c in candidates):
        verdict, reason = "VERIFIED", "all checks passed"
    else:
        verdict, reason = candidates[0][1], candidates[0][2]
    _write_verification_event(order_id, verdict, reason, "email")
    return verdict, reason


# ═══════════════════════════════════════════════════════════
# SAFE SEND HELPERS
# ═══════════════════════════════════════════════════════════
def _strip_tags(text):
    return re.sub(r"<[^>]+>", "", text or "")


async def send(bot, chat_id, text, kb=None):
    try:
        return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except (TimedOut, NetworkError) as e:
        log.warning("Network error, retrying: %s", e)
        await asyncio.sleep(2)
        try:
            return await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramError:
            return None
    except BadRequest as e:
        if "can't parse entities" in str(e) or "unsupported start tag" in str(e).lower():
            log.warning("HTML parse error, plain fallback: %s", e)
            return await bot.send_message(chat_id, _strip_tags(text), reply_markup=kb)
        log.error("BadRequest: %s", e)
        return None
    except TelegramError as e:
        log.warning("Send fail: %s", e)
        return None


async def send_photo_safe(bot, chat_id, photo, caption="", kb=None):
    try:
        return await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
    except (TimedOut, NetworkError) as e:
        log.warning("Network error on photo: %s", e)
        await asyncio.sleep(2)
        try:
            return await bot.send_photo(chat_id, photo=photo, caption=caption, parse_mode=ParseMode.HTML, reply_markup=kb)
        except TelegramError:
            return None
    except BadRequest as e:
        if "can't parse entities" in str(e) or "unsupported start tag" in str(e).lower():
            log.warning("Caption parse error, plain: %s", e)
            return await bot.send_photo(chat_id, photo=photo, caption=_strip_tags(caption), reply_markup=kb)
        log.error("Photo BadRequest: %s", e)
        return None
    except TelegramError as e:
        log.warning("Photo fail: %s", e)
        return None


async def edit_text_safe(query, text, kb=None):
    try:
        return await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
    except BadRequest as e:
        if "can't parse entities" in str(e) or "unsupported start tag" in str(e).lower():
            log.warning("Edit parse error, plain: %s", e)
            try:
                return await query.edit_message_text(_strip_tags(text), reply_markup=kb)
            except TelegramError:
                return None
        if "message is not modified" in str(e).lower():
            return None
        log.warning("Edit fail: %s", e)
        return None
    except TelegramError as e:
        log.warning("Edit fail: %s", e)
        return None


# ═══════════════════════════════════════════════════════════
# KEYBOARDS
# ═══════════════════════════════════════════════════════════
BACK = InlineKeyboardMarkup([[InlineKeyboardButton("🏠 Main Menu", callback_data="menu")]])


def main_menu(user_id):
    rows = [
        [InlineKeyboardButton("🛍️ Browse Products", callback_data="shop"),
         InlineKeyboardButton("📦 My Purchases", callback_data="my_orders")],
        [InlineKeyboardButton("🎨 Colorful QR Generator", callback_data="qr_gen"),
         InlineKeyboardButton("🖼️ Image Gallery", callback_data="gallery_0")],
        [InlineKeyboardButton("📢 Contact Admin", callback_data="contact"),
         InlineKeyboardButton("ℹ️ About Store", callback_data="about")],
    ]
    if user_id == ADMIN_ID:
        rows.append([InlineKeyboardButton("🧠 Admin Panel", callback_data="admin_home")])
    return InlineKeyboardMarkup(rows)


def order_admin_text(o):
    return card("Payment To Verify", [
        f"🆔 <code>{h(o['id'])}</code>",
        f"👤 User: <code>{o['user_id']}</code>",
        f"🎁 {h(o['name'])}",
        f"💰 ₹{o['amount']}",
        f"🧾 UTR: <code>{h(o['utr'])}</code>",
        "", "FamPay/bank app mein verify karein.",
    ])


def order_admin_kb(order_id):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve & Deliver", callback_data=f"approve:{order_id}"),
        InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order_id}"),
    ]])


def retry_kb(order_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔁 Retry Delivery", callback_data=f"retry:{order_id}")]])


# ═══════════════════════════════════════════════════════════
# USER HANDLERS
# ═══════════════════════════════════════════════════════════
async def start(update, context):
    try:
        user = update.effective_user
        chat = update.effective_chat
        if not user or not chat:
            return
        track_user(user)
        await send(context.bot, chat.id, card(f"Welcome to {STORE_NAME}", [
            "✨ Premium digital products store",
            "⚡ Instant delivery after payment verification",
            f"📥 Bot: {BOT_USERNAME}",
            "🔐 Secure FamPay UPI payment",
            "🎨 Colorful QR Generator available!",
            "", "👇 Neeche se option chunein",
        ]), main_menu(user.id))
    except Exception as e:
        log.exception("START error: %s", e)


async def menu_cb(update, context):
    try:
        await update.callback_query.answer()
    except Exception:
        pass
    await start(update, context)


async def show_shop(update, context):
    q = update.callback_query
    await q.answer()
    with db() as conn:
        products = conn.execute("SELECT id, name, amount FROM products ORDER BY id DESC").fetchall()
    if not products:
        await send(context.bot, q.message.chat_id, card("Products", ["Abhi koi product nahi hai. Jaldi aayega! 🙏🏻"]), BACK)
        return
    rows = [[InlineKeyboardButton(f"🎁 {p['name']} ₹{p['amount']}", callback_data=f"buy_{p['id']}")] for p in products]
    rows.append([InlineKeyboardButton("🏠 Main Menu", callback_data="menu")])
    await send(context.bot, q.message.chat_id, card("Products", ["Product chunein 👇"]), InlineKeyboardMarkup(rows))


async def my_orders(update, context):
    q = update.callback_query
    await q.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT o.id, o.amount, o.status, p.name FROM orders o JOIN products p ON p.id = o.product_id "
            "WHERE o.user_id = ? ORDER BY o.created_at DESC LIMIT 10", (q.from_user.id,)
        ).fetchall()
    if not rows:
        lines = ["Abhi tak koi order nahi hai."]
    else:
        lines = [f"{STATUS_ICON.get(r['status'], '•')} <b>{h(r['name'])}</b> • ₹{r['amount']}\n    🆔 <code>{r['id']}</code> — {r['status']}" for r in rows]
    await send(context.bot, q.message.chat_id, card("My Purchases", lines), BACK)


async def contact(update, context):
    q = update.callback_query
    await q.answer()
    support = "Apna Order ID bhejein, admin jald reply karega."
    with db() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'support_text'").fetchone()
        if row:
            support = row["value"]
    await send(context.bot, q.message.chat_id, card("Contact Admin", [h(support)]), BACK)


async def about_store(update, context):
    q = update.callback_query
    await q.answer()
    await send(context.bot, q.message.chat_id, card("About Store", [
        f"<b>Store:</b> {STORE_NAME}",
        f"<b>Bot:</b> {BOT_USERNAME}",
        f"<b>Currency:</b> {CURRENCY}",
        f"<b>UPI:</b> {UPI_ID}",
        "", "Automated digital delivery + UTR verification.",
        "🎨 Colorful QR Generator included!",
    ]), BACK)


async def image_gallery(update, context):
    q = update.callback_query
    await q.answer()
    try:
        idx = int(q.data.split("_")[1])
    except (IndexError, ValueError):
        idx = 0
    with db() as conn:
        urls = conn.execute("SELECT id, url FROM product_urls ORDER BY id DESC").fetchall()
    if not urls:
        await send(context.bot, q.message.chat_id, card("Gallery", ["Koi image nahi hai."]), BACK)
        return
    idx = max(0, min(idx, len(urls) - 1))
    cur = urls[idx]
    kb_rows = []
    nav = []
    if idx > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"gallery_{idx - 1}"))
    if idx < len(urls) - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"gallery_{idx + 1}"))
    if nav:
        kb_rows.append(nav)
    kb_rows.append([InlineKeyboardButton("🏠 Menu", callback_data="menu")])
    caption = card("Image Gallery", [f"<b>Image {idx + 1}/{len(urls)}</b>", f"🔗 <code>{h(cur['url'])}</code>"])
    await send_photo_safe(context.bot, q.message.chat_id, cur["url"], caption, InlineKeyboardMarkup(kb_rows))


# ═══════════════════════════════════════════════════════════
# BUY FLOW
# ═══════════════════════════════════════════════════════════
async def buy_product(update, context):
    q = update.callback_query
    await q.answer()
    try:
        product_id = int(q.data.split("_", 1)[1])
    except (IndexError, ValueError):
        return ConversationHandler.END
    with db() as conn:
        product = conn.execute("SELECT id, name, amount FROM products WHERE id = ?", (product_id,)).fetchone()
        if product is None:
            await send(context.bot, q.message.chat_id, card("Oops", ["Ye product unavailable hai."]), BACK)
            return ConversationHandler.END
        order_id = "KX" + secrets.token_hex(4).upper()
        conn.execute(
            "INSERT INTO orders (id, user_id, product_id, amount, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (order_id, q.from_user.id, product["id"], product["amount"], "AWAITING_PAYMENT", now_iso()),
        )
    qr_png = make_payment_card(product["name"], product["amount"], order_id)
    caption = card("Payment Details", [
        f"❓ <b>Product:</b> {h(product['name'])}",
        f"💰 <b>Amount:</b> ₹{product['amount']}",
        f"🆔 <b>Order ID:</b> <code>{order_id}</code>",
        "", "1☝ QR scan karein ya UPI par exact ₹ bhejein",
        "2☝ Payment ke baad <b>12-digit UTR</b> yahan bhejein",
    ])
    await send_photo_safe(context.bot, q.message.chat_id, qr_png, caption)
    context.user_data["order_id"] = order_id
    context.user_data.pop("qr_scheme", None)
    return ENTER_UTR


async def receive_utr(update, context):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    utr = update.message.text.strip().upper()
    order_id = context.user_data.get("order_id")
    if not order_id:
        await send(context.bot, chat_id, card("Session Expired", ["/start karke dobara order karein."]), BACK)
        return ConversationHandler.END
    if not UTR_RE.fullmatch(utr):
        await send(context.bot, chat_id, card("Galat UTR", ["8-40 letters/numbers ka UTR bhejein."]))
        return ENTER_UTR
    order = None
    result = None
    with db() as conn:
        if conn.execute("SELECT 1 FROM used_utrs WHERE utr = ?", (utr,)).fetchone():
            result = "dup"
        elif conn.execute("SELECT 1 FROM orders WHERE utr = ?", (utr,)).fetchone():
            result = "dup"
        else:
            try:
                updated = conn.execute(
                    "UPDATE orders SET utr = ?, status = 'UNDER_REVIEW' WHERE id = ? AND user_id = ? AND status = 'AWAITING_PAYMENT'",
                    (utr, order_id, user_id),
                ).rowcount
                if updated:
                    conn.execute("INSERT OR IGNORE INTO used_utrs (utr, user_id, verified_at) VALUES (?, ?, ?)",
                                 (utr, user_id, now_iso()))
                result = "ok" if updated else "invalid"
            except sqlite3.IntegrityError:
                result = "dup"
        if result == "ok":
            order = conn.execute(
                "SELECT o.id, o.user_id, o.utr, o.amount, o.created_at, p.name FROM orders o "
                "JOIN products p ON p.id = o.product_id WHERE o.id = ?", (order_id,),
            ).fetchone()
    context.user_data.pop("order_id", None)
    if result == "dup":
        await send(context.bot, chat_id, card("UTR Already Used", ["Ye UTR pehle se submit ho chuka hai."]))
        context.user_data["order_id"] = order_id
        return ENTER_UTR
    if result == "invalid" or order is None:
        await send(context.bot, chat_id, card("Order Invalid", ["/start se naya order karein."]), BACK)
        return ConversationHandler.END
    checking = "⏳ Email se verify ho raha hai..." if mail_configured() else "⏳ Admin check karke deliver karega."
    await send(context.bot, chat_id, card("Payment Under Review", [
        f"🆔 <code>{h(order_id)}</code>",
        f"🎁 {h(order['name'])} • ₹{order['amount']}",
        "", checking, "Status: 📦 My Purchases",
    ]), BACK)
    try:
        await send(context.bot, ADMIN_ID, order_admin_text(order), order_admin_kb(order_id))
    except TelegramError as e:
        log.error("Admin notify fail (%s): %s", order_id, e)
    if mail_configured():
        spawn(watch_email(context.bot, order_id, utr, order["amount"], order["created_at"]))
    return ConversationHandler.END


async def cancel(update, context):
    context.user_data.clear()
    await send(context.bot, update.effective_chat.id, card("Cancelled", ["Process cancel ho gaya."]), BACK)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
# PAYMENT & DELIVERY
# ═══════════════════════════════════════════════════════════
def mark_paid(order_id, source):
    with db() as conn:
        row = conn.execute("SELECT amount FROM orders WHERE id = ? AND status = 'UNDER_REVIEW'", (order_id,)).fetchone()
        changed = conn.execute(
            "UPDATE orders SET status = 'PAID', verified_at = ? WHERE id = ? AND status = 'UNDER_REVIEW'",
            (now_iso(), order_id),
        ).rowcount
        if changed and row:
            conn.execute("INSERT INTO payments (order_id, source, amount, created_at) VALUES (?, ?, ?, ?)",
                         (order_id, source, row["amount"], now_iso()))
    return bool(changed)


async def approve(bot, order_id, source):
    if not mark_paid(order_id, source):
        return "already"
    return "delivered" if await deliver_order(bot, order_id) else "delivery_failed"


async def deliver_order(bot, order_id):
    with db() as conn:
        order = conn.execute(
            "SELECT o.user_id, o.product_id, p.name, p.file_id, p.file_type FROM orders o "
            "JOIN products p ON p.id = o.product_id WHERE o.id = ? AND o.status = 'PAID'", (order_id,),
        ).fetchone()
        if not order:
            return False
        extra_files = conn.execute("SELECT file_id, file_type FROM product_files WHERE product_id = ?",
                                   (order["product_id"],)).fetchall()
        extra_urls = conn.execute("SELECT url FROM product_urls WHERE product_id = ?",
                                  (order["product_id"],)).fetchall()
    header = card("Purchase Successful", [
        f"🎁 <b>{h(order['name'])}</b>",
        f"🆔 <code>{h(order_id)}</code>",
        "", f"{STORE_NAME} se khareedne ke liye dhanyavaad! 🙏🏻",
    ])
    success = True
    try:
        if order["file_type"] == "document":
            await bot.send_document(order["user_id"], document=order["file_id"], caption=header, parse_mode=ParseMode.HTML)
        elif order["file_type"] == "photo":
            await bot.send_photo(order["user_id"], photo=order["file_id"], caption=header, parse_mode=ParseMode.HTML)
        elif order["file_type"] == "link":
            await bot.send_message(order["user_id"], f"{header}\n\n🔗 <a href='{h(order['file_id'])}'>Download</a>", parse_mode=ParseMode.HTML)
        else:
            await bot.send_message(order["user_id"], header, parse_mode=ParseMode.HTML)
        for ef in extra_files:
            try:
                if ef["file_type"] == "document":
                    await bot.send_document(order["user_id"], document=ef["file_id"])
                elif ef["file_type"] == "photo":
                    await bot.send_photo(order["user_id"], photo=ef["file_id"])
            except Exception:
                pass
        if extra_urls:
            url_list = "\n".join([f"🔗 <a href='{h(u['url'])}'>{h(u['url'])}</a>" for u in extra_urls])
            await bot.send_message(order["user_id"], f"<b>Extra Links:</b>\n\n{url_list}", parse_mode=ParseMode.HTML)
    except BadRequest as e:
        if "can't parse entities" in str(e) or "unsupported start tag" in str(e).lower():
            try:
                await bot.send_message(order["user_id"], _strip_tags(header))
            except Exception as e2:
                log.error("Delivery plain fail: %s", e2)
                success = False
        else:
            log.error("Delivery BadRequest: %s", e)
            success = False
    except TelegramError as e:
        log.error("Delivery fail (%s): %s", order_id, e)
        success = False
    with db() as conn:
        conn.execute("INSERT INTO delivery_logs (order_id, user_id, success, details, created_at) VALUES (?, ?, ?, ?, ?)",
                     (order_id, order["user_id"], 1 if success else 0, "OK" if success else "Failed", now_iso()))
        if success:
            conn.execute("UPDATE orders SET status = 'DELIVERED', delivered_at = ? WHERE id = ? AND status = 'PAID'",
                         (now_iso(), order_id))
    return success


async def watch_email(bot, order_id, utr, amount, created_at):
    for _ in range(EMAIL_POLL_ROUNDS):
        await asyncio.sleep(EMAIL_POLL_SECONDS)
        with db() as conn:
            row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if row is None or row["status"] != "UNDER_REVIEW":
            return
        try:
            verdict, _ = await email_matches(order_id, utr, amount, created_at)
        except Exception:
            log.exception("Email check fail (%s)", order_id)
            continue
        if verdict != "VERIFIED":
            continue
        info = [f"🆔 <code>{h(order_id)}</code>", f"🧾 <code>{h(utr)}</code>", f"💰 ₹{amount}"]
        if AUTO_APPROVE:
            result = await approve(bot, order_id, "email")
            if result == "already":
                return
            if result == "delivered":
                await send(bot, ADMIN_ID, card("Auto Verified ✅", info + ["", "Delivery done."]))
            else:
                await send(bot, ADMIN_ID, card("Auto Verified ⚠️", info + ["", "Delivery fail."]), retry_kb(order_id))
        else:
            await send(bot, ADMIN_ID, card("Email Match 📧", info + ["", "Approve karein."]), order_admin_kb(order_id))
        return
    with db() as conn:
        row = conn.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if row and row["status"] == "UNDER_REVIEW":
        await send(bot, ADMIN_ID, card("Email Match Nahi Mila", [
            f"🆔 <code>{h(order_id)}</code>", f"🧾 <code>{h(utr)}</code>", f"💰 ₹{amount}",
            "", "Manual verify karein.",
        ]), order_admin_kb(order_id))


# ═══════════════════════════════════════════════════════════
# QR GENERATOR FLOW
# ═══════════════════════════════════════════════════════════
def qr_scheme_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🌈 Rainbow", callback_data="qr_scheme:rainbow"),
         InlineKeyboardButton("🌊 Ocean", callback_data="qr_scheme:ocean")],
        [InlineKeyboardButton("🌅 Sunset", callback_data="qr_scheme:sunset"),
         InlineKeyboardButton("💜 Neon", callback_data="qr_scheme:neon")],
        [InlineKeyboardButton("🌲 Forest", callback_data="qr_scheme:forest")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])


async def qr_gen_start(update, context):
    q = update.callback_query
    await q.answer()
    if context.user_data.get("order_id"):
        await send(context.bot, q.message.chat_id, card("⚠️ Pending Order", [
            "Pehle UTR submit karein ya /cancel karein.",
        ]), BACK)
        return ConversationHandler.END
    context.user_data["qr_scheme"] = "rainbow"
    await send(context.bot, q.message.chat_id, card("🎨 Colorful QR Generator", [
        "Koi bhi <b>text ya URL</b> bhejein 👇",
        "Main ek <b>colorful</b> QR banunga! 🌈",
        "", "🎨 <b>Theme:</b>", "Selected: <b>🌈 Rainbow</b>",
    ]), qr_scheme_kb())
    return QR_GEN_TEXT


async def qr_gen_pick_scheme(update, context):
    q = update.callback_query
    await q.answer()
    try:
        scheme = q.data.split(":", 1)[1]
    except IndexError:
        return QR_GEN_TEXT
    if scheme not in QR_COLOR_SCHEMES:
        scheme = "rainbow"
    context.user_data["qr_scheme"] = scheme
    emoji = SCHEME_EMOJI.get(scheme, "🎨")
    await edit_text_safe(q, card("🎨 Colorful QR Generator", [
        "Koi bhi <b>text ya URL</b> bhejein 👇",
        "", f"Theme: <b>{emoji} {scheme.title()}</b>",
        "Ab text type karke bhejein.",
    ]))
    return QR_GEN_TEXT


async def qr_gen_make(update, context):
    text = update.message.text.strip()
    if not text or len(text) > 1000:
        await send(context.bot, update.effective_chat.id, card("⚠️ Invalid", ["1-1000 chars ka text bhejein."]))
        return QR_GEN_TEXT
    scheme = context.user_data.get("qr_scheme", "rainbow")
    await send(context.bot, update.effective_chat.id, card("🎨 QR ban raha hai...", ["Ek second ✨"]))
    try:
        qr_img = make_colorful_qr_card(text, scheme, title="MY QR CODE")
    except Exception as e:
        log.exception("QR fail: %s", e)
        await send(context.bot, update.effective_chat.id, card("❌ QR Fail", ["Dobara try karein."]), BACK)
        context.user_data.pop("qr_scheme", None)
        return ConversationHandler.END
    emoji = SCHEME_EMOJI.get(scheme, "🎨")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🎨 Another", callback_data="qr_gen"),
        InlineKeyboardButton("🏠 Menu", callback_data="menu"),
    ]])
    await send_photo_safe(context.bot, update.effective_chat.id, qr_img, card("✅ Your Colorful QR", [
        f"🎨 Theme: <b>{emoji} {scheme.title()}</b>",
        f"📝 Data: <code>{h(text[:80])}</code>",
        "", "Scan karke check karein! 📱",
    ]), kb)
    context.user_data.pop("qr_scheme", None)
    return ConversationHandler.END


async def qr_gen_cancel(update, context):
    context.user_data.pop("qr_scheme", None)
    await send(context.bot, update.effective_chat.id, card("Cancelled", ["QR cancel ho gaya."]), BACK)
    return ConversationHandler.END


async def qr_gen_cancel_cb(update, context):
    q = update.callback_query
    try:
        await q.answer()
    except Exception:
        pass
    context.user_data.pop("qr_scheme", None)
    try:
        await start(update, context)
    except Exception as e:
        log.exception("qr cancel fail: %s", e)
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
# ADMIN PANEL
# ═══════════════════════════════════════════════════════════
async def admin_panel(update, context):
    if not is_admin(update):
        if update.callback_query:
            await update.callback_query.answer("Not authorized", show_alert=True)
        return
    if update.callback_query:
        await update.callback_query.answer()
    with db() as conn:
        u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        o = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        rev = conn.execute("SELECT SUM(amount) FROM payments").fetchone()[0] or 0
        pend = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'UNDER_REVIEW'").fetchone()[0]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📊 Dashboard (₹{rev})", callback_data="admin_dashboard")],
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add"),
         InlineKeyboardButton(f"📋 Pending ({pend})", callback_data="admin_pending")],
        [InlineKeyboardButton("📦 Products", callback_data="admin_products"),
         InlineKeyboardButton("👥 Users", callback_data="admin_users")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="admin_broadcast"),
         InlineKeyboardButton("⚙️ Settings", callback_data="admin_settings")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])
    await send(context.bot, update.effective_chat.id, card("Admin Panel", [
        f"👥 Users: {u}", f"🛒 Orders: {o}", f"💰 Revenue: ₹{rev}", f"🔵 Pending: {pend}",
        "", "Option chunein 👇",
    ]), kb)


async def admin_dashboard_cb(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    with db() as conn:
        u = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        p = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        o = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        d = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'DELIVERED'").fetchone()[0]
        rev = conn.execute("SELECT SUM(amount) FROM payments").fetchone()[0] or 0
    await send(context.bot, q.message.chat_id, card("Analytics", [
        f"👥 Users: {u}", f"📦 Products: {p}", f"🛒 Orders: {o}",
        f"✅ Delivered: {d}", f"💰 Revenue: ₹{rev}",
    ]), InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))


async def admin_pending(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    with db() as conn:
        rows = conn.execute(
            "SELECT o.id, o.user_id, o.utr, o.amount, p.name FROM orders o "
            "JOIN products p ON p.id = o.product_id WHERE o.status = 'UNDER_REVIEW' ORDER BY o.created_at LIMIT 10"
        ).fetchall()
    if not rows:
        await send(context.bot, q.message.chat_id, card("Pending", ["Koi pending nahi ✨"]),
                   InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))
        return
    for o in rows:
        await send(context.bot, q.message.chat_id, order_admin_text(o), order_admin_kb(o["id"]))


async def admin_products(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    with db() as conn:
        products = conn.execute("SELECT id, name, amount FROM products ORDER BY id DESC").fetchall()
    if not products:
        await send(context.bot, q.message.chat_id, card("Products", ["Koi product nahi."]),
                   InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))
        return
    rows = [[InlineKeyboardButton(f"🗑️ {p['name']} (₹{p['amount']})", callback_data=f"del_prod_{p['id']}")] for p in products]
    rows.append([InlineKeyboardButton("« Back", callback_data="admin_home")])
    await send(context.bot, q.message.chat_id, card("Manage Products", ["Delete:"]), InlineKeyboardMarkup(rows))


async def admin_delete_product(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    try:
        pid = int(q.data.split("_")[2])
    except (IndexError, ValueError):
        return
    with db() as conn:
        conn.execute("DELETE FROM products WHERE id = ?", (pid,))
    await edit_text_safe(q, card("Deleted", ["Product delete ho gaya."]),
                         InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))


async def admin_users(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    with db() as conn:
        rows = conn.execute("SELECT user_id, username, full_name FROM users ORDER BY joined_at DESC LIMIT 15").fetchall()
    lines = [f"• <code>{r['user_id']}</code> | @{h(r['username'])} | {h(r['full_name'])}" for r in rows] or ["Koi user nahi"]
    await send(context.bot, q.message.chat_id, card("Users (15)", lines),
               InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))


async def admin_broadcast_start(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    await send(context.bot, q.message.chat_id, card("Broadcast", ["Sabhi users ko message type karein."]))
    return BROADCAST_TEXT


async def admin_broadcast_send(update, context):
    text = update.message.text
    with db() as conn:
        users = [r["user_id"] for r in conn.execute("SELECT user_id FROM users").fetchall()]
    sent = 0
    for uid in users:
        try:
            await context.bot.send_message(uid, text, parse_mode=ParseMode.HTML)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    await send(context.bot, update.effective_chat.id, card("Broadcast Done", [f"Sent: {sent}"]),
               InlineKeyboardMarkup([[InlineKeyboardButton("« Admin", callback_data="admin_home")]]))
    return ConversationHandler.END


async def admin_settings(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    await send(context.bot, q.message.chat_id, card("Settings", [
        f"<b>Store:</b> {STORE_NAME}",
        f"<b>UPI:</b> {UPI_ID}",
        f"<b>Mail:</b> {mail_configured()}",
        f"<b>Auto Approve:</b> {AUTO_APPROVE}",
    ]), InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data="admin_home")]]))


async def admin_decide(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    await q.answer()
    action, order_id = q.data.split(":", 1)
    bot = context.bot

    if action == "approve":
        result = await approve(bot, order_id, "admin")
        if result == "already":
            await send(bot, ADMIN_ID, card("Already Processed", [f"🆔 <code>{h(order_id)}</code>"]))
            return
        if result == "delivered":
            line = "✅ Approved & Delivered"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("« Admin", callback_data="admin_home")]])
        else:
            line = "⚠️ PAID but delivery fail"
            kb = retry_kb(order_id)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send(bot, ADMIN_ID, card("Order Update", [f"🆔 <code>{h(order_id)}</code>", line]), kb)

    elif action == "reject":
        with db() as conn:
            changed = conn.execute("UPDATE orders SET status = 'FAILED' WHERE id = ? AND status = 'UNDER_REVIEW'",
                                   (order_id,)).rowcount
            row = conn.execute("SELECT user_id FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not changed:
            await send(bot, ADMIN_ID, card("Already Processed", [f"🆔 <code>{h(order_id)}</code>"]))
            return
        if row:
            await send(bot, row["user_id"], card("Payment Not Verified", [
                f"🆔 <code>{h(order_id)}</code>",
                "", "UTR verify nahi hua. /start se naya order karein.",
            ]), BACK)
        try:
            await q.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send(bot, ADMIN_ID, card("Order Rejected", [
            f"🆔 <code>{h(order_id)}</code>", "❌ Rejected",
        ]), InlineKeyboardMarkup([[InlineKeyboardButton("« Admin", callback_data="admin_home")]]))

    elif action == "retry":
        delivered = await deliver_order(bot, order_id)
        await send(bot, ADMIN_ID, card("Retry Result", [
            "✅ Delivered" if delivered else "❌ Fail",
        ]))


# ═══════════════════════════════════════════════════════════
# ADD PRODUCT (ADMIN CONVERSATION)
# ═══════════════════════════════════════════════════════════
async def add_start(update, context):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return ConversationHandler.END
    await q.answer()
    await send(context.bot, q.message.chat_id, card("New Product", ["Product ka naam bhejein 📝"]))
    return ADD_NAME


async def add_name(update, context):
    name = update.message.text.strip()
    if not 2 <= len(name) <= 100:
        await send(context.bot, update.effective_chat.id, card("Naam", ["2-100 chars ka naam."]))
        return ADD_NAME
    context.user_data["p_name"] = name
    await send(context.bot, update.effective_chat.id, card("Price", ["Price bhejein (sirf number)."]))
    return ADD_AMOUNT


async def add_amount(update, context):
    text = update.message.text.strip()
    if not text.isdigit() or not 1 <= int(text) <= 99999:
        await send(context.bot, update.effective_chat.id, card("Price", ["1-99999 ka number."]))
        return ADD_AMOUNT
    context.user_data["p_amount"] = int(text)
    await send(context.bot, update.effective_chat.id, card("File / Link", [
        "File (PDF/ZIP/Photo) bhejein", "YA https:// link bhejein",
    ]))
    return ADD_FILE


async def add_file(update, context):
    msg = update.message
    if msg.document:
        ftype, fid = "document", msg.document.file_id
    elif msg.photo:
        ftype, fid = "photo", msg.photo[-1].file_id
    elif msg.text and re.fullmatch(r"https?://\S+", msg.text.strip()):
        ftype, fid = "link", msg.text.strip()
    else:
        await send(context.bot, msg.chat_id, card("Invalid", ["File ya https:// link bhejein."]))
        return ADD_FILE
    context.user_data["p_ftype"] = ftype
    context.user_data["p_fid"] = fid
    await send(context.bot, msg.chat_id, card("Extra URLs", ["Extra URLs (one per line) ya 'SKIP' likhein."]))
    return ADD_URLS


async def add_urls(update, context):
    text = update.message.text.strip()
    urls = []
    if text.upper() != "SKIP":
        for line in text.splitlines():
            l = line.strip()
            if re.fullmatch(r"https?://\S+", l):
                urls.append(l)
    name = context.user_data.pop("p_name", None)
    amount = context.user_data.pop("p_amount", None)
    ftype = context.user_data.pop("p_ftype", "link")
    fid = context.user_data.pop("p_fid", "")
    if name is None or amount is None:
        await send(context.bot, update.effective_chat.id, card("Expired", ["Admin Panel se dobara try karein."]))
        return ConversationHandler.END
    with db() as conn:
        pid = conn.execute(
            "INSERT INTO products (name, amount, file_id, file_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (name, amount, fid, ftype, now_iso()),
        ).lastrowid
        for u in urls:
            conn.execute("INSERT INTO product_urls (product_id, url) VALUES (?, ?)", (pid, u))
    context.user_data.clear()
    await send(update.effective_chat.id, card("Product Added ✅", [
        f"🎁 {h(name)}", f"💰 ₹{amount}", f"🆔 ID: {pid}", f"🔗 Extra URLs: {len(urls)}",
    ]), InlineKeyboardMarkup([[InlineKeyboardButton("🧠 Admin", callback_data="admin_home")]]))
    return ConversationHandler.END


# ═══════════════════════════════════════════════════════════
# BOOT / ERROR
# ═══════════════════════════════════════════════════════════
async def post_init(app):
    try:
        await app.bot.delete_webhook(drop_pending_updates=True)
        log.info("🧹 Webhook cleared")
    except Exception as e:
        log.warning("Webhook clear fail: %s", e)
    try:
        me = await app.bot.get_me()
        log.info("✅ Bot online: @%s (id=%s)", me.username, me.id)
    except Exception as e:
        log.error("❌ Bot validation fail: %s", e)
        return
    if not mail_configured():
        return
    with db() as conn:
        rows = conn.execute("SELECT id, utr, amount, created_at FROM orders WHERE status = 'UNDER_REVIEW' AND utr IS NOT NULL").fetchall()
    for r in rows:
        spawn(watch_email(app.bot, r["id"], r["utr"], r["amount"], r["created_at"]))
    if rows:
        log.info("🔄 Resumed %d watchers", len(rows))


async def post_shutdown(app):
    log.info("🛑 Shutting down, cancelling %d tasks", len(BACKGROUND_TASKS))
    for task in list(BACKGROUND_TASKS):
        task.cancel()
    await asyncio.sleep(0.5)


async def on_error(update, context):
    err = context.error
    if isinstance(err, Conflict):
        log.warning("⚠️ CONFLICT: Doosra instance chal raha hai!")
        return
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("🌐 Network: %s", err)
        return
    if isinstance(err, BadRequest) and "can't parse entities" in str(err):
        log.warning("🔤 HTML parse error (handled): %s", err)
        return
    log.error("Unhandled error", exc_info=err)


# ═══════════════════════════════════════════════════════════
# MAIN (RETRY LOOP)
# ═══════════════════════════════════════════════════════════
def build_application():
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()

    buy_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(buy_product, pattern=r"^buy_\d+$")],
        states={ENTER_UTR: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_utr)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern=r"^admin_add$")],
        states={
            ADD_NAME: [MessageHandler(admin_only_filter() & filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_AMOUNT: [MessageHandler(admin_only_filter() & filters.TEXT & ~filters.COMMAND, add_amount)],
            ADD_FILE: [MessageHandler(admin_only_filter() & ~filters.COMMAND, add_file)],
            ADD_URLS: [MessageHandler(admin_only_filter() & filters.TEXT & ~filters.COMMAND, add_urls)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    broadcast_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_broadcast_start, pattern=r"^admin_broadcast$")],
        states={BROADCAST_TEXT: [MessageHandler(admin_only_filter() & filters.TEXT & ~filters.COMMAND, admin_broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    qr_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(qr_gen_start, pattern=r"^qr_gen$")],
        states={
            QR_GEN_TEXT: [
                CallbackQueryHandler(qr_gen_pick_scheme, pattern=r"^qr_scheme:(rainbow|ocean|sunset|neon|forest)$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, qr_gen_make),
            ],
        },
        fallbacks=[CommandHandler("cancel", qr_gen_cancel), CallbackQueryHandler(qr_gen_cancel_cb, pattern=r"^menu$")],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(buy_conv)
    app.add_handler(add_conv)
    app.add_handler(broadcast_conv)
    app.add_handler(qr_conv)
    app.add_handler(CallbackQueryHandler(menu_cb, pattern=r"^menu$"))
    app.add_handler(CallbackQueryHandler(show_shop, pattern=r"^shop$"))
    app.add_handler(CallbackQueryHandler(my_orders, pattern=r"^my_orders$"))
    app.add_handler(CallbackQueryHandler(contact, pattern=r"^contact$"))
    app.add_handler(CallbackQueryHandler(about_store, pattern=r"^about$"))
    app.add_handler(CallbackQueryHandler(image_gallery, pattern=r"^gallery_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern=r"^admin_home$"))
    app.add_handler(CallbackQueryHandler(admin_dashboard_cb, pattern=r"^admin_dashboard$"))
    app.add_handler(CallbackQueryHandler(admin_pending, pattern=r"^admin_pending$"))
    app.add_handler(CallbackQueryHandler(admin_products, pattern=r"^admin_products$"))
    app.add_handler(CallbackQueryHandler(admin_delete_product, pattern=r"^del_prod_\d+$"))
    app.add_handler(CallbackQueryHandler(admin_users, pattern=r"^admin_users$"))
    app.add_handler(CallbackQueryHandler(admin_settings, pattern=r"^admin_settings$"))
    app.add_handler(CallbackQueryHandler(admin_decide, pattern=r"^(approve|reject|retry):KX[0-9A-F]{8}$"))
    app.add_error_handler(on_error)
    return app


def main():
    init_db()
    print("=" * 60, flush=True)
    print("🚀 KRISHNA_ZX_STORE STARTING (RETRY LOOP)", flush=True)
    print("=" * 60, flush=True)

    attempt = 0
    while True:
        attempt += 1
        try:
            print(f"\n▶️ Attempt #{attempt}: building app...", flush=True)
            app = build_application()
            print(f"▶️ Attempt #{attempt}: starting polling...\n", flush=True)
            app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
            print(f"⚠️ Attempt #{attempt}: polling returned", flush=True)
        except KeyboardInterrupt:
            print("\n👋 Stopped by user", flush=True)
            break
        except SystemExit:
            raise
        except Exception as e:
            print(f"\n❌ Attempt #{attempt} crashed: {type(e).__name__}: {e}", flush=True)
            log.error("Bot crashed", exc_info=True)

        print("⏳ Restarting in 5 seconds...", flush=True)
        time.sleep(5)


if __name__ == "__main__":
    main()
def main():
    from keep_alive import start_keep_alive
    start_keep_alive()          # ← free web service ke liye zaroori

    init_db()
    print("🚀 KRISHNA_ZX_STORE STARTING...", flush=True)
    # ... baaki pura code same
