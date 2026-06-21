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
# БЕЗОПАСНО: токен только из переменных окружения
TELEGRAM_TOKEN = os.environ.get("BOT_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("BOT_TOKEN не найден в переменных окружения! Добавь BOT_TOKEN в Render.")

PRICE_PER_IMAGE = 1

# Админы — через переменную окружения или по умолчанию
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

# ============ КЭШ СООБЩЕНИЙ ПОДДЕРЖКИ ============
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
                logger.info("Данные загружены")
    except Exception as e:
        logger.error(f"Ошибка загрузки данных: {e}")


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
        logger.error(f"Ошибка сохранения данных: {e}")


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


def generate_image(prompt, retry=0):
    try:
        encoded = requests.utils.quote(prompt)
        url = f"https://image.pollinations.ai/prompt/{encoded}?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&model={MODEL}"
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.get(url, timeout=60, headers=headers)
                if resp.status_code == 429:
                    wait = BASE_DELAY * (attempt + 1) + random.random() * 5
                    logger.warning(f"429 — пауза {wait:.1f} сек, попытка {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                return Image.open(BytesIO(resp.content))
            except requests.exceptions.Timeout:
                logger.warning(f"Таймаут, попытка {attempt + 1}/{MAX_RETRIES}")
                time.sleep(BASE_DELAY)
                continue
            except requests.exceptions.ConnectionError:
                logger.warning(f"Ошибка соединения, попытка {attempt + 1}/{MAX_RETRIES}")
                time.sleep(BASE_DELAY * 2)
                continue
            except Exception as e:
                if attempt < MAX_RETRIES - 1:
                    logger.warning(f"Ошибка: {e}, попытка {attempt + 1}/{MAX_RETRIES}")
                    time.sleep(BASE_DELAY)
                    continue
                raise
        return None
    except Exception as e:
        logger.error(f"Ошибка генерации: {e}")
        return None


async def generate_and_send(update, context, user_id, user_prompt, final_prompt):
    try:
        status_msg = await update.message.reply_text(
            f"🎨 *Генерация создаётся...*\n⏳ Ожидайте.",
            parse_mode="Markdown"
        )

        loop = asyncio.get_event_loop()
        img = await loop.run_in_executor(None, generate_image, final_prompt)

        if img is None:
            await status_msg.edit_text("❌ Ошибка генерации. Попробуйте ещё раз.")
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
            f"✅ *Готово!*\n\n"
            f"📝 Ваш промпт: `{user_prompt[:80]}{'...' if len(user_prompt) > 80 else ''}`\n"
            f"⭐ Осталось генераций: {'♾️' if is_admin(user_id) else user_balances.get(user_id, 0)}"
        )

        await update.message.reply_photo(
            photo=img_bytes,
            caption=caption,
            parse_mode="Markdown"
        )
        await status_msg.delete()

    except Exception as e:
        logger.error(f"Ошибка в generate_and_send: {e}")
        await update.message.reply_text("❌ Произошла ошибка. Попробуйте позже.")


def get_main_keyboard():
    keyboard = [
        [KeyboardButton("🎨 Создать генерацию")],
        [KeyboardButton("👤 Профиль"), KeyboardButton("🛒 Магазин")],
        [KeyboardButton("👥 Рефералы"), KeyboardButton("📞 Поддержка")]
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
                    f"✅ Вы приглашены пользователем!\n🎁 Вы получили 1 бесплатную генерацию!"
                )
                user_balances[user_id] = user_balances.get(user_id, 0) + 1
                save_data()
        except:
            pass

    welcome_text = (
        f"🎨 *Привет, {user.first_name}!*\n\n"
        "Я создаю уникальные картинки по твоему описанию!\n"
        "💰 Стоимость: *1 звезда Telegram* за генерацию.\n\n"
        "📝 Нажми кнопку *«Создать генерацию»* и напиши промпт.\n"
        "🔄 Каждая картинка будет уникальной!\n\n"
        "Используй кнопки меню для навигации."
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

    menu_buttons = ["🎨 Создать генерацию", "👤 Профиль", "🛒 Магазин", "👥 Рефералы", "📞 Поддержка"]

    if text in menu_buttons:
        if text == "🎨 Создать генерацию":
            user_waiting_for_prompt[user_id] = True
            save_data()
            await update.message.reply_text(
                "📝 *Введите ваш промпт*\n\n"
                "Опишите, что вы хотите увидеть на картинке.\n"
                "Пример: *dragon in the sky, fantasy style*\n\n"
                "🔄 Если хотите отменить — нажмите любую другую кнопку меню.",
                parse_mode="Markdown"
            )
            return
        elif text == "👤 Профиль":
            await show_profile(update, context)
            return
        elif text == "🛒 Магазин":
            await show_shop(update, context)
            return
        elif text == "👥 Рефералы":
            await show_referrals(update, context)
            return
        elif text == "📞 Поддержка":
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
            "👑 Вы администратор. Используйте кнопки меню.",
            parse_mode="Markdown"
        )


async def forward_to_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    user_id = update.effective_user.id
    bot_id = (await context.bot.get_me()).id

    if user_id == bot_id:
        logger.warning("Бот пытался отправить сообщение в поддержку сам себе")
        return

    user_name = update.effective_user.first_name
    username = update.effective_user.username or "Нет юзернейма"

    msg = (
        f"📩 *Новое сообщение в поддержку*\n\n"
        f"👤 Пользователь: {user_name} (@{username})\n"
        f"🆔 ID пользователя: `{user_id}`\n"
        f"📝 Сообщение:\n`{text}`\n\n"
        f"💡 Ответьте на это сообщение, чтобы ответить пользователю."
    )

    try:
        sent_messages = []
        for admin_id in ADMIN_ID:
            try:
                sent_msg = await context.bot.send_message(
                    chat_id=admin_id,
                    text=msg,
                    parse_mode="Markdown"
                )
                support_cache[sent_msg.message_id] = user_id
                sent_messages.append(sent_msg)
                logger.info(f"Сообщение админу {admin_id}, msg_id: {sent_msg.message_id} -> user_id: {user_id}")
            except Exception as e:
                logger.warning(f"Не удалось отправить админу {admin_id}: {e}")

        await update.message.reply_text("✅ Ваше сообщение отправлено администратору. Ожидайте ответа.")
    except Exception as e:
        logger.error(f"Ошибка отправки админу: {e}")
        await update.message.reply_text("❌ Не удалось отправить сообщение. Попробуйте позже.")


async def forward_admin_reply(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    reply_to = update.message.reply_to_message
    admin_text = update.message.text.strip()

    if not reply_to:
        await update.message.reply_text(
            "❌ *Для ответа пользователю:*\n"
            "1. Нажмите 'Ответить' на сообщении от пользователя\n"
            "2. Или ответьте на сообщение бота с ID пользователя\n"
            "3. Или используйте команду `/reply ID текст`",
            parse_mode="Markdown"
        )
        admin_reply_mode[user_id] = False
        return

    bot_id = (await context.bot.get_me()).id
    target_user_id = None

    # 1. Проверка по кэшу
    if reply_to.message_id in support_cache:
        target_user_id = support_cache[reply_to.message_id]
        logger.info(f"ID найден по кэшу: {target_user_id}")

    # 2. Поиск в тексте
    if target_user_id is None:
        try:
            text = reply_to.text or reply_to.caption or ""
            patterns = [
                r'🆔 ID пользователя[:：]\s*`?(\d+)`?',
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
                        logger.info(f"ID найден по паттерну: {target_user_id}")
                        break
        except Exception as e:
            logger.warning(f"Ошибка парсинга текста: {e}")

    # 3. Из автора сообщения
    if target_user_id is None and reply_to.from_user:
        found_id = reply_to.from_user.id
        if found_id != bot_id and found_id != user_id:
            target_user_id = found_id
            logger.info(f"ID взят из автора сообщения: {target_user_id}")

    # 4. Поиск чисел в тексте
    if target_user_id is None:
        try:
            numbers = re.findall(r'\b(\d{8,12})\b', reply_to.text or "")
            for num in numbers:
                found_id = int(num)
                if found_id != bot_id and found_id != user_id:
                    target_user_id = found_id
                    logger.info(f"ID найден по числовому паттерну: {target_user_id}")
                    break
        except Exception as e:
            logger.warning(f"Ошибка поиска чисел: {e}")

    if target_user_id is None:
        await update.message.reply_text(
            "❌ *Не удалось найти ID пользователя.*\n\n"
            "📋 *Способы ответить:*\n"
            "1. Ответьте на сообщение пользователя напрямую\n"
            "2. Используйте команду: `/reply ID текст`\n"
            "3. Дождитесь нового сообщения от пользователя",
            parse_mode="Markdown"
        )
        admin_reply_mode[user_id] = False
        return

    if is_admin(target_user_id):
        await update.message.reply_text("👑 Это администратор. Сообщение не отправлено.")
        admin_reply_mode[user_id] = False
        return

    if target_user_id == bot_id:
        await update.message.reply_text("❌ Нельзя отправить сообщение самому боту.")
        admin_reply_mode[user_id] = False
        return

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📞 *Ответ от администратора:*\n\n{admin_text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"✅ *Ответ отправлен пользователю*\n🆔 ID: `{target_user_id}`",
            parse_mode="Markdown"
        )
        logger.info(f"Админ {user_id} ответил пользователю {target_user_id}")
    except Exception as e:
        logger.error(f"Ошибка отправки ответа: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить ответ. Ошибка: {str(e)}")

    admin_reply_mode[user_id] = False


async def process_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, user_prompt: str):
    user_id = update.effective_user.id

    if len(user_prompt) < 3:
        await update.message.reply_text("❌ Слишком короткий запрос. Напишите подробнее (минимум 3 символа).")
        return

    if not is_admin(user_id):
        if user_balances.get(user_id, 0) <= 0:
            await update.message.reply_text(
                "❌ У вас закончились генерации!\nКупите новые в магазине (кнопка 🛒 Магазин)."
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
        f"👤 *Ваш профиль*\n\n"
        f"🆔 ID: `{user_id}`\n"
        f"⭐ Баланс: {balance} генераций\n"
        f"🖼️ Всего сгенерировано: {total_gen} картинок\n"
        f"👥 Приглашено: {ref_count} человек\n"
        f"💰 Заработано рефералов: {ref_count * 0.1:.1f} звезд"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def show_shop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👑 Вы администратор, у вас бесконечные генерации!")
        return

    keyboard = [
        [InlineKeyboardButton("⭐ 1 генерация — 1 звезда", callback_data="buy_1")],
        [InlineKeyboardButton("⭐ 10 генераций — 10 звезд", callback_data="buy_10")],
        [InlineKeyboardButton("⭐ 100 генераций — 50 звезд", callback_data="buy_100")],
        [InlineKeyboardButton("⭐ 1000 генераций — 500 звезд", callback_data="buy_1000")],
        [InlineKeyboardButton("🔙 Назад", callback_data="back_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "🛒 *Магазин генераций*\n\n"
        "Выберите тариф:\n"
        "• 1 генерация — 1 звезда\n"
        "• 10 генераций — 10 звезд\n"
        "• 100 генераций — 50 звезд\n"
        "• 1000 генераций — 500 звезд\n\n"
        "💡 Оплата происходит автоматически через Telegram Stars.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def show_referrals(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_username = (await context.bot.get_me()).username
    ref_link = f"https://t.me/{bot_username}?start={user_id}"
    ref_count = user_ref_count.get(user_id, 0)

    text = (
        f"👥 *Реферальная система*\n\n"
        f"📎 Ваша реферальная ссылка:\n"
        f"`{ref_link}`\n\n"
        f"👥 Приглашено: {ref_count} человек\n"
        f"💰 Вы получаете 10% от трат каждого реферала!\n\n"
        f"🔹 1 реферал потратил 10 звезд → вы получаете 1 звезду\n"
        f"🔹 100 рефералов потратили по 10 звезд → вы получаете 100 звезд\n\n"
        f"📤 Поделитесь ссылкой с друзьями!"
    )

    keyboard = [[InlineKeyboardButton("📋 Скопировать ссылку", callback_data="copy_ref")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_admin(update.effective_user.id):
        await update.message.reply_text("👑 Вы администратор. Вам не нужна поддержка.")
        return

    text = (
        f"📞 *Поддержка*\n\n"
        f"Напишите любое сообщение, и оно будет отправлено администратору."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    balance = "♾️" if is_admin(user_id) else user_balances.get(user_id, 0)
    await update.message.reply_text(f"⭐ Ваш баланс: {balance} генераций")


async def free(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_admin(user_id):
        await update.message.reply_text("👑 Вы администратор, у вас бесконечные генерации.")
        return

    if user_free_claimed.get(user_id, False):
        await update.message.reply_text("❌ Вы уже использовали бесплатную генерацию.")
        return
    user_free_claimed[user_id] = True
    user_balances[user_id] = user_balances.get(user_id, 0) + 1
    save_data()
    await update.message.reply_text(
        "✅ Вам начислена *1 бесплатная генерация*!\n"
        "Нажмите «Создать генерацию» и отправьте промпт.",
        parse_mode="Markdown"
    )


async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE, count: int, price: int):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    title = f"{count} генераций ИИ-картинок"
    description = f"Пакет из {count} генераций уникальных картинок через ИИ."
    payload = f"generate_{user_id}_{datetime.now().timestamp()}"
    currency = "XTR"
    prices = [LabeledPrice(f"{count} генераций", price)]

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
                f"🎉 Ваш реферал потратил {total_amount} звезд!\n"
                f"💰 Вы получили {bonus:.1f} генераций в подарок!"
            )
        except:
            pass

    await update.message.reply_text(
        f"✅ Оплата подтверждена!\n"
        f"⭐ Вам начислено {count} генераций.\n"
        f"💰 Баланс: {user_balances[user_id]} генераций\n\n"
        "📝 Нажмите «Создать генерацию» и отправьте промпт!"
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
            f"📎 Ваша реферальная ссылка:\n`{ref_link}`",
            parse_mode="Markdown"
        )


async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    if not is_admin(user_id):
        await update.message.reply_text("⛔ Только для администраторов.")
        return

    args = context.args
    if len(args) < 2:
        await update.message.reply_text(
            "❌ *Использование:* `/reply ID текст`\n"
            "Пример: `/reply 123456789 Привет!`",
            parse_mode="Markdown"
        )
        return

    try:
        target_user_id = int(args[0])
        text = " ".join(args[1:])

        if is_admin(target_user_id):
            await update.message.reply_text("👑 Это администратор. Сообщение не отправлено.")
            return

        if target_user_id == (await context.bot.get_me()).id:
            await update.message.reply_text("❌ Нельзя отправить сообщение самому боту.")
            return

        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"📞 *Ответ от администратора:*\n\n{text}",
            parse_mode="Markdown"
        )
        await update.message.reply_text(
            f"✅ *Ответ отправлен пользователю*\n🆔 ID: `{target_user_id}`",
            parse_mode="Markdown"
        )
        logger.info(f"Админ {user_id} ответил пользователю {target_user_id} через команду /reply")
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
    except Exception as e:
        logger.error(f"Ошибка в reply_command: {e}")
        await update.message.reply_text(f"❌ Не удалось отправить ответ. Ошибка: {str(e)}")


def main():
    load_data()

    from telegram.request import HTTPXRequest
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
        "📝 *Команды:*\n"
        "/start — главное меню\n"
        "/balance — баланс\n"
        "/free — бесплатная генерация (1 раз)\n"
        "/reply ID текст — ответ пользователю (только для админов)\n"
        "/help — помощь\n\n"
        "💡 Используйте кнопки меню для навигации.",
        parse_mode="Markdown"
    )))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text))
    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(button_callback))

    logger.info("🤖 Бот запущен! Оплата звёздами Telegram.")
    logger.info(f"👤 ADMIN ID: {ADMIN_ID}")
    logger.info("📌 Для ответа пользователю: /reply ID текст")
    logger.info("📌 Или просто ответьте на сообщение пользователя")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()