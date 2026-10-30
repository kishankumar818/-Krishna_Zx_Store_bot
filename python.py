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
BOT_TOKEN = "9123903438:AAFQSYK0D1sP9JFoD_9NW43em1THjK2I_jE"
BOT_USERNAME = "@Krishna_Zx_Store_bot"
ADMIN_ID = 8917638509
STORE_NAME = "Krishna_Zx_Store"
STORE_DESCRIPTION = "Digital products, ek click mein payment aur turant download ✨"
CURRENCY = "INR"
UPI_ID = "yourname@fam"
GMAIL_ADDRESS = "youremail@gmail.com"
GMAIL_APP_PASSWORD = "xxxxxxxxxxxxxxxx"
IMAP_HOST = "imap.gmail.com"
PAYMENT_SENDERS = ["fampay", "npci", "yesbank", "axisbank", "icicibank", "hdfcbank", "sbi"]
REQUIRE_DKIM = True
MAIL_LOOKBACK_DAYS = 2
EMAIL_POLL_SECONDS = 30
EMAIL_POLL_ROUNDS = 20
AUTO_APPROVE = True
PAYMENT_TIMEOUT = 5 * 60
MIN_PRICE, MAX_PRICE = 9, 999
SUPPORT_LINK = "http://t.me/Krishna_Zx_0"
DATA_FILE = "store_data.json"
# ================================================================

STATUS_LABEL = {
    "awaiting_payment": "⏳ Payment pending",
    "verifying": "🔍 Verify ho raha hai",
    "approved": "✅ Approved",
    "rejected": "❌ Rejected",
    "expired": "⌛ Expired",
    "cancelled": "🚫 Cancelled",
}

DB = {"products": {}, "users": {}, "orders": {}, "pending": {}}
BG_TASKS = set()


# ===================== Data store =====================
def load_db():
    global DB
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding="utf-8") as f:
            DB = json.load(f)
    for key in ("products", "users", "orders", "pending"):
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


def utr_taken(utr):
    return any(o.get("utr") == utr and o["status"] in ("verifying", "approved")
               for o in DB["orders"].values())


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


