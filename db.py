import os
import logging
from supabase import create_client, Client

logger = logging.getLogger(__name__)

url: str = os.getenv("SUPABASE_URL", "")
key: str = os.getenv("SUPABASE_KEY", "")

supabase: Client | None = None
if url and key:
    try:
        supabase = create_client(url, key)
        logger.info("Supabase client initialized.")
    except Exception as e:
        logger.error(f"Failed to initialize Supabase: {e}")

def upsert_user(user_id: int, first_name: str, phone_number: str = None):
    if not supabase:
        return
    
    try:
        data = {
            "id": user_id,
            "first_name": first_name,
        }
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
