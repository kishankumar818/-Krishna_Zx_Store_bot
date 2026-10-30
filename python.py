import os
import io
import re
import json
import time
import uuid
import asyncio
import logging
import imaplib
import email
import textwrap
import threading
from datetime import datetime, timezone, timedelta
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

import qrcode
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup as M, Update, BotCommand
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("krishna_store")


# ============================ CONFIG ============================
# Har value ka default yahin hai. Render ke Environment Variables mein
# same naam ki value daloge to wo default ko override karegi.
def env(key, default):
    return os.environ.get(key, str(default))


def env_bool(key, default):
    return env(key, default).strip().lower() in ("1", "true", "yes", "on")


def env_int(key, default):
    try:
        return int(env(key, default).strip())
    except ValueError:
        return int(default)


def env_list(key, default):
    return [x.strip().lower() for x in env(key, default).split(",") if x.strip()]


BOT_TOKEN = env("BOT_TOKEN", "9123903438:AAFQSYK0D1sP9JFoD_9NW43em1THjK2I_jE")
BOT_USERNAME = env("BOT_USERNAME", "@Krishna_Zx_Store_bot")
ADMIN_ID = env_int("ADMIN_ID", 8917638509)
STORE_NAME = env("STORE_NAME", "Krishna_Zx_Store")
CURRENCY = env("CURRENCY", "INR")
UPI_ID = env("UPI_ID", "yourname@fam")          # sirf QR ke andar jata hai, kahin dikhta nahi
GMAIL_ADDRESS = env("GMAIL_ADDRESS", "youremail@gmail.com")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD", "xxxxxxxxxxxxxxxx")
IMAP_HOST = env("IMAP_HOST", "imap.gmail.com")
PAYMENT_SENDERS = env_list("PAYMENT_SENDERS", "fampay,npci,yesbank,axisbank,icicibank,hdfcbank,sbi")
REQUIRE_DKIM = env_bool("REQUIRE_DKIM", "true")
MAIL_LOOKBACK_DAYS = env_int("MAIL_LOOKBACK_DAYS", 2)
EMAIL_POLL_SECONDS = env_int("EMAIL_POLL_SECONDS", 30)
EMAIL_POLL_ROUNDS = env_int("EMAIL_POLL_ROUNDS", 20)
AUTO_APPROVE = env_bool("AUTO_APPROVE", "true")
USE_BUTTON_STYLES = env_bool("BUTTON_STYLES", "true")
MAX_UTR_TRIES = env_int("MAX_UTR_TRIES", 5)

PAYMENT_TIMEOUT = 5 * 60        # QR ke baad 5 minute ka window (payment + UTR)
COUNTDOWN_EDIT_SECONDS = 10     # QR caption ka timer itni second mein update hoga
MIN_PRICE, MAX_PRICE = 1, 999
SUPPORT_LINK = "http://t.me/Krishna_Zx_0"
DATA_FILE = "store_data.json"
IST = timezone(timedelta(hours=5, minutes=30))
# =================================================================

STATUS_LABEL = {
    "awaiting_payment": "⏳ Payment pending",
    "verifying": "🔍 Verify ho raha hai",
    "verified": "🟡 Verified (admin approve baaki)",
    "failed": "⚠️ Verify fail",
    "approved": "✅ Approved",
    "rejected": "❌ Rejected",
    "expired": "⌛ Expired",
    "cancelled": "🚫 Cancelled",
}
EVENT_LABEL = dict(STATUS_LABEL, utr_wrong="❌ Galat UTR")
PROBLEM_STATUSES = ("cancelled", "expired", "failed", "rejected")

# ---------- QR design defaults (admin panel se change honge) ----------
DEFAULT_QR = {
    "title": STORE_NAME.replace("_", " "),
    "subtitle": "SCAN & PAY",
    "note": "Exact amount pay karein|Payment ke baad 12-digit UTR bot mein bhejein|Verify hote hi product turant milega",
    "bg1": "#5818A9", "bg2": "#E91E63", "bg3": "#FF9800",
    "qr_color": "#1A1033", "accent": "#FFEB3B", "pill": "#00C853", "text": "#FFFFFF",
}
COLOR_FIELDS = {
    "bg1": "Background upar", "bg2": "Background beech", "bg3": "Background niche",
    "qr_color": "QR dots", "accent": "Border / Subtitle", "pill": "Amount pill", "text": "Text color",
}
THEMES = {
    "royal": ("👑 Royal", {"bg1": "#5818A9", "bg2": "#E91E63", "bg3": "#FF9800", "qr_color": "#1A1033",
                          "accent": "#FFEB3B", "pill": "#00C853", "text": "#FFFFFF"}),
    "ocean": ("🌊 Ocean", {"bg1": "#0F2027", "bg2": "#203A43", "bg3": "#2C5364", "qr_color": "#0F2027",
                          "accent": "#00E5FF", "pill": "#00B0FF", "text": "#FFFFFF"}),
    "emerald": ("💚 Emerald", {"bg1": "#004D40", "bg2": "#00897B", "bg3": "#66BB6A", "qr_color": "#004D40",
                              "accent": "#FFF59D", "pill": "#FF6F00", "text": "#FFFFFF"}),
    "gold": ("🖤 Black Gold", {"bg1": "#000000", "bg2": "#1C1C1C", "bg3": "#3A2C0F", "qr_color": "#000000",
                              "accent": "#FFD700", "pill": "#C9A227", "text": "#FFFFFF"}),
    "sunset": ("🌅 Sunset", {"bg1": "#FF512F", "bg2": "#DD2476", "bg3": "#7B1FA2", "qr_color": "#4A0E4E",
                            "accent": "#FFFFFF", "pill": "#212121", "text": "#FFFFFF"}),
    "neon": ("💜 Neon", {"bg1": "#12002F", "bg2": "#3A0CA3", "bg3": "#F72585", "qr_color": "#12002F",
                        "accent": "#4CC9F0", "pill": "#7209B7", "text": "#FFFFFF"}),
}

# ---------- Texts defaults (admin panel se change honge) ----------
DEFAULT_TEXTS = {
    "welcome": ("❤️‍🩹 Welcome to {store} ❤️‍🩹\n\n"
                "✨ Premium digital products yahan milenge — ek click mein payment aur turant download.\n\n"
                "⚡ Auto UTR verification\n"
                "🔒 100% safe UPI payment\n"
                "📥 Purchase ke baad link hamesha 🔗 My Link mein\n"
                "👤 Apni detail ke liye /profile\n\n"
                "🤖 {bot}\n\n"
                "👇 Niche se product choose karein:"),
    "product": ("🎁 {product}\n💰 Price: {amount}\n\n"
                "⚡ Instant delivery\n🔒 Secure UPI payment\n✅ Auto verification\n\n"
                "💳 Buy Now dabayein, QR khulega. Payment ke baad 12-digit UTR bhejein."),
    "caption": ("🛍 {product}\n"
                "💰 Pay karein: {amount}\n"
                "🆔 Order ID: {order_id}\n"
                "⏳ Time bacha: {time}\n\n"
                "📲 QR scan karke exact amount pay karein\n"
                "🔢 Payment ke baad 12-digit UTR is chat mein bhejein\n"
                "✅ Verify hote hi product turant mil jayega\n"
                "♻️ Galat UTR gaya to sahi UTR dobara bhej sakte hain"),
    "utr_ok": ("🔍 UTR {utr} mil gaya!\n\n"
               "📧 Payment verify ho raha hai...\n"
               "⏳ Thoda wait karein, mail aate hi product apne aap mil jayega.\n"
               "🙏 UTR dobara bhejne ki zarurat nahi."),
    "success": ("✅ Payment verify ho gaya!\n\n"
                "🎁 {product}\n💰 {amount}\n🆔 Order: {order_id}\n\n"
                "⬇️ Download button niche hai.\n🔗 Ye link 🔗 My Link mein hamesha milega.\n"
                "💖 Shukriya!"),
}
FIELD_INFO = {
    "qr:title": ("🏷 QR Title (sabse upar store naam)", 1, 40, ""),
    "qr:subtitle": ("✨ QR Subtitle (- bhejo to hat jayega)", 0, 40, ""),
    "qr:note": ("📝 QR card ki niche lines ( | se alag karein, emoji card par nahi dikhte )", 1, 240, ""),
    "txt:welcome": ("👋 Welcome text", 5, 2000, "{store} {bot}"),
    "txt:product": ("🎁 Product page text", 5, 900, "{product} {amount}"),
    "txt:caption": ("🧾 QR caption", 10, 900, "{product} {amount} {order_id} {time}"),
    "txt:utr_ok": ("🔍 UTR milne ka message", 5, 900, "{utr} {product} {amount} {order_id}"),
    "txt:success": ("✅ Success message", 5, 900, "{product} {amount} {order_id}"),
}

