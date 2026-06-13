import os
import time
import re
import html
import logging
import httpx
from telegram.helpers import escape_markdown
from dotenv import load_dotenv

load_dotenv()

# Config
WOOCOMMERCE_URL = os.getenv("WOOCOMMERCE_URL", "").rstrip("/")
WOOCOMMERCE_KEY = os.getenv("WOOCOMMERCE_KEY")
WOOCOMMERCE_SECRET = os.getenv("WOOCOMMERCE_SECRET")

logger = logging.getLogger(__name__)

# Global HTTP client to reuse TCP/TLS connections
http_client = None
store_address_cache = None

class SimpleCache:
    def __init__(self, ttl_seconds=3600):
        self.ttl = ttl_seconds
        self.store = {}

    def get(self, key):
        if key in self.store:
            data, timestamp = self.store[key]
            if time.time() - timestamp < self.ttl:
                return data
            else:
                del self.store[key]
        return None

    def set(self, key, value):
        self.store[key] = (value, time.time())

    def clear(self):
        self.store.clear()

categories_cache = SimpleCache(ttl_seconds=3600)  # 1 hour
products_cache = SimpleCache(ttl_seconds=1800)    # 30 minutes
pathao_status_cache = SimpleCache(ttl_seconds=900)  # 15 minutes
pathao_token_cache = SimpleCache(ttl_seconds=7200)   # 2 hours

SYNONYMS_MAP = {
    # Bangla terms
    "জামা": "shirt",
    "শার্ট": "shirt",
    "প্যান্ট": "pants",
    "পাঞ্জাবি": "panjabi",
    "মানিব্যাগ": "wallet",
    "গেঞ্জি": "t-shirt",

    # Banglish terms
    "jama": "shirt",
    "shart": "shirt",
    "shurt": "shirt",
    "pant": "pants",
    "pants": "pants",
    "tshirt": "t-shirt",
    "t-shirt": "t-shirt",
    "teeshirt": "t-shirt",
    "genji": "t-shirt",
    "panjabi": "panjabi",
    "punjabi": "panjabi",
    "wallet": "wallet",
    "moneybag": "wallet",
    "bag": "wallet",
    "polo": "polo",
    "half sleeve": "half sleeve",
    "halfshirt": "half sleeve",
}

def preprocess_search_query(query):
    if not query:
        return ""
    q_clean = query.strip().lower()
    if q_clean in SYNONYMS_MAP:
        return SYNONYMS_MAP[q_clean]
    words = q_clean.split()
    mapped_words = [SYNONYMS_MAP.get(w, w) for w in words]
    return " ".join(mapped_words)

async def get_store_address():
    return (
        "DEEN Mirpur 12 Outlet\n"
        "📍 ৩য় তলা, রমজান্নেছা সুপার মার্কেট, মিরপুর ১২, ঢাকা।\n"
        "📞 01972 627 981\n"
        "🕐 প্রতিদিন সকাল ১০টা থেকে রাত ১০টা পর্যন্ত। (সাপ্তাহিক বন্ধ রবিবার)\n"
        "গুগল ম্যাপঃ https://g.co/kgs/3pCJkAZ\n\n"
        "DEEN Wari Outlet\n"
        "📍 Ground Floor, 41 A.K Famous Tower, Rankin Street, Wari, Dhaka 1203.\n"
        "📞 01972-627983\n"
        "🕐 প্রতিদিন সকাল ১০টা থেকে রাত ১০টা পর্যন্ত। (সাপ্তাহিক বন্ধ রবিবার)\n"
        "গুগল ম্যাপঃ https://g.co/kgs/Cu71N8U\n\n"
        "DEEN Cumilla Outlet\n"
        "📍 4th floor, QR Tower, F56H+PF5, QR Tower, Badurtola, Cumilla.\n"
        "📞 01972 627984\n"
        "🕐 প্রতিদিন সকাল ১০টা থেকে রাত ১০টা পর্যন্ত। (সাপ্তাহিক বন্ধ শুক্রবার)\n"
        "গুগল ম্যাপঃ https://g.co/kgs/Dav6rNx\n\n"
        "DEEN Sylhet Outlet\n"
        "📍 Block-A, House-54/2, Kumar Para, Sylhet\n"
        "📞 01972-627985\n"
        "🕐 প্রতিদিন সকাল ১০টা থেকে রাত ১০টা পর্যন্ত। (সাপ্তাহিক বন্ধ শুক্রবার)\n"
        "গুগল ম্যাপঃ https://g.co/kgs/QsvRbtH"
    )

