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
import threading
from datetime import datetime
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

import qrcode
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as M, Update
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
UPI_ID = env("UPI_ID", "yourname@fam")
GMAIL_ADDRESS = env("GMAIL_ADDRESS", "youremail@gmail.com")
GMAIL_APP_PASSWORD = env("GMAIL_APP_PASSWORD", "xxxxxxxxxxxxxxxx")
IMAP_HOST = env("IMAP_HOST", "imap.gmail.com")
PAYMENT_SENDERS = env_list("PAYMENT_SENDERS", "fampay,npci,yesbank,axisbank,icicibank,hdfcbank,sbi")
REQUIRE_DKIM = env_bool("REQUIRE_DKIM", "true")
MAIL_LOOKBACK_DAYS = env_int("MAIL_LOOKBACK_DAYS", 2)
EMAIL_POLL_SECONDS = env_int("EMAIL_POLL_SECONDS", 30)
EMAIL_POLL_ROUNDS = env_int("EMAIL_POLL_ROUNDS", 20)
AUTO_APPROVE = env_bool("AUTO_APPROVE", "true")

PAYMENT_TIMEOUT = 5 * 60        # QR ke baad 5 minute ka window (payment + UTR)
COUNTDOWN_EDIT_SECONDS = 10     # QR caption ka timer itni second mein update hoga
MIN_PRICE, MAX_PRICE = 1, 999
SUPPORT_LINK = "http://t.me/Krishna_Zx_0"
DATA_FILE = "store_data.json"
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

DB = {"products": {}, "users": {}, "orders": {}, "pending": {}, "settings": {}}
BG_TASKS = set()
RUNNING_COUNTDOWN = set()
RUNNING_VERIFY = set()


# ===================== Data store =====================
def load_db():
    global DB
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            DB = json.load(f)
    for key in ("products", "users", "orders", "pending", "settings"):
        DB.setdefault(key, {})


def save_db():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def now_str():
    return datetime.now().strftime("%d-%m-%Y %H:%M")


def money(amount):
    return f"₹{amount}" if CURRENCY == "INR" else f"{amount} {CURRENCY}"


def welcome_text():
    custom = DB["settings"].get("welcome")
    if custom:
        return custom
    return (f"🙏 Welcome to {STORE_NAME} 🙏\n\n"
            "✨ Digital products yahan milenge, ek click mein payment aur turant download.\n"
            f"🤖 {BOT_USERNAME}\n\nNiche se product choose karein:")


def utr_used(utr):
    return any(o.get("utr") == utr and o["status"] in ("verifying", "verified", "approved")
               for o in DB["orders"].values())


def finish_pending(o):
    if DB["pending"].get(str(o["user_id"])) == o["id"]:
        DB["pending"].pop(str(o["user_id"]), None)


def touch_user(u):
    rec = DB["users"].setdefault(str(u.id), {"id": u.id, "first_seen": now_str()})
    rec["name"] = u.full_name
    rec["username"] = u.username or ""
    rec["last_seen"] = now_str()
    rec["last_ts"] = time.time()
    save_db()


def spawn(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)


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


