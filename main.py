from fastapi import FastAPI, Request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes
import httpx
import os
from dotenv import load_dotenv
import logging

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# Config
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL")
WOOCOMMERCE_KEY = os.getenv("WOOCOMMERCE_KEY")
WOOCOMMERCE_SECRET = os.getenv("WOOCOMMERCE_SECRET")

# Check environment variables
if not all([TELEGRAM_BOT_TOKEN, WOOCOMMERCE_URL, WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET]):
    logger.error("Missing environment variables!")
    logger.error(f"TELEGRAM_BOT_TOKEN: {bool(TELEGRAM_BOT_TOKEN)}")
    logger.error(f"WOOCOMMERCE_URL: {bool(WOOCOMMERCE_URL)}")
    logger.error(f"WOOCOMMERCE_KEY: {bool(WOOCOMMERCE_KEY)}")
    logger.error(f"WOOCOMMERCE_SECRET: {bool(WOOCOMMERCE_SECRET)}")

application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

# ==================== WooCommerce API Helpers ====================

async def get_all_products(limit=20):
    """Fetch all products from WooCommerce"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/products",
                params={"per_page": limit, "orderby": "date", "order": "desc"},
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10
            )
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching products: {str(e)}")
        return {"error": str(e)}

async def get_product_by_id(product_id):
    """Fetch single product"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/products/{product_id}",
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10
            )
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching product {product_id}: {str(e)}")
        return {"error": str(e)}

