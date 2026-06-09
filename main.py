from contextlib import asynccontextmanager
import html
import logging
import os
import re
import socket

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


def html_table_to_markdown(table_html):
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', table_html, re.DOTALL | re.IGNORECASE)
    md_rows = []

    for row in rows:
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row, re.DOTALL | re.IGNORECASE)
        clean_cells = []
        for cell in cells:
            c = re.sub(r'<[^>]+>', '', cell)
            c = html.unescape(c)
            c = c.replace('\xa0', ' ').replace('\u200b', '')
            c = c.strip()
            clean_cells.append(c)
        if clean_cells:
            md_rows.append(clean_cells)

    if not md_rows:
        return ""

    header_title = ""
    start_idx = 0
    if len(md_rows[0]) == 1 and len(md_rows) > 1:
        header_title = f"📏 *{md_rows[0][0]}*"
        start_idx = 1
    elif len(md_rows[0]) == 1:
        return f"📏 *{md_rows[0][0]}*"

    table_lines = []
    rows_to_format = md_rows[start_idx:]
    if not rows_to_format:
        return header_title

    col_widths = {}
    for r in rows_to_format:
        for col_idx, cell in enumerate(r):
            col_widths[col_idx] = max(col_widths.get(col_idx, 0), len(cell))

    for idx, r in enumerate(rows_to_format):
        row_str = " | ".join(f"{cell:<{col_widths.get(col_idx, len(cell))}}" for col_idx, cell in enumerate(r))
        table_lines.append(row_str)
        if idx == 0:
            separator = "-+-".join("-" * col_widths.get(col_idx, len(cell)) for col_idx in range(len(r)))
            table_lines.append(separator)

    table_text = "\n".join(table_lines)

    res = ""
    if header_title:
        res += header_title + "\n"
    res += f"```\n{table_text}\n```"
    return res


def extract_and_format_size_chart(product):
    if not isinstance(product, dict):
        return None
    for field in ["short_description", "description"]:
        html_content = product.get(field, "")
        if not html_content:
            continue
        tables = re.findall(r'<table[^>]*>.*?</table>', html_content, re.DOTALL | re.IGNORECASE)
        for table in tables:
            if any(x in table.lower() for x in ["size", "chart", "guide", "dimension", "measure"]):
                return html_table_to_markdown(table)
    return None


