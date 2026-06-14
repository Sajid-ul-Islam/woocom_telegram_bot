import os
import logging
from datetime import datetime, timezone
from supabase import create_client, Client

logger = logging.getLogger(__name__)

url: str = os.getenv("SUPABASE_URL", "")
key: str = os.getenv("SUPABASE_KEY", "")

supabase: Client | None = None
if url and key:
    try:
        supabase = create_client(url, key)
        logger.info("Supabase client initialized.")
        # Auto-migrate: ensure required columns exist (safe to run multiple times)
        # If this fails (rpc not available), user must run the SQL manually.
        _migration_sql = (
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_subscribed BOOLEAN DEFAULT TRUE; "
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ; "
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS command_counts JSONB DEFAULT '{}';"
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_number VARCHAR;"
        )
        try:
            supabase.rpc("run_sql", {"query": _migration_sql}).execute()
            logger.info("DB migration completed.")
        except Exception:
            pass  # rpc may not be available; user must run migration manually
    except Exception as e:
        logger.error(f"Failed to initialize Supabase: {e}")


def upsert_user(user_id: int, first_name: str, phone_number: str = None):
    if not supabase:
        return
    try:
        data = {"id": user_id, "first_name": first_name}
        if phone_number:
            data["phone_number"] = phone_number
        supabase.table("users").upsert(data).execute()
    except Exception as e:
        logger.error(f"Failed to upsert user {user_id} to Supabase: {e}")


def get_user_history(user_id: int) -> list:
    if not supabase:
        return []
    try:
        response = supabase.table("users").select("chat_history").eq("id", user_id).execute()
        if response.data and len(response.data) > 0:
            history = response.data[0].get("chat_history")
            if isinstance(history, list):
                return history
    except Exception as e:
        logger.error(f"Failed to get history for user {user_id}: {e}")
    return []


def update_user_history(user_id: int, history: list):
    if not supabase:
        return
    try:
        supabase.table("users").update({"chat_history": history}).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to update history for user {user_id}: {e}")


def set_subscription(user_id: int, is_subscribed: bool):
    if not supabase:
        return
    try:
        supabase.table("users").update({"is_subscribed": is_subscribed}).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to update subscription for {user_id}: {e}")


def track_command(user_id: int, action: str):
    """Track a command or user action. Increments the count for 'action' and updates last_active."""
    if not supabase or not user_id:
        return
    try:
        resp = supabase.table("users").select("command_counts").eq("id", user_id).execute()
        counts = {}
        if resp.data:
            counts = resp.data[0].get("command_counts") or {}
        counts[action] = counts.get(action, 0) + 1
        supabase.table("users").update({
            "command_counts": counts,
            "last_active": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_id).execute()
    except Exception as e:
        logger.error(f"Failed to track action '{action}' for user {user_id}: {e}")
