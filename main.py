import html
import logging
import os
import re
import socket

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.helpers import escape_markdown
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Force IPv4 resolution globally (resolves Hugging Face IPv6 DNS/routing issues)
orig_getaddrinfo = socket.getaddrinfo


def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == socket.AF_UNSPEC or family == 0:
        family = socket.AF_INET
    return orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = patched_getaddrinfo

load_dotenv()

app = FastAPI()

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL", "").rstrip("/")
WOOCOMMERCE_KEY = os.getenv("WOOCOMMERCE_KEY")
WOOCOMMERCE_SECRET = os.getenv("WOOCOMMERCE_SECRET")

required_env = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_WEBHOOK_SECRET": TELEGRAM_WEBHOOK_SECRET,
    "WOOCOMMERCE_URL": WOOCOMMERCE_URL,
    "WOOCOMMERCE_KEY": WOOCOMMERCE_KEY,
    "WOOCOMMERCE_SECRET": WOOCOMMERCE_SECRET,
}
missing_env = [name for name, value in required_env.items() if not value]
if missing_env:
    raise RuntimeError(f"Missing environment variables: {', '.join(missing_env)}")

application = Application.builder().token(TELEGRAM_BOT_TOKEN).updater(None).build()


# ==================== Formatting Helpers ====================

def md(value):
    """Escape dynamic values before interpolating into Telegram Markdown."""
    return escape_markdown(str(value or ""), version=1)


def strip_html(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text).strip()


def product_button_name(name):
    clean_name = str(name or "Product").strip()
    return clean_name[:32] if clean_name else "Product"


def main_menu():
    keyboard = [
        [InlineKeyboardButton("👔 Browse Products", callback_data="browse")],
        [InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("📦 My Order", callback_data="my_order")],
    ]
    text = (
        "🎉 *Welcome to DeenCommerce!*\n\n"
        "Browse our fashion collection, check stock, and view a specific order."
    )
    return text, InlineKeyboardMarkup(keyboard)


# ==================== WooCommerce API Helpers ====================