async def get_pathao_tracking_status(consignment_id):
    cache_key = f"pathao_{consignment_id}"
    cached_status = pathao_status_cache.get(cache_key)
    if cached_status is not None:
        logger.info("Using cached Pathao tracking status for consignment: %s", consignment_id)
        return cached_status

    try:
        base_url = os.getenv("PATHAO_BASE_URL", "https://api-hermes.pathao.com").rstrip("/")
        client_id = os.getenv("PATHAO_CLIENT_ID")
        client_secret = os.getenv("PATHAO_CLIENT_SECRET")
        username = os.getenv("PATHAO_USERNAME")
        password = os.getenv("PATHAO_PASSWORD")

        if not all([client_id, client_secret, username, password]):
            return None

        async with httpx.AsyncClient(timeout=4.0) as client:
            async def _get_token():
                """Request a fresh Pathao auth token."""
                token_resp = await client.post(f"{base_url}/aladdin/api/v1/issue-token", json={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "username": username,
                    "password": password,
                    "grant_type": "password"
                })
                if token_resp.status_code != 200:
                    return None
                new_token = token_resp.json().get("access_token")
                if new_token:
                    pathao_token_cache.set("auth_token", new_token)
                return new_token

            token = pathao_token_cache.get("auth_token")
            if not token:
                logger.info("Pathao auth token not cached. Requesting a new one.")
                token = await _get_token()

            if not token:
                return None

            async def _do_track(tkn):
                headers = {"Authorization": f"Bearer {tkn}", "Accept": "application/json"}
                return await client.get(
                    f"{base_url}/aladdin/api/v1/packages/{consignment_id}/track",
                    headers=headers
                )

            track_resp = await _do_track(token)

            # On 401, clear stale token and retry once with a fresh one
            if track_resp.status_code == 401:
                logger.warning("Pathao token 401 — refreshing and retrying...")
                pathao_token_cache.store.pop("auth_token", None)
                token = await _get_token()
                if not token:
                    return None
                track_resp = await _do_track(token)

            if track_resp.status_code != 200:
                return None

            data = track_resp.json()
            if data.get("error") and "Unauthorized" in data.get("message", ""):
                logger.warning("Pathao track API returned Unauthorized, clearing cached token.")
                pathao_token_cache.store.pop("auth_token", None)
                return None

            track_data = data.get("data", {})
            status = track_data.get("status", "Unknown")
            history = track_data.get("history", [])

            text = f"📍 *Pathao Courier Status*: {md(status.upper())}\n"
            if history:
                text += "*Tracking History*:\n"
                for h in history[:5]:
                    time_str = h.get("time", "")
                    desc = h.get("description", h.get("status", ""))
                    text += f"  • _{md(time_str)}_: {md(desc)}\n"

            if text:
                pathao_status_cache.set(cache_key, text)
            return text
    except Exception as e:
        logger.error("Error fetching Pathao tracking status: %s", str(e))
        return None

def get_tracking_info(order):
    if not isinstance(order, dict):
        return None, None
    meta_data = order.get("meta_data", [])
    consignment_id = None
    provider = None

    for meta in meta_data:
        key = str(meta.get("key", "")).lower()
        value = str(meta.get("value", "")).strip()
        if not value:
            continue

        if "ptc_consignment_id" in key or "pathao_consignment" in key:
            consignment_id = value
            provider = "Pathao"
            break
        elif "steadfast_consignment" in key or "steadfast_id" in key:
            consignment_id = value
            provider = "Steadfast"
            break
        elif "consignment_id" in key or "tracking_number" in key or "tracking_code" in key:
            consignment_id = value
            if "pathao" in key or value.upper().startswith("DD"):
                provider = "Pathao"
            elif "steadfast" in key:
                provider = "Steadfast"
            else:
                provider = "Courier"
            break

    if consignment_id:
        if provider == "Pathao":
            url = f"https://merchant.pathao.com/tracking?consignment_id={consignment_id}"
        elif provider == "Steadfast":
            url = f"https://steadfast.com.bd/t/{consignment_id}"
        else:
            if consignment_id.upper().startswith("DD"):
                url = f"https://merchant.pathao.com/tracking?consignment_id={consignment_id}"
                provider = "Pathao"
            else:
                url = f"https://steadfast.com.bd/t/{consignment_id}"
                provider = "Steadfast"
        return consignment_id, url
    return None, None

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
        header_title = f"📏 *{md(md_rows[0][0])}*"
        start_idx = 1
    elif len(md_rows[0]) == 1:
        return f"📏 *{md(md_rows[0][0])}*"

    rows_to_format = md_rows[start_idx:]
    if not rows_to_format:
        return header_title

    if len(rows_to_format) > 1:
        headers = rows_to_format[0]
        data_rows = rows_to_format[1:]
        
        lines = []
        if header_title:
            lines.append(header_title)
            
        for r in data_rows:
            primary_val = r[0] if len(r) > 0 else ""
            primary_head = headers[0] if len(headers) > 0 else ""
            
            lines.append(f"▪ *{md(primary_head)}: {md(primary_val)}*")
            
            details = []
            for col_idx in range(1, len(r)):
                head = headers[col_idx] if col_idx < len(headers) else f"Col{col_idx}"
                val = r[col_idx]
                details.append(f"{md(head)}: {md(val)}")
            
            if details:
                lines.append("   " + " • ".join(details))
            lines.append("")
            
        return "\n".join(lines).strip()
    else:
        # Just one row, print it normally
        return " \\| ".join(md(c) for c in rows_to_format[0])

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

def md(value):
    """Escape dynamic values before interpolating into Telegram Markdown."""
    return escape_markdown("" if value is None else str(value), version=1)

def strip_html(value):
    text = html.unescape(str(value or ""))
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", text).strip()

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
