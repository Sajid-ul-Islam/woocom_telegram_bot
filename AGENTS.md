# Agent Notes

## Project

This is a FastAPI webhook app for a Telegram bot that reads products, categories, and orders from WooCommerce.

## Runtime

- Python app entrypoint: `main.py`
- Dependency file: `requirements.txt`
- Local environment template: `.env.example`
- Docker entrypoint: `uvicorn main:app --host 0.0.0.0 --port 8000`

Required environment variables:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_WEBHOOK_SECRET`
- `WOOCOMMERCE_URL`
- `WOOCOMMERCE_KEY`
- `WOOCOMMERCE_SECRET`

## Development Rules

- Keep the bot database-free unless the user explicitly asks for persistence.
- Do not expose customer orders by email alone. Order lookup must require at least order number plus billing email.
- Keep Telegram webhook validation using `X-Telegram-Bot-Api-Secret-Token`.
- Escape user and WooCommerce text before sending Markdown messages.
- Use WooCommerce `stock_status` for availability. Show exact `stock_quantity` only when `manage_stock` is enabled and quantity is present.
- Prefer small, direct helper functions over large framework changes.

## Validation

Run these before finishing changes:

```bash
python -m py_compile main.py
git diff --check
```

If dependencies are installed, also run an import check with dummy environment variables.