def _center(draw, text, y, fnt, fill, width):
    l, _, r, _ = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((width - (r - l)) // 2, y), text, font=fnt, fill=fill)


def make_qr_card(upi_link, amount, order_id):
    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(upi_link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#1a1033", back_color="white").convert("RGB")
    qr_img = qr_img.resize((560, 560), Image.NEAREST)

    W, H = 800, 1180
    card = _gradient(W, H, [(0.0, (88, 24, 169)), (0.5, (233, 30, 99)), (1.0, (255, 152, 0))])
    draw = ImageDraw.Draw(card)

    _center(draw, STORE_NAME.replace("_", " "), 45, _font(46), (255, 255, 255), W)
    _center(draw, "SCAN & PAY", 108, _font(26), (255, 235, 59), W)

    # white QR panel with shadow
    draw.rounded_rectangle([102, 184, 722, 804], radius=36, fill=(55, 10, 90))
    draw.rounded_rectangle([90, 170, 710, 790], radius=36, fill=(255, 255, 255),
                           outline=(255, 235, 59), width=8)
    card.paste(qr_img, (120, 200))

    # amount pill
    symbol = "Rs" if CURRENCY == "INR" else CURRENCY
    draw.rounded_rectangle([180, 830, 620, 920], radius=45, fill=(0, 200, 83))
    _center(draw, f"{symbol} {amount}", 850, _font(50), (255, 255, 255), W)

    _center(draw, f"Order: {order_id}", 955, _font(30), (255, 255, 255), W)
    _center(draw, f"UPI: {UPI_ID}", 1010, _font(26), (255, 255, 255), W)
    _center(draw, "Payment ke baad 12-digit UTR bhejein", 1080, _font(24), (255, 235, 59), W)

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
    rows = [[B(f"🎁 {p['name']} - {money(p['price'])}", callback_data=f"prod:{pid}")]
            for pid, p in DB["products"].items() if p["active"]]
    rows.append([B("🧾 History", callback_data="history"), B("🔗 My Link", callback_data="mylink")])
    rows.append([B("💬 Support", url=SUPPORT_LINK)])
    if uid == ADMIN_ID:
        rows.append([B("🛠 Admin Panel", callback_data="adm")])
    return M(rows)


def admin_menu():
    return M([
        [B("➕ Add Product", callback_data="adm_add")],
        [B("📦 Products", callback_data="adm_products"), B("👥 Users", callback_data="adm_users")],
        [B("🧾 Orders", callback_data="adm_orders")],
        [B("✏️ Welcome Text", callback_data="adm_welcome")],
        [B("⬅️ Back", callback_data="home")],
    ])


def cancel_kb(oid):
    return M([[B("❌ Order cancel karein", callback_data=f"cancel:{oid}")]])


def admin_decision_kb(oid):
    return M([
        [B("✅ Approve", callback_data=f"approve:{oid}"), B("❌ Reject", callback_data=f"reject:{oid}")],
        [B("⚡ Force Approve", callback_data=f"force:{oid}")],
    ])


def admin_order_text(o, status_line):
    return (f"🆕 Payment request - {STORE_NAME}\n"
            f"Order: {o['id']}\n"
            f"User: {o['user_name']} (@{o['username'] or '-'})\n"
            f"User ID: {o['user_id']}\n"
            f"Product: {o['product_name']}\n"
            f"Amount: {money(o['amount'])}\n"
            f"UTR: {o['utr'] or '-'}\n\n"
            f"Status: {status_line}")


def product_text(p):
    state = "Active ✅" if p["active"] else "Hidden ⏸"
    return (f"📦 {p['name']}\n💰 {money(p['price'])}\n🔗 {p['link']}\n"
            f"📌 Status: {state}\n📅 Added: {p['created']}")


def caption_for(o, remaining):
    mins, secs = divmod(max(remaining, 0), 60)
    return (f"🧾 {o['product_name']} - {money(o['amount'])}\n"
            f"⏳ Time bacha: {mins:02d}:{secs:02d}\n\n"
            "Payment ke baad 12-digit UTR is chat mein bhejein.\n"
            "Galat UTR bhejne par sahi UTR dobara bhej sakte hain.")


def user_card(uid):
    u = DB["users"].get(str(uid), {})
    orders = sorted([o for o in DB["orders"].values() if o["user_id"] == uid],
                    key=lambda o: o["created"], reverse=True)
    approved = sum(1 for o in orders if o["status"] == "approved")
    lines = [
        f"👤 {u.get('name', '-')}",
        f"🆔 ID: {uid}",
        f"🔹 Username: @{u.get('username') or '-'}",
        f"📅 First seen: {u.get('first_seen', '-')}",
        f"🕒 Last seen: {u.get('last_seen', '-')}",
        f"🛍 Approved purchases: {approved}",
        f"🧾 Total orders: {len(orders)}",
    ]
    if orders:
        lines.append("\nHistory:")
        for o in orders[:10]:
            lines.append(f"• {o['product_name']} | {money(o['amount'])} | "
                         f"{STATUS_LABEL[o['status']]} | {o['created_str']}")
    return "\n".join(lines)


async def send_profile(bot, chat_id, uid, caption, markup=None):
    caption = caption[:1000]
    file_id = None
    try:
        photos = await bot.get_user_profile_photos(uid, limit=1)
        if photos.total_count:
            file_id = photos.photos[0][-1].file_id
    except Exception as e:
        log.warning("Profile photo nahi mila: %s", e)
    if file_id:
        await bot.send_photo(chat_id, photo=file_id, caption=caption, reply_markup=markup)
    else:
        await bot.send_message(chat_id, caption + "\n\n(Profile photo nahi hai)", reply_markup=markup)


async def reply_or_edit(update, text, markup=None):
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


# ===================== Caption & admin notify helpers =====================
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
                o["status"] = "expired"
                finish_pending(o)
                save_db()
                await set_caption(bot, o, "⌛ Time khatam. Ye order expire ho gaya, naya order karein.", None)
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
    o.update(status="approved", approved_at=now_str(), approved_by=by)
    finish_pending(o)
    save_db()

    await set_caption(bot, o, f"✅ Payment verify ho gaya!\n🧾 {o['product_name']}", None)
    markup = M([[B("⬇️ Download", url=o["link"])]]) if o.get("link") else None
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=(f"✅ Payment verify ho gaya!\n\n🎁 {o['product_name']}\n"
                  "⬇️ Download button niche hai.\nYe link 🔗 My Link mein bhi milega."),
            reply_markup=markup)
    except Exception as e:
        log.error("User ko product message nahi gaya: %s", e)
        by += " (user ko message nahi gaya)"
    await notify_admin(bot, oid, f"✅ APPROVED - {by}")


async def reject(bot, oid, by):
    o = DB["orders"][oid]
    if o["status"] not in ("verifying", "verified", "failed"):
        return
    o["status"] = "rejected"
    finish_pending(o)
    save_db()
    await set_caption(bot, o, "❌ Ye payment verify nahi ho paya.", None)
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=f"❌ Payment verify nahi ho paya. Support se contact karein: {SUPPORT_LINK}")
    except Exception as e:
        log.error("User ko reject message nahi gaya: %s", e)
    await notify_admin(bot, oid, f"❌ REJECTED - {by}")


