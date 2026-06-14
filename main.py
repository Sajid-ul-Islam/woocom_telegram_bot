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
import schedule
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse
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
from telegram.error import BadRequest

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
    get_store_address,
    format_price_display
)
from rag_agent import RAGAgent, PROVIDER_HEALTH
from db import upsert_user, set_subscription, track_command
from product_embeddings import VectorStore
from woocommerce_knowledge_base import setup_knowledge_base

global_vector_store = None

async def update_knowledge_base_daily():
    logger.info("Updating knowledge base from WooCommerce daily...")
    try:
        kb, docs = await setup_knowledge_base(WOOCOMMERCE_URL, WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET, "woo_knowledge_base.json")
        global global_vector_store
        if global_vector_store:
            await asyncio.to_thread(global_vector_store.create_from_knowledge_base, "woo_knowledge_base.json")
        logger.info("Knowledge base and embeddings updated successfully.")
    except Exception as e:
        logger.error("Error updating knowledge base: %s", str(e))

async def scheduler_task():
    while True:
        schedule.run_pending()
        await asyncio.sleep(60)

@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Lifecycle events for FastAPI application."""
    logger.info("Initializing Telegram application...")
    try:
        # Check Supabase status
        from db import supabase
        if supabase is None:
            logger.warning(
                "⚠️ Supabase client is NOT initialized! "
                "Conversation history and user persistence will be disabled. "
                "Please configure SUPABASE_URL and SUPABASE_KEY in your environment."
            )
        else:
            logger.info("Supabase validation succeeded: persistence is enabled.")

        # Initialize global HTTP client
        utils.http_client = httpx.AsyncClient(
            auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
            timeout=10.0
        )

        # Initialize Vector Store
        global global_vector_store
        global_vector_store = VectorStore()
        try:
            if not os.path.exists("woo_knowledge_base.json"):
                logger.info("Initial run: Fetching WooCommerce data and generating new embeddings for Supabase...")
                await setup_knowledge_base(WOOCOMMERCE_URL, WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET, "woo_knowledge_base.json")
                await asyncio.to_thread(global_vector_store.create_from_knowledge_base, "woo_knowledge_base.json")
            else:
                logger.info("VectorStore initialized with Supabase integration.")
        except Exception as e:
            logger.error("Failed to initialize VectorStore: %s", str(e))
            
        # Schedule daily updates at 2 AM
        schedule.every().day.at("02:00").do(lambda: asyncio.create_task(update_knowledge_base_daily()))
        asyncio.create_task(scheduler_task())

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
            "offers": "Discount Offers",
            "search": "Search products",
            "my_order": "View order status",
            "ask": "Ask the AI Shopping Assistant",
            "unsubscribe": "Opt out of promotional messages",
            "subscribe": "Opt in to promotional messages"
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
        [InlineKeyboardButton("👔 Categories", callback_data="browse"),
         InlineKeyboardButton("🎁 Offers", callback_data="offers")],
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
    products = await woo_get(
        "products",
        params={
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": "publish",
            "stock_status": "instock",
        },
    )
    if isinstance(products, list):
        products = [p for p in products if p.get("status") == "publish"]
    return products


async def _has_published_products(category_id) -> bool:
    """Return True if a category has at least one published product."""
    result = await woo_get(
        "products",
        params={"category": category_id, "status": "publish", "per_page": 1},
    )
    return isinstance(result, list) and len(result) > 0


async def get_categories(limit=100):
    """Fetch product categories that have published products (with caching)."""
    cache_key = f"categories_{limit}"
    cached = categories_cache.get(cache_key)
    if cached is not None:
        logger.info("Using cached categories list.")
        return cached

    categories = await woo_get(
        "products/categories",
        params={"per_page": limit, "orderby": "name", "order": "asc", "hide_empty": True},
    )
    if not isinstance(categories, list):
        return categories

    # WooCommerce term_count includes draft/private products when accessed via admin API.
    # Validate each category in parallel to ensure it has at least 1 published product.
    import asyncio
    candidates = [c for c in categories if c.get("count", 0) > 0]
    checks = await asyncio.gather(*[_has_published_products(c["id"]) for c in candidates])
    valid_categories = [c for c, ok in zip(candidates, checks) if ok]

    if valid_categories:
        categories_cache.set(cache_key, valid_categories)
    return valid_categories


async def get_products_by_category(category_id, page=1, limit=8):
    """Fetch published products from a category (all stock statuses shown with badge)."""
    products = await woo_get(
        "products",
        params={
            "category": category_id,
            "page": page,
            "per_page": limit,
            "orderby": "date",
            "order": "desc",
            "status": "publish",  # Only published — never show drafts/private
        },
    )
    if isinstance(products, list):
        products = [p for p in products if p.get("status") == "publish"]
    return products


async def get_products_page(page=1, limit=8):
    """Fetch a page of latest products."""
    products = await woo_get(
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
    if isinstance(products, list):
        products = [p for p in products if p.get("status") == "publish"]
    return products


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
    processed_keyword = await preprocess_search_query(keyword)
    logger.info("Searching products. Original: %s -> Processed: %s", keyword, processed_keyword)
    products = await woo_get(
        "products",
        params={
            "search": processed_keyword,
            "per_page": 10,
            "status": "publish",
            "stock_status": "instock",
        },
    )
    if isinstance(products, list):
        products = [p for p in products if p.get("status") == "publish"]
    return products


async def get_order_by_id(order_id):
    """Fetch a single order by ID."""
    return await woo_get(f"orders/{order_id}")




# ── Usage Tracking ────────────────────────────────────────────────────────────
def _track(user_id: int | None, action: str):
    """Fire-and-forget usage tracker. Never raises."""
    if user_id:
        try:
            track_command(user_id, action)
        except Exception:
            pass


# Store agents per user (so each user has their own conversation)
# Includes last-access timestamp for TTL-based memory cleanup
user_agents = {}          # { user_id: RAGAgent }
user_agents_last_used = {}  # { user_id: float (epoch) }

USER_AGENT_TTL = 3600  # Evict agents unused for 1 hour

def _evict_stale_agents():
    """Remove in-memory agents that haven't been used recently."""
    now = time.time()
    stale = [uid for uid, ts in user_agents_last_used.items() if now - ts > USER_AGENT_TTL]
    for uid in stale:
        user_agents.pop(uid, None)
        user_agents_last_used.pop(uid, None)
    if stale:
        logger.info("Evicted %d stale user agents from memory.", len(stale))