def strip_html_excluding_table(html_content):
    if not html_content:
        return ""
    cleaned = re.sub(r'<table[^>]*>.*?</table>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    return strip_html(cleaned)


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


def main_menu(first_name=None):
    keyboard = [
        [InlineKeyboardButton("👔 Categories", callback_data="browse")],
        [InlineKeyboardButton("🆕 Latest Products", callback_data="products_all_1")],
        [InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("📦 My Order", callback_data="my_order")],
        [InlineKeyboardButton("🤖 Ask AI Agent", callback_data="ask_ai")],
    ]
    greeting = f"Assalamu Alaikum {md(first_name)}" if first_name else "Assalamu Alaikum"
    text = (
        f"🎉 *{greeting}! Welcome to DeenCommerce!*\n\n"
        "Browse by category, check stock, view a specific order, or ask our AI assistant."
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


async def get_category_by_id(category_id):
    """Fetch a single category."""
    return await woo_get(f"products/categories/{category_id}")


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


# ==================== Conversational AI RAG Agent ====================

SYSTEM_PROMPT = """You are an intelligent fashion shopping assistant for DeenCommerce,
a Bangladeshi e-commerce store selling clothing and fashion items on deencommerce.com.

You must ALWAYS talk and respond ONLY in the context of deencommerce.com and its products, categories, orders, policies, and services.
If the customer asks or talks about anything unrelated to deencommerce.com (such as general knowledge, other websites, coding, general questions, or non-DeenCommerce items/topics), you must politely decline to answer, inform them that you are the DeenCommerce shopping assistant, and redirect them back to deencommerce.com products, clothing items, or order inquiries.

You have access to tools to:
1. Search products by keyword or category
2. Get product details (price, description, stock, images)
3. Provide personalized recommendations

Your goals:
- Help customers find exactly what they're looking for
- Answer questions about products, prices, and availability
- Make personalized recommendations based on their needs
- Be conversational and friendly
- Handle queries intelligently by using tools when needed

Language & Response Style:
- Understand and reply in the user's preferred language, including English, Bangla (Bengali), and Banglish (Bengali written in Latin script).
- Keep responses extremely to-the-point, concise, and direct without unnecessary fluff.
- Be concise in Telegram (max 1000 characters per message).
- Use emojis to make responses engaging.
- Always mention prices in ৳ (Taka).

Telegram Bot Context:
You operate inside a Telegram bot. The user can also use the following slash commands:
- /start : Go to the Main Menu and welcome greeting.
- /browse : Browse clothing categories.
- /search : Search for products.
- /my_order : Check order status (requires order ID + email/phone).
- /ask : Ask the AI assistant questions (e.g., "/ask blue shirts").
If a user wants to perform these actions, you can mention or guide them to use these slash commands.

When a customer asks for a size chart or size guide of a product, retrieve the product details and output its size_chart string exactly as provided (with the monospace code block formatting).

When recommending or listing products, always include their website link (permalink) so the customer can easily view/buy them on the website.

When a customer asks a question:
1. Understand their intent (searching, browsing, recommendation, etc.)
2. Decide which tools to use
3. Retrieve relevant information from our database
4. Provide a helpful, conversational response
"""


def get_providers_chain(primary_provider_name=None):
    """Get a list of all configured and valid providers starting with the primary one."""
    if not primary_provider_name:
        primary_provider_name = os.getenv("AI_PROVIDER", "anthropic").lower().strip()

    providers_info = {
        "anthropic": {
            "key_var": "ANTHROPIC_API_KEY",
            "type": "anthropic",
            "default_model": "claude-3-5-sonnet-20241022",
            "constructor": lambda key: ("anthropic", AsyncAnthropic(api_key=key))
        },
        "openrouter": {
            "key_var": "OPENROUTER_API_KEY",
            "type": "openai",
            "default_model": "google/gemini-2.5-flash",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://openrouter.ai/api/v1"))
        },
        "gemini": {
            "key_var": "GEMINI_API_KEY",
            "type": "openai",
            "default_model": "gemini-1.5-flash",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/"))
        },
        "groq": {
            "key_var": "GROQ_API_KEY",
            "type": "openai",
            "default_model": "llama3-8b-8192",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1"))
        },
        "openai": {
            "key_var": "OPENAI_API_KEY",
            "type": "openai",
            "default_model": "gpt-4o-mini",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key))
        },
        "grok": {
            "key_var": "GROK_API_KEY",
            "type": "openai",
            "default_model": "grok-2-1212",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://api.x.ai/v1"))
        }
    }

    chain = []

    def is_valid_key(val):
        if not val:
            return False
        val_lower = val.lower().strip()
        return not (val_lower.startswith("your_") or val_lower.endswith("_here") or "placeholder" in val_lower)

    # First, add the primary provider if valid
    primary_info = providers_info.get(primary_provider_name)
    if primary_info:
        key = os.getenv(primary_info["key_var"])
        if is_valid_key(key):
            try:
                ctype, client = primary_info["constructor"](key)
                model = os.getenv("AI_MODEL", "").strip() or primary_info["default_model"]
                chain.append({
                    "name": primary_provider_name,
                    "client_type": ctype,
                    "client": client,
                    "model_name": model
                })
            except Exception as e:
                logger.error("Failed to initialize primary provider %s: %s", primary_provider_name, str(e))

    # Then add other valid fallback providers
    fallback_order = ["openrouter", "gemini", "groq", "anthropic", "openai", "grok"]
    for p_name in fallback_order:
        if p_name == primary_provider_name:
            continue
        p_info = providers_info[p_name]
        key = os.getenv(p_info["key_var"])
        if is_valid_key(key):
            try:
                ctype, client = p_info["constructor"](key)
                chain.append({
                    "name": p_name,
                    "client_type": ctype,
                    "client": client,
                    "model_name": p_info["default_model"]
                })
            except Exception as e:
                logger.error("Failed to initialize fallback provider %s: %s", p_name, str(e))

    return chain