async def utr_not_matched(bot, oid):
    o = DB["orders"][oid]
    if o["status"] != "verifying":
        return
    bad_utr = o["utr"]
    o.setdefault("wrong_utrs", []).append(bad_utr)

    if time.time() < o["expires"]:
        # Abhi time bacha hai: user ko sahi UTR dobara bhejne do
        o.update(status="awaiting_payment", utr=None)
        save_db()
        await bot.send_message(
            chat_id=o["user_id"],
            text=(f"❌ UTR {bad_utr} ki payment mail nahi mili.\n"
                  "Payment app mein sahi 12-digit UTR dekh kar dobara bhejein. Timer chal raha hai ⏳"))
        spawn(countdown(bot, oid))
    else:
        o["status"] = "failed"
        save_db()
        await set_caption(bot, o, "⚠️ Payment verify nahi ho paya. Admin ko details bhej di gayi hain.", None)
        try:
            await bot.send_message(
                chat_id=o["user_id"],
                text=f"⚠️ Payment verify nahi ho paya. Support se contact karein: {SUPPORT_LINK}")
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
                    o["status"] = "verified"
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
    await update.message.reply_text(f"Aapka Telegram ID: {update.effective_user.id}")


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    await send_profile(context.bot, update.effective_chat.id, user.id, user_card(user.id))


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
    text = (f"🎁 {prod['name']}\n💰 Price: {money(prod['price'])}\n\n"
            "💳 Buy Now dabayein, QR khulega. Payment ke baad UTR bhejein.")
    markup = M([[B("💳 Buy Now", callback_data=f"buy:{pid}")],
                [B("⬅️ Back", callback_data="home")]])
    await reply_or_edit(update, text, markup)


async def create_order(update, context, pid):
    q = update.callback_query
    user = q.from_user
    prod = DB["products"].get(pid)
    if not prod or not prod["active"]:
        return await q.answer("Ye product abhi available nahi hai.", show_alert=True)
    await q.answer()

    old_id = DB["pending"].pop(str(user.id), None)
    if old_id:
        old = DB["orders"].get(old_id)
        if old and old["status"] == "awaiting_payment":
            old["status"] = "cancelled"
            await set_caption(context.bot, old, "🚫 Ye order cancel ho gaya (naya order bana).", None)

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
    DB["pending"][str(user.id)] = oid
    save_db()

    upi = (f"upi://pay?pa={UPI_ID}&pn={quote(STORE_NAME)}"
           f"&am={prod['price']}&cu={CURRENCY}&tn={oid}")
    card = make_qr_card(upi, prod["price"], oid)
    sent = await context.bot.send_photo(
        chat_id=user.id, photo=card, caption=caption_for(order, PAYMENT_TIMEOUT),
        reply_markup=cancel_kb(oid))
    order["qr_msg_id"] = sent.message_id
    save_db()
    spawn(countdown(context.bot, oid))