DB = {"products": {}, "users": {}, "orders": {}, "pending": {}, "settings": {}, "events": []}
BG_TASKS = set()
RUNNING_COUNTDOWN = set()
RUNNING_VERIFY = set()


# ===================== Colorful button helper =====================
try:
    InlineKeyboardButton("t", callback_data="x", style="primary")
    _STYLE_NATIVE = True
except TypeError:
    _STYLE_NATIVE = False


def B(text, style=None, **kw):
    """style: 'success' (hara), 'danger' (laal), 'primary' (neela)"""
    if style and USE_BUTTON_STYLES:
        if _STYLE_NATIVE:
            return InlineKeyboardButton(text, style=style, **kw)
        return InlineKeyboardButton(text, api_kwargs={"style": style}, **kw)
    return InlineKeyboardButton(text, **kw)


# ===================== Data store =====================
def load_db():
    global DB
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            DB = json.load(f)
    for key in ("products", "users", "orders", "pending", "settings"):
        DB.setdefault(key, {})
    DB.setdefault("events", [])
    if "welcome" in DB["settings"]:  # purana welcome text naye system mein
        DB["settings"].setdefault("texts", {}).setdefault("welcome", DB["settings"].pop("welcome"))


def save_db():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def now_str():
    return datetime.now(IST).strftime("%d-%m-%Y %I:%M:%S %p")


def money(amount):
    return f"₹{amount}" if CURRENCY == "INR" else f"{amount} {CURRENCY}"


def fill(tpl, **kw):
    for k, v in kw.items():
        tpl = tpl.replace("{" + k + "}", str(v))
    return tpl


def txt(key):
    return DB["settings"].get("texts", {}).get(key) or DEFAULT_TEXTS[key]


def qr_cfg():
    cfg = dict(DEFAULT_QR)
    cfg.update(DB["settings"].get("qr", {}))
    return cfg


def welcome_text():
    return fill(txt("welcome"), store=STORE_NAME, bot=BOT_USERNAME)


def utr_used(utr):
    return any(o.get("utr") == utr and o["status"] in ("verifying", "verified", "approved")
               for o in DB["orders"].values())


def finish_pending(o):
    if DB["pending"].get(str(o["user_id"])) == o["id"]:
        DB["pending"].pop(str(o["user_id"]), None)


def add_event(o, etype, note=""):
    DB["events"].append({
        "ts": time.time(), "at": now_str(), "user_id": o["user_id"],
        "name": o.get("user_name", "-"), "username": o.get("username", ""),
        "order_id": o["id"], "product": o.get("product_name", ""), "type": etype, "note": note,
    })
    if len(DB["events"]) > 1000:
        DB["events"] = DB["events"][-1000:]


def set_status(o, status, note=""):
    o["status"] = status
    o["status_at"] = now_str()
    o.setdefault("timeline", []).append({"at": o["status_at"], "status": status, "note": note})
    add_event(o, status, note)


def touch_user(u):
    rec = DB["users"].setdefault(str(u.id), {"id": u.id, "first_seen": now_str()})
    rec["name"] = u.full_name
    rec["first_name"] = u.first_name or ""
    rec["last_name"] = u.last_name or ""
    rec["username"] = u.username or ""
    rec["language"] = u.language_code or ""
    rec["premium"] = bool(getattr(u, "is_premium", False))
    rec["last_seen"] = now_str()
    rec["last_ts"] = time.time()
    save_db()


def spawn(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)


def extract_utr(text):
    m = re.search(r"(?<!\d)\d{12}(?!\d)", text)
    if m:
        return m.group()
    compact = re.sub(r"[\s\-]", "", text)
    m = re.search(r"(?<!\d)\d{12}(?!\d)", compact)
    return m.group() if m else None


# ===================== Render health check =====================
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.environ.get("PORT", "10000"))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()


# ===================== Premium QR card =====================
def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def norm_hex(t):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", t.strip())
    return "#" + m.group(1).upper() if m else None


def card_safe(t):
    # Card font emoji support nahi karta, isliye emoji hata do (caption mein emoji chalega)
    return "".join(ch for ch in t if ord(ch) < 0x2000).strip()


def _gradient(w, h, stops):
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / (h - 1)
        color = stops[-1][1]
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                k = (t - t0) / (t1 - t0)
                color = tuple(int(c0[i] + (c1[i] - c0[i]) * k) for i in range(3))
                break
        d.line([(0, y), (w, y)], fill=color)
    return img


def _fit(draw, text, max_w, size, min_size=16):
    while True:
        f = _font(size)
        l, _, r, _ = draw.textbbox((0, 0), text, font=f)
        if r - l <= max_w or size <= min_size:
            return f
        size -= 2