# ===================== Colorful QR card =====================
def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def centered(draw, text, y, fnt, fill, width):
    l, _, r, _ = draw.textbbox((0, 0), text, font=fnt)
    draw.text(((width - (r - l)) // 2, y), text, font=fnt, fill=fill)


def make_qr_card(upi_link, amount, order_id):
    qr = qrcode.QRCode(box_size=8, border=3)
    qr.add_data(upi_link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    pad = 40
    width = qr_img.width + pad * 2
    height = qr_img.height + 330
    card = Image.new("RGB", (width, height), (255, 243, 224))
    draw = ImageDraw.Draw(card)
    draw.rectangle([0, 0, width - 1, height - 1], outline=(255, 87, 34), width=16)
    draw.rectangle([16, 16, width - 17, 96], fill=(33, 150, 243))
    centered(draw, STORE_NAME, 40, font(34), "white", width)
    draw.rounded_rectangle([pad - 12, 118, width - pad + 12, 118 + qr_img.height + 24],
                           radius=20, fill="white", outline=(156, 39, 176), width=6)
    card.paste(qr_img, (pad, 130))

    y = 130 + qr_img.height + 40
    draw.rounded_rectangle([pad, y, width - pad, y + 60], radius=18, fill=(76, 175, 80))
    centered(draw, f"Pay: {amount} {CURRENCY}", y + 12, font(32), "white", width)
    centered(draw, f"Order: {order_id}", y + 85, font(22), (0, 0, 0), width)
    centered(draw, "Scan karke payment karein, phir UTR bhejein",
             y + 125, font(20), (156, 39, 176), width)

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

    auth = " ".join(msg.get_all("Authentication-Results", None) or []).lower()
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


def home_text():
    return (f"🙏 Welcome to {STORE_NAME} 🙏\n\n{STORE_DESCRIPTION}\n"
            f"🤖 {BOT_USERNAME}\n\nNiche se product choose karein:")


def admin_menu():
    return M([
        [B("➕ Add Product", callback_data="adm_add")],
        [B("📦 Products", callback_data="adm_products"), B("👥 Users", callback_data="adm_users")],
        [B("🧾 Orders", callback_data="adm_orders")],
        [B("⬅️ Back", callback_data="home")],
    ])


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
            f"UTR: {o['utr']}\n\n"
            f"Status: {status_line}")


def product_text(p):
    state = "Active ✅" if p["active"] else "Hidden ⏸"
    return (f"📦 {p['name']}\n💰 {money(p['price'])}\n🔗 {p['link']}\n"
            f"📌 Status: {state}\n📅 Added: {p['created']}")


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
        await q.answer()
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


# ===================== Admin: update admin message =====================
async def update_admin(bot, oid, status_line):
    o = DB["orders"][oid]
    if not o.get("admin_msg_id"):
        return
    markup = admin_decision_kb(oid) if o["status"] == "verifying" else None
    try:
        await bot.edit_message_text(
            chat_id=ADMIN_ID, message_id=o["admin_msg_id"],
            text=admin_order_text(o, status_line), reply_markup=markup)
    except Exception as e:
        log.warning("Admin message edit failed: %s", e)


async def approve(bot, oid, by):
    o = DB["orders"][oid]
    if o["status"] != "verifying":
        return
    prod = DB["products"].get(o["product_id"], {})
    o.update(status="approved", approved_at=now_str(), link=prod.get("link", ""), approved_by=by)
    save_db()
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=(f"✅ Payment verify ho gaya!\n\n🎁 {o['product_name']}\n"
                  "⬇️ Download button niche hai.\nYe link 🔗 My Link mein bhi milega."),
            reply_markup=M([[B("⬇️ Download", url=o["link"])]]) if o["link"] else None)
    except Exception as e:
        log.error("User ko product message nahi gaya: %s", e)
        by += " (user ko message nahi gaya)"
    await update_admin(bot, oid, f"✅ APPROVED - {by}")


async def reject(bot, oid, by):
    o = DB["orders"][oid]
    if o["status"] != "verifying":
        return
    o["status"] = "rejected"
    save_db()
    try:
        await bot.send_message(
            chat_id=o["user_id"],
            text=f"❌ Payment verify nahi ho paya. Support se contact karein: {SUPPORT_LINK}")
    except Exception as e:
        log.error("User ko reject message nahi gaya: %s", e)
    await update_admin(bot, oid, f"❌ REJECTED - {by}")


async def verify_loop(bot, oid):
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
                await update_admin(bot, oid, f"✅ Gmail verified: {note}\nAb Approve dabayein.")
            return
        if rnd < EMAIL_POLL_ROUNDS:
            await update_admin(bot, oid, f"⏳ Gmail check {rnd}/{EMAIL_POLL_ROUNDS}: {note}")
            await asyncio.sleep(EMAIL_POLL_SECONDS)

    await update_admin(
        bot, oid,
        f"⚠️ Auto verify fail: {o.get('verify_note', '')}\n"
        "Approve (sirf verified hone par), Force Approve ya Reject karein.")


# ===================== User side =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = remember(update)
    await update.message.reply_text(home_text(), reply_markup=home_markup(user.id))


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
    await q.answer()
    user = q.from_user
    prod = DB["products"].get(pid)
    if not prod or not prod["active"]:
        return await q.message.chat.send_message("❌ Ye product abhi available nahi hai.")

    old = DB["pending"].pop(str(user.id), None)
    if old and DB["orders"].get(old, {}).get("status") == "awaiting_payment":
        DB["orders"][old]["status"] = "cancelled"

    oid = uuid.uuid4().hex[:8].upper()
    now = time.time()
    order = {
        "id": oid, "user_id": user.id, "user_name": user.full_name,
        "username": user.username or "", "product_id": pid,
        "product_name": prod["name"], "amount": prod["price"],
        "status": "awaiting_payment", "created": now, "created_str": now_str(),
        "expires": now + PAYMENT_TIMEOUT, "utr": None, "link": "",
        "qr_msg_id": None, "admin_msg_id": None, "verified": False, "verify_note": "",
    }
    DB["orders"][oid] = order
    DB["pending"][str(user.id)] = oid
    save_db()

    upi = (f"upi://pay?pa={UPI_ID}&pn={quote(STORE_NAME)}"
           f"&am={prod['price']}&cu={CURRENCY}&tn={oid}")
    card = make_qr_card(upi, prod["price"], oid)
    caption = (f"🧾 {prod['name']}\n💰 {money(prod['price'])}\n"
               f"⏳ {PAYMENT_TIMEOUT // 60} minute ke andar payment karein.\n\n"
               "🔢 Payment ke baad 12-digit UTR is chat mein bhejein.")
    sent = await context.bot.send_photo(
        chat_id=user.id, photo=card, caption=caption,
        reply_markup=M([[B("❌ Order cancel karein", callback_data=f"cancel:{oid}")]]))
    order["qr_msg_id"] = sent.message_id
    save_db()


async def cancel_order(update, context, oid):
    q = update.callback_query
    o = DB["orders"].get(oid)
    if not o or o["user_id"] != q.from_user.id or o["status"] != "awaiting_payment":
        return await q.answer("Ye order cancel nahi ho sakta.", show_alert=True)
    o["status"] = "cancelled"
    DB["pending"].pop(str(q.from_user.id), None)
    save_db()
    await q.answer("Order cancel ho gaya")
    try:
        await q.edit_message_caption(caption="🚫 Ye order cancel ho gaya. Dobara order karne ke liye product choose karein.")
    except Exception:
        pass


async def handle_utr(update, context, oid, raw_text):
    user = update.effective_user
    o = DB["orders"].get(oid)

    if not o or o["status"] != "awaiting_payment":
        DB["pending"].pop(str(user.id), None)
        save_db()
        return await update.message.reply_text(
            "Ye order active nahi hai. Dobara product choose karein.", reply_markup=home_markup(user.id))

    if time.time() > o["expires"]:
        o["status"] = "expired"
        DB["pending"].pop(str(user.id), None)
        save_db()
        return await update.message.reply_text(
            "⌛ Time khatam ho gaya. Naya order karein.", reply_markup=home_markup(user.id))

    utr = re.sub(r"\s+", "", raw_text)
    if not re.fullmatch(r"\d{12}", utr):
        return await update.message.reply_text(
            "❌ UTR galat hai. UTR 12 digit ka hota hai, sirf numbers. Payment app mein dekh kar dobara bhejein.")

    if utr_taken(utr):
        return await update.message.reply_text(
            "❌ Ye UTR pehle hi use ho chuka hai. Ek UTR dobara verify nahi hota.")

    o.update(utr=utr, status="verifying", utr_at=now_str())
    DB["pending"].pop(str(user.id), None)
    save_db()

    await update.message.reply_text(
        "⏳ UTR mil gaya. Payment Gmail se verify ho raha hai, thoda wait karein.")
    try:
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID, text=admin_order_text(o, "⏳ Gmail verify ho raha hai"),
            reply_markup=admin_decision_kb(oid))
        o["admin_msg_id"] = admin_msg.message_id
        save_db()
    except Exception as e:
        log.error("Admin ko message nahi gaya: %s (admin ne bot ko /start kiya hai?)", e)

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
    await reply_or_edit(update, "🔗 Aapke purchased products. Download ke liye neeche button dabayein:", M(rows))


