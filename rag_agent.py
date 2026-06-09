import json
import os
import re
import html
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

# Initialize agent
agent = RAGAgent(
    woocommerce_url=WOOCOMMERCE_URL,
    woocommerce_key=WOOCOMMERCE_KEY,
    woocommerce_secret=WOOCOMMERCE_SECRET
)