def _center(draw, text, y, fnt, fill_color, width):
    l, _, r, _ = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((width - (r - l)) // 2, y), text, font=fnt, fill=fill_color)


def make_qr_card(upi_link, amount, order_id):
    c = qr_cfg()
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(upi_link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color=c["qr_color"], back_color="white").convert("RGB")
    qr_img = qr_img.resize((560, 560), Image.NEAREST)

    title = card_safe(c["title"]) or STORE_NAME.replace("_", " ")
    subtitle = card_safe(c["subtitle"])
    lines = []
    for part in c["note"].split("|"):
        part = card_safe(part)
        if part:
            lines.extend(textwrap.wrap(part, 42))
    lines = lines[:6]

    W = 800
    H = 1045 + 46 * len(lines) + 45
    card = _gradient(W, H, [(0.0, hex_rgb(c["bg1"])), (0.5, hex_rgb(c["bg2"])), (1.0, hex_rgb(c["bg3"]))])
    d = ImageDraw.Draw(card)
    text_c, acc = hex_rgb(c["text"]), hex_rgb(c["accent"])
    shadow = tuple(int(x * 0.45) for x in hex_rgb(c["bg1"]))

    # 1) Upar: store naam
    _center(d, title, 38, _fit(d, title, 740, 54), text_c, W)
    if subtitle:
        _center(d, subtitle, 110, _fit(d, subtitle, 700, 28), acc, W)

    # 2) Beech: QR
    d.rounded_rectangle([102, 184, 722, 804], radius=36, fill=shadow)
    d.rounded_rectangle([90, 170, 710, 790], radius=36, fill=(255, 255, 255), outline=acc, width=8)
    card.paste(qr_img, (120, 200))

    # 3) Niche: Order ID + amount
    d.rounded_rectangle([140, 825, 660, 895], radius=35, fill=shadow, outline=acc, width=3)
    t = f"ORDER ID: {order_id}"
    _center(d, t, 843, _fit(d, t, 480, 30), text_c, W)

    symbol = "Rs" if CURRENCY == "INR" else CURRENCY
    d.rounded_rectangle([140, 915, 660, 1010], radius=48, fill=hex_rgb(c["pill"]),
                        outline=(255, 255, 255), width=3)
    t = f"PAY {symbol} {amount}"
    _center(d, t, 933, _fit(d, t, 480, 54), (255, 255, 255), W)

    # 4) Sabse niche: description
    y = 1045
    for i, line in enumerate(lines):
        _center(d, line, y, _fit(d, line, 740, 26), acc if i == 0 else text_c, W)
        y += 46

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    buf.name = "qr.png"
    return buf


# ===================== Gmail verification =====================
def mail_body_text(msg):
    texts = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        raw = part.get_payload(decode=True) or b""
        text = raw.decode(part.get_content_charset() or "utf-8", errors="ignore")
        if ctype == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        texts.append(text)
    return " ".join(texts)


def evaluate_mail(msg, order):
    body = mail_body_text(msg)
    if order["utr"] not in body:
        return False, "UTR mail mein nahi mila"

    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    domain = from_addr.split("@")[-1]
    sender = next((s for s in PAYMENT_SENDERS if s in domain), None)
    if not sender:
        return False, f"sender allowed nahi ({domain or 'unknown'})"

    auth = " ".join(msg.get_all("Authentication-Results", []) or []).lower()
    if REQUIRE_DKIM and not ("dkim=pass" in auth and sender in auth):
        return False, "DKIM verify nahi hua (fake mail ho sakta hai)"

    amt = re.escape(str(order["amount"]))
    if not re.search(rf"(?<![\d.]){amt}(?:\.0+)?(?!\d)", body):
        return False, f"amount {money(order['amount'])} mail mein nahi mila"

    try:
        mail_ts = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        return False, "mail ka date read nahi hua"
    if mail_ts < order["created"] - 60:
        return False, "mail order se pehle ka hai"

    return True, f"{sender} mail, DKIM pass, amount aur UTR match"


def check_gmail(order):
    imap = None
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")
        query = f'"{order["utr"]} newer_than:{MAIL_LOOKBACK_DAYS}d"'
        typ, data = imap.search(None, "X-GM-RAW", query)
        ids = data[0].split() if typ == "OK" and data and data[0] else []
        note = "abhi mail nahi mili"
        for num in ids[-20:]:
            _, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            ok, note = evaluate_mail(msg, order)
            if ok:
                return True, note
        return False, note
    except Exception as e:
        log.error("Gmail check failed: %s", e)
        return False, "Gmail connection error"
    finally:
        if imap:
            try:
                imap.logout()
            except Exception:
                pass


# ===================== Keyboards & texts =====================
def home_markup(uid):
    rows = [[B(f"🎁 {p['name']} • {money(p['price'])}", "success", callback_data=f"prod:{pid}")]
            for pid, p in DB["products"].items() if p["active"]]
    rows.append([B("🧾 History", "primary", callback_data="history"),
                 B("🔗 My Link", "primary", callback_data="mylink")])
    rows.append([B("👤 Profile", "primary", callback_data="profile"),
                 B("💬 Support", url=SUPPORT_LINK)])
    if uid == ADMIN_ID:
        rows.append([B("🛠 Admin Panel", "danger", callback_data="adm")])
    return M(rows)


def back_home():
    return M([[B("🏠 Home", "primary", callback_data="home")]])


def admin_menu():
    return M([
        [B("➕ Add Product", "success", callback_data="adm_add")],
        [B("📦 Products", "primary", callback_data="adm_products"),
         B("👥 Users", "primary", callback_data="adm_users")],
        [B("⚠️ Problem Users", "danger", callback_data="adm_users:bad"),
         B("📊 Activity", "danger", callback_data="adm_activity")],
        [B("🧾 Orders", "primary", callback_data="adm_orders")],
        [B("🎨 QR Design", "success", callback_data="adm_qr"),
         B("✏️ Texts", "success", callback_data="adm_texts")],
        [B("⬅️ Back", callback_data="home")],
    ])


def cancel_kb(oid):
    return M([[B("❌ Order Cancel", "danger", callback_data=f"cancel:{oid}")]])


def admin_decision_kb(oid):
    return M([
        [B("✅ Approve", "success", callback_data=f"approve:{oid}"),
         B("❌ Reject", "danger", callback_data=f"reject:{oid}")],
        [B("⚡ Force Approve", "primary", callback_data=f"force:{oid}")],
    ])


def admin_order_text(o, status_line):
    return (f"🆕 Payment request - {STORE_NAME}\n"
            f"🆔 Order: {o['id']}\n"
            f"👤 User: {o['user_name']} (@{o['username'] or '-'})\n"
            f"🔢 User ID: {o['user_id']}\n"
            f"🎁 Product: {o['product_name']}\n"
            f"💰 Amount: {money(o['amount'])}\n"
            f"🧾 UTR: {o['utr'] or '-'}\n"
            f"🕒 Order time: {o['created_str']}\n\n"
            f"📌 Status: {status_line}")


def product_text(p):
    state = "Active ✅" if p["active"] else "Hidden ⏸"
    return (f"📦 {p['name']}\n💰 {money(p['price'])}\n🔗 {p['link']}\n"
            f"📌 Status: {state}\n📅 Added: {p['created']}")


def caption_for(o, remaining):
    mins, secs = divmod(max(remaining, 0), 60)
    return fill(txt("caption"), product=o["product_name"], amount=money(o["amount"]),
                order_id=o["id"], time=f"{mins:02d}:{secs:02d}")[:1000]


def user_orders(uid):
    return sorted([o for o in DB["orders"].values() if o["user_id"] == uid],
                  key=lambda o: o["created"], reverse=True)


def problem_summary(uid):
    orders = user_orders(uid)
    c = {s: sum(1 for o in orders if o["status"] == s) for s in PROBLEM_STATUSES}
    c["wrong"] = sum(len(o.get("wrong_utrs", [])) for o in orders)
    return c


def problem_short(c):
    parts = []
    for key, emo in (("cancelled", "🚫"), ("expired", "⌛"), ("failed", "⚠️"), ("rejected", "❌"), ("wrong", "🔢")):
        if c[key]:
            parts.append(f"{emo}{c[key]}")
    return " ".join(parts)


async def profile_text(bot, uid, admin_view=False):
    u = DB["users"].get(str(uid), {})
    chat = None
    try:
        chat = await bot.get_chat(uid)
    except Exception as e:
        log.warning("get_chat fail: %s", e)

    first = getattr(chat, "first_name", None) or u.get("first_name") or "-"
    last = getattr(chat, "last_name", None) or u.get("last_name") or "-"
    username = getattr(chat, "username", None) or u.get("username") or ""
    bio = getattr(chat, "bio", None) or "-"
    bday = getattr(chat, "birthdate", None)
    bday_s = f"{bday.day:02d}-{bday.month:02d}" + (f"-{bday.year}" if bday.year else "") if bday else "-"

    photo_count = 0
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        photo_count = photos.total_count
    except Exception:
        pass

    orders = user_orders(uid)
    approved = [o for o in orders if o["status"] == "approved"]
    spent = sum(o["amount"] for o in approved)
    pc = problem_summary(uid)

    lines = [
        "👤 ━━━ PROFILE ━━━ 👤", "",
        f"📛 Naam: {u.get('name') or (first + ' ' + (last if last != '-' else '')).strip()}",
        f"🔹 First name: {first}",
        f"🔸 Last name: {last}",
        f"🆔 Telegram ID: {uid}",
        f"🔗 Username: @{username or '-'}",
        f"🌐 Language: {u.get('language') or '-'}",
        f"⭐ Telegram Premium: {'Haan ✅' if u.get('premium') else 'Nahi'}",
        f"📝 Bio: {bio}",
        f"🎂 Birthday: {bday_s}",
        f"🖼 Profile photos: {photo_count}",
        f"🔗 Permanent link: tg://user?id={uid}",
        "",
        "🛍 ━━━ STORE INFO ━━━ 🛍",
        f"📅 Pehli baar aaye: {u.get('first_seen', '-')}",
        f"🕒 Last active: {u.get('last_seen', '-')}",
        f"🧾 Total orders: {len(orders)}",
        f"✅ Approved purchases: {len(approved)}",
        f"💰 Total kharcha: {money(spent)}",
        f"🚫 Cancelled: {pc['cancelled']} | ⌛ Expired: {pc['expired']}",
        f"⚠️ Failed: {pc['failed']} | ❌ Rejected: {pc['rejected']} | 🔢 Galat UTR: {pc['wrong']}",
    ]
    if orders:
        lines += ["", "📜 ━━━ HISTORY ━━━ 📜"]
        for o in orders[:8 if admin_view else 5]:
            lines.append(f"• {o['product_name']} | {money(o['amount'])} | {STATUS_LABEL[o['status']]}")
            if admin_view:
                lines.append(f"   🆔 {o['id']} | 🕒 Bana: {o['created_str']}")
                if o["status"] != "awaiting_payment":
                    lines.append(f"   📌 {STATUS_LABEL[o['status']]} at: {o.get('status_at', '-')}")
                if o.get("utr"):
                    lines.append(f"   🧾 UTR: {o['utr']}")
                if o.get("wrong_utrs"):
                    lines.append(f"   🔢 Galat UTR: {', '.join(o['wrong_utrs'])}")
            else:
                lines.append(f"   🕒 {o['created_str']}")
    return "\n".join(lines)


async def send_profile(bot, chat_id, uid, text, markup=None):
    file_id = None
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count:
            file_id = photos.photos[0][-1].file_id
    except Exception as e:
        log.warning("Profile photo nahi mila: %s", e)
    if file_id and len(text) <= 1000:
        await bot.send_photo(chat_id, photo=file_id, caption=text, reply_markup=markup)
    elif file_id:
        await bot.send_photo(chat_id, photo=file_id)
        await bot.send_message(chat_id, text[:4000], reply_markup=markup)
    else:
        await bot.send_message(chat_id, (text + "\n\n(Profile photo nahi hai)")[:4000], reply_markup=markup)


async def reply_or_edit(update, text, markup=None):
    text = text[:4000]
    q = update.callback_query
    if q:
        try:
            await q.answer()
        except BadRequest:
            pass
        try:
            await q.edit_message_text(text, reply_markup=markup)
            return
        except BadRequest:
            pass
        await q.message.chat.send_message(text, reply_markup=markup)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup)


