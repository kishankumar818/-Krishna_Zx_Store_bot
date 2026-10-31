import os
import io
import re
import time
import uuid
import asyncio
import logging
import imaplib
import email
import threading
from email.utils import parseaddr, parsedate_to_datetime
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, HTTPServer

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import qrcode
from PIL import Image, ImageDraw, ImageFont
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, Conflict, InvalidToken, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)

from keep_alive import start_keep_alive, update_bot_state

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("krishna_store")
logging.getLogger("httpx").setLevel(logging.WARNING)


# ============================ CONFIG ============================
BOT_TOKEN = "PASTE_BOT_TOKEN_HERE"
BOT_USERNAME = "@Krishna_Zx_Store_bot"
ADMIN_ID = 8917638509
STORE_NAME = "Krishna_Zx_Store"
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
PAYMENT_TIMEOUT = 5 * 60          # QR ke baad 5 minute mein UTR bhejna hai
SUPPORT_LINK = "http://t.me/Krishna_Zx_0"

PRODUCTS = {
    "p1": {"name": "Premium Pack 1", "price": 99, "content": "Yahan product ka link ya content likho"},
    "p2": {"name": "Premium Pack 2", "price": 199, "content": "Yahan product ka link ya content likho"},
}
# =================================================================

orders = {}        # order_id -> dict
awaiting_utr = {}  # user_id -> order_id
used_utrs = set()


def money(amount):
    prefix = "₹" if CURRENCY == "INR" else f"{CURRENCY} "
    return f"{prefix}{amount}"


# ===================== Render & 24/7 Keep-Alive =====================
def start_health_server():
    start_keep_alive()


# ===================== QR card (colorful box) =====================
def load_font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def center_text(draw, text, y, font, fill, width):
    box = draw.textbbox((0, 0), text, font=font)
    w = box[2] - box[0]
    draw.text(((width - w) // 2, y), text, font=font, fill=fill)


def make_qr_card(upi_link, amount, order_id):
    qr = qrcode.QRCode(box_size=8, border=2)
    qr.add_data(upi_link)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    width = qr_img.width + 80
    height = qr_img.height + 260
    card = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(card)

    draw.rectangle([0, 0, width - 1, height - 1], outline=(255, 87, 34), width=14)
    draw.rectangle([14, 14, width - 15, 80], fill=(33, 150, 243))
    center_text(draw, STORE_NAME, 30, load_font(30), "white", width)
    card.paste(qr_img, (40, 100))

    y = 100 + qr_img.height + 20
    center_text(draw, f"Amount: {CURRENCY} {amount}", y, load_font(30), (0, 0, 0), width)
    center_text(draw, f"Order: {order_id}", y + 45, load_font(22), (76, 175, 80), width)
    center_text(draw, "Scan karke payment karein, phir UTR bhejein",
                y + 85, load_font(20), (156, 39, 176), width)

    buf = io.BytesIO()
    card.save(buf, format="PNG")
    buf.seek(0)
    return buf


def caption_for(order, remaining):
    mins, secs = divmod(max(remaining, 0), 60)
    return (f"🧾 {PRODUCTS[order['product']]['name']} - {money(order['amount'])}\n"
            f"⏳ Time bacha: {mins:02d}:{secs:02d}\n\n"
            "Payment ke baad UTR/Transaction ID is chat mein bhejein.")


# ===================== Gmail verification =====================
def mail_body_text(msg):
    texts = []
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        ctype = part.get_content_type()
        if ctype not in ("text/plain", "text/html"):
            continue
        payload = part.get_payload(decode=True) or b""
        text = payload.decode(part.get_content_charset() or "utf-8", errors="ignore")
        if ctype == "text/html":
            text = re.sub(r"<[^>]+>", " ", text)
        texts.append(text)
    return " ".join(texts)


def evaluate_mail(msg, order):
    """Ek mail ko 5 checks se verify karta hai. Sab pass hone par (True, note)."""
    body = mail_body_text(msg)

    # 1. UTR mail ke andar hona chahiye
    if order["utr"] not in body.upper():
        return False, "UTR mail mein nahi mila"

    # 2. Sender allowed list mein hona chahiye
    from_addr = parseaddr(msg.get("From", ""))[1].lower()
    domain = from_addr.split("@")[-1]
    sender = next((s for s in PAYMENT_SENDERS if s in domain), None)
    if not sender:
        return False, f"sender allowed nahi ({domain or 'unknown'})"

    # 3. DKIM pass hona chahiye (fake mail se bachav)
    auth = " ".join(msg.get_all("Authentication-Results", []) or []).lower()
    if REQUIRE_DKIM and not ("dkim=pass" in auth and sender in auth):
        return False, "DKIM verify nahi hua (fake mail ho sakta hai)"

    # 4. Amount exactly match hona chahiye
    amt = re.escape(str(order["amount"]))
    if not re.search(rf"(?<![\d.]){amt}(?:\.0+)?(?!\d)", body):
        return False, f"amount {money(order['amount'])} mail mein nahi mila"

    # 5. Mail order banne ke baad ka hona chahiye
    try:
        mail_time = parsedate_to_datetime(msg.get("Date")).timestamp()
    except Exception:
        return False, "mail ka date read nahi hua"
    if mail_time < order["created"] - 60:
        return False, "mail order se pehle ka hai"

    return True, f"{sender} mail, DKIM pass, amount match, UTR match"


def check_gmail_for_order(order):
    try:
        imap = imaplib.IMAP4_SSL(IMAP_HOST)
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")
        query = f'"{order["utr"]} newer_than:{MAIL_LOOKBACK_DAYS}d"'
        typ, data = imap.search(None, "X-GM-RAW", query)
        ids = data[0].split() if typ == "OK" and data and data[0] else []

        notes = []
        for num in ids[-20:]:
            _, msg_data = imap.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            ok, note = evaluate_mail(msg, order)
            if ok:
                imap.logout()
                return True, note
            notes.append(note)
        imap.logout()
        return False, notes[-1] if notes else "abhi mail nahi mili"
    except Exception as e:
        log.error("Gmail check failed: %s", e)
        return False, "Gmail connection error"


# ===================== Bot UI helpers =====================
def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛒 Products", callback_data="products")],
        [InlineKeyboardButton("💬 Support", url=SUPPORT_LINK)],
    ])