async def cancel_order(update, context, oid):
    q = update.callback_query
    o = DB["orders"].get(oid)
    if not o or o["user_id"] != q.from_user.id or o["status"] != "awaiting_payment":
        return await q.answer("Ye order cancel nahi ho sakta.", show_alert=True)
    o["status"] = "cancelled"
    finish_pending(o)
    save_db()
    await q.answer("Order cancel ho gaya")
    await set_caption(context.bot, o, "🚫 Ye order cancel ho gaya. Dobara order karne ke liye product choose karein.", None)


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
        o["status"] = "expired"
        finish_pending(o)
        save_db()
        return await update.message.reply_text(
            "⌛ Time khatam ho gaya. Naya order karein.", reply_markup=home_markup(user.id))

    utr = re.sub(r"\s+", "", raw_text)
    if not re.fullmatch(r"\d{12}", utr):
        return await update.message.reply_text(
            "❌ UTR galat hai. UTR 12 digit ka hota hai, sirf numbers. "
            "Payment app mein dekh kar sahi UTR bhejein.")

    if utr_used(utr):
        return await update.message.reply_text(
            "❌ Ye UTR pehle hi use ho chuka hai. Ek UTR dobara verify nahi hota.")

    o.update(utr=utr, status="verifying", utr_at=now_str())
    save_db()

    await update.message.reply_text(
        "🔍 UTR mil gaya. Payment Gmail se verify ho raha hai. Mail aate hi product mil jayega, thoda wait karein.")
    await set_caption(context.bot, o, f"🔍 UTR {utr} verify ho raha hai...\nKoi tension nahi, product mil jayega.", None)
    await notify_admin(context.bot, oid, "🔍 Gmail verify ho raha hai")
    spawn(verify_loop(context.bot, oid))


async def show_history(update, context):
    user = update.effective_user
    orders = sorted([o for o in DB["orders"].values() if o["user_id"] == user.id],
                    key=lambda o: o["created"], reverse=True)[:15]
    if not orders:
        text = "🧾 Abhi koi order nahi hai."
    else:
        lines = ["🧾 Aapki History:\n"]
        for o in orders:
            lines.append(f"• {o['product_name']} | {money(o['amount'])} | "
                         f"{STATUS_LABEL[o['status']]} | {o['created_str']}")
        text = "\n".join(lines)
    await reply_or_edit(update, text, M([[B("⬅️ Back", callback_data="home")]]))


async def show_mylink(update, context):
    user = update.effective_user
    approved = [o for o in DB["orders"].values()
                if o["user_id"] == user.id and o["status"] == "approved" and o.get("link")]
    if not approved:
        return await reply_or_edit(
            update, "🔗 Abhi koi purchased product nahi hai.", M([[B("⬅️ Back", callback_data="home")]]))
    rows = [[B(f"⬇️ {o['product_name']}", url=o["link"])] for o in approved]
    rows.append([B("⬅️ Back", callback_data="home")])
    await reply_or_edit(update, "🔗 Aapke purchased products. Download ke liye button dabayein:", M(rows))


# ===================== Admin side =====================
async def admin_add_start(update, context):
    context.user_data.clear()
    context.user_data["step"] = "name"
    context.user_data["draft"] = {}
    await reply_or_edit(update, "➕ Product ka naam likhein (2 se 60 characters):",
                        M([[B("❌ Cancel", callback_data="adm")]]))


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
                   f"💰 Price: {money(draft['price'])}\n🔗 Link: {draft['link']}\n\n"
                   "Submit karein?")
        return await update.message.reply_text(
            summary, reply_markup=M([[B("✅ Submit", callback_data="adm_add_submit"),
                                      B("❌ Cancel", callback_data="adm_add_cancel")]]))

    if step == "confirm":
        return await update.message.reply_text("Upar wale buttons se Submit ya Cancel karein.")

    if step == "welcome":
        if not 5 <= len(text) <= 2000:
            return await update.message.reply_text("❌ Welcome text 5 se 2000 characters ke beech hona chahiye.")
        DB["settings"]["welcome"] = text
        save_db()
        context.user_data.clear()
        return await update.message.reply_text("✅ Welcome text update ho gaya. /start karke dekhein.",
                                               reply_markup=admin_menu())


async def admin_products(update, context):
    rows = [[B(f"{'✅' if p['active'] else '⏸'} {p['name']} - {money(p['price'])}",
               callback_data=f"adm_prod:{pid}")]
            for pid, p in DB["products"].items()]
    rows.append([B("➕ Add Product", callback_data="adm_add")])
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
        [B("⏸ Hide / ▶️ Show", callback_data=f"adm_toggle:{pid}")],
        [B("🗑 Delete", callback_data=f"adm_del:{pid}")],
        [B("⬅️ Back", callback_data="adm_products")],
    ])
    await reply_or_edit(update, product_text(p), markup)