def remember(update):
    touch_user(update.effective_user)
    return update.effective_user


# ===================== QR message helpers =====================
async def set_caption(bot, o, caption, markup=None):
    if not o.get("qr_msg_id"):
        return
    try:
        await bot.edit_message_caption(
            chat_id=o["user_id"], message_id=o["qr_msg_id"],
            caption=caption[:1000], reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Caption edit failed: %s", e)
    except Exception as e:
        log.warning("Caption edit failed: %s", e)


async def delete_qr(bot, o):
    """QR photo ko chat se hata do (cancel / expire / approve / reject par)"""
    mid = o.get("qr_msg_id")
    if not mid:
        return
    try:
        await bot.delete_message(chat_id=o["user_id"], message_id=mid)
    except Exception as e:
        log.warning("QR delete nahi hua: %s", e)
    o["qr_msg_id"] = None
    save_db()


async def notify_admin(bot, oid, status_line):
    o = DB["orders"][oid]
    markup = admin_decision_kb(oid) if o["status"] in ("verifying", "verified", "failed") else None
    text = admin_order_text(o, status_line)
    try:
        if o.get("admin_msg_id"):
            await bot.edit_message_text(chat_id=ADMIN_ID, message_id=o["admin_msg_id"],
                                        text=text, reply_markup=markup)
        else:
            msg = await bot.send_message(chat_id=ADMIN_ID, text=text, reply_markup=markup)
            o["admin_msg_id"] = msg.message_id
            save_db()
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            log.warning("Admin message update failed: %s", e)
    except Exception as e:
        log.error("Admin ko message nahi gaya: %s (admin ne bot ko /start kiya hai?)", e)


# ===================== Timer (5 minute, live) =====================
async def countdown(bot, oid):
    if oid in RUNNING_COUNTDOWN:
        return
    RUNNING_COUNTDOWN.add(oid)
    try:
        while True:
            o = DB["orders"].get(oid)
            if not o or o["status"] != "awaiting_payment":
                return
            remaining = int(o["expires"] - time.time())
            if remaining <= 0:
                set_status(o, "expired", "5 minute mein UTR nahi aaya")
                finish_pending(o)
                save_db()
                await delete_qr(bot, o)
                try:
                    await bot.send_message(
                        o["user_id"],
                        f"⌛ Order {o['id']} ka time khatam ho gaya, QR hata diya gaya.\n"
                        "🔁 Naya order karne ke liye niche se product choose karein.",
                        reply_markup=home_markup(o["user_id"]))
                except Exception as e:
                    log.warning("Expire message nahi gaya: %s", e)
                return
            await set_caption(bot, o, caption_for(o, remaining), cancel_kb(oid))
            await asyncio.sleep(min(COUNTDOWN_EDIT_SECONDS, max(remaining, 1)))
    finally:
        RUNNING_COUNTDOWN.discard(oid)


# ===================== Verify & approve =====================
async def approve(bot, oid, by):
    o = DB["orders"][oid]
    if o["status"] not in ("verifying", "verified", "failed"):
        return
    set_status(o, "approved", by)
    o.update(approved_at=now_str(), approved_by=by)
    finish_pending(o)
    save_db()
    await delete_qr(bot, o)

    markup = M([[B("⬇️ Download", "success", url=o["link"])]]) if o.get("link") else None
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=fill(txt("success"), product=o["product_name"], amount=money(o["amount"]), order_id=o["id"]),
            reply_markup=markup)
    except Exception as e:
        log.error("User ko product message nahi gaya: %s", e)
        by += " (user ko message nahi gaya)"
    await notify_admin(bot, oid, f"✅ APPROVED - {by}")


async def reject(bot, oid, by):
    o = DB["orders"][oid]
    if o["status"] not in ("verifying", "verified", "failed"):
        return
    set_status(o, "rejected", by)
    finish_pending(o)
    save_db()
    await delete_qr(bot, o)
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=f"❌ Order {o['id']} ki payment verify nahi ho payi.\n💬 Support se contact karein: {SUPPORT_LINK}",
            reply_markup=back_home())
    except Exception as e:
        log.error("User ko reject message nahi gaya: %s", e)
    await notify_admin(bot, oid, f"❌ REJECTED - {by}")