async def search_products(keyword):
    """Search products by keyword"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/products",
                params={"search": keyword, "per_page": 10},
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10
            )
            return response.json()
    except Exception as e:
        logger.error(f"Error searching products: {str(e)}")
        return {"error": str(e)}

async def get_customer_orders(customer_email):
    """Fetch orders for a customer by email"""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{WOOCOMMERCE_URL}/wp-json/wc/v3/orders",
                params={"customer": customer_email},
                auth=(WOOCOMMERCE_KEY, WOOCOMMERCE_SECRET),
                timeout=10
            )
            return response.json()
    except Exception as e:
        logger.error(f"Error fetching orders for {customer_email}: {str(e)}")
        return {"error": str(e)}

# ==================== Telegram Handlers ====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start command - main menu"""
    keyboard = [
        [InlineKeyboardButton("👔 Browse Products", callback_data="browse")],
        [InlineKeyboardButton("🔍 Search", callback_data="search")],
        [InlineKeyboardButton("📦 My Orders", callback_data="my_orders")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await update.message.reply_text(
        "🎉 *Welcome to DeenCommerce!*\n\n"
        "Browse our fashion collection, check stock, and view your orders.",
        reply_markup=reply_markup,
        parse_mode="Markdown"
    )

async def browse_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show featured products"""
    query = update.callback_query
    await query.answer()
    
    try:
        products = await get_all_products(limit=5)
        
        if "error" in products:
            await query.edit_message_text(text=f"❌ Error: {products['error']}")
            return
        
        text = "📦 *Latest Products*\n\n"
        keyboard = []
        
        for product in products[:5]:
            stock = product.get('stock_quantity', 0)
            status = "✅ In Stock" if product.get('in_stock') else "❌ Out of Stock"
            
            text += f"*{product['name']}*\n"
            text += f"💰 ৳{product['price']}\n"
            text += f"📊 Stock: {stock} {status}\n\n"
            
            keyboard.append([
                InlineKeyboardButton(
                    f"View {product['name'][:20]}",
                    callback_data=f"product_{product['id']}"
                )
            ])
        
        keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
    
    except Exception as e:
        logger.error(f"Error in browse_products: {str(e)}")
        await query.edit_message_text(text=f"❌ Error: {str(e)}")

async def view_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show product details"""
    query = update.callback_query
    product_id = query.data.split("_")[1]
    
    await query.answer()
    
    try:
        product = await get_product_by_id(product_id)
        
        if "error" in product:
            await query.edit_message_text(text=f"❌ Error: {product['error']}")
            return
        
        stock = product.get('stock_quantity', 0)
        status = "✅ In Stock" if product.get('in_stock') else "❌ Out of Stock"
        
        text = f"*{product['name']}*\n\n"
        text += f"💰 Price: ৳{product['price']}\n"
        text += f"📊 Stock: {stock} {status}\n\n"
        
        # Description (first 300 chars)
        desc = product.get('description', 'No description')
        desc_clean = desc.replace('<p>', '').replace('</p>', '').replace('<br/>', '')
        text += f"📝 {desc_clean[:300]}...\n\n"
        
        keyboard = [
            [InlineKeyboardButton("← Back", callback_data="browse")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        
        # Send product image if available
        if product.get('images') and len(product['images']) > 0:
            try:
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=product['images'][0]['src'],
                    caption=f"_{product['name']}_",
                    parse_mode="Markdown"
                )
            except Exception as e:
                logger.warning(f"Could not send product image: {str(e)}")
    
    except Exception as e:
        logger.error(f"Error in view_product: {str(e)}")
        await query.edit_message_text(text=f"❌ Error: {str(e)}")

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle search command"""
    query = update.callback_query
    
    if query:
        await query.answer()
        await query.edit_message_text(
            text="🔍 *Search Products*\n\nType a product name (e.g., shirt, jeans, dress):",
            parse_mode="Markdown"
        )
        context.user_data['waiting_for_search'] = True
    else:
        # Text message with search query
        search_term = update.message.text
        context.user_data['waiting_for_search'] = False
        
        try:
            products = await search_products(search_term)
            
            if "error" in products or not products:
                await update.message.reply_text(f"❌ No products found for '{search_term}'")
                return
            
            text = f"🔍 *Search Results for '{search_term}'*\n\n"
            keyboard = []
            
            for product in products[:5]:
                stock = product.get('stock_quantity', 0)
                text += f"*{product['name']}*\n💰 ৳{product['price']}\n📊 Stock: {stock}\n\n"
                
                keyboard.append([
                    InlineKeyboardButton(
                        f"View {product['name'][:20]}",
                        callback_data=f"product_{product['id']}"
                    )
                ])
            
            keyboard.append([InlineKeyboardButton("← Back", callback_data="start_menu")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        
        except Exception as e:
            logger.error(f"Error in search_handler: {str(e)}")
            await update.message.reply_text(f"❌ Error: {str(e)}")

async def my_orders_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's orders"""
    query = update.callback_query
    
    if query:
        await query.answer()
        await query.edit_message_text(
            text="📦 *View Your Orders*\n\nPlease enter your email address (the one used for orders):",
            parse_mode="Markdown"
        )
        context.user_data['waiting_for_email'] = True

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle text input (search or email)"""
    user_text = update.message.text
    
    # Searching for products
    if context.user_data.get('waiting_for_search'):
        await search_handler(update, context)
    
    # Getting customer email for orders
    elif context.user_data.get('waiting_for_email'):
        context.user_data['waiting_for_email'] = False
        customer_email = user_text.strip()
        
        try:
            orders = await get_customer_orders(customer_email)
            
            if "error" in orders or not orders:
                await update.message.reply_text(
                    f"❌ No orders found for *{customer_email}*\n\nMake sure you use the exact email from your WooCommerce account.",
                    parse_mode="Markdown"
                )
                return
            
            text = f"📦 *Your Orders* ({len(orders)} total)\n\n"
            
            for order in orders:
                order_id = order['id']
                status = order['status'].upper()
                total = order['total']
                date = order['date_created'][:10]
                
                # Status emoji
                status_emoji = {
                    'PENDING': '⏳',
                    'PROCESSING': '🔄',
                    'ON-HOLD': '⏸️',
                    'COMPLETED': '✅',
                    'CANCELLED': '❌',
                    'REFUNDED': '🔄'
                }.get(status, '📦')
                
                text += f"{status_emoji} *Order #{order_id}*\n"
                text += f"Status: {status}\n"
                text += f"Total: ৳{total}\n"
                text += f"Date: {date}\n"
                
                # Items list
                items = order['line_items']
                text += "Items:\n"
                for item in items:
                    text += f"  • {item['name']} (qty: {item['quantity']})\n"
                
                text += "\n"
            
            keyboard = [[InlineKeyboardButton("← Back", callback_data="start_menu")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(text=text, reply_markup=reply_markup, parse_mode="Markdown")
        
        except Exception as e:
            logger.error(f"Error fetching orders: {str(e)}")
            await update.message.reply_text(f"❌ Error fetching orders: {str(e)}")

async def back_to_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to main menu"""
    query = update.callback_query
    await query.answer()
    await start(update, context)

# ==================== Register Handlers ====================

application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(browse_products, pattern="^browse$"))
application.add_handler(CallbackQueryHandler(search_handler, pattern="^search$"))
application.add_handler(CallbackQueryHandler(my_orders_handler, pattern="^my_orders$"))
application.add_handler(CallbackQueryHandler(view_product, pattern="^product_"))
application.add_handler(CallbackQueryHandler(back_to_menu, pattern="^start_menu$"))
application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

# ==================== FastAPI Routes ====================

_application_initialized = False

@app.post("/telegram/webhook")
async def webhook(request: Request):
    """Telegram webhook"""
    global _application_initialized
    
    try:
        # Initialize on first request
        if not _application_initialized:
            logger.info("Initializing Telegram application on first request...")
            try:
                await application.initialize()
                _application_initialized = True
                logger.info("Telegram application initialized!")
            except Exception as e:
                logger.error(f"Failed to initialize application: {str(e)}")
                return {"ok": False, "error": "Initialization failed"}
        
        data = await request.json()
        update = Update.de_json(data, application.bot)
        await application.process_update(update)
        return {"ok": True}
    except Exception as e:
        logger.error(f"Error processing update: {str(e)}")
        return {"ok": False, "error": str(e)}

@app.get("/")
async def root():
    return {"status": "Telegram bot running", "bot": "DeenCommerce"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)