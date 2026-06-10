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
- Implement/maintain multi-provider AI fallback logic starting with the configured `AI_PROVIDER` and falling back sequentially.
- Ensure all final AI agent responses are accompanied by navigation buttons (`🗑️ Reset Chat` -> `reset_ai_chat`, `← Back to Menu` -> `start_menu`) for continuous chat UI/UX.
- Always include `max_tokens=1000` on OpenAI-compatible LLM completion calls to prevent token limit blocks.
- Welcome the customer dynamically using their first name (e.g. `Assalamu Alaikum {first_name}`) in the main menu, safely escaped using the `md` helper.
- Dynamically synchronize bot commands on startup with Telegram's `set_my_commands` API by parsing `application.handlers`.
- Return product website permalinks in RAG tool contexts and attach `View on Website` buttons to products, category lists, and search results.

## Validation

Run these before finishing changes:

```bash
python -m py_compile main.py
git diff --check
```

If dependencies are installed, also run an import check with dummy environment variables.