async def utr_not_matched(bot, oid):
    o = DB["orders"][oid]
    if o["status"] != "verifying":
        return
    bad_utr = o["utr"]
    o.setdefault("wrong_utrs", []).append(bad_utr)
    add_event(o, "utr_wrong", f"UTR {bad_utr} - {o.get('verify_note', '')}")

    if time.time() < o["expires"] and len(o["wrong_utrs"]) < MAX_UTR_TRIES:
        set_status(o, "awaiting_payment", "UTR dobara maanga")
        o["utr"] = None
        save_db()
        await bot.send_message(
            chat_id=o["user_id"],
            text=(f"❌ UTR {bad_utr} ki payment nahi mili.\n"
                  "📲 Payment app mein sahi 12-digit UTR dekh kar dobara bhejein. Timer chal raha hai ⏳"))
        spawn(countdown(bot, oid))
    else:
        set_status(o, "failed", o.get("verify_note", ""))
        save_db()
        await delete_qr(bot, o)
        try:
            await bot.send_message(
                chat_id=o["user_id"],
                text=(f"⚠️ Order {o['id']} ki payment auto verify nahi ho payi.\n"
                      f"🛠 Admin check karega. Jaldi ke liye support: {SUPPORT_LINK}"),
                reply_markup=back_home())
        except Exception as e:
            log.error("User ko fail message nahi gaya: %s", e)
        await notify_admin(bot, oid, f"⚠️ Auto verify fail: {o.get('verify_note', '')}\n"
                                     "Approve (verified ho to), Force Approve ya Reject karein.")


async def verify_loop(bot, oid):
    if oid in RUNNING_VERIFY:
        return
    RUNNING_VERIFY.add(oid)
    try:
        o = DB["orders"][oid]
        for rnd in range(1, EMAIL_POLL_ROUNDS + 1):
            if o["status"] != "verifying":
                return
            ok, note = await asyncio.to_thread(check_gmail, o)
            o["verify_note"] = note
            save_db()
            if ok:
                o["verified"] = True
                save_db()
                if AUTO_APPROVE:
                    await approve(bot, oid, "AUTO (Gmail verified)")
                else:
                    set_status(o, "verified", note)
                    save_db()
                    await notify_admin(bot, oid, f"✅ Gmail verified: {note}\nApprove dabayein.")
                return
            if rnd < EMAIL_POLL_ROUNDS:
                await asyncio.sleep(EMAIL_POLL_SECONDS)
        await utr_not_matched(bot, oid)
    finally:
        RUNNING_VERIFY.discard(oid)


# ===================== User side =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    await update.message.reply_text(welcome_text(), reply_markup=home_markup(user.id))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember(update)
    await update.message.reply_text(f"🆔 Aapka Telegram ID: {update.effective_user.id}")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    text = await profile_text(context.bot, user.id)
    await send_profile(context.bot, update.effective_chat.id, user.id, text, back_home())


async def cmd_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember(update)
    await show_history(update, context)


async def cmd_mylink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember(update)
    await show_mylink(update, context)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    if user.id != ADMIN_ID:
        return await update.message.reply_text("❌ Ye command sirf admin ke liye hai.")
    await update.message.reply_text("🛠 Admin Panel", reply_markup=admin_menu())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember(update)
    context.user_data.clear()
    await update.message.reply_text("✅ Cancel ho gaya.", reply_markup=home_markup(update.effective_user.id))


async def show_product(update, context, pid):
    q = update.callback_query
    prod = DB["products"].get(pid)
    if not prod or not prod["active"]:
        return await q.answer("Ye product abhi available nahi hai.", show_alert=True)
    text = fill(txt("product"), product=prod["name"], amount=money(prod["price"]))
    markup = M([[B("💳 Buy Now", "success", callback_data=f"buy:{pid}")],
                [B("⬅️ Back", callback_data="home")]])
    await reply_or_edit(update, text, markup)


async def create_order(update, context, pid):
    q = update.callback_query
    user = q.from_user
    prod = DB["products"].get(pid)
    if not prod or not prod["active"]:
        return await q.answer("Ye product abhi available nahi hai.", show_alert=True)
    await q.answer("QR ban raha hai... ⏳")

    old_id = DB["pending"].pop(str(user.id), None)
    if old_id:
        old = DB["orders"].get(old_id)
        if old and old["status"] == "awaiting_payment":
            set_status(old, "cancelled", "Naya order bana diya")
            await delete_qr(context.bot, old)

    oid = uuid.uuid4().hex[:8].upper()
    now = time.time()
    order = {
        "id": oid, "user_id": user.id, "user_name": user.full_name,
        "username": user.username or "", "product_id": pid,
        "product_name": prod["name"], "amount": prod["price"], "link": prod["link"],
        "status": "awaiting_payment", "created": now, "created_str": now_str(),
        "expires": now + PAYMENT_TIMEOUT, "utr": None,
        "qr_msg_id": None, "admin_msg_id": None, "verified": False, "verify_note": "",
    }
    DB["orders"][oid] = order
    set_status(order, "awaiting_payment", "Order bana, QR bheja")
    DB["pending"][str(user.id)] = oid
    save_db()

    upi = (f"upi://pay?pa={UPI_ID}&pn={quote(STORE_NAME)}"
           f"&am={prod['price']}&cu={CURRENCY}&tn={oid}")
    card = await asyncio.to_thread(make_qr_card, upi, prod["price"], oid)
    try:
        sent = await context.bot.send_photo(
            chat_id=user.id, photo=card, caption=caption_for(order, PAYMENT_TIMEOUT),
            reply_markup=cancel_kb(oid))
    except Exception as e:
        log.error("QR nahi gaya: %s", e)
        set_status(order, "cancelled", "QR send fail")
        finish_pending(order)
        save_db()
        return
    order["qr_msg_id"] = sent.message_id
    save_db()
    spawn(countdown(context.bot, oid))


async def cancel_order(update, context, oid):
    q = update.callback_query
    o = DB["orders"].get(oid)
    if not o or o["user_id"] != q.from_user.id or o["status"] != "awaiting_payment":
        return await q.answer("Ye order cancel nahi ho sakta.", show_alert=True)
    set_status(o, "cancelled", "User ne khud cancel kiya")
    finish_pending(o)
    save_db()
    await q.answer("🚫 Order cancel ho gaya")
    await delete_qr(context.bot, o)
    await context.bot.send_message(
        o["user_id"], f"🚫 Order {oid} cancel ho gaya, QR hata diya gaya.\n👇 Naya product choose karein:",
        reply_markup=home_markup(o["user_id"]))