async def woo_get(path, params=None):
    """Fetch JSON from WooCommerce and normalize API/HTTP failures."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/{path.lstrip('/')}",
                params=params,
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10,
            )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("WooCommerce API returned %s for %s", e.response.status_code, path)
        return {"error": f"WooCommerce API returned {e.response.status_code}"}
    except Exception as e:
        logger.error("Error fetching WooCommerce path %s: %s", path, str(e))
        return {"error": str(e)}


async def get_all_products(limit=20):
    """Fetch latest products from WooCommerce."""
    return await woo_get(
        "products",
        params={"per_page": limit, "orderby": "date", "order": "desc"},
    )


async def get_product_by_id(product_id):
    """Fetch a single product."""
    return await woo_get(f"products/{product_id}")


async def search_products(keyword):
    """Search products by keyword."""
    return await woo_get("products", params={"search": keyword, "per_page": 10})


async def get_order_by_id(order_id):
    """Fetch a single order by ID."""
    return await woo_get(f"orders/{order_id}")


# ==================== Telegram Handlers ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - main menu."""
    text, reply_markup = main_menu()

    if update.callback_query:
        await update.callback_query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown",
        )
        return

    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def browse_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show featured products."""
    query = update.callback_query
    await query.answer()

    try:
        products = await get_all_products(limit=5)

        if isinstance(products, dict) and "error" in products:
            await query.edit_message_text(text=f"❌ Error: {md(products['error'])}", parse_mode="Markdown")
            return

        if not isinstance(products, list) or not products:
            await query.edit_message_text(text="No products found.")
            return

        text = "📦 *Latest Products*\n\n"
        keyboard = []

        for product in products[:5]:
            stock = product.get("stock_quantity")
            stock_text = stock if stock is not None else "N/A"
            status = "✅ In Stock" if product.get("in_stock") else "❌ Out of Stock"

            text += f"*{md(product.get('name', 'Product'))}*\n"
            text += f"💰 ৳{md(product.get('price', ''))}\n"
            text += f"📊 Stock: {md(stock_text)} {status}\n\n"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"View {product_button_name(product.get('name'))}",
                        callback_data=f"product_{product['id']}",
                    )
                ]
            )

        keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error("Error in browse_products: %s", str(e))
        await query.edit_message_text(text=f"❌ Error: {md(e)}", parse_mode="Markdown")


async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show product details."""
    query = update.callback_query
    product_id = query.data.removeprefix("product_")

    await query.answer()

    try:
        product = await get_product_by_id(product_id)

        if isinstance(product, dict) and "error" in product:
            await query.edit_message_text(text=f"❌ Error: {md(product['error'])}", parse_mode="Markdown")
            return

        stock = product.get("stock_quantity")
        stock_text = stock if stock is not None else "N/A"
        status = "✅ In Stock" if product.get("in_stock") else "❌ Out of Stock"

        text = f"*{md(product.get('name', 'Product'))}*\n\n"
        text += f"💰 Price: ৳{md(product.get('price', ''))}\n"
        text += f"📊 Stock: {md(stock_text)} {status}\n\n"

        desc_clean = strip_html(product.get("description", "No description"))
        if desc_clean:
            text += f"📝 {md(desc_clean[:300])}"
            if len(desc_clean) > 300:
                text += "..."
            text += "\n\n"

        keyboard = [[InlineKeyboardButton("← Back", callback_data="browse")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

        if product.get("images"):
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=product["images"][0]["src"],
                    caption=f"_{md(product.get('name', 'Product'))}_",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Could not send product image: %s", str(e))

    except Exception as e:
        logger.error("Error in view_product: %s", str(e))
        await query.edit_message_text(text=f"❌ Error: {md(e)}", parse_mode="Markdown")


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search command."""
    query = update.callback_query

    if query:
        await query.answer()
        await query.edit_message_text(
            text="🔍 *Search Products*\n\nType a product name, for example: shirt, jeans, dress.",
            parse_mode="Markdown",
        )
        context.user_data["waiting_for_search"] = True
        context.user_data.pop("waiting_for_order_lookup", None)
        return

    search_term = update.message.text.strip()
    context.user_data["waiting_for_search"] = False

    try:
        products = await search_products(search_term)

        if isinstance(products, dict) and "error" in products:
            await update.message.reply_text(f"❌ Error: {md(products['error'])}", parse_mode="Markdown")
            return

        if not products:
            await update.message.reply_text(f"❌ No products found for '{search_term}'")
            return

        text = f"🔍 *Search Results for '{md(search_term)}'*\n\n"
        keyboard = []

        for product in products[:5]:
            stock = product.get("stock_quantity")
            stock_text = stock if stock is not None else "N/A"
            text += f"*{md(product.get('name', 'Product'))}*\n"
            text += f"💰 ৳{md(product.get('price', ''))}\n"
            text += f"📊 Stock: {md(stock_text)}\n\n"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"View {product_button_name(product.get('name'))}",
                        callback_data=f"product_{product['id']}",
                    )
                ]
            )

        keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error("Error in search_handler: %s", str(e))
        await update.message.reply_text(f"❌ Error: {md(e)}", parse_mode="Markdown")


async def my_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user for a single order lookup."""
    query = update.callback_query

    if query:
        await query.answer()
        await query.edit_message_text(
            text=(
                "📦 *View Your Order*\n\n"
                "Enter your order number and billing email in one message:\n"
                "`1234 customer@example.com`"
            ),
            parse_mode="Markdown",
        )
        context.user_data["waiting_for_order_lookup"] = True
        context.user_data.pop("waiting_for_search", None)


def parse_order_lookup(user_text):
    match = re.match(r"^\s*#?(\d+)\s+([^\s@]+@[^\s@]+\.[^\s@]+)\s*$", user_text)
    if not match:
        return None, None
    return match.group(1), match.group(2).lower()


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for search or order lookup."""
    user_text = update.message.text

    if context.user_data.get("waiting_for_search"):
        await search_handler(update, context)
        return

    if context.user_data.get("waiting_for_order_lookup"):
        order_id, customer_email = parse_order_lookup(user_text)
        if not order_id:
            await update.message.reply_text(
                "Please send the order number and billing email like this:\n"
                "`1234 customer@example.com`",
                parse_mode="Markdown",
            )
            return

        context.user_data["waiting_for_order_lookup"] = False

        try:
            order = await get_order_by_id(order_id)

            if isinstance(order, dict) and "error" in order:
                await update.message.reply_text("❌ No matching order found.")
                return

            billing_email = (
                order.get("billing", {})
                .get("email", "")
                .strip()
                .lower()
            )
            if billing_email != customer_email:
                await update.message.reply_text("❌ No matching order found.")
                return

            status = str(order.get("status", "")).upper()
            total = order.get("total", "")
            date_created = str(order.get("date_created", ""))[:10]
            status_emoji = {
                "PENDING": "⏳",
                "PROCESSING": "🔄",
                "ON-HOLD": "⏸️",
                "COMPLETED": "✅",
                "CANCELLED": "❌",
                "REFUNDED": "🔄",
                "FAILED": "❌",
            }.get(status, "📦")

            text = f"{status_emoji} *Order #{md(order.get('id', order_id))}*\n\n"
            text += f"Status: {md(status)}\n"
            text += f"Total: ৳{md(total)}\n"
            text += f"Date: {md(date_created)}\n\n"

            items = order.get("line_items", [])
            if items:
                text += "Items:\n"
                for item in items:
                    text += f"  • {md(item.get('name', 'Item'))} (qty: {md(item.get('quantity', ''))})\n"

            keyboard = [[InlineKeyboardButton("← Back", callback_data="start_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)

            await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

        except Exception as e:
            logger.error("Error fetching order: %s", str(e))
            await update.message.reply_text("❌ Error fetching order.")


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to main menu."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await start(update, context)


# ==================== Register Handlers ====================

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^browse$"))
application.add_handler(CallbackQueryHandler(search_handler, pattern="^search$"))
application.add_handler(CallbackQueryHandler(my_order_handler, pattern="^my_order$"))
application.add_handler(CallbackQueryHandler(view_product, pattern="^product_"))
application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^start_menu$"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))


# ==================== FastAPI Routes ====================

@app.on_event("startup")
async def startup():
    """Initialize and start the Telegram application."""
    logger.info("Initializing Telegram application...")
    try:
        await application.initialize()
        await application.start()
        logger.info("Telegram application initialized and started.")
    except Exception as e:
        logger.critical("Failed to initialize Telegram application on startup: %s", str(e))
        raise


@app.on_event("shutdown")
async def shutdown():
    """Clean up on shutdown."""
    logger.info("Shutting down Telegram application...")
    if application.running:
        await application.stop()
    await application.shutdown()


@app.post("/telegram/webhook")
async def webhook(request: Request):
    """Telegram webhook."""
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != TELEGRAM_WEBHOOK_SECRET:
        logger.warning("Rejected Telegram webhook request with invalid secret token.")
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error("Error processing update: %s", str(e))
        return {"ok": False, "error": str(e)}


@app.get("/")
async def root():
    return {"status": "Telegram bot running", "bot": "DeenCommerce"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