async def ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, direct_message: str = None):
    """Handle conversational AI queries"""
    user_id = update.effective_user.id
    
    if direct_message:
        user_message = direct_message
        context.user_data["last_ai_query"] = user_message
    elif update.callback_query and update.callback_query.data == "retry_ai_chat":
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

    # Evict stale agents periodically (runs every message, cheap O(n) scan)
    _evict_stale_agents()

    # Create agent for user if doesn't exist
    if user_id not in user_agents:
        user_agents[user_id] = RAGAgent(
            woocommerce_url=WOOCOMMERCE_URL,
            woocommerce_key=WOOCOMMERCE_KEY,
            woocommerce_secret=WOOCOMMERCE_SECRET,
            user_id=user_id
        )
    user_agents_last_used[user_id] = time.time()

    # Show typing indicator
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing"
    )

    try:
        # Retrieve the user's cart
        cart = context.user_data.get("cart", [])

        # Process message with RAG agent
        response, extra_buttons, extra_images = await user_agents[user_id].process_message(user_message, user_id, cart=cart)
        _track(user_id, "ai_chat")

        # Send any queued images first
        if extra_images:
            for img_url in extra_images:
                try:
                    await context.bot.send_photo(
                        chat_id=update.effective_chat.id,
                        photo=img_url
                    )
                except Exception as e:
                    logger.warning("Could not send AI photo %s: %s", img_url, str(e))

        # Attach continuous chat options and extra buttons to the final response
        keyboard = []
        if extra_buttons:
            for btn in extra_buttons:
                if "url" in btn:
                    keyboard.append([InlineKeyboardButton(btn["text"], url=btn["url"])])
                elif "callback_data" in btn:
                    keyboard.append([InlineKeyboardButton(btn["text"], callback_data=btn["callback_data"])])

        keyboard.append([
            InlineKeyboardButton("🗑️ Reset Chat", callback_data="reset_ai_chat"),
            InlineKeyboardButton("← Back to Menu", callback_data="start_menu")
        ])
        reply_markup = InlineKeyboardMarkup(keyboard)

        # Split long responses (Telegram has 4096 char limit)
        if len(response) > 4000:
            for i in range(0, len(response), 4000):
                chunk = response[i:i+4000]
                is_last = (i + 4000 >= len(response))
                markup = reply_markup if is_last else None
                try:
                    await update.effective_message.reply_text(
                        chunk,
                        reply_markup=markup,
                        parse_mode="Markdown"
                    )
                except BadRequest:
                    await update.effective_message.reply_text(
                        chunk,
                        reply_markup=markup
                    )
        else:
            try:
                await update.effective_message.reply_text(
                    response,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            except BadRequest:
                await update.effective_message.reply_text(
                    response,
                    reply_markup=reply_markup
                )

    except Exception as e:
        logger.error("AI chat error: %s", str(e))
        text = (
            "🤖 *Oops! The AI Assistant is temporarily unavailable.*\n\n"
            "Don't worry, you can still browse or query our store manually! Please choose one of the options below or use these commands:\n"
            "• /browse - Browse clothing categories\n"
            "• /search - Search for specific products\n"
            "• /my\\_order - Securely track your order\n"
            "• /help - Support FAQs & contact info"
        )
        keyboard = [
            [InlineKeyboardButton("👔 Browse Categories", callback_data="browse")],
            [InlineKeyboardButton("🔍 Search Products", callback_data="search")],
            [InlineKeyboardButton("📦 My Order", callback_data="my_order")],
            [InlineKeyboardButton("📞 FAQ & Support", callback_data="help_menu")],
            [
                InlineKeyboardButton("🔄 Try Again", callback_data="retry_ai_chat"),
                InlineKeyboardButton("← Back to Menu", callback_data="start_menu")
            ]
        ]
        await update.effective_message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
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
    await ai_chat_handler(update, context, direct_message=question)


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
    # Also clear persisted history in Supabase so it doesn't reload on next message
    from db import update_user_history
    update_user_history(user_id, [])

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
        _track(update.effective_user.id, "start")
    
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

    # Clear pending states but NOT the cart
    context.user_data.pop("waiting_for_search", None)
    context.user_data.pop("waiting_for_order_lookup", None)

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
    _track(update.effective_user.id if update.effective_user else None, "browse")

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

        # Hide categories with 0 products
        categories = [c for c in categories if c.get("count", 0) > 0]

        def is_offer(cat):
            name = cat.get("name", "").lower()
            return "%" in name or "offer" in name or "discount" in name or "sale" in name

        # Determine mode from callback data or command
        mode = "browse"
        if query and query.data == "offers":
            mode = "offers"
        elif update.message and update.message.text and "/offers" in update.message.text:
            mode = "offers"

        if mode == "offers":
            target_categories = [c for c in categories if is_offer(c)]
            if not target_categories:
                msg = "No special offers available right now."
                if query:
                    await query.edit_message_text(text=msg)
                else:
                    await update.effective_message.reply_text(text=msg)
                return
        else:
            target_categories = [c for c in categories if not is_offer(c)]

        # Organize categories hierarchically
        category_ids = {c["id"] for c in target_categories}
        roots = [c for c in target_categories if c.get("parent", 0) == 0 or c.get("parent") not in category_ids]

        roots.sort(key=lambda x: (x.get("menu_order", 0), x.get("name", "").lower()))

        categories_by_parent = {}
        for c in target_categories:
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

        text = ""
        keyboard = []

        if mode == "offers":
            text += "🎁 *Discount Offers*\n\n"
        else:
            text += "👔 *Select a Category*\n\n"

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
            empty_msg = (
                "⚠️ *No published products in this category.*\n\n"
                "All items here may be drafts or temporarily unavailable. "
                "Browse other categories or search for products."
            )
            # Invalidate categories cache so next browse re-validates
            categories_cache.clear()
            await query.edit_message_text(
                text=empty_msg,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
            )
            return

        text = f"{title}\nPage {page}\n\n"
        keyboard = []

        for product in products:
            text += f"*{md(product.get('name', 'Product'))}*\n"
            text += f"💰 {format_price_display(product)}\n"
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
    _track(update.effective_user.id if update.effective_user else None, "product_view")

    try:
        product = await get_product_by_id(product_id)

        if isinstance(product, dict) and "error" in product:
            await query.edit_message_text(text=f"❌ Error: {md(product['error'])}", parse_mode="Markdown")
            return

        if product.get("status") != "publish":
            await query.edit_message_text(text="⚠️ *This product is not available.*", parse_mode="Markdown")
            return

        text = f"*{md(product.get('name', 'Product'))}*\n\n"
        text += f"💰 Price: {format_price_display(product)}\n"
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
    _track(update.effective_user.id if update.effective_user else None, "size_chart")

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
    _track(update.effective_user.id if update.effective_user else None, "search")

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
            text += f"💰 {format_price_display(product)}\n"
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


from telegram import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle shared contact for phone number."""
    contact = update.effective_message.contact
    if contact:
        user_id = update.effective_user.id
        phone = contact.phone_number
        from db import upsert_user
        upsert_user(user_id, update.effective_user.first_name, phone_number=phone)
        
        await update.message.reply_text("✅ Phone number saved!", reply_markup=ReplyKeyboardRemove())
        await _fetch_and_show_orders_by_phone(update, context, phone)

async def _render_and_send_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order: dict, order_id=None):
    if not order_id:
        order_id = order.get("id")
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

    text = f"{status_emoji} *Order #{md(str(order_id))}*\n\n"
    text += f"Status: {md(status)}\n"
    text += f"Total: ৳{md(str(total))}\n"
    text += f"Date: {md(date_created)}\n\n"

    items = order.get("line_items", [])
    if items:
        text += "Items:\n"
        for item in items:
            text += f"  • {md(item.get('name', 'Item'))} (qty: {md(str(item.get('quantity', ''))})\n"

    consignment_id, tracking_url = get_tracking_info(order)
    if consignment_id and tracking_url:
        text += f"\n🚚 *Courier Tracking*\n"
        text += f"Tracking ID: `{md(consignment_id)}`\n"
        if "pathao" in tracking_url.lower():
            pathao_status = await get_pathao_tracking_status(consignment_id)
            if pathao_status:
                text += f"\n{pathao_status}"

    keyboard = []
    if consignment_id and tracking_url:
        keyboard.append([InlineKeyboardButton("🌐 Track Package", url=tracking_url)])

    keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    if update.callback_query:
        await update.callback_query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.effective_message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")

async def _fetch_and_show_orders_by_phone(update: Update, context: ContextTypes.DEFAULT_TYPE, phone_number: str):
    search_phone = phone_number.replace("+", "")
    orders = await woo_get("orders", params={"search": search_phone, "per_page": 5})
    
    if not isinstance(orders, list) or len(orders) == 0:
        msg = "❌ We couldn't find any recent orders associated with your phone number.\nYou can still look up an order manually by typing:\n`1234 customer@example.com`"
        if update.callback_query:
            await update.callback_query.edit_message_text(msg, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(msg, parse_mode="Markdown")
        context.user_data["waiting_for_order_lookup"] = True
        return

    text = "📦 *Your Recent Orders:*\n\n"
    keyboard = []
    for o in orders:
        o_id = o.get("id")
        status = o.get("status", "unknown").capitalize()
        total = o.get("total")
        currency = o.get("currency", "")
        text += f"• Order #{o_id} - {status} ({total} {currency})\n"
        keyboard.append([InlineKeyboardButton(f"View Order #{o_id}", callback_data=f"view_order_{o_id}")])
    
    keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
    
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.effective_message.reply_text(text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(keyboard))

async def view_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    order_id = query.data.replace("view_order_", "")
    
    user_id = update.effective_user.id
    from db import supabase
    phone_number = None
    if supabase:
        try:
            resp = supabase.table("users").select("phone_number").eq("id", user_id).execute()
            if resp.data and resp.data[0].get("phone_number"):
                phone_number = resp.data[0]["phone_number"]
        except: pass
        
    if not phone_number:
        await query.edit_message_text("❌ Unauthorized. Phone number not verified.")
        return
        
    order = await get_order_by_id(order_id)
    if isinstance(order, dict) and "error" in order:
        await query.edit_message_text("❌ Order not found.")
        return
        
    billing_phone = re.sub(r"[^\d\+]", "", order.get("billing", {}).get("phone", ""))
    clean_input_phone = re.sub(r"[^\d\+]", "", phone_number)
    
    is_match = False
    if billing_phone and clean_input_phone and len(clean_input_phone) >= 10:
        if billing_phone.endswith(clean_input_phone) or clean_input_phone.endswith(billing_phone):
            is_match = True
            
    if not is_match:
        await query.edit_message_text("❌ Unauthorized access to this order.")
        return
        
    await _render_and_send_order(update, context, order, order_id)

async def my_order_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Prompt user for a single order lookup or show recent if phone is known."""
    query = update.callback_query
    user_id = update.effective_user.id if update.effective_user else None
    _track(user_id, "my_order")
    
    phone_number = None
    from db import supabase
    if supabase and user_id:
        try:
            resp = supabase.table("users").select("phone_number").eq("id", user_id).execute()
            if resp.data and resp.data[0].get("phone_number"):
                phone_number = resp.data[0]["phone_number"]
        except:
            pass

    if phone_number:
        if query:
            await query.answer()
        await _fetch_and_show_orders_by_phone(update, context, phone_number)
        return

    text = (
        "📦 *View Your Order*\n\n"
        "To easily see all your orders, please share your contact number using the button below.\n\n"
        "Alternatively, enter your order number and billing email or phone in one message:\n"
        "`1234 customer@example.com`\n"
        "or\n"
        "`1234 01700000000`"
    )

    context.user_data["waiting_for_order_lookup"] = True
    context.user_data.pop("waiting_for_search", None)

    reply_markup = ReplyKeyboardMarkup(
        [[KeyboardButton(text="📱 Share Phone Number", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )

    if query:
        await query.answer()
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
        )
    else:
        await update.effective_message.reply_text(
            text=text,
            parse_mode="Markdown",
            reply_markup=reply_markup
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
        
        if not contact_info and user_text.strip().lstrip('#').isdigit():
            order_id = user_text.strip().lstrip('#')
            from db import supabase
            phone_number = None
            if supabase:
                try:
                    resp = supabase.table("users").select("phone_number").eq("id", update.effective_user.id).execute()
                    if resp.data and resp.data[0].get("phone_number"):
                        phone_number = resp.data[0]["phone_number"]
                except:
                    pass
            if phone_number:
                contact_info = phone_number

        if not order_id or not contact_info:
            await update.message.reply_text(
                "Please send the order number and billing email or phone like this:\n"
                "`1234 customer@example.com`\n"
                "or\n"
                "`1234 01700000000`",
                parse_mode="Markdown",
                reply_markup=ReplyKeyboardRemove()
            )
            return

        context.user_data["waiting_for_order_lookup"] = False
        _track(update.effective_user.id if update.effective_user else None, "order_lookup")

        try:
            order = await get_order_by_id(order_id)

            if isinstance(order, dict) and "error" in order:
                await update.message.reply_text("❌ No matching order found.", reply_markup=ReplyKeyboardRemove())
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
                await update.message.reply_text("❌ No matching order found.", reply_markup=ReplyKeyboardRemove())
                return

            await _render_and_send_order(update, context, order, order_id)
            
            msg = await context.bot.send_message(chat_id=update.effective_chat.id, text=".", reply_markup=ReplyKeyboardRemove())
            await msg.delete()

        except Exception as e:
            logger.error("Error fetching order: %s", str(e))
            await update.message.reply_text("❌ Error fetching order.", reply_markup=ReplyKeyboardRemove())
        return

    # Route normal text messages to conversational AI
    # Skip very short messages (e.g. "ok", "k") to avoid unnecessary API calls
    if len(user_text.strip()) < 3:
        return
    await ai_chat_handler(update, context)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Help command - displays FAQ options."""
    _track(update.effective_user.id if update.effective_user else None, "help")
    context.user_data.pop("waiting_for_search", None)
    context.user_data.pop("waiting_for_order_lookup", None)

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
            f"📍 *Our Outlets:*\n{address}"  # address is trusted hardcoded text, no md() escaping needed
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
    _track(update.effective_user.id if update.effective_user else None, "add_cart")
    
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
        
    _track(update.effective_user.id if update.effective_user else None, "checkout")
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


async def unsubscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow user to opt out of broadcasts."""
    _track(update.effective_user.id, "unsubscribe")
    set_subscription(update.effective_user.id, False)
    await update.message.reply_text("🔇 You have been *unsubscribed* from promotional broadcasts.\n\nUse /subscribe to opt back in.", parse_mode="Markdown")


async def subscribe_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow user to opt into broadcasts."""
    _track(update.effective_user.id, "subscribe")
    set_subscription(update.effective_user.id, True)
    await update.message.reply_text("🔊 You are now *subscribed* to promotional broadcasts!", parse_mode="Markdown")

# ==================== Register Handlers ====================

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("browse", browse_products))
application.add_handler(CommandHandler("offers", browse_products))
application.add_handler(CommandHandler("search", search_handler))
application.add_handler(CommandHandler("my_order", my_order_handler))
application.add_handler(CommandHandler("ask", ask_command))
application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
application.add_handler(CommandHandler("subscribe", subscribe_command))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^(browse|offers)$"))
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
application.add_handler(CallbackQueryHandler(start_menu, pattern="^start_menu$"))
application.add_handler(CallbackQueryHandler(help_command, pattern="^help_menu$"))
application.add_handler(CallbackQueryHandler(faq_handler, pattern="^faq_"))
application.add_handler(CallbackQueryHandler(view_order_handler, pattern="^view_order_"))
application.add_handler(MessageHandler(filters.CONTACT, contact_handler))
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


class BroadcastMessage(BaseModel):
    secret: str
    message: str


@app.post("/admin/broadcast")
async def broadcast_offer(payload: BroadcastMessage):
    """Broadcast a message (like a 50% discount) to all known users."""
    if payload.secret != TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    from db import supabase
    if supabase is None:
        raise HTTPException(status_code=500, detail="Supabase not configured. Cannot fetch users.")

    # Fetch all users — try with is_subscribed, fall back if column doesn't exist yet
    try:
        response = supabase.table("users").select("id, is_subscribed").execute()
        users = response.data or []
    except Exception:
        logger.warning("is_subscribed column missing, broadcasting to all users.")
        response = supabase.table("users").select("id").execute()
        users = response.data or []

    success_count = 0
    for user in users:
        # Default to True if is_subscribed is None/missing
        if user.get("is_subscribed") is False:
            continue
        try:
            await application.bot.send_message(chat_id=user["id"], text=payload.message, parse_mode="Markdown")
            success_count += 1
        except Exception as e:
            logger.warning("Failed to send broadcast to %s: %s", user["id"], str(e))

    return {"status": "Broadcast complete", "sent": success_count, "total": len(users)}


@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page():
    """Display the login form for the admin dashboard."""
    return """
    <html>
        <head>
            <title>Admin Login - DeenCommerce</title>
            <style>
                body { font-family: system-ui, sans-serif; background: #f4f4f9; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
                .login-card { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); width: 300px; text-align: center; }
                input[type="password"] { width: 100%; padding: 10px; margin: 15px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
                button { background: #007bff; color: white; border: none; padding: 10px 15px; border-radius: 4px; cursor: pointer; width: 100%; font-weight: bold; }
                button:hover { background: #0056b3; }
            </style>
        </head>
        <body>
            <div class="login-card">
                <h2>🔒 Admin Access</h2>
                <form method="POST" action="/admin/login">
                    <input type="password" name="password" placeholder="Enter Webhook Secret" required>
                    <button type="submit">Login</button>
                </form>
            </div>
        </body>
    </html>
    """


@app.post("/admin/login")
async def admin_login_post(request: Request):
    """Process the login form submission."""
    body = await request.body()
    import urllib.parse
    parsed = urllib.parse.parse_qs(body.decode('utf-8'))
    password = parsed.get("password", [""])[0]
    
    if password == TELEGRAM_WEBHOOK_SECRET:
        response = RedirectResponse(url="/admin/dashboard", status_code=303)
        # Set a session cookie valid for 1 day
        response.set_cookie(key="admin_session", value=TELEGRAM_WEBHOOK_SECRET, httponly=True, max_age=86400)
        return response
        
    return HTMLResponse("<h1>❌ Invalid Password</h1><a href='/admin/login'>Try again</a>", status_code=401)


@app.get("/admin/logout")
async def admin_logout():
    """Log out the admin user."""
    response = RedirectResponse(url="/admin/login", status_code=303)
    response.delete_cookie("admin_session")
    return response


@app.post("/admin/toggle_provider/{provider_name}")
async def toggle_provider(provider_name: str, request: Request):
    """Toggle the active state of an AI provider."""
    session_cookie = request.cookies.get("admin_session")
    if session_cookie != TELEGRAM_WEBHOOK_SECRET:
        return RedirectResponse(url="/admin/login", status_code=303)
        
    if provider_name in PROVIDER_HEALTH:
        PROVIDER_HEALTH[provider_name]["active"] = not PROVIDER_HEALTH[provider_name]["active"]
        
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/admin/add_provider")
async def add_provider(request: Request):
    """Add a custom AI provider."""
    session_cookie = request.cookies.get("admin_session")
    if session_cookie != TELEGRAM_WEBHOOK_SECRET:
        return RedirectResponse(url="/admin/login", status_code=303)
        
    form = await request.form()
    p_name = form.get("name", "").strip().lower().replace(" ", "_")
    base_url = form.get("base_url", "").strip()
    api_key = form.get("api_key", "").strip()
    default_model = form.get("default_model", "").strip()
    
    if p_name and base_url and api_key:
        from db import supabase
        if supabase:
            try:
                supabase.table("ai_providers").upsert({
                    "name": p_name,
                    "base_url": base_url,
                    "api_key": api_key,
                    "default_model": default_model
                }).execute()
            except Exception as e:
                logger.error("Failed to add AI provider to Supabase: %s", str(e))
            
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    """Enhanced admin dashboard with command tracking and activity analytics."""
    session_cookie = request.cookies.get("admin_session")
    if session_cookie != TELEGRAM_WEBHOOK_SECRET:
        return RedirectResponse(url="/admin/login", status_code=303)

    from db import supabase
    if not supabase:
        return "<h1>Supabase not configured</h1>"

    # Fetch users — try with all tracking columns, fall back gracefully
    migration_needed = False
    try:
        response = supabase.table("users").select(
            "id, first_name, is_subscribed, chat_history, command_counts, last_active"
        ).execute()
        users = response.data or []
    except Exception as e:
        err_str = str(e)
        if "42703" in err_str or "command_counts" in err_str or "last_active" in err_str or "is_subscribed" in err_str:
            logger.warning("Tracking columns missing. Run the migration SQL in Supabase.")
            migration_needed = True
            try:
                response = supabase.table("users").select("id, first_name, chat_history").execute()
                users = response.data or []
            except Exception as e2:
                return f"<h1>Error: {html.escape(str(e2))}</h1>"
        else:
            return f"<h1>Error fetching data: {html.escape(str(e))}</h1>"

    # ── Compute stats ────────────────────────────────────────────────────────
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)

    def parse_ts(ts_str):
        if not ts_str:
            return None
        try:
            return datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
        except Exception:
            return None

    def time_ago(ts_str):
        ts = parse_ts(ts_str)
        if not ts:
            return "—"
        diff = now - ts
        if diff.days > 30:
            return f"{diff.days // 30}mo ago"
        if diff.days > 0:
            return f"{diff.days}d ago"
        if diff.seconds > 3600:
            return f"{diff.seconds // 3600}h ago"
        if diff.seconds > 60:
            return f"{diff.seconds // 60}m ago"
        return "Just now"

    total_users = len(users)
    subscribed_users = sum(1 for u in users if u.get("is_subscribed") is not False)
    active_today = sum(1 for u in users if parse_ts(u.get("last_active")) and (now - parse_ts(u.get("last_active"))).days < 1)
    active_week  = sum(1 for u in users if parse_ts(u.get("last_active")) and (now - parse_ts(u.get("last_active"))).days < 7)

    total_ai_queries = 0
    active_ai_users  = 0
    global_cmd_totals = {}

    for u in users:
        history = u.get("chat_history") or []
        q = sum(1 for m in history if m.get("role") == "user")
        if q > 0:
            active_ai_users += 1
        total_ai_queries += q
        counts = u.get("command_counts") or {}
        for cmd, n in counts.items():
            global_cmd_totals[cmd] = global_cmd_totals.get(cmd, 0) + n

    total_commands = sum(global_cmd_totals.values())

    # ── Fetch Vector Store Stats ─────────────────────────────────────────────
    try:
        if global_vector_store and global_vector_store.has_connection:
            prod_resp = supabase.table("product_embeddings").select("id", count="exact").execute()
            page_resp = supabase.table("page_embeddings").select("id", count="exact").execute()
            total_products_indexed = prod_resp.count if hasattr(prod_resp, 'count') else "N/A"
            total_pages_indexed = page_resp.count if hasattr(page_resp, 'count') else "N/A"
        else:
            total_products_indexed = "Not Connected"
            total_pages_indexed = "Not Connected"
    except Exception as e:
        total_products_indexed = f"Error: {e}"
        total_pages_indexed = f"Error: {e}"

    # ── Build command breakdown table rows ───────────────────────────────────
    COMMAND_LABELS = {
        "start": "🏠 /start — Main Menu",
        "browse": "👔 /browse — Browse Categories",
        "search": "🔍 /search — Product Search",
        "my_order": "📦 /my_order — Order Status",
        "ask": "🤖 /ask — AI Query (command)",
        "ai_chat": "💬 AI Chat Message",
        "help": "❓ /help — FAQ & Support",
        "order_lookup": "🔎 Order Lookup (form)",
        "product_view": "🛍️ Product Detail View",
        "add_cart": "🛒 Add to Cart",
        "checkout": "💳 Checkout",
        "size_chart": "📏 Size Chart View",
        "subscribe": "🔔 Subscribe",
        "unsubscribe": "🔕 Unsubscribe",
    }
    cmd_rows = ""
    for cmd, count in sorted(global_cmd_totals.items(), key=lambda x: -x[1]):
        label = COMMAND_LABELS.get(cmd, f"/{cmd}")
        pct = f"{100*count/total_commands:.1f}%" if total_commands else "0%"
        bar_w = int(100 * count / total_commands) if total_commands else 0
        cmd_rows += f"""
        <tr>
            <td>{label}</td>
            <td style="text-align:right;font-weight:bold">{count:,}</td>
            <td style="width:180px">
                <div style="background:#eee;border-radius:4px;height:12px">
                    <div style="background:#4e73df;border-radius:4px;height:12px;width:{bar_w}%"></div>
                </div>
            </td>
            <td style="text-align:right;color:#666">{pct}</td>
        </tr>"""
    if not cmd_rows:
        cmd_rows = "<tr><td colspan='4' style='color:#aaa;text-align:center'>No command data yet. Run migration SQL to enable tracking.</td></tr>"

    # ── Build user table rows ────────────────────────────────────────────────
    user_rows = ""
    for u in users:
        history = u.get("chat_history") or []
        ai_q = sum(1 for m in history if m.get("role") == "user")
        counts = u.get("command_counts") or {}
        total_cmds = sum(counts.values())
        top_cmd = max(counts, key=counts.get) if counts else "—"
        top_label = COMMAND_LABELS.get(top_cmd, top_cmd).split(" ")[0] if counts else "—"
        sub_badge = "<span class='badge-yes'>✅ Yes</span>" if u.get("is_subscribed") is not False else "<span class='badge-no'>❌ No</span>"
        name = html.escape(u.get("first_name") or "N/A")
        la = time_ago(u.get("last_active"))
        logs_btn = f"<a href='/admin/chat_logs/{u.get('id')}' class='btn-view'>💬 Logs</a>" if ai_q > 0 else "<span style='color:#ccc'>—</span>"
        user_rows += f"<tr><td>{u.get('id')}</td><td>{name}</td><td>{la}</td><td>{sub_badge}</td><td style='text-align:center'>{ai_q}</td><td style='text-align:center'>{total_cmds}</td><td style='text-align:center'>{top_label}</td><td>{logs_btn}</td></tr>"

    migration_warn = ""
    if migration_needed:
        migration_warn = """
        <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:8px;padding:14px 20px;margin-bottom:20px">
            ⚠️ <strong>Run this SQL in Supabase to enable command tracking:</strong><br>
            <code style="font-size:0.9em">
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ;<br>
            ALTER TABLE users ADD COLUMN IF NOT EXISTS command_counts JSONB DEFAULT '{}';
            </code>
        </div>"""

    provider_rows_html = ""
    for p_name, p_data in PROVIDER_HEALTH.items():
        status_color = "#27ae60" if p_data["status"] == "ok" else ("#e74c3c" if p_data["status"] == "error" else "#6c757d")
        status_text = p_data["status"].upper()
        active_btn_text = "Deactivate" if p_data["active"] else "Activate"
        active_btn_color = "#e74c3c" if p_data["active"] else "#27ae60"
        
        last_error = html.escape(p_data["last_error"])
        if len(last_error) > 100:
            last_error = last_error[:100] + "..."
            
        provider_rows_html += f'''
        <tr>
            <td style="font-weight:bold;text-transform:capitalize">{p_name}</td>
            <td style="color:{status_color};font-weight:bold">{status_text}</td>
            <td style="color:#e74c3c;font-size:0.85em;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="{html.escape(p_data["last_error"])}">{last_error}</td>
            <td style="text-align:right">
                <form method="POST" action="/admin/toggle_provider/{p_name}" style="margin:0">
                    <button type="submit" style="background:{active_btn_color};color:white;border:none;padding:6px 12px;border-radius:4px;cursor:pointer;font-size:12px;font-weight:bold">
                        {active_btn_text}
                    </button>
                </form>
            </td>
        </tr>
        '''

    return f"""
    <html>
    <head>
        <title>DeenCommerce Admin</title>
        <link rel="stylesheet" href="https://cdn.datatables.net/1.13.6/css/jquery.dataTables.min.css">
        <style>
            *{{box-sizing:border-box;margin:0;padding:0}}
            body{{font-family:'Segoe UI',system-ui,sans-serif;background:#f0f2f5;color:#333}}
            .topbar{{background:#1a1a2e;color:white;padding:16px 30px;display:flex;justify-content:space-between;align-items:center}}
            .topbar h1{{font-size:1.3em;font-weight:600}}
            .logout-btn{{background:#e74c3c;color:white;text-decoration:none;padding:8px 16px;border-radius:6px;font-size:13px;font-weight:600}}
            .content{{padding:28px 32px}}
            .stats-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:14px;margin-bottom:24px}}
            .stat-card{{background:white;border-radius:10px;padding:18px;box-shadow:0 2px 8px rgba(0,0,0,.08);text-align:center}}
            .stat-num{{font-size:2em;font-weight:700;color:#1a1a2e}}
            .stat-lbl{{color:#888;font-size:.8em;margin-top:4px}}
            .card{{background:white;border-radius:10px;padding:22px;box-shadow:0 2px 8px rgba(0,0,0,.08);margin-bottom:22px}}
            .card h2{{font-size:1.1em;margin-bottom:14px;color:#1a1a2e}}
            table{{width:100%;border-collapse:collapse}}
            th,td{{padding:10px 12px;border-bottom:1px solid #f0f0f0;font-size:.88em}}
            th{{background:#f8f9fb;font-weight:600;color:#555}}
            .btn-view{{background:#17a2b8;color:white;text-decoration:none;padding:4px 10px;border-radius:4px;font-size:12px;white-space:nowrap}}
            .badge-yes{{color:#27ae60;font-weight:600}}
            .badge-no{{color:#e74c3c;font-weight:600}}
        </style>
    </head>
    <body>
        <div class="topbar">
            <h1>🤖 DeenCommerce Bot — Admin Dashboard</h1>
            <a href="/admin/logout" class="logout-btn">Logout</a>
        </div>
        <div class="content">
            {migration_warn}
            <div class="stats-grid">
                <div class="stat-card"><div class="stat-num">{total_users}</div><div class="stat-lbl">👥 Total Users</div></div>
                <div class="stat-card"><div class="stat-num">{active_today}</div><div class="stat-lbl">🟢 Active Today</div></div>
                <div class="stat-card"><div class="stat-num">{active_week}</div><div class="stat-lbl">📅 Active This Week</div></div>
                <div class="stat-card"><div class="stat-num">{subscribed_users}</div><div class="stat-lbl">🔔 Subscribed</div></div>
                <div class="stat-card"><div class="stat-num">{active_ai_users}</div><div class="stat-lbl">🤖 AI Users</div></div>
                <div class="stat-card"><div class="stat-num">{total_ai_queries:,}</div><div class="stat-lbl">💬 AI Messages</div></div>
                <div class="stat-card"><div class="stat-num">{total_commands:,}</div><div class="stat-lbl">🖱️ Commands Run</div></div>
                <div class="stat-card"><div class="stat-num">{total_products_indexed}</div><div class="stat-lbl">📦 Vector Products</div></div>
                <div class="stat-card"><div class="stat-num">{total_pages_indexed}</div><div class="stat-lbl">📄 Vector Pages</div></div>
            </div>

            <div class="card">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:14px;">
                    <h2 style="margin:0;">🤖 AI Provider Health & Status</h2>
                    <button onclick="document.getElementById('add-provider-modal').style.display='flex'" style="background:#007bff;color:white;border:none;padding:8px 15px;border-radius:4px;cursor:pointer;font-weight:bold;">+ Add Custom Provider</button>
                </div>
                <table>
                    <thead><tr><th>Provider</th><th>Status</th><th>Last Error</th><th style="text-align:right">Action</th></tr></thead>
                    <tbody>{provider_rows_html}</tbody>
                </table>
            </div>

            <!-- Modal -->
            <div id="add-provider-modal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
                <div style="background:white; padding:30px; border-radius:8px; width:400px; max-width:90%;">
                    <h2 style="margin-bottom:20px;">Add Custom AI Provider</h2>
                    <form method="POST" action="/admin/add_provider">
                        <input type="text" name="name" placeholder="Provider Name (e.g. together)" required style="width:100%; padding:10px; margin-bottom:15px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box;">
                        <input type="url" name="base_url" placeholder="Base URL (e.g. https://api.together.xyz/v1)" required style="width:100%; padding:10px; margin-bottom:15px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box;">
                        <input type="password" name="api_key" placeholder="API Key" required style="width:100%; padding:10px; margin-bottom:15px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box;">
                        <input type="text" name="default_model" placeholder="Default Model (e.g. mistralai/Mixtral-8x7B)" style="width:100%; padding:10px; margin-bottom:20px; border:1px solid #ddd; border-radius:4px; box-sizing:border-box;">
                        <div style="display:flex; justify-content:space-between;">
                            <button type="button" onclick="document.getElementById('add-provider-modal').style.display='none'" style="background:#6c757d; color:white; border:none; padding:10px 15px; border-radius:4px; cursor:pointer; font-weight:bold;">Cancel</button>
                            <button type="submit" style="background:#28a745; color:white; border:none; padding:10px 15px; border-radius:4px; cursor:pointer; font-weight:bold;">Save Provider</button>
                        </div>
                    </form>
                </div>
            </div>

            <div class="card">
                <h2>📊 Command Usage Breakdown</h2>
                <table>
                    <thead><tr><th>Command / Action</th><th style="text-align:right">Count</th><th>Usage Bar</th><th style="text-align:right">%</th></tr></thead>
                    <tbody>{cmd_rows}</tbody>
                </table>
            </div>

            <div class="card">
                <h2>👥 Customer Activity</h2>
                <table id="usersTable" class="display">
                    <thead>
                        <tr>
                            <th>Telegram ID</th><th>Name</th><th>Last Active</th>
                            <th>Subscribed</th><th>AI Msgs</th><th>Cmds</th><th>Top Action</th><th>Logs</th>
                        </tr>
                    </thead>
                    <tbody>{user_rows}</tbody>
                </table>
            </div>
        </div>
        <script src="https://code.jquery.com/jquery-3.7.0.min.js"></script>
        <script src="https://cdn.datatables.net/1.13.6/js/jquery.dataTables.min.js"></script>
        <script>
            $(document).ready(function(){{
                $('#usersTable').DataTable({{pageLength:25,order:[[4,"desc"]]}});
            }});
        </script>
    </body>
    </html>
    """


@app.get("/admin/chat_logs/{user_id}", response_class=HTMLResponse)
async def admin_chat_logs(user_id: int, request: Request):
    """Display the chat history for a specific user."""
    session_cookie = request.cookies.get("admin_session")
    if session_cookie != TELEGRAM_WEBHOOK_SECRET:
        return RedirectResponse(url="/admin/login", status_code=303)
    
    from db import supabase
    if not supabase:
        return "<h1>Supabase not configured</h1>"
    
    try:
        response = supabase.table("users").select("first_name, chat_history").eq("id", user_id).execute()
        if not response.data:
            return "<h1>User not found</h1>"
        user_data = response.data[0]
    except Exception as e:
        return f"<h1>Error fetching data: {e}</h1>"
    
    history = user_data.get("chat_history") or []
    name = html.escape(user_data.get("first_name") or "User")
    
    chat_html = ""
    for msg in history:
        role = msg.get("role")
        content = msg.get("content")
        
        # Handle potential list content (like Anthropic tool execution results) safely
        if isinstance(content, list) or isinstance(content, dict):
            import json
            content_text = json.dumps(content, indent=2)
        else:
            content_text = str(content) if content else ""
            
        safe_content = html.escape(content_text)
            
        if role == "user":
            if isinstance(content, list):
                chat_html += f"<div class='msg tool'><b>System Update (Tool Return):</b><br><pre>{safe_content}</pre></div>"
            else:
                chat_html += f"<div class='msg user'><b>👤 User:</b><br>{safe_content}</div>"
        elif role == "assistant":
            if not content_text and msg.get("tool_calls"):
                chat_html += "<div class='msg tool'><b>🤖 AI:</b> <i>[Triggered Tool Search]</i></div>"
            else:
                chat_html += f"<div class='msg ai'><b>🤖 AI:</b><br><pre>{safe_content}</pre></div>"
        elif role == "tool":
            tool_name = msg.get("name", "Unknown Tool")
            chat_html += f"<div class='msg tool'><b>🔧 Tool ({html.escape(tool_name)}):</b> <i>Executed</i></div>"

    if not chat_html:
        chat_html = "<p>No AI chat history available for this user.</p>"

    return f"""
    <html>
        <head>
            <title>Chat Logs - {name}</title>
            <style>
                body {{ font-family: system-ui, sans-serif; margin: 40px; background: #f4f4f9; color: #333; }}
                .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }}
                .back-btn {{ background: #6c757d; color: white; text-decoration: none; padding: 8px 15px; border-radius: 4px; font-weight: bold; font-size: 14px; }}
                .back-btn:hover {{ background: #5a6268; }}
                .chat-container {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); max-width: 800px; margin: auto; }}
                .msg {{ padding: 12px; margin-bottom: 10px; border-radius: 8px; line-height: 1.5; }}
                .user {{ background: #d1ecf1; color: #0c5460; text-align: left; border-left: 5px solid #17a2b8; }}
                .ai {{ background: #e2e3e5; color: #383d41; text-align: left; border-left: 5px solid #6c757d; }}
                .tool {{ background: #f8f9fa; color: #6c757d; font-size: 0.85em; border: 1px dashed #ddd; }}
                pre {{ white-space: pre-wrap; margin: 5px 0 0 0; font-family: inherit; }}
            </style>
        </head>
        <body>
            <div style="max-width: 800px; margin: auto;">
                <div class="header">
                    <h2>Chat Logs: {name} (ID: {user_id})</h2>
                    <a href="/admin/dashboard" class="back-btn">← Back to Dashboard</a>
                </div>
                <div class="chat-container">{chat_html}</div>
            </div>
        </body>
    </html>
    """

@app.get("/", response_class=HTMLResponse)
async def root():
    return """
    <html>
        <head>
            <title>DeenCommerce Bot</title>
            <meta http-equiv="refresh" content="0; url=/admin/login">
            <style>
                body { font-family: system-ui, sans-serif; display: flex; justify-content: center;
                       align-items: center; height: 100vh; margin: 0; background: #f4f4f9; }
                .box { text-align: center; }
                a { color: #007bff; }
            </style>
        </head>
        <body>
            <div class="box">
                <h2>🤖 DeenCommerce Bot is Running</h2>
                <p>Redirecting to <a href="/admin/login">Admin Dashboard</a>…</p>
            </div>
        </body>
    </html>
    """


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
