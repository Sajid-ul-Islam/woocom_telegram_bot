---
title: DeenCommerce Telegram Bot
emoji: 🤖
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 8000
pinned: false
---

# WooCommerce Telegram Bot

A simple Telegram bot for your WooCommerce store that allows customers to:
- ✅ Browse products and check stock
- ✅ Search for products
- ✅ View their existing orders

**Features:**
- No database required
- Direct integration with WooCommerce REST API
- Real-time product and order data
- Product category browsing with paginated product lists
- Support for Bangla text (৳ Taka currency)

---

## Setup Instructions

### Step 1: Create Telegram Bot

1. Open Telegram and message [@BotFather](https://t.me/botfather)
2. Type `/newbot` and follow the prompts
3. You'll receive a **Bot Token** (save this)
4. Example: `123456789:ABCDEFGHijklmnopqrstuvwxyz`

### Step 2: Get WooCommerce API Keys

1. Go to your WordPress Admin: `https://YourSiteName.com/wp-admin`
2. Navigate to: **WooCommerce** → **Settings** → **Advanced** → **REST API**
3. Click **Create an API key**
4. Set Permissions to **Read** (minimum)
5. Copy **Consumer Key** and **Consumer Secret**

### Step 3: Configure Environment

1. Copy `.env.example` to `.env`:
   ```bash
   cp .env.example .env
   ```

2. Edit `.env` and add your credentials:
   ```
   TELEGRAM_BOT_TOKEN=your_bot_token_from_botfather
   TELEGRAM_WEBHOOK_SECRET=change_this_to_a_long_random_string
   WOOCOMMERCE_URL=https://YourSiteName.com
   WOOCOMMERCE_KEY=your_consumer_key
   WOOCOMMERCE_SECRET=your_consumer_secret
   ```

### Step 4: Install Dependencies

```bash
pip install -r requirements.txt
```

### Step 5: Run Locally (Testing)

```bash
python main.py
```

You should see:
```
INFO:     Started server process
INFO:     Uvicorn running on http://0.0.0.0:8000
```

### Step 6: Deploy to Production (Render)

1. Go to [render.com](https://render.com)
2. Create a new **Web Service**
3. Connect your GitHub repository
4. Set build command: `pip install -r requirements.txt` (or select Docker to use the provided Dockerfile)
5. Set start command: `uvicorn main:app --host 0.0.0.0 --port 8000`
6. Add the 5 required environment variables (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_WEBHOOK_SECRET`, `WOOCOMMERCE_URL`, `WOOCOMMERCE_KEY`, `WOOCOMMERCE_SECRET`)
7. Click **Deploy Web Service**

### Step 7: Telegram Webhook Registration (Automatic)

The bot will automatically register the webhook with Telegram on startup using Render's built-in `RENDER_EXTERNAL_URL` environment variable. **No manual setup or curl commands are required!**

Once the deployment finishes and the service starts up, the webhook is automatically configured.

---

## Testing

1. Find your bot on Telegram (search for the username you set with @BotFather).
2. Start the bot: `/start`
3. Test features:
   - Click "Categories"
   - Select a category and page through products
   - Click "Search" and type a product name
   - Click "My Order" and enter your order number plus billing email

---

## Bot Features

### 📦 Browse Products
- Shows WooCommerce product categories
- Shows products by selected category with next/previous paging
- Displays price in ৳ (Taka)
- Shows real WooCommerce availability, and stock count when WooCommerce tracks product quantity
- Click to view full product details with image

### 🔍 Search
- Search products by keyword
- Enter any product name
- Results show price and stock

### 📦 My Order
- Enter your order number and billing email
- Shows only that matching order
- Displays order status (Pending, Processing, Completed, etc.)
- Shows items and total price

---

## Troubleshooting

### Bot not responding
- Check Telegram webhook URL is correct
- Verify bot token in `.env`
- Check server logs for errors

### "No products found"
- Verify WooCommerce API key is correct
- Check WooCommerce REST API is enabled
- Ensure products are published

### Stock looks wrong
- If WooCommerce does not manage stock quantity for a product, the bot shows availability instead of a number
- Enable **Manage stock** on the WooCommerce product if you want the bot to show the exact quantity
- For variable products, make sure stock is configured on the product or its variations

### "No matching order found"
- Customer must enter the exact order number and billing email
- Email must match the billing email on the WooCommerce order

### WooCommerce API errors
1. Go to WooCommerce → Settings → Advanced
2. Regenerate API key
3. Update `.env` file

---

## File Structure

```
deen_telegram_bot/
├── main.py              # Main bot application
├── requirements.txt     # Python dependencies
├── .env.example        # Environment variables template
├── .env                # Your actual credentials (don't commit)
└── README.md           # This file
```

---

## Future Enhancements

Add later (step by step):
- Database (SQLite/PostgreSQL)
- Shopping cart
- Order placement
- Payment integration (bKash/Nagad)
- Shipping integration (Pathao)
- AI product recommendations

---

## Support

For issues or questions:
1. Check Telegram logs: `/telegram/webhook` responses
2. Verify WooCommerce REST API is working
3. Test API manually: `curl https://YourSiteName.com/wp-json/wc/v3/products`

---

## Security Notes

⚠️ **Important:**
- Never commit `.env` file to GitHub
- Use `.gitignore`:
  ```
  .env
  __pycache__/
  *.pyc
  ```
- Keep your bot token and WooCommerce keys secret
- Keep `TELEGRAM_WEBHOOK_SECRET` secret and set it when registering the Telegram webhook
- Consider using read-only API keys for bot

---

**Bot Version:** 1.0  
**Last Updated:** 2026