def admin_keyboard(order_id):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Approve", callback_data=f"approve:{order_id}"),
         InlineKeyboardButton("❌ Reject", callback_data=f"reject:{order_id}")],
        [InlineKeyboardButton("⚡ Force Approve", callback_data=f"force:{order_id}")],
    ])


def admin_text(order, status_line):
    p = PRODUCTS[order["product"]]
    return (f"🆕 Payment request - {STORE_NAME}\n"
            f"Order: {order['order_id']}\n"
            f"User: {order['user_name']} (@{order['username'] or '-'})\n"
            f"User ID: {order['user_id']}\n"
            f"Product: {p['name']}\n"
            f"Amount: {money(order['amount'])}\n"
            f"UTR: {order['utr']}\n\n"
            f"Status: {status_line}")


async def update_admin(bot, order, status_line):
    markup = admin_keyboard(order["order_id"]) if order["status"] == "verifying" else None
    try:
        await bot.edit_message_text(
            chat_id=ADMIN_ID, message_id=order["admin_msg_id"],
            text=admin_text(order, status_line), reply_markup=markup)
    except Exception as e:
        log.warning("Admin message edit failed: %s", e)


# ===================== Bot handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"🙏 Welcome to {STORE_NAME} 🙏\n\n"
        f"✨ Digital products yahan milenge.\n🤖 {BOT_USERNAME}\n\nNiche se choose karein:",
        reply_markup=main_menu(),
    )


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Aapka Telegram ID: {update.effective_user.id}")


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [[InlineKeyboardButton(f"{p['name']} - {money(p['price'])}", callback_data=f"buy:{pid}")]
               for pid, p in PRODUCTS.items()]
    await query.edit_message_text("🛍️ Product choose karein:", reply_markup=InlineKeyboardMarkup(buttons))


