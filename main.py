from contextlib import asynccontextmanager
import html
import logging
import os
import re
import socket
import time

import httpx
import json
from anthropic import AsyncAnthropic
import openai
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
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
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# Force IPv4 resolution globally (resolves Hugging Face IPv6 DNS/routing issues)
orig_getaddrinfo = socket.getaddrinfo


def patched_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == socket.AF_UNSPEC or family == 0:
        family = socket.AF_INET
    return orig_getaddrinfo(host, port, family, type, proto, flags)


socket.getaddrinfo = patched_getaddrinfo

load_dotenv()

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

import utils
from utils import (
    categories_cache,
    products_cache,
    preprocess_search_query,
    get_pathao_tracking_status,
    get_tracking_info,
    extract_and_format_size_chart,
    md,
    strip_html,
    strip_html_excluding_table,
    product_button_name,
    stock_display,
    woo_get,
    get_store_address
)
from rag_agent import RAGAgent
from db import upsert_user

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Lifecycle events for FastAPI application."""
    logger.info("Initializing Telegram application...")
    try:
        # Initialize global HTTP client
        utils.http_client = httpx.AsyncClient(
            auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
            timeout=10.0
        )

        await application.initialize()
        await application.start()
        logger.info("Telegram application initialized and started.")

        # Collect and set bot commands dynamically from registered handlers
        bot_commands = []
        registered_set = set()
        descriptions = {
            "start": "Start the bot & main menu",
            "help": "Support and FAQs",
            "browse": "Browse categories",
            "search": "Search products",
            "my_order": "View order status",
            "ask": "Ask the AI Shopping Assistant"
        }

        for group in application.handlers.values():
            for handler in group:
                if isinstance(handler, CommandHandler):
                    for command in handler.commands:
                        if command not in registered_set:
                            registered_set.add(command)
                            desc = descriptions.get(command, f"Use /{command} command")
                            bot_commands.append(BotCommand(command, desc))

        if bot_commands:
            logger.info("Registering bot commands dynamically: %s", [c.command for c in bot_commands])
            await application.bot.delete_my_commands()
            await application.bot.set_my_commands(bot_commands)

        # Auto-register webhook if external URL is provided
        webhook_base = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("WEBHOOK_URL")
        if webhook_base:
            webhook_url = f"{webhook_base.rstrip('/')}/telegram/webhook"
            logger.info("Auto-registering Telegram webhook: %s", webhook_url)
            await application.bot.set_webhook(
                url=webhook_url,
                secret_token=TELEGRAM_WEBHOOK_SECRET
            )
            logger.info("Telegram webhook auto-registered successfully.")
        else:
            logger.warning("No RENDER_EXTERNAL_URL or WEBHOOK_URL environment variable found. Webhook was not auto-registered.")
    except Exception as e:
        logger.critical("Failed to initialize Telegram application on startup: %s", str(e))
        raise

    yield

    logger.info("Shutting down Telegram application...")
    if application.running:
        await application.stop()
    await application.shutdown()

    # Close global HTTP client
    if utils.http_client:
        await utils.http_client.aclose()

app = FastAPI(lifespan=lifespan)







def main_menu(first_name=None, cart_count=0):
    keyboard = [
        [InlineKeyboardButton("👔 Categories", callback_data="browse")],
        [InlineKeyboardButton("🆕 Latest Products", callback_data="products_all_1")],
        [InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("📦 My Order", callback_data="my_order")],
        [InlineKeyboardButton("🤖 Ask DEEN AI Agent", callback_data="ask_ai")],
    ]
    if cart_count > 0:
        keyboard.insert(0, [InlineKeyboardButton(f"🛍️ View Cart ({cart_count})", callback_data="view_cart")])
        
    greeting = f"Assalamu Alaikum {md(first_name)}" if first_name else "Assalamu Alaikum"
    text = (
        f"🎉 *{greeting}! Welcome to DeenCommerce!*\n\n"
        "Browse by category, check stock, view a specific order, or ask our AI assistant."
    )
    return text, InlineKeyboardMarkup(keyboard)



async def get_all_products(limit=20):
    """Fetch latest products from WooCommerce."""
    return await woo_get(
        "products",
        params={
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": "publish",
            "stock_status": "instock",
        },
    )


async def get_categories(limit=100):
    """Fetch product categories that have products (with caching)."""
    cache_key = f"categories_{limit}"
    cached = categories_cache.get(cache_key)
    if cached is not None:
        logger.info("Using cached categories list.")
        return cached

    categories = await woo_get(
        "products/categories",
        params={"per_page": limit, "orderby": "name", "order": "asc", "hide_empty": True},
    )
    if isinstance(categories, list) and len(categories) > 0:
        categories_cache.set(cache_key, categories)
    return categories


async def get_products_by_category(category_id, page=1, limit=8):
    """Fetch products from a category."""
    return await woo_get(
        "products",
        params={
            "category": category_id,
            "page": page,
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": "publish",
            "stock_status": "instock",
        },
    )


async def get_products_page(page=1, limit=8):
    """Fetch a page of latest products."""
    return await woo_get(
        "products",
        params={
            "page": page,
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": "publish",
            "stock_status": "instock",
        },
    )


async def get_product_by_id(product_id):
    """Fetch a single product (with caching)."""
    cache_key = f"product_{product_id}"
    cached = products_cache.get(cache_key)
    if cached is not None:
        logger.info("Using cached product data for product ID: %s", product_id)
        return cached

    product = await woo_get(f"products/{product_id}")
    if isinstance(product, dict) and "error" not in product:
        products_cache.set(cache_key, product)
    return product


async def get_category_by_id(category_id):
    """Fetch a single category."""
    return await woo_get(f"products/categories/{category_id}")


async def search_products(keyword):
    """Search products by keyword."""
    processed_keyword = preprocess_search_query(keyword)
    logger.info("Searching products. Original: %s -> Processed: %s", keyword, processed_keyword)
    return await woo_get(
        "products",
        params={
            "search": processed_keyword,
            "per_page": 10,
            "status": "publish",
            "stock_status": "instock",
        },
    )


async def get_order_by_id(order_id):
    """Fetch a single order by ID."""
    return await woo_get(f"orders/{order_id}")




# Store agents per user (so each user has their own conversation)
user_agents = {}


async def ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle conversational AI queries"""
    user_id = update.effective_user.id
    
    if update.callback_query and update.callback_query.data == "retry_ai_chat":
        await update.callback_query.answer()
        user_message = context.user_data.get("last_ai_query")
        if not user_message:
            await update.effective_message.reply_text("❌ No previous query found.")
            return
    else:
        user_message = update.message.text
        if user_message:
            context.user_data["last_ai_query"] = user_message

    if not user_message:
        return

    # Create agent for user if doesn't exist
    if user_id not in user_agents:
        user_agents[user_id] = RAGAgent(
            woocommerce_url=WOOCOMMERCE_URL,
            woocommerce_key=WOOCOMMERCE_KEY,
            woocommerce_secret=WOOCOMMERCE_SECRET,
            user_id=user_id
        )

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    try:
        # Process message with RAG agent
        response = await user_agents[user_id].process_message(user_message, user_id)

        # Attach continuous chat options to the final response
        keyboard = [
            [
                InlineKeyboardButton("🗑️ Reset Chat", callback_data="reset_ai_chat"),
                InlineKeyboardButton("← Back to Menu", callback_data="start_menu")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Split long responses (Telegram has 4096 char limit)
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                if i + 4000 >= len(response):
                    await update.effective_message.reply_text(
                        response[i:],
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
                else:
                    await update.effective_message.reply_text(
                        response[i:i+4000],
                        parse_mode="Markdown"
                    )
        else:
            await update.effective_message.reply_text(
                response,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error("AI chat error: %s", str(e))
        keyboard = [[InlineKeyboardButton("🔄 Try Again", callback_data="retry_ai_chat")]]
        await update.effective_message.reply_text(
            "❌ Error processing your request. Please try again.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )


async def ask_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start AI chat - /ask <question>"""
    if not context.args:
        await update.message.reply_text(
            "🤖 AI Shopping Assistant\n\n"
            "Examples:\n"
            "/ask I need a blue shirt\n"
            "/ask What's your best summer dress?\n"
            "/ask Show me affordable pants\n\n"
            "Or just chat naturally - I'll help find what you need!"
        )
        return

    question = " ".join(context.args)
    original_text = update.message.text
    update.message.text = question
    try:
        await ai_chat_handler(update, context)
    finally:
        update.message.text = original_text


async def ask_ai_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback when user clicks 'Ask AI Agent' from the main menu."""
    query = update.callback_query
    await query.answer()

    # Clear other states
    context.user_data.pop("waiting_for_search", None)
    context.user_data.pop("waiting_for_order_lookup", None)

    text = (
        "🤖 *Ask AI Agent*\n\n"
        "Ask me anything about our products, categories, or recommendations!\n\n"
        "Examples:\n"
        "• _What blue shirts do you have?_\n"
        "• _Recommend some trendy clothes._\n"
        "• _Do you have jeans in stock?_\n\n"
        "Just type your question below 👇"
    )
    keyboard = [[InlineKeyboardButton("← Back", callback_data="start_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def reset_ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reset the AI chat history for the user."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    if user_id in user_agents:
        user_agents[user_id].conversation_history = []

    text = (
        "🗑️ *AI Chat Reset Successful!*\n\n"
        "Your previous conversation history has been cleared. Ask me a new question!"
    )
    keyboard = [[InlineKeyboardButton("← Back to Menu", callback_data="start_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


# ==================== Telegram Handlers ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - main menu."""
    if update.effective_user:
        upsert_user(update.effective_user.id, update.effective_user.first_name)
    
    first_name = update.effective_user.first_name if update.effective_user else None
    cart_items = context.user_data.get("cart", [])
    cart_count = sum(item.get("quantity", 1) for item in cart_items)
    
    text, reply_markup = main_menu(first_name, cart_count)

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


async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    first_name = update.effective_user.first_name if update.effective_user else None
    cart_items = context.user_data.get("cart", [])
    cart_count = sum(item.get("quantity", 1) for item in cart_items)
    
    text, reply_markup = main_menu(first_name, cart_count)
    await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")


async def browse_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show product categories (handles both callback query and direct command)."""
    query = update.callback_query
    if query:
        await query.answer()

    try:
        categories = await get_categories()

        if isinstance(categories, dict) and "error" in categories:
            error_text = f"❌ Error: {md(categories['error'])}"
            if query:
                await query.edit_message_text(text=error_text, parse_mode="Markdown")
            else:
                await update.effective_message.reply_text(text=error_text, parse_mode="Markdown")
            return

        if not isinstance(categories, list) or not categories:
            no_cat_text = "No categories found."
            if query:
                await query.edit_message_text(text=no_cat_text)
            else:
                await update.effective_message.reply_text(text=no_cat_text)
            return

        # Organize categories hierarchically
        category_ids = {c["id"] for c in categories}
        # A category's parent is considered "missing/root" if parent ID is 0 or parent ID is not in our category list.
        roots = [c for c in categories if c.get("parent", 0) == 0 or c.get("parent") not in category_ids]

        # Sort roots by menu_order then name
        roots.sort(key=lambda x: (x.get("menu_order", 0), x.get("name", "").lower()))

        categories_by_parent = {}
        for c in categories:
            p_id = c.get("parent", 0)
            categories_by_parent.setdefault(p_id, []).append(c)

        for p_id in categories_by_parent:
            categories_by_parent[p_id].sort(key=lambda x: (x.get("menu_order", 0), x.get("name", "").lower()))

        ordered_categories = []
        visited = set()

        def add_children(cat, depth=0):
            if cat["id"] in visited:
                return
            visited.add(cat["id"])
            ordered_categories.append((cat, depth))
            p_id = cat["id"]
            if p_id in categories_by_parent:
                for child in categories_by_parent[p_id]:
                    add_children(child, depth + 1)

        for root in roots:
            add_children(root, 0)

        text = "👔 *Select a Category*\n\n"
        keyboard = []

        for category, depth in ordered_categories:
            name = category.get("name", "Category")
            count = category.get("count", 0)
            indent = "  " * depth + "↳ " if depth > 0 else ""

            # Truncate to ensure the button label looks neat
            display_name = f"{indent}{name}"
            if len(display_name) > 28:
                display_name = display_name[:25] + "..."

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"{display_name} ({count})",
                        callback_data=f"cat_{category['id']}_1",
                    )
                ]
            )

        keyboard.append([InlineKeyboardButton("🆕 All Latest Products", callback_data="products_all_1")])
        keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        if query:
            await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error("Error in browse_products: %s", str(e))
        error_text = f"❌ Error: {md(e)}"
        if query:
            await query.edit_message_text(text=error_text, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(text=error_text, parse_mode="Markdown")


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show a page of products, optionally filtered by category."""
    query = update.callback_query
    await query.answer()

    parts = query.data.split("_")
    is_category = parts[0] == "cat"
    category_id = parts[1] if is_category else None
    page = int(parts[2])
    limit = 8

    try:
        category_slug = None
        if is_category:
            products = await get_products_by_category(category_id, page=page, limit=limit)
            category = await get_category_by_id(category_id)
            if isinstance(category, dict) and "error" not in category:
                category_name = category.get("name", "Category")
                category_slug = category.get("slug")
                title = f"📦 *{md(category_name)} Products*"
            else:
                title = "📦 *Category Products*"
            back_callback = "browse"
            page_prefix = f"cat_{category_id}"
        else:
            products = await get_products_page(page=page, limit=limit)
            title = "🆕 *Latest Products*"
            back_callback = "start_menu"
            page_prefix = "products_all"

        if isinstance(products, dict) and "error" in products:
            await query.edit_message_text(text=f"❌ Error: {md(products['error'])}", parse_mode="Markdown")
            return

        if not isinstance(products, list) or not products:
            keyboard = [[InlineKeyboardButton("← Back", callback_data=back_callback)]]
            await query.edit_message_text(
                text="No products found.",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
            return

        text = f"{title}\nPage {page}\n\n"
        keyboard = []

        for product in products:
            text += f"*{md(product.get('name', 'Product'))}*\n"
            text += f"💰 ৳{md(product.get('price', ''))}\n"
            text += f"{stock_display(product)}\n\n"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"View {product_button_name(product.get('name'))}",
                        callback_data=f"product_{product['id']}",
                    )
                ]
            )

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("← Prev", callback_data=f"{page_prefix}_{page - 1}"))
        if len(products) == limit:
            nav_row.append(InlineKeyboardButton("Next →", callback_data=f"{page_prefix}_{page + 1}"))
        if nav_row:
            keyboard.append(nav_row)

        if is_category and category_slug:
            category_url = f"{WOOCOMMERCE_URL}/product-category/{category_slug}/"
            keyboard.append([InlineKeyboardButton("🌐 View Category on Website", url=category_url)])

        keyboard.append([InlineKeyboardButton("← Back", callback_data=back_callback)])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error("Error in show_products: %s", str(e))
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

        text = f"*{md(product.get('name', 'Product'))}*\n\n"
        text += f"💰 Price: ৳{md(product.get('price', ''))}\n"
        text += f"{stock_display(product)}\n\n"

        # Strip table before rendering text to avoid clutter
        desc_clean = strip_html_excluding_table(product.get("description", "No description"))
        if desc_clean:
            text += f"📝 {md(desc_clean[:300])}"
            if len(desc_clean) > 300:
                text += "..."
            text += "\n\n"

        keyboard = []
        permalink = product.get("permalink") if isinstance(product, dict) else None
        if permalink:
            keyboard.append([InlineKeyboardButton("🌐 View on Website", url=permalink)])

        # Check if size chart is available
        size_chart = extract_and_format_size_chart(product)
        if size_chart:
            keyboard.append([InlineKeyboardButton("📏 Size Chart", callback_data=f"size_chart_{product_id}")])

        keyboard.append([InlineKeyboardButton("🛒 Add to Cart", callback_data=f"add_cart_{product_id}")])
        keyboard.append([InlineKeyboardButton("← Back", callback_data="browse")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

        images = product.get("images")
        if isinstance(images, list) and len(images) > 0:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=images[0]["src"],
                    caption=f"_{md(product.get('name', 'Product'))}_",
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.warning("Could not send product image: %s", str(e))

    except Exception as e:
        logger.error("Error in view_product: %s", str(e))
        await query.edit_message_text(text=f"❌ Error: {md(e)}", parse_mode="Markdown")


async def show_size_chart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback when user clicks 'Size Chart' button on a product."""
    query = update.callback_query
    product_id = query.data.removeprefix("size_chart_")
    await query.answer()

    try:
        product = await get_product_by_id(product_id)
        if isinstance(product, dict) and "error" in product:
            await query.edit_message_text(text=f"❌ Error: {md(product['error'])}", parse_mode="Markdown")
            return

        size_chart = extract_and_format_size_chart(product)
        if not size_chart:
            await query.edit_message_text(
                text="❌ No size chart available for this product.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back to Product", callback_data=f"product_{product_id}")]])
            )
            return

        text = f"📏 *Size Chart for {md(product.get('name', 'Product'))}*\n\n{size_chart}"
        keyboard = [[InlineKeyboardButton("← Back to Product", callback_data=f"product_{product_id}")]]
        reply_markup = InlineKeyboardMarkup(keyboard)

        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error("Error in show_size_chart_handler: %s", str(e))
        await query.edit_message_text(text=f"❌ Error: {md(e)}", parse_mode="Markdown")


async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search command (via button click or direct /search command)."""
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

    # Check if triggered by command '/search <term>'
    message_text = update.message.text
    if message_text.startswith("/"):
        parts = message_text.split(maxsplit=1)
        if len(parts) > 1:
            search_term = parts[1].strip()
        else:
            await update.message.reply_text(
                text="🔍 *Search Products*\n\nType a product name, for example: shirt, jeans, dress.",
                parse_mode="Markdown",
            )
            context.user_data["waiting_for_search"] = True
            context.user_data.pop("waiting_for_order_lookup", None)
            return
    else:
        search_term = message_text.strip()
        context.user_data["waiting_for_search"] = False

    try:
        products = await search_products(search_term)

        if isinstance(products, dict) and "error" in products:
            await update.message.reply_text(f"❌ Error: {md(products['error'])}", parse_mode="Markdown")
            return

        if not products:
            await update.message.reply_text(f"❌ No products found for '{md(search_term)}'", parse_mode="Markdown")
            return

        text = f"🔍 *Search Results for '{md(search_term)}'*\n\n"
        keyboard = []

        for product in products[:5]:
            text += f"*{md(product.get('name', 'Product'))}*\n"
            text += f"💰 ৳{md(product.get('price', ''))}\n"
            text += f"{stock_display(product)}\n\n"

            keyboard.append(
                [
                    InlineKeyboardButton(
                        f"View {product_button_name(product.get('name'))}",
                        callback_data=f"product_{product['id']}",
                    )
                ]
            )

        import urllib.parse
        search_url = f"{WOOCOMMERCE_URL}/?s={urllib.parse.quote(search_term)}&post_type=product"
        keyboard.append([InlineKeyboardButton("🌐 View Search on Website", url=search_url)])

        keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

    except Exception as e:
        logger.error("Error in search_handler: %s", str(e))
        await update.message.reply_text(f"❌ Error: {md(e)}", parse_mode="Markdown")


async def my_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user for a single order lookup."""
    query = update.callback_query
    text = (
        "📦 *View Your Order*\n\n"
        "Enter your order number and billing email or phone in one message:\n"
        "`1234 customer@example.com`\n"
        "or\n"
        "`1234 01700000000`"
    )

    context.user_data["waiting_for_order_lookup"] = True
    context.user_data.pop("waiting_for_search", None)

    if query:
        await query.answer()
        await query.edit_message_text(
            text=text,
            parse_mode="Markdown",
        )
    else:
        await update.effective_message.reply_text(
            text=text,
            parse_mode="Markdown",
        )


def parse_order_lookup(user_text):
    if not user_text:
        return None, None
    match = re.match(r"^\s*#?(\d+)\s+([^\s]+)\s*$", user_text)
    if not match:
        return None, None
    return match.group(1), match.group(2)


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input for search or order lookup."""
    user_text = update.message.text
    if not user_text:
        return

    if context.user_data.get("waiting_for_search"):
        await search_handler(update, context)
        return

    if context.user_data.get("waiting_for_order_lookup"):
        order_id, contact_info = parse_order_lookup(user_text)
        if not order_id:
            await update.message.reply_text(
                "Please send the order number and billing email or phone like this:\n"
                "`1234 customer@example.com`\n"
                "or\n"
                "`1234 01700000000`",
                parse_mode="Markdown",
            )
            return

        context.user_data["waiting_for_order_lookup"] = False

        try:
            order = await get_order_by_id(order_id)

            if isinstance(order, dict) and "error" in order:
                await update.message.reply_text("❌ No matching order found.")
                return

            billing_email = order.get("billing", {}).get("email", "").strip().lower()
            billing_phone = re.sub(r"[^\d\+]", "", order.get("billing", {}).get("phone", ""))

            clean_input = contact_info.strip().lower()
            clean_input_phone = re.sub(r"[^\d\+]", "", clean_input)

            is_match = False
            if billing_email and billing_email == clean_input:
                is_match = True
            elif billing_phone and clean_input_phone and len(clean_input_phone) >= 10:
                if billing_phone.endswith(clean_input_phone) or clean_input_phone.endswith(billing_phone):
                    is_match = True

            if not is_match:
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
        return

    # Route normal text messages to conversational AI
    await ai_chat_handler(update, context)


async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to main menu."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await start(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command - displays FAQ options."""
    context.user_data.clear()

    text = (
        "🤖 *DEEN Commerce Customer Care*\n\n"
        "Welcome! How can we assist you today? Please choose a topic below:\n\n"
        "💳 *Payment*: bKash, Bank, or Cash on Delivery.\n"
        "🚚 *Shipping*: Dhaka: 24-48h (৳50), Outside Dhaka: 3-5 days (৳90).\n"
        "🔄 *Exchange*: Exchange within 7 days for sizing issues.\n"
        "📞 *Live Agent*: Direct support contact info."
    )
    keyboard = [
        [InlineKeyboardButton("💳 Payment Info", callback_data="faq_payment")],
        [InlineKeyboardButton("🚚 Delivery & Shipping", callback_data="faq_shipping")],
        [InlineKeyboardButton("🔄 Return & Exchange", callback_data="faq_returns")],
        [InlineKeyboardButton("📞 Contact Support", callback_data="faq_support")],
        [InlineKeyboardButton("← Main Menu", callback_data="start_menu")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            text=text,
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )
        return

    await update.effective_message.reply_text(
        text,
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )


async def faq_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle FAQ sub-menus."""
    query = update.callback_query
    await query.answer()

    faq_type = query.data
    keyboard = [[InlineKeyboardButton("← Back to Support", callback_data="help_menu")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if faq_type == "faq_payment":
        text = (
            "💳 *Payment Methods*\n\n"
            "1. *Cash on Delivery (COD)*:\n"
            "   Available all over Bangladesh. Pay only after receiving the product.\n\n"
            "2. *Mobile Financial Services (MFS)*:\n"
            "   Prepay securely using *bKash* or *Nagad* during checkout or via direct transfer.\n\n"
            "⚠️ *Important Note*: We do not charge any extra fees for MFS payments."
        )
    elif faq_type == "faq_shipping":
        text = (
            "🚚 *Delivery Details*\n\n"
            "• *Inside Dhaka*: 24 to 48 Hours. Delivery Fee: *৳50*.\n"
            "• *Outside Dhaka*: 3 to 5 Days (via Pathao). Delivery Fee: *৳90*.\n\n"
            "📦 You will receive a tracking link via SMS once your parcel is dispatched."
        )
    elif faq_type == "faq_returns":
        text = (
            "🔄 *Return & Exchange Policy*\n\n"
            "• You can request an exchange or return within *7 days* of receiving your package.\n"
            "• The item must be unused, unwashed, and with original tags intact.\n"
            "• Sizing exchanges are free (only delivery charge applies for sending back)."
        )
    elif faq_type == "faq_support":
        address = await get_store_address()
        text = (
            "📞 *Contact DEEN Commerce Support*\n\n"
            "Need to talk to a human agent? We are here to help!\n\n"
            "💬 *Messenger*: [Click here to message us](https://m.me/deencommerce)\n"
            "🟢 *WhatsApp*: `+8801752700500`\n"
            "📞 *Hotline*: `+8809612345678` (10:00 AM - 8:00 PM)\n"
            "✉️ *Email*: `support@deencommerce.com`\n\n"
            f"📍 *Store Address*: {md(address)}"
        )
    else:
        text = "Topic not found."

    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    product_id = query.data.removeprefix("add_cart_")
    
    product = await get_product_by_id(product_id)
    if isinstance(product, dict) and "error" in product:
        await query.answer("❌ Failed to add product. It may not exist.", show_alert=True)
        return

    # Check if it's a variable product
    if product.get("type") == "variable":
        # Guide user to website for variation selection
        permalink = product.get("permalink")
        await query.answer("This product has sizes/options. Please select them on our website.", show_alert=True)
        if permalink:
            keyboard = [[InlineKeyboardButton("🌐 Select Size & Buy on Website", url=permalink)]]
            await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if "cart" not in context.user_data:
        context.user_data["cart"] = []
        
    cart = context.user_data["cart"]
    found = False
    for item in cart:
        if str(item["id"]) == product_id:
            item["quantity"] += 1
            found = True
            break
            
    if not found:
        name = product.get("name", f"Product #{product_id}")
        cart.append({
            "id": product_id,
            "name": name,
            "quantity": 1
        })
        
    await query.answer(f"Added to cart! 🛒")

async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    cart = context.user_data.get("cart", [])
    if not cart:
        await query.edit_message_text(
            "🛒 *Your Cart is Empty*",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("← Back", callback_data="start_menu")]]),
            parse_mode="Markdown"
        )
        return
        
    text = "🛒 *Your Shopping Cart*\n\n"
    for idx, item in enumerate(cart, 1):
        text += f"{idx}. {md(item['name'])} (x{item['quantity']})\n"
        
    keyboard = [
        [InlineKeyboardButton("💳 Checkout", callback_data="checkout")],
        [InlineKeyboardButton("🗑️ Empty Cart", callback_data="empty_cart")],
        [InlineKeyboardButton("← Back to Menu", callback_data="start_menu")]
    ]
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

async def empty_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    context.user_data["cart"] = []
    await query.answer("Cart empty!")
    await view_cart(update, context)

async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    cart = context.user_data.get("cart", [])
    if not cart:
        await query.answer("Cart is empty!")
        return
        
    await query.answer()
    
    # WooCommerce standard add-to-cart URL only supports one product reliably via query params.
    # For multiple products, we should ideally use a custom endpoint or just link to the cart page
    # if we can't reliably sync the session.
    # However, since we want to "add" them, we'll use the first item and advise user.
    
    if len(cart) == 1:
        item = cart[0]
        checkout_url = f"{WOOCOMMERCE_URL}/checkout/?add-to-cart={item['id']}&quantity={item['quantity']}"
    else:
        # For multiple items, WooCommerce doesn't natively support comma-separated IDs in the standard 'add-to-cart' param.
        # We will link to the cart page and advise.
        checkout_url = f"{WOOCOMMERCE_URL}/cart/"

    text = "💳 *Ready to Checkout!*\n\n"
    if len(cart) > 1:
        text += "Since you have multiple items, please add them on our website for the best experience.\n\n"
    else:
        text += "Click the link below to securely complete your order on DeenCommerce.\n\n"
    
    text += f"[Go to DeenCommerce]({checkout_url})"
    
    keyboard = [
        [InlineKeyboardButton("🌐 Proceed to Website", url=checkout_url)],
        [InlineKeyboardButton("← Back to Cart", callback_data="view_cart")]
    ]
    
    await query.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )

# ==================== Register Handlers ====================

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("browse", browse_products))
application.add_handler(CommandHandler("search", search_handler))
application.add_handler(CommandHandler("my_order", my_order_handler))
application.add_handler(CommandHandler("ask", ask_command))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^browse$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^cat_\d+_\d+$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^products_all_\d+$"))
application.add_handler(CallbackQueryHandler(search_handler, pattern="^search$"))
application.add_handler(CallbackQueryHandler(my_order_handler, pattern="^my_order$"))
application.add_handler(CallbackQueryHandler(ask_ai_callback_handler, pattern="^ask_ai$"))
application.add_handler(CallbackQueryHandler(ai_chat_handler, pattern="^retry_ai_chat$"))
application.add_handler(CallbackQueryHandler(reset_ai_chat_handler, pattern="^reset_ai_chat$"))
application.add_handler(CallbackQueryHandler(view_product, pattern="^product_"))
application.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add_cart_"))
application.add_handler(CallbackQueryHandler(view_cart, pattern="^view_cart$"))
application.add_handler(CallbackQueryHandler(empty_cart, pattern="^empty_cart$"))
application.add_handler(CallbackQueryHandler(checkout, pattern="^checkout$"))
application.add_handler(CallbackQueryHandler(show_size_chart_handler, pattern="^size_chart_"))
application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^start_menu$"))
application.add_handler(CallbackQueryHandler(help_command, pattern="^help_menu$"))
application.add_handler(CallbackQueryHandler(faq_handler, pattern="^faq_"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))


# ==================== FastAPI Routes ====================


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
