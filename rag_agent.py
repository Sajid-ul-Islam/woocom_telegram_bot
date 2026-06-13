import json
import os
import re
import html
import time
import httpx
import logging
from anthropic import AsyncAnthropic
import openai
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL", "").rstrip("/")
WOOCOMMERCE_KEY = os.getenv("WOOCOMMERCE_KEY")
WOOCOMMERCE_SECRET = os.getenv("WOOCOMMERCE_SECRET")

from utils import (
    products_cache,
    preprocess_search_query,
    woo_get,
    get_store_address,
    md,
    html_table_to_markdown,
    extract_and_format_size_chart,
    extract_bengali_order_context,
    bn_to_arabic,
    format_price_display,
)
from main import get_categories
from db import get_user_history, update_user_history


SYSTEM_PROMPT = """You are an intelligent fashion shopping assistant for DEEN Commerce,
a Bangladeshi e-commerce store selling clothing and fashion items on deencommerce.com.

You must ALWAYS talk and respond ONLY in the context of deencommerce.com and its products, categories, orders, policies, and services.
If the customer asks or talks about anything unrelated to deencommerce.com (such as general knowledge, other websites, coding, general questions, or non-DEEN Commerce items/topics), you must politely decline to answer, inform them that you are the DEEN Commerce shopping assistant, and redirect them back to deencommerce.com products, clothing items, or order inquiries.

You have access to tools to:
1. Search products by keyword or category
2. Get product details (price, description, stock, images)
3. Provide personalized recommendations
4. View the user's shopping cart (using view_cart) and checkout (using checkout)
5. Show product categories (using show_categories)
6. Securely lookup order status and track courier package (using order_lookup)
7. Trigger manual forms or FAQ menus (using trigger_order_lookup, trigger_search, get_help)

Your goals:
- Help customers find exactly what they're looking for
- Answer questions about products, prices, and availability
- Make personalized recommendations based on their needs
- Be conversational and friendly
- Handle queries intelligently by using tools when needed

Security & Order Tracking:
- When a customer wants to check their order status or track it, you MUST request BOTH the order number/ID and the billing email or phone number. Both parameters are strictly required by the `order_lookup` tool for safety.
- If the order has been shipped or has a Pathao Courier consignment ID, you must fetch the live status from the Pathao Courier API (which the `order_lookup` tool automatically fetches) and display the current shipping status and tracking history to the customer.

Language & Response Style:
- Understand and reply in the user's preferred language, including English, Bangla (Bengali), and Banglish (Bengali written in Latin script).
- When a customer writes in Bengali, respond in Bengali.
- Keep responses extremely to-the-point, concise, and direct without unnecessary fluff.
- Be concise in Telegram (max 1000 characters per message).
- Use emojis to make responses engaging.
- Always mention prices in ৳ (Taka).
- If a product is on sale (has sale_price), calculate the discount percentage and savings. Mention the discount in Bengali like: "২০% ছাড়ে আপনি পাচ্ছেন এত টাকায়, সাশ্রয় হচ্ছে এত টাকা". When displaying the price in your list, use the 'formatted_price' field exactly as provided to show the crossed-out regular price and current sale price (e.g. ৳3̶4̶9̶ ৳279).

Bengali Query Handling:
- Customers may search in Bengali (e.g. "সুতির কাপড় আছে?", "কালো পাঞ্জাবি", "জিন্স আছে?"). Understand and translate these naturally.
- Colors in Bengali: লাল=red, নীল=blue, সবুজ=green, কালো=black, সাদা=white, কমলা=orange, হলুদ=yellow, গোলাপি=pink, বেগুনি=purple, ধূসর=gray.
- Fabrics in Bengali: সুতি/সুতীর=cotton, লিনেন=linen, সিল্ক/রেশম=silk, ডেনিম=denim.
- Clothing in Bengali: গেঞ্জি=t-shirt, শার্ট=shirt, পাঞ্জাবি=panjabi, প্যান্ট=pants, জিন্স=jeans, পোলো=polo, হুডি=hoodie.

Bengali Order-Placement Handling:
- Customers may send a combined message with product + size + their name + address + phone, all in one block (in Bengali or mixed). For example:
  "এই দুইটা পাঞ্জাবী ৪৮ সাইজ অর্ডার করতে চাচ্ছি শাহীন আলম ঝাউলাহাটি চৌরাস্তা ঢাকা 01614225311"
- When you receive a [PARSED ORDER CONTEXT] block with `Order Placement Intent`, use that structured data directly.
- ALWAYS search for the requested product first using the search_products tool to confirm availability and price.
- Then respond with a friendly order summary in Bengali showing:
  ✅ Product name + price
  📦 Quantity & Size
  👤 Customer name
  📍 Delivery address
  📞 Phone number(s)
- Ask them to confirm, then guide them to complete the order on the website (provide permalink) or via the checkout button.
- If size or quantity is missing, ask for it before confirming.
- NEVER place an order without confirming details with the customer first.

Bengali Order-Status Handling:
- Customers may ask about their order delivery status in Bengali. For example:
  "আমি একটি পাঞ্জাবি অডার করেছিলাম ১১/০৬/২০২৬ অর্ডার নাম্বার -২০০৮৬৫ কতদিন পর ডেলিভারি হবে? আমার মোবাইল নাম্বার -০১৭০৩৫২২৫৫৪"
- When you receive a [PARSED ORDER CONTEXT] block with `Order Status Intent`, use the extracted Order ID and Phone/Email to IMMEDIATELY call the `order_lookup` tool.
- Provide the status to the customer in a friendly Bengali response based on the `order_lookup` result.

Telegram Bot Context:
You operate inside a Telegram bot. The user can also use the following slash commands:
- /start : Go to the Main Menu and welcome greeting.
- /browse : Browse clothing categories.
- /search : Search for products.
- /my_order : Check order status (requires order ID + email/phone).
- /ask : Ask the AI assistant questions (e.g., "/ask blue shirts").
If a user wants to perform these actions, you can mention or guide them to use these slash commands.

When a customer asks for a size chart or size guide of a product, retrieve the product details and output its size_chart string exactly as provided (with the monospace code block formatting).

CRITICAL: If a requested product is 'Out of Stock', you MUST automatically trigger the `get_recommendations` tool based on the same category and offer 3 available alternatives immediately. Do not just say it's out of stock.

When recommending or listing products, always include their website link (permalink) so the customer can easily view/buy them on the website.

When a customer asks a question:
1. Understand their intent (searching, browsing, recommendation, order placement, etc.)
2. Decide which tools to use
3. Retrieve relevant information from our database
4. Provide a helpful, conversational response
"""

