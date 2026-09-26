import os
import logging
import requests
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# Logging configuration
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ERPNEXT_URL = os.getenv("ERPNEXT_URL", "https://erp.yourdomain.com").rstrip('/')
ERPNEXT_API_KEY = os.getenv("ERPNEXT_API_KEY")
ERPNEXT_API_SECRET = os.getenv("ERPNEXT_API_SECRET")
ALLOWED_CHAT_IDS = [
    int(cid.strip()) for cid in os.getenv("ALLOWED_CHAT_IDS", "").split(",") if cid.strip()
]

# ERPNext API Request Helper
def erp_request(method: str, path: str, params: dict = None, json_data: dict = None):
    url = f"{ERPNEXT_URL}/api/{path}"
    headers = {
        "Authorization": f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    response = requests.request(method, url, headers=headers, params=params, json=json_data, timeout=15)
    response.raise_for_status()
    return response.json()

# Security Middleware: Verify Chat ID
async def is_authorized(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True  # If no IDs specified, allow all (not recommended for production)
    user_id = update.effective_chat.id
    if user_id not in ALLOWED_CHAT_IDS:
        await update.message.reply_text("⛔ Unauthorized access.")
        return False
    return True

# Command: /start
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    msg = (
        "🤖 *ERPNext Telegram Bot*\n\n"
        "Available Commands:\n"
        "• `/quotes` - List recent open Quotations\n"
        "• `/customer <name>` - Search for a customer\n"
        "• `/ping` - Test ERPNext connectivity"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

# Command: /ping
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    try:
        data = erp_request("GET", "method/frappe.handler.ping")
        if data.get("message") == "pong":
            await update.message.reply_text("✅ Connected successfully to ERPNext!")
        else:
            await update.message.reply_text("⚠️ Connected, but unexpected response received.")
    except Exception as e:
        logger.error(f"Ping failed: {e}")
        await update.message.reply_text(f"❌ Connection failed: {str(e)}")

# Command: /quotes
async def quotes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    try:
        params = {
            "fields": '["name", "customer_name", "grand_total", "status", "transaction_date"]',
            "filters": '[["docstatus", "=", 0]]',  # Draft / Open quotes
            "limit_page_length": 10,
            "order_by": "creation desc"
        }
        res = erp_request("GET", "resource/Quotation", params=params)
        quotes = res.get("data", [])

        if not quotes:
            await update.message.reply_text("No open quotations found.")
            return

        text = "📋 *Recent Open Quotations:*\n\n"
        for q in quotes:
            text += (
                f"• *{q['name']}*\n"
                f"  Customer: {q['customer_name']}\n"
                f"  Amount: `${q['grand_total']:,.2f}`\n"
                f"  Date: {q['transaction_date']}\n\n"
            )
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error fetching quotes: {e}")
        await update.message.reply_text(f"❌ Failed to fetch quotations: {str(e)}")

# Command: /customer <name>
async def customer_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_authorized(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/customer <name>`", parse_mode="Markdown")
        return

    search_term = " ".join(context.args)
    try:
        params = {
            "fields": '["name", "customer_name", "customer_type", "territory"]',
            "filters": f'[["customer_name", "like", "%{search_term}%"]]',
            "limit_page_length": 5
        }
        res = erp_request("GET", "resource/Customer", params=params)
        customers = res.get("data", [])

        if not customers:
            await update.message.reply_text(f"No customers matching *'{search_term}'*.", parse_mode="Markdown")
            return

        text = f"🔍 *Customer Search Results for '{search_term}':*\n\n"
        for c in customers:
            text += f"• *{c['customer_name']}* ({c['name']})\n  Territory: {c.get('territory', 'N/A')}\n"
        await update.message.reply_text(text, parse_mode="Markdown")
    except Exception as e:
        logger.error(f"Error searching customer: {e}")
        await update.message.reply_text(f"❌ Failed to search customer: {str(e)}")

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required.")

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("quotes", quotes_command))
    app.add_handler(CommandHandler("customer", customer_command))

    logger.info("Bot started successfully. Listening for commands...")
    app.run_polling()

if __name__ == "__main__":
    main()