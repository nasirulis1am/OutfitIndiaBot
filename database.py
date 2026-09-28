from supabase import create_client, Client
from config import SUPABASE_URL, SUPABASE_KEY

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# ---------- CATEGORIES ----------

def get_categories():
    response = (
        supabase
        .table("categories")
        .select("*")
        .eq("is_active", True)
        .order("display_order")
        .execute()
    )
    return response.data


# ---------- PRODUCTS ----------

def get_products(category_id=None):
    query = (
        supabase
        .table("products")
        .select("*")
        .eq("is_active", True)
        .order("created_at", desc=True)
    )

    if category_id:
        query = query.eq("category_id", category_id)

    response = query.execute()
    return response.data


def get_product(product_id):
    response = (
        supabase
        .table("products")
        .select("*")
        .eq("id", product_id)
        .single()
        .execute()
    )
    return response.data


# ---------- ADMIN ----------

def is_admin(telegram_user_id):
    response = (
        supabase
        .table("admins")
        .select("id")
        .eq("telegram_user_id", telegram_user_id)
        .eq("is_active", True)
        .execute()
    )

    return len(response.data) > 0
