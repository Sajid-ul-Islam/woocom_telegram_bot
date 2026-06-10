from contextlib import asynccontextmanager
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

# Global HTTP client to reuse TCP/TLS connections
http_client = None


@asynccontextmanager
async def lifespan(fastapi_app: FastAPI):
    """Lifecycle events for FastAPI application."""
    logger.info("Initializing Telegram application...")
    try:
        # Initialize global HTTP client
        global http_client
        http_client = httpx.AsyncClient(
            auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
            timeout=10.0
        )

        await application.initialize()
        await application.start()
        logger.info("Telegram application initialized and started.")

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
    if http_client:
        await http_client.aclose()


app = FastAPI(lifespan=lifespan)


# ==================== Formatting Helpers ====================

def md(value):
    """Escape dynamic values before interpolating into Telegram Markdown."""
    return escape_markdown("" if value is None else str(value), version=1)


def strip_html(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text).strip()


def product_button_name(name):
    clean_name = str(name or "Product").strip()
    return clean_name[:32] if clean_name else "Product"


def stock_display(product):
    stock_status = str(product.get("stock_status") or "").lower()
    stock_quantity = product.get("stock_quantity")
    manage_stock = bool(product.get("manage_stock"))

    if stock_status == "instock":
        status = "✅ In Stock"
    elif stock_status == "onbackorder":
        status = "🟡 On Backorder"
    elif stock_status == "outofstock":
        status = "❌ Out of Stock"
    elif product.get("in_stock"):
        status = "✅ In Stock"
    else:
        status = "❌ Out of Stock"

    if manage_stock and stock_quantity is not None:
        return f"📊 Stock: {md(stock_quantity)} {status}"

    return f"📊 Availability: {status}"


def main_menu():
    keyboard = [
        [InlineKeyboardButton("👔 Categories", callback_data="browse")],
        [InlineKeyboardButton("🆕 Latest Products", callback_data="products_all_1")],
        [InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("📦 My Order", callback_data="my_order")],
    ]
    text = (
        "🎉 *Welcome to DeenCommerce!*\n\n"
        "Browse by category, check stock, and view a specific order."
    )
    return text, InlineKeyboardMarkup(keyboard)


# ==================== WooCommerce API Helpers ====================

async def woo_get(path, params=None):
    """Fetch JSON from WooCommerce and normalize API/HTTP failures."""
    global http_client
    client_to_use = http_client
    own_client = False
    try:
        if client_to_use is None:
            client_to_use = httpx.AsyncClient(
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10.0
            )
            own_client = True

        response = await client_to_use.get(
            f"{WOOCOMMERCE_URL}/wp-json/wc/v3/{path.lstrip('/')}",
            params=params,
        )
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as e:
        logger.error("WooCommerce API returned %s for %s", e.response.status_code, path)
        return {"error": f"WooCommerce API returned {e.response.status_code}"}
    except Exception as e:
        logger.error("Error fetching WooCommerce path %s: %s", path, str(e))
        return {"error": str(e)}
    finally:
        if own_client and client_to_use:
            await client_to_use.aclose()


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
    """Fetch product categories that have products."""
    return await woo_get(
        "products/categories",
        params={"per_page": limit, "orderby": "name", "order": "asc", "hide_empty": True},
    )


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
    """Fetch a single product."""
    return await woo_get(f"products/{product_id}")


async def search_products(keyword):
    """Search products by keyword."""
    return await woo_get(
        "products",
        params={
            "search": keyword,
            "per_page": 10,
            "status": "publish",
            "stock_status": "instock",
        },
    )


async def get_order_by_id(order_id):
    """Fetch a single order by ID."""
    return await woo_get(f"orders/{order_id}")


# ==================== Telegram Handlers ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - main menu."""
    context.user_data.clear()
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
        if is_category:
            products = await get_products_by_category(category_id, page=page, limit=limit)
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

        desc_clean = strip_html(product.get("description", "No description"))
        if desc_clean:
            text += f"📝 {md(desc_clean[:300])}"
            if len(desc_clean) > 300:
                text += "..."
            text += "\n\n"

        keyboard = [[InlineKeyboardButton("← Back", callback_data="browse")]]
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
        "💳 *Payment*: bKash, Nagad, or Cash on Delivery.\n"
        "🚚 *Shipping*: Dhaka: 24-48h (৳80), Outside Dhaka: 3-5 days (৳150).\n"
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
            "• *Inside Dhaka*: 24 to 48 Hours. Delivery Fee: *৳80*.\n"
            "• *Outside Dhaka*: 3 to 5 Days (via Pathao / Steadfast). Delivery Fee: *৳150*.\n\n"
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
        text = (
            "📞 *Contact DEEN Commerce Support*\n\n"
            "Need to talk to a human agent? We are here to help!\n\n"
            "💬 *Messenger*: [Click here to message us](https://m.me/deencommerce)\n"
            "🟢 *WhatsApp*: `+8801700000000` (Mock/Placeholder number)\n"
            "📞 *Hotline*: `+8809612345678` (10:00 AM - 8:00 PM)\n"
            "✉️ *Email*: `support@deencommerce.com`"
        )
    else:
        text = "Topic not found."

    await query.edit_message_text(
        text=text,
        reply_markup=reply_markup,
        parse_mode="Markdown",
        disable_web_page_preview=True
    )


# ==================== Register Handlers ====================

application.add_handler(CommandHandler(["start", "strat"], start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CommandHandler("browse", browse_products))
application.add_handler(CommandHandler("search", search_handler))
application.add_handler(CommandHandler("my_order", my_order_handler))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^browse$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^cat_\d+_\d+$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^products_all_\d+$"))
application.add_handler(CallbackQueryHandler(search_handler, pattern="^search$"))
application.add_handler(CallbackQueryHandler(my_order_handler, pattern="^my_order$"))
application.add_handler(CallbackQueryHandler(view_product, pattern="^product_"))
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
