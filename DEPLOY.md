# Quick Deployment Guide

This guide describes how to deploy the Telegram bot on **Render.com** and how to run it locally.

## Render.com Deployment

### 1. Create GitHub Repo
Initialize a Git repository and push your code to GitHub:
```bash
git init
git add .
git commit -m "Initial commit"
git push origin main
```

### 2. Deploy on Render
1. Go to [render.com](https://render.com) and log in.
2. Click **New +** and select **Web Service**.
3. Connect your GitHub account and select your `deen_telegram_bot` repository.
4. Configure the Web Service:
   - **Name**: `deen-telegram-bot`
   - **Region**: Choose a region closest to your WooCommerce server or users (e.g., `Singapore`).
   - **Branch**: `main`
   - **Runtime**: `Python` (or `Docker` to use the provided Dockerfile).
     - *If using Python:*
       - **Build Command**: `pip install -r requirements.txt`
       - **Start Command**: `uvicorn main:app --host 0.0.0.0 --port 8000`
     - *If using Docker:*
       - Render will automatically build the image using the [Dockerfile](file:///g:/deen_telegram_bot/Dockerfile).
   - **Instance Type**: `Free` (or custom tier).

### 3. Add Environment Variables
Scroll down to the **Environment** section, click **Add Environment Variable**, and configure these required keys:
* `TELEGRAM_BOT_TOKEN`: Your bot token from @BotFather
* `TELEGRAM_WEBHOOK_SECRET`: A secure random secret used to authenticate webhook updates (sent via `X-Telegram-Bot-Api-Secret-Token`)
* `WOOCOMMERCE_URL`: Your WooCommerce site URL (e.g., `https://example.com`)
* `WOOCOMMERCE_KEY`: WooCommerce Consumer Key (`ck_...`)
* `WOOCOMMERCE_SECRET`: WooCommerce Consumer Secret (`cs_...`)

### 4. Create Web Service
Click **Create Web Service** at the bottom of the page and wait for the deployment to complete.

### 5. Set Telegram Webhook
After the build succeeds and the service starts, copy your Render app URL (e.g., `https://deen-telegram-bot.onrender.com`).
Register the webhook with Telegram by sending a POST request (replace placeholders with actual values):

```bash
curl -X POST "https://api.telegram.org/botYOUR_TELEGRAM_BOT_TOKEN/setWebhook" \
  -d "url=https://YOUR_RENDER_DOMAIN.onrender.com/telegram/webhook" \
  -d "secret_token=YOUR_TELEGRAM_WEBHOOK_SECRET"
```

---

## Local Development

### Run Locally
```bash
# Create a virtual environment
python -m venv .venv

# Activate the virtual environment
# On Windows PowerShell:
.venv\Scripts\Activate.ps1
# On Windows CMD:
.venv\Scripts\activate.bat
# On macOS/Linux:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Copy and edit environment variables
cp .env.example .env
# Edit .env and fill in your credentials

# Run the bot server
python main.py
```

Server will run at `http://localhost:8000`

**Note:** For local testing without a public webhook, you can use a tunneling tool like **ngrok** to forward traffic to `http://localhost:8000/telegram/webhook`.

---

## Verify Bot is Working

After setting the webhook, test it by checking the webhook status:
```bash
curl "https://api.telegram.org/botYOUR_TELEGRAM_BOT_TOKEN/getWebhookInfo"
```

Expected response format:
```json
{
  "ok": true,
  "result": {
    "url": "https://YOUR_RENDER_DOMAIN.onrender.com/telegram/webhook",
    "has_custom_certificate": false,
    "pending_update_count": 0,
    "ip_address": "..."
  }
}
```

---

## Troubleshooting Deployment

### Bot not responding
1. Verify the webhook URL is registered correctly.
2. Check your Render logs for application start errors.
3. Confirm that all environment variables are correctly configured in the Render dashboard.

### 502 Bad Gateway
- The server might still be booting up. Wait 30 seconds and try again.
- Check Render logs for python startup errors.
- Ensure the start command uses host `0.0.0.0` and port `8000` (which matches Render's port exposure).

### Connection refused
- The bot server might be offline or failing health checks. Check the Render dashboard for service status.

---

## Update Bot Code

When you make changes to the code:
```bash
git add .
git commit -m "Update bot features"
git push origin main
```
Render will automatically detect the push and redeploy your service.

---

## Environment Variables Checklist

Ensure the following 5 variables are defined in the Render dashboard:
- [ ] `TELEGRAM_BOT_TOKEN` (from @BotFather)
- [ ] `TELEGRAM_WEBHOOK_SECRET` (custom webhook authorization secret)
- [ ] `WOOCOMMERCE_URL` (your store's URL)
- [ ] `WOOCOMMERCE_KEY` (WooCommerce API Consumer Key)
- [ ] `WOOCOMMERCE_SECRET` (WooCommerce API Consumer Secret)

All **5** variables are required for the bot to run.