async def payment_timer(bot, order_id):
    order = orders[order_id]
    while order["status"] == "awaiting_payment":
        remaining = int(order["expires"] - time.time())
        if remaining <= 0:
            order["status"] = "expired"
            awaiting_utr.pop(order["user_id"], None)
            try:
                await bot.edit_message_caption(
                    chat_id=order["user_id"], message_id=order["qr_msg_id"],
                    caption="⌛ Time khatam. Ye order expire ho gaya, dobara order karein.")
            except Exception as e:
                log.warning("Expire caption edit failed: %s", e)
            return
        try:
            await bot.edit_message_caption(
                chat_id=order["user_id"], message_id=order["qr_msg_id"],
                caption=caption_for(order, remaining))
        except Exception as e:
            log.warning("Timer caption edit failed: %s", e)
        await asyncio.sleep(min(60, max(remaining, 1)))


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user
    pid = query.data.split(":", 1)[1]
    product = PRODUCTS.get(pid)
    if not product:
        return

    old = awaiting_utr.pop(user.id, None)
    if old and orders[old]["status"] == "awaiting_payment":
        orders[old]["status"] = "cancelled"

    order_id = uuid.uuid4().hex[:8].upper()
    upi_link = (f"upi://pay?pa={UPI_ID}&pn={quote(STORE_NAME)}"
                f"&am={product['price']}&cu={CURRENCY}&tn={order_id}")
    now = time.time()
    order = {
        "order_id": order_id, "user_id": user.id, "user_name": user.full_name,
        "username": user.username, "product": pid, "amount": product["price"],
        "status": "awaiting_payment", "created": now, "expires": now + PAYMENT_TIMEOUT,
        "utr": None, "qr_msg_id": None, "admin_msg_id": None,
        "verified": False, "verify_note": "",
    }
    orders[order_id] = order
    awaiting_utr[user.id] = order_id

    card = make_qr_card(upi_link, product["price"], order_id)
    sent = await context.bot.send_photo(
        chat_id=user.id, photo=card, caption=caption_for(order, PAYMENT_TIMEOUT))
    order["qr_msg_id"] = sent.message_id
    asyncio.create_task(payment_timer(context.bot, order_id))


async def approve_order_to_user(bot, order, by):
    order["status"] = "approved"
    p = PRODUCTS[order["product"]]
    try:
        await bot.send_message(
            chat_id=order["user_id"],
            text=f"✅ Payment verify ho gaya!\n\n🎁 {p['name']}:\n{p['content']}\n\nThanks for buying ❤️")
    except Exception as e:
        log.error("User ko product message nahi gaya: %s", e)
        by += " (user ko message nahi gaya)"
    await update_admin(bot, order, f"✅ APPROVED - {by}")


async def verify_payment(bot, order_id):
    order = orders[order_id]
    for rnd in range(1, EMAIL_POLL_ROUNDS + 1):
        if order["status"] != "verifying":
            return  # admin ne pehle hi decision le liya

        ok, note = await asyncio.to_thread(check_gmail_for_order, order)
        order["verify_note"] = note
        if ok:
            order["verified"] = True
            if AUTO_APPROVE:
                await approve_order_to_user(bot, order, "AUTO (Gmail verified)")
            else:
                await update_admin(bot, order, f"✅ Gmail verified: {note}\nAb Approve dabayein.")
            return

        if rnd < EMAIL_POLL_ROUNDS:
            await update_admin(bot, order, f"⏳ Gmail check {rnd}/{EMAIL_POLL_ROUNDS}: {note}")
            await asyncio.sleep(EMAIL_POLL_SECONDS)

    await update_admin(
        bot, order,
        f"⚠️ Auto verify fail: {order['verify_note']}\n"
        "Approve (sirf verified ho), Force Approve ya Reject karein.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = (update.message.text or "").strip().upper()
    order_id = awaiting_utr.get(user.id)
    if not order_id:
        await update.message.reply_text(
            "Koi active order nahi hai. Product choose karne ke liye neeche wala button dabayein.",
            reply_markup=main_menu())
        return

    order = orders[order_id]
    if order["status"] != "awaiting_payment":
        awaiting_utr.pop(user.id, None)
        await update.message.reply_text("Ye order ab active nahi hai. Dobara order karein.")
        return

    if not re.fullmatch(r"[A-Z0-9]{8,22}", text):
        await update.message.reply_text(
            "❌ Ye valid UTR nahi lag raha. Sirf UTR/Transaction ID bhejein (8-22 characters).")
        return
    if text in used_utrs:
        await update.message.reply_text("❌ Ye UTR pehle use ho chuka hai.")
        return

    used_utrs.add(text)
    order["utr"] = text
    order["status"] = "verifying"
    awaiting_utr.pop(user.id, None)
    await update.message.reply_text(
        "⏳ UTR mil gaya. Payment Gmail se verify ho raha hai, thoda wait karein.")

    try:
        admin_msg = await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=admin_text(order, "⏳ Gmail verify ho raha hai"),
            reply_markup=admin_keyboard(order_id))
        order["admin_msg_id"] = admin_msg.message_id
    except Exception as e:
        log.error("Admin ko message nahi gaya: %s (admin ne bot ko /start kiya hai?)", e)

    asyncio.create_task(verify_payment(context.bot, order_id))


