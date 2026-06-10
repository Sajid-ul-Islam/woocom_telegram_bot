import json
import os
from anthropic import Anthropic
from dotenv import load_dotenv
import httpx
from datetime import datetime

load_dotenv()

WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL", "").rstrip("/")
WOOCOMMERCE_KEY = os.getenv("WOOCOMMERCE_KEY")
WOOCOMMERCE_SECRET = os.getenv("WOOCOMMERCE_SECRET")

client = Anthropic()

# System prompt that defines the agent's behavior
SYSTEM_PROMPT = """You are an intelligent fashion shopping assistant for DeenCommerce, 
a Bangladeshi e-commerce store selling clothing and fashion items.

You have access to tools to:
1. Search products by keyword or category
2. Get product details (price, description, stock, images)
3. Look up customer orders
4. Add items to cart
5. Provide personalized recommendations

Your goals:
- Help customers find exactly what they're looking for
- Answer questions about products, prices, and availability
- Make personalized recommendations based on their needs
- Be conversational and friendly (in English or Bengali)
- Handle queries intelligently by using tools when needed

When recommending or listing products, always include their website link (permalink) so the customer can easily view/buy them on the website.

When a customer asks a question:
1. Understand their intent (searching, browsing, recommendation, etc.)
2. Decide which tools to use
3. Retrieve relevant information from our database
4. Provide a helpful, conversational response

Be concise in Telegram (max 1000 characters per message).
Use emojis to make responses engaging.
Always mention prices in ৳ (Taka).
"""

class RAGAgent:
    def __init__(self, woocommerce_url, woocommerce_key, woocommerce_secret):
        self.woo_url = woocommerce_url
        self.woo_key = woocommerce_key
        self.woo_secret = woocommerce_secret
        self.conversation_history = []
    
    async def search_products(self, query: str, limit: int = 5):
        """Search products by keyword"""
        async with httpx.AsyncClient(
            auth=(self.woo_key, self.woo_secret),
            timeout=10
        ) as client:
            response = await client.get(
                f"{self.woo_url}/wp-json/wc/v3/products",
                params={"search": query, "per_page": limit}
            )
            products = response.json()
            
            # Format for Claude
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
            
            return {
                "id": p["id"],
                "name": p["name"],
                "price": p["price"],
                "description": p.get("description", ""),
                "stock": p.get("stock_quantity", "N/A"),
                "categories": [c.get("name") for c in p.get("categories", [])],
                "images": [img["src"] for img in p.get("images", [])],
                "sku": p.get("sku", ""),
                "attributes": p.get("attributes", []),
                "permalink": p.get("permalink", "")
            }
    
    async def get_recommendations(self, category: str = None, price_range: str = None):
        """Get personalized product recommendations"""
        params = {"per_page": 5}
        
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
        """Process user message with RAG + Claude"""
        
        # Add user message to history
        self.conversation_history.append({
            "role": "user",
            "content": user_message
        })
        
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
        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
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
                
                print(f"🔧 Using tool: {tool_name} with input: {tool_input}")
                
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
            response = client.messages.create(
                model="claude-3-5-sonnet-20241022",
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

# Initialize agent
agent = RAGAgent(
    woocommerce_url=WOOCOMMERCE_URL,
    woocommerce_key=WOOCOMMERCE_KEY,
    woocommerce_secret=WOOCOMMERCE_SECRET
)