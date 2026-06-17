# CRM Agent Rules & Verification Checklist

This document details the rules, design guidelines, verification checklist, and platform constraints for the DeenCommerce CRM Shopping Assistant Bot.

---

## 🤖 CRM Bot Rules & Guidelines

1. **Assalamu Alaikum Greeting**: Welcome the customer dynamically using their first name (e.g. `Assalamu Alaikum {first_name}`) in the main menu, safely escaped using the `md` helper.
2. **AI Continuous UI/UX**: Ensure all final AI agent responses are accompanied by navigation buttons (`🗑️ Reset Chat` -> `reset_ai_chat`, `← Back to Menu` -> `start_menu`).
3. **No Order Exposure**: Do not expose customer orders by email alone. Order lookup must require at least the order number plus billing email/phone.
4. **Markdown Escaping**: Escape user and WooCommerce text using the `md` helper before sending Markdown messages to prevent parsing errors.
5. **WooCommerce Stock Rules**: Use WooCommerce `stock_status` for availability. Show exact `stock_quantity` only when `manage_stock` is enabled and quantity is present.
6. **AI Provider Fallbacks**: Implement and maintain multi-provider AI fallback logic starting with the configured `AI_PROVIDER` and falling back sequentially.
7. **Store Outlet Info**: The AI agent and the `/help` menu must dynamically fetch the WooCommerce physical store address using `get_store_address()` to ensure accurate contact info.
8. **Token Limits**: Always include `max_tokens=1000` on OpenAI-compatible LLM completion calls to prevent token limit blocks.
9. **Startup Commands Sync**: Dynamically synchronize bot commands on startup with Telegram's `set_my_commands` API by parsing `application.handlers`.
10. **Website Links**: Return product website permalinks in RAG tool contexts and attach `View on Website` / `View Shop on Website` buttons to products, category lists, and search results.

---

## ✅ Rule Verification Checklist

| Rule | Status | Implementation Details |
| :--- | :---: | :--- |
| **1. Dynamic Welcome Greeting** | **Met** | `main_menu()` in [main.py](file:///h:/Repo/deen_telegram_bot/main.py) formats greeting dynamically and escapes first name via `md()`. |
| **2. Continuous AI UI/UX Buttons** | **Met** | AI responses in `ai_chat_handler()` are appended with inline keyboard buttons for `reset_ai_chat` and `start_menu`. |
| **3. Secure Order Lookup** | **Met** | `order_lookup()` in [rag_agent.py](file:///h:/Repo/deen_telegram_bot/rag_agent.py) and text lookup handlers in [main.py](file:///h:/Repo/deen_telegram_bot/main.py) enforce order ID matching billing email or phone. |
| **4. Safe Markdown Escaping** | **Met** | Utilizes the `md()` helper (wrapping `escape_markdown`) globally for WooCommerce data injection. |
| **5. Stock & Quantity Logic** | **Met** | `stock_display()` in [utils.py](file:///h:/Repo/deen_telegram_bot/utils.py) prefers `stock_status` and only shows count if `manage_stock` is `True`. |
| **6. Multi-Provider Fallbacks** | **Met** | `get_providers_chain()` cascading sequence tries OpenRouter -> Groq -> OpenAI -> Anthropic -> xAI -> Gemini. |
| **7. Dynamic Outlet Info** | **Met** | `/help` commands and RAG prompts dynamically invoke `get_store_address()` in [utils.py](file:///h:/Repo/deen_telegram_bot/utils.py). |
| **8. Token Limit Settings** | **Met** | `max_tokens=1000` is strictly configured on all OpenAI-compatible LLM completions. |
| **9. Commands Auto-Sync** | **Met** | Handlers are automatically registered and synchronized in `lifespan` startup on [main.py](file:///h:/Repo/deen_telegram_bot/main.py). |
| **10. Product Website Permalinks** | **Met** | Permalinks are included in search/semantic outputs. `View on Website` / `View Shop on Website` buttons are attached. |

---

## ☁️ Render.com Capability & Hosting Optimization

Render.com Web Services are fully capable of hosting this FastAPI bot, but have specific platform behaviors that have been optimized:

* **Ephemeral Filesystem**: Render filesystems are ephemeral. Local SQLite or cache files will be wiped on restarts/redeploys.
  - *Optimization*: The bot uses **Supabase PostgreSQL** (`pgvector`) for storing user info, chat logs, and vector embeddings. No state is kept locally on Render disk.
* **Sleep Cycles (Free Instance Tier)**: Free services sleep after 15 minutes of inactivity. When a message is sent, the server takes ~30 seconds to wake up (cold start).
  - *Optimization*: Webhook handling is robust, and the bot registers the webhook automatically on startup using Render's `RENDER_EXTERNAL_URL`.
* **lifespan Startup Timeouts**: Render shuts down services that do not start accepting traffic within a short boot window. Running heavy sync code like FastEmbed embedding generation during startup blocks boot.
  - *Optimization*: We implemented a database existence check (`check_embeddings_exist()`). If embeddings are already stored in Supabase, heavy startup processes are skipped. If missing, they run in a non-blocking background task (`asyncio.create_task(run_initial_indexing())`), allowing immediate boot.

---

## 💬 WhatsApp Integration Assessment

Integrating with **WhatsApp** has strict constraints that make it unavailable on standard free hosting services like Render:

1. **Official Meta API Requirements**: Requiring official business verification, phone numbers registered with Meta Business, and pre-approved messaging templates (paid model).
2. **Third-Party Gateways (Twilio/Gupshup)**: Requires paid subscriptions and specific SDK handlers that are outside the scope of the current webhook bot logic.
3. **No Webhook Polling/Free Gateways**: Unlike Telegram which offers a free API, WhatsApp webhook verification uses specific challenge parameters (`hub.mode`, `hub.challenge`) which would require writing a custom Meta Webhook verification route.

*Conclusion*: WhatsApp-specific messaging capabilities are skipped and disabled, keeping the focus on the fully functional Telegram bot.
