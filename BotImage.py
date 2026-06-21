import os
import random
import logging
import requests
import json
import hashlib
import time
import asyncio
import re
from io import BytesIO
from datetime import datetime
from PIL import Image
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, PreCheckoutQueryHandler, filters, ContextTypes

# ============ НАСТРОЙКИ ============
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables!")

PRICE_PER_IMAGE = 1

ADMIN_IDS_STR = os.environ.get("ADMIN_IDS", "6962544606,5437954093")
ADMIN_ID = [int(x.strip()) for x in ADMIN_IDS_STR.split(",") if x.strip().isdigit()]

REFERRAL_PERCENT = 0.1

# ============ НАСТРОЙКИ ГЕНЕРАЦИИ ============
IMAGE_WIDTH = 1024
IMAGE_HEIGHT = 1024
MODEL = "turbo"
MAX_RETRIES = 5
BASE_DELAY = 5

# ============ СПИСОК ДЕТАЛЕЙ ============
UNIQUE_DETAILS = [
    "fantasy style", "epic scene", "detailed background",
    "dramatic lighting", "mystical atmosphere", "beautiful composition",
    "intricate details", "cinematic shot", "rich colors",
    "deep shadows", "bright highlights", "soft focus"
]

# ============ ЛОГИРОВАНИЕ ============
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============ БАЗА ДАННЫХ ============
user_balances = {}
user_referrals = {}
user_ref_count = {}
user_free_claimed = {}
user_total_generations = {}
user_waiting_for_prompt = {}
prompt_cache = {}

support_cache = {}
admin_reply_mode = {}

DATA_FILE = "bot_data.json"


def is_admin(user_id):
    return user_id in ADMIN_ID


def load_data():
    global user_balances, user_referrals, user_ref_count, user_free_claimed, user_total_generations
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                user_balances = {int(k): v for k, v in data.get("balances", {}).items()}
                user_referrals = {int(k): int(v) for k, v in data.get("referrals", {}).items()}
                user_ref_count = {int(k): v for k, v in data.get("ref_count", {}).items()}
                user_free_claimed = {int(k): v for k, v in data.get("free_claimed", {}).items()}
                user_total_generations = {int(k): v for k, v in data.get("total_generations", {}).items()}
                logger.info("Data loaded")
    except Exception as e:
        logger.error(f"Data load error: {e}")