async def handle_utr(update, context, oid, raw_text):
    user = update.effective_user
    o = DB["orders"].get(oid)

    if not o:
        DB["pending"].pop(str(user.id), None)
        save_db()
        return await update.message.reply_text("Koi active order nahi hai.", reply_markup=home_markup(user.id))

    if o["status"] == "verifying":
        return await update.message.reply_text(
            f"⏳ Aapka UTR {o['utr']} abhi verify ho raha hai. Wait karein, dobara na bhejein.")

    if o["status"] != "awaiting_payment":
        finish_pending(o)
        save_db()
        return await update.message.reply_text(
            "Ye order ab active nahi hai. Naya product choose karein.", reply_markup=home_markup(user.id))

    if time.time() > o["expires"]:
        set_status(o, "expired", "UTR time ke baad aaya")
        finish_pending(o)
        save_db()
        await delete_qr(context.bot, o)
        return await update.message.reply_text(
            "⌛ Time khatam ho gaya. Naya order karein.", reply_markup=home_markup(user.id))

    utr = extract_utr(raw_text)
    if not utr:
        digits = len(re.sub(r"\D", "", raw_text))
        return await update.message.reply_text(
            f"❌ Sahi UTR nahi mila (aapne {digits} digit bheje).\n\n"
            "🔢 UTR 12 digit ka hota hai, sirf numbers.\n"
            "📲 Payment app → Transaction details → UTR / UPI Ref No. copy karke bhejein.\n"
            "Example: 412345678901")

    if utr in o.get("wrong_utrs", []):
        return await update.message.reply_text(
            "❌ Ye UTR pehle check ho chuka hai, payment nahi mili. Sahi UTR bhejein.")

    if utr_used(utr):
        return await update.message.reply_text(
            "❌ Ye UTR pehle hi use ho chuka hai. Ek UTR dobara verify nahi hota.")

    o.update(utr=utr, utr_at=now_str())
    set_status(o, "verifying", f"UTR {utr}")
    save_db()

    await update.message.reply_text(
        fill(txt("utr_ok"), utr=utr, product=o["product_name"], amount=money(o["amount"]), order_id=oid))
    await set_caption(context.bot, o,
                      f"🔍 UTR {utr} verify ho raha hai...\n🆔 Order: {oid}\nKoi tension nahi, product mil jayega 💖")
    await notify_admin(context.bot, oid, "🔍 Gmail verify ho raha hai")
    spawn(verify_loop(context.bot, oid))


async def show_history(update, context):
    user = update.effective_user
    orders = user_orders(user.id)[:15]
    if not orders:
        text = "🧾 Abhi koi order nahi hai."
    else:
        lines = ["🧾 ━━━ Aapki History ━━━ 🧾\n"]
        for o in orders:
            lines.append(f"• {o['product_name']} | {money(o['amount'])}\n"
                         f"   {STATUS_LABEL[o['status']]} | 🕒 {o['created_str']}")
        text = "\n".join(lines)
    await reply_or_edit(update, text, back_home())


async def show_mylink(update, context):
    user = update.effective_user
    approved = [o for o in DB["orders"].values()
                if o["user_id"] == user.id and o["status"] == "approved" and o.get("link")]
    if not approved:
        return await reply_or_edit(update, "🔗 Abhi koi purchased product nahi hai.", back_home())
    rows = [[B(f"⬇️ {o['product_name']}", "success", url=o["link"])] for o in approved]
    rows.append([B("🏠 Home", "primary", callback_data="home")])
    await reply_or_edit(update, "🔗 Aapke purchased products. Download ke liye button dabayein:", M(rows))


# ===================== Admin: products =====================
async def admin_add_start(update, context):
    context.user_data.clear()
    context.user_data["step"] = "name"
    context.user_data["draft"] = {}
    await reply_or_edit(update, "➕ Product ka naam likhein (2 se 60 characters):",
                        M([[B("❌ Cancel", "danger", callback_data="adm")]]))


async def admin_products(update, context):
    rows = [[B(f"{'✅' if p['active'] else '⏸'} {p['name']} - {money(p['price'])}", "primary",
               callback_data=f"adm_prod:{pid}")]
            for pid, p in DB["products"].items()]
    rows.append([B("➕ Add Product", "success", callback_data="adm_add")])
    rows.append([B("⬅️ Back", callback_data="adm")])
    text = "📦 Products (tap karke manage karein):" if DB["products"] else \
        "📦 Abhi koi product nahi hai. Add Product dabayein."
    await reply_or_edit(update, text, M(rows))


async def admin_product_detail(update, pid):
    p = DB["products"].get(pid)
    if not p:
        return await reply_or_edit(update, "❌ Product nahi mila.",
                                   M([[B("⬅️ Back", callback_data="adm_products")]]))
    markup = M([
        [B("⏸ Hide / ▶️ Show", "primary", callback_data=f"adm_toggle:{pid}")],
        [B("🗑 Delete", "danger", callback_data=f"adm_del:{pid}")],
        [B("⬅️ Back", callback_data="adm_products")],
    ])
    await reply_or_edit(update, product_text(p), markup)


# ===================== Admin: users / activity / orders =====================
async def admin_users(update, context, only_bad=False):
    users = sorted(DB["users"].values(), key=lambda u: u.get("last_ts", 0), reverse=True)
    rows = []
    for u in users:
        pc = problem_summary(u["id"])
        short = problem_short(pc)
        if only_bad and not short:
            continue
        icon = "⚠️" if short else "👤"
        rows.append([B(f"{icon} {u.get('name', '-')} (@{u.get('username') or '-'}) {short}".strip(),
                       "danger" if short else "primary", callback_data=f"adm_user:{u['id']}")])
        if len(rows) >= 30:
            break
    rows.append([B("⬅️ Back", callback_data="adm")])
    if only_bad:
        text = ("⚠️ Problem Users (cancel / expire / fail / reject / galat UTR):\n"
                "🚫 cancel  ⌛ expire  ⚠️ fail  ❌ reject  🔢 galat UTR") if len(rows) > 1 \
            else "✅ Abhi koi problem user nahi hai."
    else:
        text = "👥 Users (latest pehle, tap karke poori detail dekhein):" if len(rows) > 1 \
            else "👥 Abhi koi user nahi aaya."
    await reply_or_edit(update, text, M(rows))


async def admin_activity(update, context):
    types = PROBLEM_STATUSES + ("utr_wrong",)
    events = [e for e in reversed(DB["events"]) if e["type"] in types][:25]
    if not events:
        text = "📊 Abhi koi cancel / expire / fail activity nahi hai."
    else:
        blocks = ["📊 Recent Activity (latest pehle):"]
        for e in events:
            b = (f"🕒 {e['at']}\n{EVENT_LABEL.get(e['type'], e['type'])} | {e['name']} (@{e['username'] or '-'})\n"
                 f"🆔 {e['user_id']} | Order {e['order_id']} | {e.get('product', '')}")
            if e.get("note"):
                b += f"\n📝 {e['note']}"
            blocks.append(b)
        text = "\n\n".join(blocks)
    await reply_or_edit(update, text, M([[B("🔄 Refresh", "primary", callback_data="adm_activity")],
                                         [B("⬅️ Back", callback_data="adm")]]))


async def admin_orders(update, context):
    orders = sorted(DB["orders"].values(), key=lambda o: o["created"], reverse=True)[:20]
    if not orders:
        text = "🧾 Abhi koi order nahi hai."
    else:
        lines = ["🧾 Latest orders:\n"]
        for o in orders:
            lines.append(f"{o['id']} | {o['product_name']} | {money(o['amount'])}\n"
                         f"   {STATUS_LABEL[o['status']]} @ {o.get('status_at', o['created_str'])} | "
                         f"@{o['username'] or o['user_id']}")
        text = "\n".join(lines)
    await reply_or_edit(update, text, M([[B("⬅️ Back", callback_data="adm")]]))