async def admin_welcome(update, context):
    text = f"✏️ Welcome text (user ko /start par dikhta hai):\n\n{welcome_text()}"
    await reply_or_edit(update, text, M([
        [B("✏️ Edit karein", callback_data="adm_welcome_edit")],
        [B("🔄 Default par wapas", callback_data="adm_welcome_reset")],
        [B("⬅️ Back", callback_data="adm")],
    ]))


async def admin_users(update, context):
    users = sorted(DB["users"].values(), key=lambda u: u.get("last_ts", 0), reverse=True)[:30]
    rows = [[B(f"👤 {u.get('name', '-')} (@{u.get('username') or '-'})",
               callback_data=f"adm_user:{u['id']}")] for u in users]
    rows.append([B("⬅️ Back", callback_data="adm")])
    text = "👥 Users (latest pehle, tap karke detail dekhein):" if users else "👥 Abhi koi user nahi aaya."
    await reply_or_edit(update, text, M(rows))


async def admin_orders(update, context):
    orders = sorted(DB["orders"].values(), key=lambda o: o["created"], reverse=True)[:20]
    if not orders:
        text = "🧾 Abhi koi order nahi hai."
    else:
        lines = ["🧾 Latest orders:\n"]
        for o in orders:
            lines.append(f"{o['id']} | {o['product_name']} | {money(o['amount'])} | "
                         f"{STATUS_LABEL[o['status']]} | @{o['username'] or o['user_id']}")
        text = "\n".join(lines)
    await reply_or_edit(update, text, M([[B("⬅️ Back", callback_data="adm")]]))


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
                [B("🗑 Delete", callback_data=f"adm_del:{pid}"),
                 B("⏸ Hide / Show", callback_data=f"adm_toggle:{pid}")],
                [B("➕ Add Another", callback_data="adm_add")],
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
            M([[B("✅ Haan, delete", callback_data=f"adm_delok:{arg}"),
                B("❌ Nahi", callback_data=f"adm_prod:{arg}")]]))
    if action == "adm_delok":
        DB["products"].pop(arg, None)
        save_db()
        return await admin_products(update, context)

    # ---- admin: users & orders ----
    if action == "adm_users":
        return await admin_users(update, context)
    if action == "adm_user":
        await q.answer()
        uid = int(arg)
        markup = M([
            [B("📤 Purchased links bhejo", callback_data=f"adm_resend:{uid}")],
            [B("⬅️ Back", callback_data="adm_users")],
        ])
        return await send_profile(context.bot, q.message.chat.id, uid, user_card(uid), markup)
    if action == "adm_resend":
        uid = int(arg)
        approved = [o for o in DB["orders"].values()
                    if o["user_id"] == uid and o["status"] == "approved" and o.get("link")]
        for o in approved:
            await context.bot.send_message(
                chat_id=uid, text=f"🎁 {o['product_name']}\nDownload button niche hai.",
                reply_markup=M([[B("⬇️ Download", url=o["link"])]]))
        return await q.answer(f"{len(approved)} link bhej diye", show_alert=True)
    if action == "adm_orders":
        return await admin_orders(update, context)

    # ---- admin: welcome text ----
    if action == "adm_welcome":
        return await admin_welcome(update, context)
    if action == "adm_welcome_edit":
        context.user_data.clear()
        context.user_data["step"] = "welcome"
        return await reply_or_edit(
            update, "✏️ Naya welcome text likhein. Store ka naam, description, sab aap likh sakte ho:",
            M([[B("❌ Cancel", callback_data="adm_welcome")]]))
    if action == "adm_welcome_reset":
        DB["settings"].pop("welcome", None)
        save_db()
        return await admin_welcome(update, context)

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

    await update.message.reply_text("Menu se choose karein:", reply_markup=home_markup(user.id))


# ===================== main =====================
async def post_init(app):
    # Restart ke baad adhoore kaam dobara chalu karein
    for oid, o in list(DB["orders"].items()):
        if o["status"] == "awaiting_payment":
            if time.time() < o["expires"]:
                spawn(countdown(app.bot, oid))
            else:
                o["status"] = "expired"
                finish_pending(o)
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
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot running")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