def get_providers_chain(primary_provider_name=None):
    """Get a list of all configured and valid providers starting with the primary one."""
    if not primary_provider_name:
        primary_provider_name = os.getenv("AI_PROVIDER", "openrouter").lower().strip()

    providers_info = {
        "openrouter": {
            "key_vars": ["OPENROUTER_API_KEY"],
            "type": "openai",
            "default_model": "google/gemini-2.5-flash",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://openrouter.ai/api/v1", timeout=10.0))
        },
        "groq": {
            "key_vars": ["GROQ_API_KEY"],
            "type": "openai",
            "default_model": "llama3-8b-8192",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://api.groq.com/openai/v1", timeout=10.0))
        },
        "openai": {
            "key_vars": ["OPENAI_API_KEY"],
            "type": "openai",
            "default_model": "gpt-4o-mini",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, timeout=10.0))
        },
        "anthropic": {
            "key_vars": ["ANTHROPIC_API_KEY"],
            "type": "anthropic",
            "default_model": "claude-3-5-sonnet-20241022",
            "constructor": lambda key: ("anthropic", AsyncAnthropic(api_key=key, timeout=10.0))
        },
        "xai": {
            "key_vars": ["XAI_API_KEY", "GROK_API_KEY"],
            "type": "openai",
            "default_model": "grok-2-1212",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://api.x.ai/v1", timeout=10.0))
        },
        "grok": {
            "key_vars": ["GROK_API_KEY", "XAI_API_KEY"],
            "type": "openai",
            "default_model": "grok-2-1212",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://api.x.ai/v1", timeout=10.0))
        },
        "gemini": {
            "key_vars": ["GEMINI_API_KEY"],
            "type": "openai",
            "default_model": "gemini-1.5-flash",
            "constructor": lambda key: ("openai", openai.AsyncOpenAI(api_key=key, base_url="https://generativelanguage.googleapis.com/v1beta/openai/", timeout=10.0))
        }
    }

    chain = []

    def is_valid_key(val):
        if not val:
            return False
        val_lower = val.lower().strip()
        return not (val_lower.startswith("your_") or val_lower.endswith("_here") or "placeholder" in val_lower)

    def get_api_key(p_info):
        for kv in p_info.get("key_vars", []):
            val = os.getenv(kv)
            if is_valid_key(val):
                return val
        return None

    # First, add the primary provider if valid
    primary_info = providers_info.get(primary_provider_name)
    if primary_info:
        key = get_api_key(primary_info)
        if key:
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

    # Then add other valid fallback providers in cascade order
    fallback_order = ["openrouter", "groq", "openai", "anthropic", "xai", "gemini"]
    for p_name in fallback_order:
        is_same_as_primary = (
            (p_name == primary_provider_name) or
            (p_name == "xai" and primary_provider_name == "grok") or
            (p_name == "grok" and primary_provider_name == "xai")
        )
        if is_same_as_primary:
            continue

        p_info = providers_info[p_name]
        key = get_api_key(p_info)
        if key:
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
    def __init__(self, woocommerce_url, woocommerce_key, woocommerce_secret, user_id=None):
        self.woo_url = woocommerce_url
        self.woo_key = woocommerce_key
        self.woo_secret = woocommerce_secret
        self.user_id = user_id
        
        self.conversation_history = []
        if self.user_id:
            self.conversation_history = get_user_history(self.user_id) or []
            
        self.providers_chain = get_providers_chain()

    async def search_products(self, query: str, limit: int = 5):
        """Search products by keyword"""
        processed_query = preprocess_search_query(query)
        logger.info("RAG search. Original: %s -> Processed: %s", query, processed_query)
        products = await woo_get(
            "products",
            params={
                "search": processed_query,
                "per_page": limit,
                "status": "publish",
                "stock_status": "instock"
            }
        )
        if isinstance(products, dict) and "error" in products:
            return []
            
        # Format for LLM
        return [
            {
                "id": p["id"],
                "name": p["name"],
                "price": p["price"],
                "regular_price": p.get("regular_price", ""),
                "sale_price": p.get("sale_price", ""),
                "formatted_price": format_price_display(p),
                "description": p.get("description", "")[:200],
                "stock": p.get("stock_quantity", "N/A"),
                "image": p.get("images", [{}])[0].get("src", ""),
                "permalink": p.get("permalink", "")
            }
            for p in products[:limit]
        ]

    async def get_product_details(self, product_id: int):
        """Get detailed product information (with caching)"""
        cache_key = f"product_{product_id}"
        p = products_cache.get(cache_key)
        if p is None:
            p = await woo_get(f"products/{product_id}")
            if isinstance(p, dict) and "error" not in p:
                products_cache.set(cache_key, p)

        if isinstance(p, dict) and "error" in p:
            return {"error": "Product not found."}

        size_chart = extract_and_format_size_chart(p)
        return {
            "id": p["id"],
            "name": p["name"],
            "price": p["price"],
            "regular_price": p.get("regular_price", ""),
            "sale_price": p.get("sale_price", ""),
            "formatted_price": format_price_display(p),
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

        products = await woo_get(
            "products",
            params=params
        )
        if isinstance(products, dict) and "error" in products:
            return []

        return [
            {
                "name": p["name"],
                "price": p["price"],
                "regular_price": p.get("regular_price", ""),
                "sale_price": p.get("sale_price", ""),
                "formatted_price": format_price_display(p),
                "reason": f"Popular in {category or 'our store'}",
                "permalink": p.get("permalink", "")
            }
            for p in products[:5]
        ]

    async def process_message(self, user_message: str, user_id: int = None, cart: list = None) -> tuple[str, list]:
        """Process a user message using ReAct loop"""
        self.cart = cart or []
        self.extra_buttons = []
        if user_id is not None and not self.conversation_history:
            self.conversation_history = get_user_history(user_id)

        # --- Bengali order-intent detection ---
        # Parse the raw message for structured order data and prepend a context
        # block so the AI can immediately reason about it without extra turns.
        _ascii_msg = bn_to_arabic(user_message)
        _ctx = extract_bengali_order_context(_ascii_msg)
        if _ctx["is_order_intent"] or _ctx["is_status_intent"]:
            ctx_lines = ["[PARSED ORDER CONTEXT]"]
            if _ctx["is_status_intent"]:
                ctx_lines.append("  Intent: Order Status Tracking")
            elif _ctx["is_order_intent"]:
                ctx_lines.append("  Intent: Order Placement")

            if _ctx["order_id"]:
                ctx_lines.append(f"  Order ID: {_ctx['order_id']}")
            if _ctx["product"]:
                ctx_lines.append(f"  Product: {_ctx['product']}")
            if _ctx["quantity"]:
                ctx_lines.append(f"  Quantity: {_ctx['quantity']}")
            if _ctx["size"]:
                ctx_lines.append(f"  Size: {_ctx['size']}")
            if _ctx["name"]:
                ctx_lines.append(f"  Customer name: {_ctx['name']}")
            if _ctx["address"]:
                ctx_lines.append(f"  Delivery address: {_ctx['address']}")
            if _ctx["phones"]:
                ctx_lines.append(f"  Phone(s): {', '.join(_ctx['phones'])}")
            if _ctx["emails"]:
                ctx_lines.append(f"  Email(s): {', '.join(_ctx['emails'])}")
            ctx_lines.append("[END CONTEXT]")
            augmented_message = "\n".join(ctx_lines) + "\n\n" + user_message
        else:
            augmented_message = user_message
        # Use the augmented message for AI processing, keep original for history
        internal_message = augmented_message
            
        store_address = await get_store_address()
        dynamic_system_prompt = (
            SYSTEM_PROMPT +
            f"\n\n[STORE ADDRESS]\nThe physical outlet addresses for DEEN Commerce are:\n{store_address}\n\n"
            f"[OUTLET INSTRUCTIONS]\n"
            f"- If a customer asks for a specific outlet's address (e.g., Mirpur, Wari, Cumilla, or Sylhet), provide ONLY that specific outlet's details (address, phone, hours, and map link), NOT all of them.\n"
            f"- If a customer asks for outlets in Dhaka, provide ONLY the Mirpur and Wari outlets' details.\n"
            f"- If they ask generally about your store locations, outlets, or where they can visit, list all 4 outlets."
        )

        messages = self.conversation_history + [{"role": "user", "content": internal_message}]

        if not self.providers_chain:
            raise RuntimeError("No valid AI providers configured in environment variables.")

        # Save a backup of conversation history before this processing run
        history_backup = list(self.conversation_history)

        # Append the ORIGINAL user message to history (not the augmented context block)
        # so that stored conversation stays clean and human-readable.
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

            # Trim history to last 20 messages to prevent context window overflow
            MAX_HISTORY = 20
            if len(self.conversation_history) > MAX_HISTORY:
                logger.info(
                    "Trimming conversation history from %d to %d messages.",
                    len(self.conversation_history), MAX_HISTORY
                )
                self.conversation_history = self.conversation_history[-MAX_HISTORY:]
                history_backup = list(self.conversation_history)

            # Clear extra buttons before each provider attempt in case of partial executions
            self.extra_buttons = []

            try:
                if client_type == "anthropic":
                    response = await self._process_anthropic(client, model_name, dynamic_system_prompt)
                else:
                    response = await self._process_openai(client, model_name, dynamic_system_prompt)

                logger.info("Successfully processed message using AI provider '%s'.", provider_name)
                if self.user_id:
                    update_user_history(self.user_id, self.conversation_history)
                return response, self.extra_buttons
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

    async def view_cart(self):
        """View the current items in the user's shopping cart"""
        self.extra_buttons.append({"text": "🛒 View Cart", "callback_data": "view_cart"})
        if not self.cart:
            return "Your shopping cart is currently empty."

        self.extra_buttons.append({"text": "💳 Checkout", "callback_data": "checkout"})
        items_desc = []
        for idx, item in enumerate(self.cart, 1):
            items_desc.append(f"{idx}. {item['name']} (x{item['quantity']})")
        return "Here are the items in your cart:\n" + "\n".join(items_desc)

    async def checkout(self):
        """Proceed to checkout the items in the cart"""
        if not self.cart:
            return "Your cart is empty. Please add some products to your cart before checking out."

        self.extra_buttons.append({"text": "💳 Checkout", "callback_data": "checkout"})
        return "Please click the button below to proceed to checkout on our website."

    async def show_categories(self):
        """Browse or show categories of products"""
        self.extra_buttons.append({"text": "👔 Browse Categories", "callback_data": "browse"})
        self.extra_buttons.append({"text": "🎁 Offers & Discounts", "callback_data": "offers"})
        categories = await get_categories(limit=20)

        if isinstance(categories, list) and categories:
            cat_names = [c["name"] for c in categories if c.get("count", 0) > 0][:10]
            return "Here are some of our clothing categories:\n" + ", ".join(cat_names) + "\n\nClick the buttons below to browse all categories or current offers!"
        return "Click the buttons below to browse all clothing categories."

    async def trigger_order_lookup(self):
        """Guide the user to check their order using the secure lookup form"""
        self.extra_buttons.append({"text": "📦 My Order", "callback_data": "my_order"})
        return "Please click the button below to check your order status using our secure lookup form."

    async def trigger_search(self):
        """Guide the user to search products using the manual search input"""
        self.extra_buttons.append({"text": "🔍 Search Products", "callback_data": "search"})
        return "Click the button below to search our products manually."

    async def get_help(self):
        """Show the support and help menu with FAQs"""
        self.extra_buttons.append({"text": "📞 Help & FAQ Menu", "callback_data": "help_menu"})
        return "Here is our customer care FAQ menu where you can find details about payments, shipping, returns, and support."

    async def order_lookup(self, order_id: str, email_or_phone: str):
        """Look up the status and details of an order. Both order ID and billing email or phone number are required for security."""
        if not order_id or not email_or_phone:
            return "Error: Both order number and billing email/phone are required to check order status."

        try:
            order = await woo_get(f"orders/{order_id}")
            if isinstance(order, dict) and "error" in order:
                return "No matching order found."

            billing_email = order.get("billing", {}).get("email", "").strip().lower()
            billing_phone = re.sub(r"[^\d\+]", "", order.get("billing", {}).get("phone", ""))

            clean_input = email_or_phone.strip().lower()
            clean_input_phone = re.sub(r"[^\d\+]", "", clean_input)

            is_match = False
            if billing_email and billing_email == clean_input:
                is_match = True
            elif billing_phone and clean_input_phone and len(clean_input_phone) >= 10:
                if billing_phone.endswith(clean_input_phone) or clean_input_phone.endswith(billing_phone):
                    is_match = True

            if not is_match:
                return "No matching order found."

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

            text = f"{status_emoji} *Order #{md(order_id)}*\n\nStatus: {md(status)}\nTotal: ৳{md(total)}\nDate: {md(date_created)}\n\n"

            items = order.get("line_items", [])
            if items:
                text += "Items:\n"
                for item in items:
                    text += f"  • {md(item.get('name', 'Item'))} (qty: {md(item.get('quantity', ''))})\n"

            from utils import get_tracking_info, get_pathao_tracking_status
            consignment_id, tracking_url = get_tracking_info(order)
            if consignment_id and tracking_url:
                text += f"\n🚚 *Courier Tracking*\nTracking ID: `{md(consignment_id)}`\n"
                if "pathao" in tracking_url.lower():
                    pathao_status = await get_pathao_tracking_status(consignment_id)
                    if pathao_status:
                        text += f"\n{pathao_status}"

                self.extra_buttons.append({"text": "🚚 Track Package", "url": tracking_url})

            return text
        except Exception as e:
            logger.error("Error in AI order lookup: %s", str(e))
            return "An error occurred while fetching your order details."

    async def _process_anthropic(self, client, model_name: str, dynamic_system_prompt: str) -> str:
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
            },
            {
                "name": "view_cart",
                "description": "View the current items in the user's shopping cart (returns list of items in cart and allows checking out)",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "checkout",
                "description": "Proceed to checkout the items in the cart",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "show_categories",
                "description": "Browse or list categories of products",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "trigger_order_lookup",
                "description": "Guide the user to track or check their order using the secure lookup form",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "trigger_search",
                "description": "Guide the user to search products manually",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "get_help",
                "description": "Show the support, payments, shipping, returns and customer care FAQ menu",
                "input_schema": {
                    "type": "object",
                    "properties": {}
                }
            },
            {
                "name": "order_lookup",
                "description": "Directly check the status and details of an order using its order ID and the billing email/phone for security.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "order_id": {
                            "type": "string",
                            "description": "The order ID/number"
                        },
                        "email_or_phone": {
                            "type": "string",
                            "description": "The billing email or billing phone number associated with the order"
                        }
                    },
                    "required": ["order_id", "email_or_phone"]
                }
            }
        ]

        # Call Claude with tools
        response = await client.messages.create(
            model=model_name,
            max_tokens=1000,
            system=dynamic_system_prompt,
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
                    elif tool_name == "view_cart":
                        result = await self.view_cart()
                    elif tool_name == "checkout":
                        result = await self.checkout()
                    elif tool_name == "show_categories":
                        result = await self.show_categories()
                    elif tool_name == "trigger_order_lookup":
                        result = await self.trigger_order_lookup()
                    elif tool_name == "trigger_search":
                        result = await self.trigger_search()
                    elif tool_name == "get_help":
                        result = await self.get_help()
                    elif tool_name == "order_lookup":
                        result = await self.order_lookup(
                            order_id=str(tool_input["order_id"]),
                            email_or_phone=str(tool_input["email_or_phone"])
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
            # Serialize Anthropic SDK content blocks to plain dicts for JSON-safe storage
            serialized_content = []
            for block in response.content:
                if hasattr(block, "type"):
                    block_dict = {"type": block.type}
                    if hasattr(block, "text"):
                        block_dict["text"] = block.text
                    if hasattr(block, "id"):
                        block_dict["id"] = block.id
                    if hasattr(block, "name"):
                        block_dict["name"] = block.name
                    if hasattr(block, "input"):
                        block_dict["input"] = block.input
                    serialized_content.append(block_dict)
                else:
                    serialized_content.append(str(block))

            self.conversation_history.append({
                "role": "assistant",
                "content": serialized_content
            })

            self.conversation_history.append({
                "role": "user",
                "content": tool_results
            })

            # Call Claude again with tool results
            response = await client.messages.create(
                model=model_name,
                max_tokens=1000,
                system=dynamic_system_prompt,
                tools=tools,
                messages=self.conversation_history
            )

        # Extract final text response
        final_response = ""
        for block in response.content:
            if hasattr(block, "text"):
                final_response += block.text

        if not final_response.strip():
            raise ValueError("Anthropic returned an empty response (no text blocks in final message).")

        # Add assistant response to history
        self.conversation_history.append({
            "role": "assistant",
            "content": final_response
        })

        return final_response

    async def _process_openai(self, client, model_name: str, dynamic_system_prompt: str) -> str:
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
            },
            {
                "type": "function",
                "function": {
                    "name": "view_cart",
                    "description": "View the current items in the user's shopping cart (returns list of items in cart and allows checking out)",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "checkout",
                    "description": "Proceed to checkout the items in the cart",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "show_categories",
                    "description": "Browse or list categories of products",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "trigger_order_lookup",
                    "description": "Guide the user to track or check their order using the secure lookup form",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "trigger_search",
                    "description": "Guide the user to search products manually",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "get_help",
                    "description": "Show the support, payments, shipping, returns and customer care FAQ menu",
                    "parameters": {
                        "type": "object",
                        "properties": {}
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "order_lookup",
                    "description": "Directly check the status and details of an order using its order ID and the billing email/phone for security.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "order_id": {
                                "type": "string",
                                "description": "The order ID/number"
                            },
                            "email_or_phone": {
                                "type": "string",
                                "description": "The billing email or billing phone number associated with the order"
                            }
                        },
                        "required": ["order_id", "email_or_phone"]
                    }
                }
            }
        ]

        # Call OpenAI with tools
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "system", "content": dynamic_system_prompt}] + self.conversation_history,
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

            assistant_dict = {
                "role": "assistant",
                "tool_calls": tool_calls_list
            }
            if assistant_msg.content is not None:
                assistant_dict["content"] = assistant_msg.content

            self.conversation_history.append(assistant_dict)

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
                    elif tool_name == "view_cart":
                        result = await self.view_cart()
                    elif tool_name == "checkout":
                        result = await self.checkout()
                    elif tool_name == "show_categories":
                        result = await self.show_categories()
                    elif tool_name == "trigger_order_lookup":
                        result = await self.trigger_order_lookup()
                    elif tool_name == "trigger_search":
                        result = await self.trigger_search()
                    elif tool_name == "get_help":
                        result = await self.get_help()
                    elif tool_name == "order_lookup":
                        result = await self.order_lookup(
                            order_id=str(tool_input["order_id"]),
                            email_or_phone=str(tool_input["email_or_phone"])
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
                messages=[{"role": "system", "content": dynamic_system_prompt}] + self.conversation_history,
                tools=tools,
                tool_choice="auto",
                max_tokens=1000
            )
            assistant_msg = response.choices[0].message

        # Final response
        final_response = (assistant_msg.content or "").strip()
        if not final_response:
            raise ValueError("OpenAI-compatible provider returned an empty response (no content in final message).")

        self.conversation_history.append({
            "role": "assistant",
            "content": final_response
        })
        return final_response

# Initialize agent
agent = RAGAgent(
    woocommerce_url=WOOCOMMERCE_URL,
    woocommerce_key=WOOCOMMERCE_KEY,
    woocommerce_secret=WOOCOMMERCE_SECRET
)