# ===================== Admin: QR design & texts =====================
def qr_menu():
    c = qr_cfg()
    text = ("🎨 QR Design\n\n"
            f"🏷 Title: {c['title']}\n✨ Subtitle: {c['subtitle'] or '-'}\n"
            f"📝 Card lines: {c['note'].replace('|', ' | ')}\n\n"
            "QR card layout: upar store naam → beech mein QR → niche Order ID + Pay amount → description.\n"
            "UPI ID kahin nahi dikhti.")
    markup = M([
        [B("🏷 Title", "primary", callback_data="adm_edit:qr:title"),
         B("✨ Subtitle", "primary", callback_data="adm_edit:qr:subtitle")],
        [B("📝 Card Lines", "primary", callback_data="adm_edit:qr:note")],
        [B("🎨 Colors", "success", callback_data="adm_colors"),
         B("🌈 Themes", "success", callback_data="adm_themes")],
        [B("👁 Preview", "success", callback_data="adm_qrprev")],
        [B("🔄 Reset Design", "danger", callback_data="adm_qrreset")],
        [B("⬅️ Back", callback_data="adm")],
    ])
    return text, markup


def colors_menu():
    c = qr_cfg()
    rows = [[B(f"🎨 {label}: {c[k]}", "primary", callback_data=f"adm_edit:qr:{k}")]
            for k, label in COLOR_FIELDS.items()]
    rows.append([B("⬅️ Back", callback_data="adm_qr")])
    return "🎨 Kaunsa color badalna hai? (hex code jaise #FF0000)", M(rows)


def themes_menu():
    rows = [[B(label, "success", callback_data=f"adm_theme:{k}")] for k, (label, _) in THEMES.items()]
    rows.append([B("⬅️ Back", callback_data="adm_qr")])
    return "🌈 Ready theme choose karein (sirf colors badlenge):", M(rows)


def texts_menu():
    rows = [[B(FIELD_INFO[k][0], "primary", callback_data=f"adm_edit:{k}")]
            for k in FIELD_INFO if k.startswith("txt:")]
    rows.append([B("🔄 Sab texts default", "danger", callback_data="adm_txtreset")])
    rows.append([B("⬅️ Back", callback_data="adm")])
    return "✏️ Kaunsa text badalna hai?", M(rows)


def menu_for_key(key):
    if key.startswith("txt:"):
        return texts_menu()
    if key.split(":")[1] in COLOR_FIELDS:
        return colors_menu()
    return qr_menu()


async def admin_edit_start(update, context, key):
    section, field = key.split(":", 1)
    if section == "qr" and field in COLOR_FIELDS:
        current = qr_cfg()[field]
        prompt = f"🎨 {COLOR_FIELDS[field]}\nAbhi: {current}\n\nNaya hex color bhejein (jaise #E91E63):"
    elif key in FIELD_INFO:
        label, mn, mx, ph = FIELD_INFO[key]
        current = qr_cfg()[field] if section == "qr" else txt(field)
        prompt = f"{label}\n\n📄 Abhi ka text:\n{current}\n\n✍️ Naya text bhejein ({mn}-{mx} characters)."
        if ph:
            prompt += f"\n🔤 Ye placeholders use kar sakte ho: {ph}"
    else:
        return
    context.user_data.clear()
    context.user_data.update(step="edit", edit_key=key)
    back = "adm_texts" if section == "txt" else ("adm_colors" if field in COLOR_FIELDS else "adm_qr")
    await reply_or_edit(update, prompt, M([[B("❌ Cancel", "danger", callback_data=back)]]))


async def admin_preview(update, context):
    q = update.callback_query
    await q.answer("Preview ban raha hai...")
    sample = {"id": "PREVIEW1", "product_name": "Sample Product", "amount": 99}
    card = await asyncio.to_thread(make_qr_card, "upi://pay?pa=preview@upi&am=99", 99, "PREVIEW1")
    await context.bot.send_photo(q.message.chat.id, photo=card, caption=caption_for(sample, PAYMENT_TIMEOUT),
                                 reply_markup=M([[B("🎨 QR Design", "success", callback_data="adm_qr")]]))


# ===================== Admin text steps =====================
async def admin_step(update, context, text):
    step = context.user_data.get("step")
    draft = context.user_data.setdefault("draft", {})

    if step == "name":
        if not 2 <= len(text) <= 60:
            return await update.message.reply_text("❌ Naam 2 se 60 characters ke beech hona chahiye.")
        draft["name"] = text
        context.user_data["step"] = "price"
        return await update.message.reply_text(
            f"💰 Price likhein ({money(MIN_PRICE)} se {money(MAX_PRICE)} ke beech, sirf number):")

    if step == "price":
        digits = re.sub(r"[₹\s,]", "", text)
        if not digits.isdigit() or not MIN_PRICE <= int(digits) <= MAX_PRICE:
            return await update.message.reply_text(
                f"❌ Price {money(MIN_PRICE)} se {money(MAX_PRICE)} ke beech hona chahiye.")
        draft["price"] = int(digits)
        context.user_data["step"] = "link"
        return await update.message.reply_text("🔗 Download link bhejein (https:// se shuru hona chahiye):")

    if step == "link":
        if not re.fullmatch(r"https?://\S+", text):
            return await update.message.reply_text("❌ Valid link bhejein jo https:// ya http:// se shuru ho.")
        draft["link"] = text
        context.user_data["step"] = "confirm"
        summary = (f"📋 Product summary:\n\n📦 Naam: {draft['name']}\n"
                   f"💰 Price: {money(draft['price'])}\n🔗 Link: {draft['link']}\n\nSubmit karein?")
        return await update.message.reply_text(
            summary, reply_markup=M([[B("✅ Submit", "success", callback_data="adm_add_submit"),
                                      B("❌ Cancel", "danger", callback_data="adm_add_cancel")]]))

    if step == "confirm":
        return await update.message.reply_text("Upar wale buttons se Submit ya Cancel karein.")

    if step == "edit":
        key = context.user_data.get("edit_key", "")
        section, field = key.split(":", 1)
        if section == "qr" and field in COLOR_FIELDS:
            value = norm_hex(text)
            if not value:
                return await update.message.reply_text("❌ Galat color. Aise bhejein: #E91E63")
        else:
            label, mn, mx, _ = FIELD_INFO[key]
            value = "" if (key == "qr:subtitle" and text == "-") else text
            if value and not mn <= len(value) <= mx:
                return await update.message.reply_text(f"❌ Text {mn} se {mx} characters ke beech hona chahiye.")
            if not value and mn > 0:
                return await update.message.reply_text("❌ Text khali nahi ho sakta.")
        DB["settings"].setdefault("qr" if section == "qr" else "texts", {})[field] = value
        save_db()
        context.user_data.clear()
        menu_text, markup = menu_for_key(key)
        return await update.message.reply_text(f"✅ Update ho gaya!\n\n{menu_text}", reply_markup=markup)