class RAGAgent:
    def __init__(self, woocommerce_url, woocommerce_key, woocommerce_secret):
        self.woo_url = woocommerce_url
        self.woo_key = woocommerce_key
        self.woo_secret = woocommerce_secret
        self.conversation_history = []
        self.providers_chain = get_providers_chain()

    async def search_products(self, query: str, limit: int = 5):
        """Search products by keyword"""
        async with httpx.AsyncClient(
            auth=(self.woo_key, self.woo_secret),
            timeout=10
        ) as client:
            response = await client.get(
                f"{self.woo_url}/wp-json/wc/v3/products",
                params={
                    "search": query,
                    "per_page": limit,
                    "status": "publish",
                    "stock_status": "instock"
                }
            )
            products = response.json()

            # Format for LLM
            return [
                {
                    "id": p["id"],
                    "name": p["name"],
                    "price": p["price"],
                    "description": p.get("description", "")[:200],
                    "stock": p.get("stock_quantity", "N/A"),
                    "image": p.get("images", [{}])[0].get("src", ""),
                    "permalink": p.get("permalink", "")
                }
                for p in products[:limit]
            ]

    async def get_product_details(self, product_id: int):
        """Get detailed product information"""
        async with httpx.AsyncClient(
            auth=(self.woo_key, self.woo_secret),
            timeout=10
        ) as client:
            response = await client.get(
                f"{self.woo_url}/wp-json/wc/v3/products/{product_id}"
            )
            p = response.json()

            size_chart = extract_and_format_size_chart(p)
            return {
                "id": p["id"],
                "name": p["name"],
                "price": p["price"],
                "description": p.get("description", ""),
                "short_description": p.get("short_description", ""),
                "size_chart": size_chart if size_chart else "No size chart available.",
                "stock": p.get("stock_quantity", "N/A"),
                "categories": [c.get("name") for c in p.get("categories", [])],
                "images": [img["src"] for img in p.get("images", [])],
                "sku": p.get("sku", ""),
                "attributes": p.get("attributes", []),
                "permalink": p.get("permalink", "")
            }

    async def get_recommendations(self, category: str = None, price_range: str = None):
        """Get personalized product recommendations"""
        params = {
            "per_page": 5,
            "status": "publish",
            "stock_status": "instock"
        }

        if category:
            params["category"] = category

        async with httpx.AsyncClient(
            auth=(self.woo_key, self.woo_secret),
            timeout=10
        ) as client:
            response = await client.get(
                f"{self.woo_url}/wp-json/wc/v3/products",
                params=params
            )
            products = response.json()

            return [
                {
                    "name": p["name"],
                    "price": p["price"],
                    "reason": f"Popular in {category or 'our store'}",
                    "permalink": p.get("permalink", "")
                }
                for p in products[:5]
            ]

    async def process_message(self, user_message: str, user_id: int = None) -> str:
        """Process user message with RAG + LLM, falling back to other providers if needed"""
        if not self.providers_chain:
            raise RuntimeError("No valid AI providers configured in environment variables.")

        # Save a backup of conversation history before this processing run
        history_backup = list(self.conversation_history)

        # Append user message once
        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })

        last_error = None
        for provider in self.providers_chain:
            client_type = provider["client_type"]
            client = provider["client"]
            model_name = provider["model_name"]
            provider_name = provider["name"]

            logger.info("Trying AI provider '%s' (model: %s)...", provider_name, model_name)

            try:
                if client_type == "anthropic":
                    response = await self._process_anthropic(client, model_name)
                else:
                    response = await self._process_openai(client, model_name)

                logger.info("Successfully processed message using AI provider '%s'.", provider_name)
                return response
            except Exception as e:
                logger.error("AI provider '%s' failed: %s", provider_name, str(e))
                last_error = e
                # Restore history to state before this attempt, retaining the user message
                self.conversation_history = list(history_backup)
                self.conversation_history.append({
                    "role": "user",
                    "content": user_message
                })

        # If all providers failed, restore history to original state (before user message) and raise
        self.conversation_history = history_backup
        raise last_error or RuntimeError("All AI providers in chain failed.")

    async def _process_anthropic(self, client, model_name: str) -> str:
        """Process user message using AsyncAnthropic"""

        # Define available tools for Claude
        tools = [
            {
                "name": "search_products",
                "description": "Search for products by keyword (shirt, jeans, dress, etc.)",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Product search query"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of results (default 5)",
                            "default": 5
                        }
                    },
                    "required": ["query"]
                }
            },
            {
                "name": "get_product_details",
                "description": "Get detailed information about a specific product",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "product_id": {
                            "type": "integer",
                            "description": "Product ID"
                        }
                    },
                    "required": ["product_id"]
                }
            },
            {
                "name": "get_recommendations",
                "description": "Get personalized product recommendations",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Product category (shirts, pants, dresses, etc.)"
                        },
                        "price_range": {
                            "type": "string",
                            "description": "Price range (budget, mid-range, premium)"
                        }
                    }
                }
            }
        ]

        # Call Claude with tools
        response = await client.messages.create(
            model=model_name,
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=self.conversation_history
        )

        # Process Claude's response
        while response.stop_reason == "tool_use":
            # Claude wants to use a tool
            tool_calls = [block for block in response.content if block.type == "tool_use"]

            # Execute tools and collect results
            tool_results = []

            for tool_call in tool_calls:
                tool_name = tool_call.name
                tool_input = tool_call.input

                logger.info("🔧 Using tool: %s with input: %s", tool_name, tool_input)

                try:
                    if tool_name == "search_products":
                        result = await self.search_products(
                            query=tool_input["query"],
                            limit=tool_input.get("limit", 5)
                        )
                    elif tool_name == "get_product_details":
                        result = await self.get_product_details(
                            product_id=tool_input["product_id"]
                        )
                    elif tool_name == "get_recommendations":
                        result = await self.get_recommendations(
                            category=tool_input.get("category"),
                            price_range=tool_input.get("price_range")
                        )
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}
                except Exception as e:
                    result = {"error": str(e)}

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call.id,
                    "content": json.dumps(result)
                })

            # Add assistant response and tool results to history
            self.conversation_history.append({
                "role": "assistant",
                "content": response.content
            })

            self.conversation_history.append({
                "role": "user",
                "content": tool_results
            })

            # Call Claude again with tool results
            response = await client.messages.create(
                model=model_name,
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                tools=tools,
                messages=self.conversation_history
            )

        # Extract final text response
        final_response = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_response += block.text

        # Add assistant response to history
        self.conversation_history.append({
            "role": "assistant",
            "content": final_response
        })

        return final_response

    async def _process_openai(self, client, model_name: str) -> str:
        """Process user message using OpenAI-compatible API"""

        # Define tools in OpenAI format
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "search_products",
                    "description": "Search for products by keyword (shirt, jeans, dress, etc.)",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Product search query"
                            },
                            "limit": {
                                "type": "integer",
                                "description": "Number of results (default 5)",
                                "default": 5
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_product_details",
                    "description": "Get detailed information about a specific product",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "product_id": {
                                "type": "integer",
                                "description": "Product ID"
                            }
                        },
                        "required": ["product_id"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_recommendations",
                    "description": "Get personalized product recommendations",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "category": {
                                "type": "string",
                                "description": "Product category (shirts, pants, dresses, etc.)"
                            },
                            "price_range": {
                                "type": "string",
                                "description": "Price range (budget, mid-range, premium)"
                            }
                        }
                    }
                }
            }
        ]

        # Call OpenAI with tools
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "system", "content": SYSTEM_PROMPT}] + self.conversation_history,
            tools=tools,
            tool_choice="auto",
            max_tokens=1000
        )
        assistant_msg = response.choices[0].message

        while assistant_msg.tool_calls:
            # Format and save assistant's message including tool calls
            tool_calls_list = []
            for tc in assistant_msg.tool_calls:
                tool_calls_list.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments
                    }
                })

            self.conversation_history.append({
                "role": "assistant",
                "content": assistant_msg.content or "",
                "tool_calls": tool_calls_list
            })

            # Execute tools
            for tc in assistant_msg.tool_calls:
                tool_name = tc.function.name
                tool_input = json.loads(tc.function.arguments)

                logger.info("🔧 Using tool: %s with input: %s", tool_name, tool_input)

                try:
                    if tool_name == "search_products":
                        result = await self.search_products(
                            query=tool_input["query"],
                            limit=tool_input.get("limit", 5)
                        )
                    elif tool_name == "get_product_details":
                        result = await self.get_product_details(
                            product_id=tool_input["product_id"]
                        )
                    elif tool_name == "get_recommendations":
                        result = await self.get_recommendations(
                            category=tool_input.get("category"),
                            price_range=tool_input.get("price_range")
                        )
                    else:
                        result = {"error": f"Unknown tool: {tool_name}"}
                except Exception as e:
                    result = {"error": str(e)}

                # Add tool result message to history
                self.conversation_history.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tool_name,
                    "content": json.dumps(result)
                })

            # Call OpenAI again with tool results
            response = await client.chat.completions.create(
                model=model_name,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}] + self.conversation_history,
                tools=tools,
                tool_choice="auto",
                max_tokens=1000
            )
            assistant_msg = response.choices[0].message

        # Final response
        final_response = assistant_msg.content or ""
        self.conversation_history.append({
            "role": "assistant",
            "content": final_response
        })
        return final_response


