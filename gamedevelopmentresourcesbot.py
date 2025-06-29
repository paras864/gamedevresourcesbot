import logging
import datetime
import sqlite3
from telegram import Update, ChatPermissions, ChatInviteLink
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    ContextTypes, filters, CallbackContext
)
import time
from telegram import InlineKeyboardMarkup, InlineKeyboardButton

from telegram.ext import CallbackQueryHandler

# --- CONFIGURATION ---
TOKEN = '7702130558:AAE3fjdFbg8Tz9KOznVleYPR2jxAdJ4anMY'
ADMIN_ID = 6470227146  # Replace with your Telegram user ID
PREMIUM_CHANNEL_ID = -1002856460300  # Replace with your channel ID
DB_PATH = "telegram_premium_bot.db"

pending_downloads = {}
# --- LOGGING ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# --- DATABASE ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            email TEXT,
            level INTEGER DEFAULT 1,
            join_date TEXT,
            daily_count INTEGER DEFAULT 0,
            last_reset TEXT,
            approved INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS files (
            file_key TEXT PRIMARY KEY,
            file_id TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS downloads (
            user_id INTEGER,
            file_key TEXT,
            date TEXT
        )
    """)
    conn.commit()
    conn.close()

def register_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.datetime.now().isoformat()
    cursor.execute("INSERT OR IGNORE INTO users (user_id, join_date, last_reset) VALUES (?, ?, ?)",
                   (user_id, now, now))
    conn.commit()
    conn.close()

def approve_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET approved=1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def is_user_approved(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT approved FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row and row[0] == 1

def set_user_level(user_id, level):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET level=? WHERE user_id=?", (level, user_id))
    conn.commit()
    conn.close()

def get_user_level(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT level FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else None

def get_user_join_date(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT join_date FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return datetime.datetime.fromisoformat(row[0]) if row else None

def reset_user_downloads(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM downloads WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()

def check_download_limit(user_id):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    now = datetime.datetime.now()
    cursor.execute("SELECT level, daily_count, last_reset FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return False, "You are not registered."

    level, daily_count, last_reset = row
    limit = 4 if level == 1 else 8
    reset_time = datetime.datetime.fromisoformat(last_reset)

    if reset_time.date() < now.date():
        cursor.execute("UPDATE users SET daily_count=1, last_reset=? WHERE user_id=?",
                       (now.isoformat(), user_id))
        conn.commit()
        conn.close()
        return True, None

    if daily_count >= limit:
        conn.close()
        return False, f"You've reached your daily limit of {limit} downloads."

    cursor.execute("UPDATE users SET daily_count = daily_count + 1 WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
    return True, None

async def remove_expired_users(context: CallbackContext):
    now = datetime.datetime.now()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, join_date FROM users")
    users = cursor.fetchall()
    for user_id, join_date in users:
        join_time = datetime.datetime.fromisoformat(join_date)
        if now - join_time > datetime.timedelta(days=30):
            try:
                await context.bot.ban_chat_member(PREMIUM_CHANNEL_ID, user_id)
                await context.bot.unban_chat_member(PREMIUM_CHANNEL_ID, user_id)
                await context.bot.send_message(user_id, "Your access has expired after 30 days.")
                cursor.execute("UPDATE users SET approved = 0 WHERE user_id = ?", (user_id,))
            except Exception as e:
                logger.error(f"Failed to remove user {user_id}: {e}")
    conn.close()

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT approved, level, join_date FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if row:
        approved = "✅ Approved" if row[0] else "❌ Not Approved"
        level = row[1]
        join_date_str = row[2]
        join_date = datetime.datetime.fromisoformat(join_date_str)
        expiry = join_date + datetime.timedelta(days=30)

        await query.message.reply_text(
            f"👤 **Your Status:**\n"
            f"🔹 Level: {level}\n"
            f"🔒 Approval: {approved}\n"
            f"📅 Joined: {join_date.date()}\n"
            f"⏳ Expires: {expiry.date()}",
            parse_mode="Markdown"
        )
    else:
        await query.message.reply_text("❌ You’re not registered. Please press 🔹 Start again.")

    conn.close()

# --- HANDLERS ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id

    # Always register
    register_user(user_id)

    # Check for deep link args like ?start=download_filekey
    args = context.args
    if args:
        payload = args[0]
        if payload.startswith("download_"):
            file_key = payload.replace("download_", "")
            pending_downloads[user_id] = file_key
            await handle_download_request(update, context, file_key)
            return

    # If a download was previously handled, clear state and show normal message
    if user_id in pending_downloads:
        del pending_downloads[user_id]             

    # Proceed with your existing logic
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT approved FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()
    conn.close()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Check Status", callback_data="check_status")]
    ])

    if row and row[0] == 1:
        await update.message.reply_text(
            "✅ You are already a premium member.\nGo to the channel and start downloading!",
            reply_markup=keyboard
        )
    else:
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔹 Start", callback_data="start_info"),
             InlineKeyboardButton("📊 Check Status", callback_data="check_status")]
        ])
        await update.message.reply_text(
            "👋 Welcome! Please send a screenshot of your payment, your email, and your level (1 or 2).\nWe'll verify and get back to you.",
            reply_markup=keyboard
        )

async def reset_limit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("⛔ You are not authorized to use this command.")

    if not context.args:
        return await update.message.reply_text("⚠️ Please provide a user ID.\nUsage: `/resetlimit 123456789`", parse_mode="Markdown")

    try:
        target_user_id = int(context.args[0])
        reset_user_downloads(target_user_id)
        await update.message.reply_text(f"✅ Download limit reset for user `{target_user_id}`", parse_mode="Markdown")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Error: {str(e)}")

async def handle_download_request(update: Update, context: ContextTypes.DEFAULT_TYPE, file_key: str):
    user_id = update.effective_user.id
    today = datetime.datetime.now().date().isoformat()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Check if user is approved and get level
    cursor.execute("SELECT approved, level FROM users WHERE user_id=?", (user_id,))
    row = cursor.fetchone()

    if not row or row[0] != 1:
        conn.close()
        return await update.message.reply_text("❌ You are not approved for downloads. Please contact the admin.")

    level = row[1]
    max_downloads = 4 if level == 1 else 8

    # Count today's downloads
    cursor.execute("SELECT COUNT(*) FROM downloads WHERE user_id = ? AND date = ?", (user_id, today))
    used = cursor.fetchone()[0]

    if used >= max_downloads:
        conn.close()
        return await update.message.reply_text("🚫 You’ve reached your daily download limit. Try again tomorrow.")

    # Fetch file from DB
    cursor.execute("SELECT file_id FROM files WHERE file_key = ?", (file_key,))
    result = cursor.fetchone()

    if not result:
        conn.close()
        return await update.message.reply_text("❌ The requested file was not found.")

    file_id = result[0]

    # Send file
    try:
        await context.bot.send_document(chat_id=user_id, document=file_id)
    except Exception as e:
        conn.close()
        return await update.message.reply_text(f"⚠️ Failed to send file: {str(e)}")

    # Log the download
    cursor.execute(
        "INSERT INTO downloads (user_id, file_key, date) VALUES (?, ?, ?)",
        (user_id, file_key, today)
    )
    conn.commit()

    remaining = max_downloads - (used + 1)

    await context.bot.send_message(
        chat_id=user_id,
        text=f"📦 File sent successfully!\n✅ Downloads used: {used + 1}/{max_downloads}\n⏳ Remaining today: {remaining}"
    )

    conn.close()

async def forward_payment_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    message_text = update.message.caption or update.message.text or "(no text)"
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📩 New request:\n🆔 User ID: {user.id}\n👤 Username: @{user.username or 'N/A'}\n\n📝 Message:\n{message_text}"
        )
        if update.message.photo:
            await context.bot.send_photo(chat_id=ADMIN_ID, photo=update.message.photo[-1].file_id)
        elif update.message.document:
            await context.bot.send_document(chat_id=ADMIN_ID, document=update.message.document.file_id)
    except Exception as e:
        logger.error(f"Error forwarding user info: {e}")

async def approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You're not authorized.")
        return
    try:
        user_id = int(context.args[0])
        approve_user(user_id)
        link = await context.bot.create_chat_invite_link(
            chat_id=PREMIUM_CHANNEL_ID,
            member_limit=1,
            expire_date=int(time.time()) + 86400
        )
        await context.bot.send_message(user_id, f"✅ You’ve been approved! Join here: {link.invite_link}")
        await update.message.reply_text(f"✅ User {user_id} approved and link sent.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def setlevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ You're not authorized.")
        return
    try:
        user_id = int(context.args[0])
        level = int(context.args[1])
        if level not in (1, 2):
            await update.message.reply_text("❌ Level must be 1 or 2.")
            return
        set_user_level(user_id, level)
        await update.message.reply_text(f"✅ Set level {level} for user {user_id}")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

async def getlevel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    try:
        user_id = int(context.args[0])
        level = get_user_level(user_id)
        await update.message.reply_text(f"User {user_id} is Level {level}")
    except:
        await update.message.reply_text("Usage: /getlevel <user_id>")

def get_file_id_by_key(key):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    #print(f"🔍 Looking for file_key: '{key}'")  # ADD THIS LINE
    cursor.execute("SELECT file_id FROM files WHERE file_key=?", (key,))
    row = cursor.fetchone()
    #print(f"🎯 Match found: {row}")  # ADD THIS LINE
    conn.close()
    return row[0] if row else None

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_user_approved(user_id):
        await update.message.reply_text("⛔ Access denied. You’re not approved yet.")
        return

    if len(context.args) == 0:
        await update.message.reply_text("Usage: /download <file_key>\nExample: /download unity-effects")
        return

    key = context.args[0].lower()
    file_id = get_file_id_by_key(key)
    if not file_id:
        await update.message.reply_text("❌ File not found.")
        return

    allowed, msg = check_download_limit(user_id)
    if not allowed:
        await update.message.reply_text(f"❌ {msg}")
        return

    await update.message.reply_document(file_id, caption=f"📦 Yeah! Grab Your: {key}")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT level FROM users WHERE user_id=?", (user_id,))
    level = cursor.fetchone()[0]

    # Determine max downloads
    max_downloads = 4 if level == 1 else 8

    # Count current day usage
    today = datetime.datetime.now().date().isoformat()
    cursor.execute("SELECT COUNT(*) FROM downloads WHERE user_id=? AND date=?", (user_id, today))
    used = cursor.fetchone()[0]

    remaining = max_downloads - used
    cursor.execute(
        "INSERT INTO downloads (user_id, file_key, date) VALUES (?, ?, ?)",
        (user_id, key, today)
    )
    conn.commit()
    await context.bot.send_message(
        chat_id=user_id,
        text=f"📦 Download successful!\n✅ You’ve used {used + 1}/{max_downloads} downloads today.\n⏳ Remaining: {remaining}"
    )
    
    conn.close()
    

async def auto_save_uploaded_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ADMIN_ID:
        return

    doc = update.message.document
    if doc:
        key = doc.file_name.split('.')[0].strip().lower().replace(' ', '_')
        file_id = doc.file_id
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO files (file_key, file_id) VALUES (?, ?)", (key, file_id))
        conn.commit()
        conn.close()
        bot_username = (await context.bot.get_me()).username
        download_link = f"https://t.me/{bot_username}?start=download_{key}"

        await update.message.reply_text(
            f"✅ File `{key}` uploaded and saved.\n\n"
            f"📎 Shareable Download Link:\n"
            f"[Click to Download]({download_link})",
            parse_mode="Markdown",
            disable_web_page_preview=True
        )


# --- MAIN ---
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setlevel", setlevel))
    app.add_handler(CommandHandler("getlevel", getlevel))
    app.add_handler(CommandHandler("download", download))
    app.add_handler(CommandHandler("approve", approve))
    app.add_handler(CommandHandler("resetlimit", reset_limit))
    app.add_handler(MessageHandler(filters.Document.ALL, auto_save_uploaded_file))
    app.add_handler(MessageHandler(filters.TEXT | filters.PHOTO | filters.Document.ALL, forward_payment_info))
    


    app.add_handler(CallbackQueryHandler(button_handler))

    app.job_queue.run_repeating(remove_expired_users, interval=43200, first=10)  # every 12 hours

    app.run_polling()

if __name__ == '__main__':
    main()