# ===================== Admin side =====================
async def show_admin(update, context):
    await reply_or_edit(update, "🛠 Admin Panel", admin_menu())


async def admin_add_start(update, context):
    context.user_data["step"] = "name"
    context.user_data["draft"] = {}
    await reply_or_edit(update, "➕ Product ka naam likhein (2 se 60 characters):",
                        M([[B("❌ Cancel", callback_data="adm")]]))


async def admin_step(update, context, text):
    step = context.user_data["step"]
    draft = context.user_data.setdefault("draft", {})

    if step == "name":
        if not 2 <= len(text) <= 60:
            return await update.message.reply_text("❌ Naam 2 se 60 characters ke beech hona chahiye.")
        draft["name"] = text
        context.user_data["step"] = "price"
        return await update.message.reply_text(
            f"💰 Price likhein ({money(MIN_PRICE)} se {money(MAX_PRICE)} ke beech, sirf number):")

    if step == "price":
        digits = re.sub(r"[₹\s]", "", text)
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
        return await update.message.reply_text(
            "Upar wale buttons se Submit ya Cancel karein, ya /cancel likhein.")


async def admin_products(update, context):
    rows = [[B(f"{'✅' if p['active'] else '⏸'} {p['name']} - {money(p['price'])}",
               callback_data=f"adm_prod:{pid}")]
            for pid, p in DB["products"].items()]
    rows.append([B("⬅️ Back", callback_data="adm")])
    text = "📦 Products (tap karke manage karein):" if rows[:-1] else "📦 Abhi koi product nahi hai."
    await reply_or_edit(update, text, M(rows))


async def admin_product_detail(update, context, pid):
    p = DB["products"].get(pid)
    if not p:
        return await reply_or_edit(update, "❌ Product nahi mila.", M([[B("⬅️ Back", callback_data="adm_products")]]))
    markup = M([
        [B("⏸ Hide / ▶️ Show", callback_data=f"adm_toggle:{pid}")],
        [B("🗑 Delete", callback_data=f"adm_delete:{pid}")],
        [B("⬅️ Back", callback_data="adm_products")],
    ])
    await reply_or_edit(update, product_text(p), markup)


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

    if action == "home":
        return await reply_or_edit(update, home_text(), home_markup(user.id))
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

    if action == "adm":
        context.user_data.clear()
        return await show_admin(update, context)
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
        return await reply_or_edit(update, f"✅ Product add ho gaya!\n\n{product_text(DB['products'][pid])}",
                                   admin_menu())
    if action == "adm_products":
        return await admin_products(update, context)
    if action == "adm_prod":
        return await admin_product_detail(update, context, arg)
    if action == "adm_toggle":
        p = DB["products"].get(arg)
        if p:
            p["active"] = not p["active"]
            save_db()
        return await admin_product_detail(update, context, arg)
    if action == "adm_delete":
        DB["products"].pop(arg, None)
        save_db()
        return await admin_products(update, context)
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

    if action in ("approve", "force", "reject"):
        o = DB["orders"].get(arg)
        if not o or o["status"] != "verifying":
            return await q.answer("Ye order pehle hi process ho chuka hai.", show_alert=True)
        if action == "approve":
            if not o.get("verified"):
                return await q.answer(
                    f"Gmail verify nahi hua: {o.get('verify_note', '')}\nAgar payment confirm hai to Force dabayein.",
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
    for oid, o in DB["orders"].items():
        if o["status"] == "verifying":
            spawn(verify_loop(app.bot, oid))


def main():
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