# Store agents per user (so each user has their own conversation)
user_agents = {}


async def ai_chat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle conversational AI queries"""
    user_id = update.effective_user.id
    user_message = update.message.text

    # Create agent for user if doesn't exist
    if user_id not in user_agents:
        user_agents[user_id] = RAGAgent(
            WOOCOMMERCE_URL,
            WOOCOMMERCE_KEY,
            WOOCOMMERCE_SECRET
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
                    await update.message.reply_text(
                        response[i:],
                        reply_markup=reply_markup,
                        parse_mode="Markdown"
                    )
                else:
                    await update.message.reply_text(
                        response[i:i+4000],
                        parse_mode="Markdown"
                    )
        else:
            await update.message.reply_text(
                response,
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

    except Exception as e:
        logger.error("AI chat error: %s", str(e))
        await update.message.reply_text(
            "❌ Error processing your request. Please try again."
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
    context.user_data.clear()
    first_name = update.effective_user.first_name if update.effective_user else None
    text, reply_markup = main_menu(first_name)

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

        description_field = product.get("description") or product.get("short_description") or ""
        desc_clean = strip_html_excluding_table(description_field)
        if desc_clean:
            text += f"📝 {md(desc_clean[:300])}"
            if len(desc_clean) > 300:
                text += "..."
            text += "\n\n"

        size_chart = extract_and_format_size_chart(product)
        if size_chart:
            text += f"{size_chart}\n\n"

        keyboard = []
        permalink = product.get("permalink") if isinstance(product, dict) else None
        if permalink:
            keyboard.append([InlineKeyboardButton("🌐 View on Website", url=permalink)])
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
application.add_handler(CommandHandler("ask", ask_command))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^browse$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^cat_\d+_\d+$"))
application.add_handler(CallbackQueryHandler(show_products, pattern=r"^products_all_\d+$"))
application.add_handler(CallbackQueryHandler(search_handler, pattern="^search$"))
application.add_handler(CallbackQueryHandler(my_order_handler, pattern="^my_order$"))
application.add_handler(CallbackQueryHandler(ask_ai_callback_handler, pattern="^ask_ai$"))
application.add_handler(CallbackQueryHandler(reset_ai_chat_handler, pattern="^reset_ai_chat$"))
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