def save_data():
    try:
        data = {
            "balances": user_balances,
            "referrals": user_referrals,
            "ref_count": user_ref_count,
            "free_claimed": user_free_claimed,
            "total_generations": user_total_generations
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Data save error: {e}")


def uniquify_prompt(user_prompt):
    base_prompt = user_prompt.strip()
    if len(base_prompt) < 5:
        base_prompt += " fantasy art"
    prompt_hash = hashlib.md5(base_prompt.encode()).hexdigest()
    used_details = prompt_cache.get(prompt_hash, [])
    available_details = [d for d in UNIQUE_DETAILS if d not in used_details]
    if not available_details:
        used_details = []
        prompt_cache[prompt_hash] = []
        available_details = UNIQUE_DETAILS.copy()
    chosen_detail = random.choice(available_details)
    used_details.append(chosen_detail)
    prompt_cache[prompt_hash] = used_details
    final_prompt = f"{base_prompt}, {chosen_detail} variant {random.randint(1, 999)}"
    return final_prompt


def generate_image(prompt):
    try:
        encoded = requests.utils.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&model={MODEL}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, timeout=60, headers=headers)
                if resp.status_code == 429:
                    wait = BASE_DELAY * (attempt + 1) + random.random() * 5
                    logger.warning(f"429 — pause {wait:.1f}s, attempt {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return Image.open(BytesIO(resp.content))
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout, attempt {attempt + 1}/{MAX_RETRIES}")
                time.sleep(BASE_DELAY)
                continue
            except requests.exceptions.ConnectionError:
                logger.warning(f"Connection error, attempt {attempt + 1}/{MAX_RETRIES}")
                time.sleep(BASE_DELAY * 2)
                continue
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"Error: {e}, attempt {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(BASE_DELAY)
                    continue
                raise
        return None
    except Exception as e:
        logger.error(f"Image generation error: {e}")
        return None


async def generate_and_send(update, context, user_id, user_prompt, final_prompt):
    try:
        status_msg = await update.message.reply_text(
            f"🎨 *Generating image...*\n⏳ Please wait.",
            parse_mode="Markdown"
        )

        loop = asyncio.get_event_loop()
        img = await loop.run_in_executor(None, generate_image, final_prompt)

        if img is None:
            await status_msg.edit_text("❌ Generation error. Try again.")
            if not is_admin(user_id):
                user_balances[user_id] = user_balances.get(user_id, 0) + 1
                save_data()
            return

        user_total_generations[user_id] = user_total_generations.get(user_id, 0) + 1
        save_data()

        img_bytes = BytesIO()
        img.save(img_bytes, format="PNG")
        img_bytes.seek(0)

        caption = (
            f"✅ *Done!*\n\n"
            f"📝 Prompt: `{user_prompt[:80]}{'...' if len(user_prompt) > 80 else ''}`\n"
            f"⭐ Generations left: {'♾️' if is_admin(user_id) else user_balances.get(user_id, 0)}"
        )

        await update.message.reply_photo(
            photo=img_bytes,
            caption=caption,
            parse_mode="Markdown"
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Generate and send error: {e}")
        await update.message.reply_text("❌ An error occurred. Please try again later.")


def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🎨 Create generation")],
        [KeyboardButton("👤 Profile"), KeyboardButton("🛒 Shop")],
        [KeyboardButton("👥 Referrals"), KeyboardButton("📞 Support")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    if context.args and len(context.args) > 0:
        try:
            referrer_id = int(context.args[0])
            if referrer_id != user_id and user_id not in user_referrals:
                user_referrals[user_id] = referrer_id
                user_ref_count[referrer_id] = user_ref_count.get(referrer_id, 0) + 1
                save_data()
                await update.message.reply_text(
                    f"✅ You were invited by a user!\n🎁 You got 1 free generation!"
                )
                user_balances[user_id] = user_balances.get(user_id, 0) + 1
                save_data()
        except:
            pass

    welcome_text = (
        f"🎨 *Hello, {user.first_name}!*\n\n"
        "I create unique images from your description!\n"
        "💰 Price: *1 Telegram Star* per generation.\n\n"
        "📝 Click *'Create generation'* and enter your prompt.\n"
        "🔄 Each image will be unique!\n\n"
        "Use the menu buttons to navigate."
    )

    await update.message.reply_text(
        welcome_text,
        parse_mode="Markdown"
    )

    await update.message.reply_text(
        "👇",
        reply_markup=get_main_keyboard()
    )

    user_waiting_for_prompt[user_id] = False
    save_data()


async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id

    if is_admin(user_id) and admin_reply_mode.get(user_id, False):
        await forward_admin_reply(update, context)
        return

    if update.message.reply_to_message and is_admin(update.message.reply_to_message.from_user.id):
        await forward_to_admin(update, context, text)
        return

    menu_buttons = ["🎨 Create generation", "👤 Profile", "🛒 Shop", "👥 Referrals", "📞 Support"]

    if text in menu_buttons:
        if text == "🎨 Create generation":
            user_waiting_for_prompt[user_id] = True
            save_data()
            await update.message.reply_text(
                "📝 *Enter your prompt*\n\n"
                "Describe what you want to see in the image.\n"
                "Example: *dragon in the sky, fantasy style*\n\n"
                "🔄 To cancel, press any other menu button.",
                parse_mode="Markdown"
            )
            return
        elif text == "👤 Profile":
            await show_profile(update, context)
            return
        elif text == "🛒 Shop":
            await show_shop(update, context)
            return
        elif text == "👥 Referrals":
            await show_referrals(update, context)
            return
        elif text == "📞 Support":
            await show_support(update, context)
            return

    if user_waiting_for_prompt.get(user_id, False):
        user_waiting_for_prompt[user_id] = False
        save_data()
        await process_prompt(update, context, text)
        return

    if not is_admin(user_id):
        if text.startswith('/'):
            return
        await forward_to_admin(update, context, text)
    else:
        await update.message.reply_text(
            "👑 You are an admin. Use the menu buttons.",
            parse_mode="Markdown"
        )


async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    bot_id = (await context.bot.get_me()).id

    if user_id == bot_id:
        logger.warning("Bot tried to send support message to itself")
        return

    user_name = update.effective_user.first_name
    username = update.effective_user.username or "No username"

    msg = (
        f"📩 *New support message*\n\n"
        f"👤 User: {user_name} (@{username})\n"
        f"🆔 User ID: `{user_id}`\n"
        f"📝 Message:\n`{text}`\n\n"
        f"💡 Reply to this message to answer the user."
    )

    try:
        for admin_id in ADMIN_ID:
            try:
                sent_msg = await context.bot.send_message(
                    chat_id=admin_id,
                    text=msg,
                    parse_mode="Markdown"
                )
                support_cache[sent_msg.message_id] = user_id
                logger.info(f"Message to admin {admin_id}, msg_id: {sent_msg.message_id} -> user_id: {user_id}")
            except Exception as e:
                logger.warning(f"Could not send to admin {admin_id}: {e}")

        await update.message.reply_text("✅ Your message has been sent to the administrator. Please wait for a response.")
    except Exception as e:
        logger.error(f"Error sending to admin: {e}")
        await update.message.reply_text("❌ Could not send message. Please try again later.")


async def forward_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply_to = update.message.reply_to_message
    admin_text = update.message.text.strip()

    if not reply_to:
        await update.message.reply_text(
            "❌ *To reply to a user:*\n"
            "1. Click 'Reply' on the user's message\n"
            "2. Or reply to the bot's message with user ID\n"
            "3. Or use command `/reply ID text`",
            parse_mode="Markdown"
        )
        admin_reply_mode[user_id] = False
        return

    bot_id = (await context.bot.get_me()).id
    target_user_id = None

    if reply_to.message_id in support_cache:
        target_user_id = support_cache[reply_to.message_id]
        logger.info(f"ID found in cache: {target_user_id}")

    if target_user_id is None:
        try:
            text = reply_to.text or reply_to.caption or ""
            patterns = [
                r'🆔 User ID[:：]\s*`?(\d+)`?',
                r'🆔 ID[:：]\s*`?(\d+)`?',
                r'ID[:：]\s*`?(\d+)`?',
                r'user_id[:：]\s*`?(\d+)`?',
            ]
            for pattern in patterns:
                match = re.search(pattern, text, re.IGNORECASE)
                if match:
                    found_id = int(match.group(1))
                    if found_id != bot_id and found_id != user_id:
                        target_user_id = found_id
                        logger.info(f"ID found by pattern: {target_user_id}")
                        break
        except Exception as e:
            logger.warning(f"Text parsing error: {e}")

    if target_user_id is None and reply_to.from_user:
        found_id = reply_to.from_user.id
        if found_id != bot_id and found_id != user_id:
            target_user_id = found_id
            logger.info(f"ID from message author: {target_user_id}")

    if target_user_id is None:
        try:
            numbers = re.findall(r'\b(\d{8,12})\b', reply_to.text or "")
            for num in numbers:
                found_id = int(num)
                if found_id != bot_id and found_id != user_id:
                    target_user_id = found_id
                    logger.info(f"ID by number pattern: {target_user_id}")
                    break
        except Exception as e:
            logger.warning(f"Number search error: {e}")

    if target_user_id is None:
        await update.message.reply_text(
            "❌ *Could not find user ID.*\n\n"
            "📋 *Ways to reply:*\n"
            "1. Reply directly to the user's message\n"
            "2. Use command: `/reply ID text`\n"
            "3. Wait for a new message from the user",
            parse_mode="Markdown"
        )
        admin_reply_mode[user_id] = False
        return

    if is_admin(target_user_id):
        await update.message.reply_text("👑 This is an admin. Message not sent.")
        admin_reply_mode[user_id] = False
        return

    if target_user_id == bot_id:
        await update.message.reply_text("❌ Cannot send message to the bot itself.")
        admin_reply_mode[user_id] = False
        return

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📞 *Response from administrator:*\n\n{admin_text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"✅ *Reply sent to user*\n🆔 ID: `{target_user_id}`",
            parse_mode="Markdown"
        )
        logger.info(f"Admin {user_id} replied to user {target_user_id}")
    except Exception as e:
        logger.error(f"Reply send error: {e}")
        await update.message.reply_text(f"❌ Could not send reply. Error: {str(e)}")

    admin_reply_mode[user_id] = False


async def process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str):
    user_id = update.effective_user.id

    if len(user_prompt) < 3:
        await update.message.reply_text("❌ Too short. Write more (at least 3 characters).")
        return

    if not is_admin(user_id):
        if user_balances.get(user_id, 0) <= 0:
            await update.message.reply_text(
                "❌ You have no generations left!\nBuy more in the Shop (🛒 Shop button)."
            )
            return
        user_balances[user_id] -= 1
    save_data()

    final_prompt = uniquify_prompt(user_prompt)
    await generate_and_send(update, context, user_id, user_prompt, final_prompt)


async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = "♾️" if is_admin(user_id) else user_balances.get(user_id, 0)
    ref_count = user_ref_count.get(user_id, 0)
    total_gen = user_total_generations.get(user_id, 0)

    text = (
        f"👤 *Your profile*\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"⭐ Balance: {balance} generations\n"
        f"🖼️ Total generated: {total_gen} images\n"
        f"👥 Invited: {ref_count} people\n"
        f"💰 Referral earnings: {ref_count * 0.1:.1f} stars"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👑 You are an admin, you have infinite generations!")
        return

    keyboard = [
        [InlineKeyboardButton("⭐ 1 generation — 1 star", callback_data="buy_1")],
        [InlineKeyboardButton("⭐ 10 generations — 10 stars", callback_data="buy_10")],
        [InlineKeyboardButton("⭐ 100 generations — 50 stars", callback_data="buy_100")],
        [InlineKeyboardButton("⭐ 1000 generations — 500 stars", callback_data="buy_1000")],
        [InlineKeyboardButton("🔙 Back", callback_data="back_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🛒 *Generations Shop*\n\n"
        "Choose a plan:\n"
        "• 1 generation — 1 star\n"
        "• 10 generations — 10 stars\n"
        "• 100 generations — 50 stars\n"
        "• 1000 generations — 500 stars\n\n"
        "💡 Payment is automatically processed via Telegram Stars.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def show_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    ref_count = user_ref_count.get(user_id, 0)

    text = (
        f"👥 *Referral system*\n\n"
        f"📎 Your referral link:\n"
        f"`{ref_link}`\n\n"
        f"👥 Invited: {ref_count} people\n"
        f"💰 You get 10% of each referral's spending!\n\n"
        f"🔹 1 referral spent 10 stars → you get 1 star\n"
        f"🔹 100 referrals spent 10 stars → you get 100 stars\n\n"
        f"📤 Share the link with your friends!"
    )

    keyboard = [[InlineKeyboardButton("📋 Copy link", callback_data="copy_ref")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👑 You are an admin. You don't need support.")
        return

    text = (
        f"📞 *Support*\n\n"
        f"Write any message and it will be sent to the administrator."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = "♾️" if is_admin(user_id) else user_balances.get(user_id, 0)
    await update.message.reply_text(f"⭐ Your balance: {balance} generations")


async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("👑 You are an admin, you have infinite generations.")
        return

    if user_free_claimed.get(user_id, False):
        await update.message.reply_text("❌ You have already used the free generation.")
        return
    user_free_claimed[user_id] = True
    user_balances[user_id] = user_balances.get(user_id, 0) + 1
    save_data()
    await update.message.reply_text(
        "✅ You have been given *1 free generation*!\n"
        "Click 'Create generation' and send your prompt.",
        parse_mode="Markdown"
    )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE, count: int, price: int):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    title = f"{count} AI image generations"
    description = f"Package of {count} unique AI-generated images."
    payload = f"generate_{user_id}_{datetime.now().timestamp()}"
    currency = "XTR"
    prices = [LabeledPrice(f"{count} generations", price)]

    await context.bot.send_invoice(
        chat_id=chat_id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency=currency,
        prices=prices,
        need_name=False,
        need_phone_number=False,
        need_email=False,
        need_shipping_address=False,
        is_flexible=False,
    )


async def pre_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.pre_checkout_query
    await query.answer(ok=True)


async def successful_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    total_amount = update.message.successful_payment.total_amount

    if total_amount == 1:
        count = 1
    elif total_amount == 10:
        count = 10
    elif total_amount == 50:
        count = 100
    elif total_amount == 500:
        count = 1000
    else:
        count = total_amount

    user_balances[user_id] = user_balances.get(user_id, 0) + count
    save_data()

    if user_id in user_referrals:
        referrer_id = user_referrals[user_id]
        bonus = total_amount * REFERRAL_PERCENT
        user_balances[referrer_id] = user_balances.get(referrer_id, 0) + bonus
        save_data()
        try:
            await context.bot.send_message(
                referrer_id,
                f"🎉 Your referral spent {total_amount} stars!\n"
                f"💰 You received {bonus:.1f} generations as a bonus!"
            )
        except:
            pass

    await update.message.reply_text(
        f"✅ Payment confirmed!\n"
        f"⭐ You received {count} generations.\n"
        f"💰 Balance: {user_balances[user_id]} generations\n\n"
        "📝 Click 'Create generation' and send your prompt!"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    data = query.data

    if data == "free":
        await free(update, context)
        await query.message.edit_reply_markup(reply_markup=None)
    elif data == "shop":
        await show_shop(update, context)
    elif data == "back_menu":
        await start(update, context)
    elif data.startswith("buy_"):
        count = int(data.split("_")[1])
        if count == 1:
            await buy(update, context, 1, 1)
        elif count == 10:
            await buy(update, context, 10, 10)
        elif count == 100:
            await buy(update, context, 100, 50)
        elif count == 1000:
            await buy(update, context, 1000, 500)
    elif data == "copy_ref":
        bot_username = (await context.bot.get_me()).username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        await query.message.reply_text(
            f"📎 Your referral link:\n`{ref_link}`",
            parse_mode="Markdown"
        )


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Only for administrators.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Usage:* `/reply ID text`\n"
            "Example: `/reply 123456789 Hello!`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(args[0])
        text = " ".join(args[1:])

        if is_admin(target_user_id):
            await update.message.reply_text("👑 This is an admin. Message not sent.")
            return

        if target_user_id == (await context.bot.get_me()).id:
            await update.message.reply_text("❌ Cannot send message to the bot itself.")
            return

        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📞 *Response from administrator:*\n\n{text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"✅ *Reply sent to user*\n🆔 ID: `{target_user_id}`",
            parse_mode="Markdown"
        )
        logger.info(f"Admin {user_id} replied to user {target_user_id} via /reply")
    except ValueError:
        await update.message.reply_text("❌ ID must be a number.")
    except Exception as e:
        logger.error(f"Reply command error: {e}")
        await update.message.reply_text(f"❌ Could not send reply. Error: {str(e)}")


def main():
    load_data()

    from telegram.request import HTTPXRequest
    import asyncio

    request = HTTPXRequest(
        connect_timeout=120.0,
        read_timeout=120.0,
        write_timeout=120.0,
        connection_pool_size=8
    )

    app = Application.builder().token(TELEGRAM_TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("free", free))
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("help", lambda u, c: u.message.reply_text(
        "📝 *Commands:*\n"
        "/start — main menu\n"
        "/balance — balance\n"
        "/free — 1 free generation (1 time)\n"
        "/reply ID text — reply to user (admins only)\n"
        "/help — help\n\n"
        "💡 Use the menu buttons to navigate.",
        parse_mode="Markdown"
    )))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🤖 Bot started! Payment via Telegram Stars.")
    logger.info(f"👤 ADMIN ID: {ADMIN_ID}")
    logger.info("📌 To reply to a user: /reply ID text")
    logger.info("📌 Or simply reply to the user's message")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    loop.run_until_complete(app.run_polling(allowed_updates=Update.ALL_TYPES))


if __name__ == "__main__":
    main()