# ===================== Callback router =====================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    user = remember(update)
    action, _, arg = q.data.partition(":")

    admin_only = action.startswith("adm") or action in ("approve", "force", "reject")
    if admin_only and user.id != ADMIN_ID:
        return await q.answer("Ye sirf admin ke liye hai", show_alert=True)

    # ---- user ----
    if action == "home":
        context.user_data.clear()
        return await reply_or_edit(update, welcome_text(), home_markup(user.id))
    if action == "prod":
        return await show_product(update, context, arg)
    if action == "buy":
        return await create_order(update, context, arg)
    if action == "cancel":
        return await cancel_order(update, context, arg)
    if action == "history":
        return await show_history(update, context)
    if action == "mylink":
        return await show_mylink(update, context)
    if action == "profile":
        await q.answer()
        text = await profile_text(context.bot, user.id)
        return await send_profile(context.bot, q.message.chat.id, user.id, text, back_home())

    # ---- admin: panel & product ----
    if action == "adm":
        context.user_data.clear()
        return await reply_or_edit(update, "🛠 Admin Panel", admin_menu())
    if action == "adm_add":
        return await admin_add_start(update, context)
    if action == "adm_add_cancel":
        context.user_data.clear()
        return await reply_or_edit(update, "❌ Product add cancel ho gaya.", admin_menu())
    if action == "adm_add_submit":
        draft = context.user_data.get("draft")
        if not draft or context.user_data.get("step") != "confirm":
            return await q.answer("Pehle product ki details bharein.", show_alert=True)
        pid = uuid.uuid4().hex[:6]
        DB["products"][pid] = {
            "id": pid, "name": draft["name"], "price": draft["price"],
            "link": draft["link"], "active": True, "created": now_str(),
        }
        save_db()
        context.user_data.clear()
        return await reply_or_edit(
            update, f"✅ Product add ho gaya!\n\n{product_text(DB['products'][pid])}",
            M([
                [B("🗑 Delete", "danger", callback_data=f"adm_del:{pid}"),
                 B("⏸ Hide / Show", "primary", callback_data=f"adm_toggle:{pid}")],
                [B("➕ Add Another", "success", callback_data="adm_add")],
                [B("⬅️ Admin Panel", callback_data="adm")],
            ]))
    if action == "adm_products":
        return await admin_products(update, context)
    if action == "adm_prod":
        return await admin_product_detail(update, arg)
    if action == "adm_toggle":
        p = DB["products"].get(arg)
        if p:
            p["active"] = not p["active"]
            save_db()
        return await admin_product_detail(update, arg)
    if action == "adm_del":
        p = DB["products"].get(arg)
        if not p:
            return await q.answer("Product nahi mila.", show_alert=True)
        return await reply_or_edit(
            update, f"🗑 '{p['name']}' ko delete karna hai?\nYe user panel se hat jayega.",
            M([[B("✅ Haan, delete", "danger", callback_data=f"adm_delok:{arg}"),
                B("❌ Nahi", "primary", callback_data=f"adm_prod:{arg}")]]))
    if action == "adm_delok":
        DB["products"].pop(arg, None)
        save_db()
        return await admin_products(update, context)

    # ---- admin: users / activity / orders ----
    if action == "adm_users":
        return await admin_users(update, context, only_bad=(arg == "bad"))
    if action == "adm_activity":
        return await admin_activity(update, context)
    if action == "adm_user":
        await q.answer()
        try:
            uid = int(arg)
        except ValueError:
            return
        markup = M([
            [B("📤 Purchased links bhejo", "success", callback_data=f"adm_resend:{uid}")],
            [B("👥 Users", "primary", callback_data="adm_users"),
             B("⚠️ Problem Users", "danger", callback_data="adm_users:bad")],
        ])
        text = await profile_text(context.bot, uid, admin_view=True)
        return await send_profile(context.bot, q.message.chat.id, uid, text, markup)
    if action == "adm_resend":
        uid = int(arg)
        approved = [o for o in DB["orders"].values()
                    if o["user_id"] == uid and o["status"] == "approved" and o.get("link")]
        sent = 0
        for o in approved:
            try:
                await context.bot.send_message(
                    chat_id=uid, text=f"🎁 {o['product_name']}\n⬇️ Download button niche hai.",
                    reply_markup=M([[B("⬇️ Download", "success", url=o["link"])]]))
                sent += 1
            except Exception as e:
                log.warning("Resend fail: %s", e)
        return await q.answer(f"{sent} link bhej diye", show_alert=True)
    if action == "adm_orders":
        return await admin_orders(update, context)

    # ---- admin: QR design & texts ----
    if action == "adm_qr":
        context.user_data.clear()
        return await reply_or_edit(update, *qr_menu())
    if action == "adm_colors":
        context.user_data.clear()
        return await reply_or_edit(update, *colors_menu())
    if action == "adm_themes":
        return await reply_or_edit(update, *themes_menu())
    if action == "adm_theme":
        if arg in THEMES:
            DB["settings"].setdefault("qr", {}).update(THEMES[arg][1])
            save_db()
            await q.answer(f"{THEMES[arg][0]} theme lag gaya ✅", show_alert=True)
        return await reply_or_edit(update, *qr_menu())
    if action == "adm_qrprev":
        return await admin_preview(update, context)
    if action == "adm_qrreset":
        DB["settings"].pop("qr", None)
        save_db()
        await q.answer("QR design default ho gaya", show_alert=True)
        return await reply_or_edit(update, *qr_menu())
    if action == "adm_texts":
        context.user_data.clear()
        return await reply_or_edit(update, *texts_menu())
    if action == "adm_txtreset":
        DB["settings"].pop("texts", None)
        save_db()
        await q.answer("Sab texts default ho gaye", show_alert=True)
        return await reply_or_edit(update, *texts_menu())
    if action == "adm_edit":
        return await admin_edit_start(update, context, arg)

    # ---- admin: approve / force / reject ----
    if action in ("approve", "force", "reject"):
        o = DB["orders"].get(arg)
        if not o or o["status"] not in ("verifying", "verified", "failed"):
            return await q.answer("Ye order pehle hi process ho chuka hai.", show_alert=True)
        if action == "approve":
            if not o.get("verified"):
                return await q.answer(
                    "Gmail verify nahi hua. Agar payment confirm hai to Force Approve dabayein.",
                    show_alert=True)
            await q.answer()
            return await approve(context.bot, arg, "Admin (verified)")
        if action == "force":
            await q.answer()
            return await approve(context.bot, arg, "Admin FORCE")
        await q.answer()
        return await reject(context.bot, arg, "Admin")

    await q.answer()


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    text = (update.message.text or "").strip()

    if user.id == ADMIN_ID and context.user_data.get("step"):
        return await admin_step(update, context, text)

    oid = DB["pending"].get(str(user.id))
    if oid:
        return await handle_utr(update, context, oid, text)

    await update.message.reply_text("👇 Menu se choose karein:", reply_markup=home_markup(user.id))


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    if DB["pending"].get(str(user.id)):
        return await update.message.reply_text(
            "📸 Screenshot se verify nahi hota.\n🔢 Payment ka 12-digit UTR number text mein bhejein.")
    await update.message.reply_text("👇 Menu se choose karein:", reply_markup=home_markup(user.id))


# ===================== main =====================
async def post_init(app):
    try:
        await app.bot.set_my_commands([
            BotCommand("start", "🏠 Store kholo"),
            BotCommand("profile", "👤 Meri poori profile"),
            BotCommand("history", "🧾 Order history"),
            BotCommand("mylink", "🔗 Purchased links"),
            BotCommand("id", "🆔 Mera Telegram ID"),
            BotCommand("cancel", "❌ Cancel"),
        ])
    except Exception as e:
        log.warning("Commands set nahi hue: %s", e)

    # Restart ke baad adhoore kaam dobara chalu karein (expire wale countdown khud handle karega)
    for oid, o in list(DB["orders"].items()):
        if o["status"] == "awaiting_payment":
            spawn(countdown(app.bot, oid))
        elif o["status"] == "verifying":
            spawn(verify_loop(app.bot, oid))
    save_db()


def main():
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", BOT_TOKEN):
        log.error("BOT_TOKEN sahi nahi hai. Render -> Environment mein BOT_TOKEN daalo (BotFather ka token).")
        return

    load_db()
    threading.Thread(target=start_health_server, daemon=True).start()
    log.info("Health server started")

    app = ApplicationBuilder().token(BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("mylink", cmd_mylink))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