async def admin_decision(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != ADMIN_ID:
        await query.answer("Ye sirf admin ke liye hai", show_alert=True)
        return

    action, order_id = query.data.split(":", 1)
    order = orders.get(order_id)
    if not order or order["status"] != "verifying":
        await query.answer("Ye order pehle hi process ho chuka hai.", show_alert=True)
        return

    if action == "approve":
        if not order["verified"]:
            await query.answer(
                f"Gmail verify nahi hua: {order['verify_note']}\nAgar payment confirm hai to Force Approve dabayein.",
                show_alert=True)
            return
        await query.answer()
        await approve_order_to_user(context.bot, order, "Admin (verified)")

    elif action == "force":
        await query.answer()
        await approve_order_to_user(context.bot, order, "Admin FORCE (Gmail verify nahi hua)")

    elif action == "reject":
        await query.answer()
        order["status"] = "rejected"
        try:
            await context.bot.send_message(
                chat_id=order["user_id"],
                text=f"❌ Payment verify nahi ho paya. Support se contact karein: {SUPPORT_LINK}")
        except Exception as e:
            log.error("User ko reject message nahi gaya: %s", e)
        await update_admin(context.bot, order, "❌ REJECTED - Admin")


# ===================== 24/7 Error Handler & Auto-Restart main =====================
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    err = context.error
    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after)
        return
    if isinstance(err, (TimedOut, NetworkError)):
        log.warning("Network/Timeout glitch (auto-recovering): %s", err)
        return
    if isinstance(err, Conflict):
        log.warning("Conflict detected: %s", err)
        await asyncio.sleep(5)
        return
    if isinstance(err, BadRequest) and "not modified" in str(err).lower():
        return
    log.error("Unhandled bot error: %s", err, exc_info=err)


def build_application():
    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(35.0)
        .get_updates_write_timeout(30.0)
        .get_updates_pool_timeout(30.0)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CallbackQueryHandler(show_products, pattern="^products$"))
    app.add_handler(CallbackQueryHandler(buy, pattern="^buy:"))
    app.add_handler(CallbackQueryHandler(admin_decision, pattern="^(approve|reject|force):"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.add_error_handler(on_error)
    return app


def main():
    if ADMIN_ID == 0:
        log.warning("ADMIN_ID set nahi hai")
    start_keep_alive()
    log.info("24/7 Keep-alive server started")

    restart_count = 0
    while True:
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = build_application()
            log.info("Bot running in 24/7 mode (restart #%d)", restart_count)
            update_bot_state(status="running", restart_count=restart_count)
            app.run_polling(
                drop_pending_updates=True,
                bootstrap_retries=-1,
                timeout=30,
                allowed_updates=Update.ALL_TYPES,
                close_loop=False,
            )
        except InvalidToken as e:
            log.error("Invalid BOT_TOKEN: %s", e)
            update_bot_state(status="invalid_token", last_error=str(e))
            time.sleep(60)
        except (KeyboardInterrupt, SystemExit):
            break
        except Exception as e:
            restart_count += 1
            log.error("Bot polling error (5s mein auto-restart #%d): %s", restart_count, e)
            update_bot_state(status="restarting", restart_count=restart_count, last_error=str(e))
            time.sleep(5)


if __name__ == "__main__":
    main()

