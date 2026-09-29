import os
import time
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from config import BOT_TOKEN, ADMIN_ID
from database import supabase

PHOTO, NAME, PRICE, CATEGORY, PLATFORM, LINK = range(6)

def main_menu(is_admin=False):
    buttons = []
    row = []
    # hidden categories are not shown; names come from the categories table
    for key, label in get_main_menu_labels():
        row.append(InlineKeyboardButton(label, callback_data=key))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(buttons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = str(user.id) == str(ADMIN_ID)
    await update.message.reply_text(
        f"Hi {user.first_name}! 👋\n\nWelcome to Outfit India! ✨\n\n"
        "Discover stylish outfits, seasonal clothing and great fashion deals from trusted shopping platforms.\n\n"
        "What are you looking for today?",
        reply_markup=main_menu(is_admin)
    )

async def show_admin_panel(query):
    keyboard = [
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add")],
        [InlineKeyboardButton("📦 Manage Products", callback_data="admin_products")],
        [InlineKeyboardButton("📂 Manage Categories", callback_data="admin_categories")],
        [InlineKeyboardButton("📊 Dashboard", callback_data="admin_dashboard")],
        [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")]
    ]
    await query.edit_message_text(
        "⚙️ Outfit India Admin Panel\n\nManage your products and categories from Telegram.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    context.user_data.clear()
    await query.edit_message_text("➕ Add Product\n\nStep 1/6\n\n🖼️ Send the product photo.\n\nSend /cancel anytime to stop.")
    return PHOTO

async def receive_photo(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    if not update.message.photo:
        await update.message.reply_text("⚠️ Please send a product photo.")
        return PHOTO
    context.user_data["image_url"] = update.message.photo[-1].file_id
    await update.message.reply_text("✅ Photo received.\n\nStep 2/6\n\n📝 Now send the product name.")
    return NAME

async def receive_name(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    name = update.message.text.strip()
    if not name:
        await update.message.reply_text("⚠️ Please enter a valid product name.")
        return NAME
    context.user_data["name"] = name
    await update.message.reply_text("Step 3/6\n\n💰 Send the sale price.\n\nExample: 499")
    return PRICE

async def receive_price(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    try:
        price = float(update.message.text.strip())
        if price < 0: raise ValueError
    except ValueError:
        await update.message.reply_text("⚠️ Please enter only a valid price.\n\nExample: 499")
        return PRICE
    context.user_data["sale_price"] = price
    buttons = add_flow_category_buttons()
    await update.message.reply_text("Step 4/6\n\n📂 Choose the main category:", reply_markup=InlineKeyboardMarkup(buttons))
    return CATEGORY

async def show_category_buttons(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END

    categories = {"men":"Men's Fashion", "women":"Women's Fashion", "seasonal":"Seasonal Wear", "deals":"Deals & Offers"}
    key = query.data.replace("addcat_", "")
    category_name = categories.get(key)
    if not category_name:
        await query.edit_message_text("❌ Invalid category.")
        return CATEGORY

    try:
        info = get_category_info(key, create=True)
        if not info: raise Exception("Category could not be loaded.")
        category_id = info["id"]
        category_name = info["name"]
    except Exception as e:
        print("CATEGORY LOAD ERROR:", e)
        await query.edit_message_text("❌ Could not save this category.\n\nPlease choose the category again.")
        return CATEGORY

    context.user_data["category_id"] = category_id
    context.user_data["category_name"] = category_name
    context.user_data["category_key"] = key

    try:
        sub_names = [r["name"] for r in get_subcategory_rows(key, category_id)]
    except Exception as e:
        print("SUBCATEGORY LOAD ERROR:", e)
        sub_names = [value for _label, value in MANAGE_SUBCATEGORIES[key]]
    items = [(sub_label(key, n), n) for n in sub_names]
    buttons = []
    for i in range(0, len(items), 2):
        buttons.append([InlineKeyboardButton(label, callback_data=f"addsub_{key}_{value}") for label, value in items[i:i+2]])
    buttons.append([InlineKeyboardButton("🔙 Back to Categories", callback_data="add_category_back")])
    await query.edit_message_text(f"📂 {category_name}\n\nChoose a subcategory:", reply_markup=InlineKeyboardMarkup(buttons))
    return CATEGORY

async def receive_subcategory(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    parts = query.data.split("_", 2)
    if len(parts) < 3:
        await query.edit_message_text("❌ Invalid subcategory.")
        return CATEGORY
    context.user_data["subcategory"] = parts[2]
    await query.edit_message_text("Step 5/6\n\n🛍️ Send the shopping platform.\n\nExample:\nAmazon\nFlipkart\nMyntra")
    return PLATFORM

async def category_back(update, context):
    query = update.callback_query
    await query.answer()
    buttons = add_flow_category_buttons()
    await query.edit_message_text("Step 4/6\n\n📂 Choose the main category:", reply_markup=InlineKeyboardMarkup(buttons))
    return CATEGORY

async def receive_platform(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    platform = update.message.text.strip()
    if not platform:
        await update.message.reply_text("⚠️ Please enter the platform name.")
        return PLATFORM
    context.user_data["platform"] = platform
    await update.message.reply_text("Step 6/6\n\n🔗 Now send the affiliate/product link.")
    return LINK

async def receive_link(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    link = update.message.text.strip()
    if not link.startswith(("http://","https://")):
        await update.message.reply_text("⚠️ Please send a valid link starting with http:// or https://")
        return LINK
    data = {
        "name":context.user_data["name"],
        "category_id":context.user_data["category_id"],
        "subcategory":context.user_data["subcategory"],
        "image_url":context.user_data["image_url"],
        "platform":context.user_data["platform"],
        "sale_price":context.user_data["sale_price"],
        "affiliate_link":link,
        "is_active":True
    }
    try:
        result = supabase.table("products").insert(data).execute()
        if not result.data: raise Exception("Product was not returned by Supabase.")
        await update.message.reply_text(
            "✅ PRODUCT PUBLISHED!\n\n"
            f"🛍️ {context.user_data['name']}\n"
            f"💰 ₹{context.user_data['sale_price']}\n"
            f"📂 {context.user_data['category_name']}\n"
            f"🗂️ {context.user_data['subcategory']}\n"
            f"🏪 {context.user_data['platform']}\n\n"
            "The product is now saved in your catalogue."
        )
    except Exception as e:
        print("PRODUCT INSERT ERROR:", e)
        await update.message.reply_text("❌ Product could not be saved.\n\nPlease try again.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_add_product(update, context):
    context.user_data.clear()
    await update.message.reply_text("❌ Add Product cancelled.")
    return ConversationHandler.END

PRODUCT_SUBCATEGORIES = {
    "men_jeans": "Jeans",
    "men_shirts": "Shirts",
    "men_tshirts": "T-Shirts",
    "men_jackets": "Jackets",
    "men_sweaters": "Sweaters & Hoodies",
    "men_shoes": "Shoes",
    "women_tops": "Tops & T-Shirts",
    "women_pants": "Jeans & Pants",
    "women_dresses": "Dresses",
    "women_jackets": "Jackets & Coats",
    "women_skirts": "Skirts & Shorts",
    "women_shoes": "Shoes",
    "winter": "Winter Collection",
    "summer": "Summer Collection",
    "deal_500": "Under ₹500",
    "deal_1000": "Under ₹1000",
    "top_rated": "Top Rated",
    "new_arrivals": "New Arrivals",
}

MAIN_MENU_TEXT = "What are you looking for today? 👇"


def main_menu_button():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Main Menu", callback_data="main")]])


# Parent category of every product callback (derived from callback_data, so the
# ambiguous "Shoes" subcategory of Men and Women gets the correct parent)
def parent_from_callback(callback_data):
    if callback_data.startswith("sub_"):
        key = callback_data[len("sub_"):].partition("_")[0]
        return key if key in CATEGORY_ORDER else None
    if callback_data.startswith("men_"):
        return "men"
    if callback_data.startswith("women_"):
        return "women"
    if callback_data in ("winter", "summer"):
        return "seasonal"
    if callback_data in ("deal_500", "deal_1000", "top_rated", "new_arrivals"):
        return "deals"
    return None


PARENT_BACK_LABELS = {
    "men": "🔙 Back to Men's Fashion",
    "women": "🔙 Back to Women's Fashion",
    "seasonal": "🔙 Back to Seasonal Wear",
    "deals": "🔙 Back to Deals & Offers",
}


# Same category names that the Add Product flow stores in the "categories" table
PARENT_CATEGORY_NAMES = {
    "men": "Men's Fashion",
    "women": "Women's Fashion",
    "seasonal": "Seasonal Wear",
    "deals": "Deals & Offers",
}


def get_category_id(parent_key):
    """Look up the real category ID (categories.id) for a parent category key.
    Returns None if the category does not exist yet (so no products can exist)."""
    info = get_category_info(parent_key)
    return info["id"] if info else None


def back_button(parent_key, header_id=None, label=None):
    """Category-specific Back button (never goes to main).
    Product messages use the short label "🔙 Back" and carry the listing header's
    message ID in the callback data, so the header can be deleted too."""
    callback = f"back_{parent_key}" if header_id is None else f"back_{parent_key}_{header_id}"
    return InlineKeyboardButton(label or PARENT_BACK_LABELS[parent_key], callback_data=callback)


def category_screen(key):
    """Return (text, keyboard) for a main category's subcategory screen (customer side)."""
    if key not in CATEGORY_ORDER:
        return None, None
    info = safe_category_info(key)
    name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
    try:
        names = visible_subcategory_names(key, info["id"] if info else None)
    except Exception as e:
        print("SUBCATEGORY LOAD ERROR:", e)
        names = [value for _label, value in MANAGE_SUBCATEGORIES[key]]

    buttons = [InlineKeyboardButton(sub_label(key, n), callback_data=f"sub_{key}_{n}") for n in names]
    per_row = 2 if key in ("men", "women") else 1
    keyboard = [buttons[i:i + per_row] for i in range(0, len(buttons), per_row)]
    keyboard.append([InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")])

    if key == "men":
        text = f"Here's what you can explore in {name} 👇"
    elif key == "women":
        text = f"Explore {name} 👇"
    elif key == "seasonal":
        text = "Choose your season 👇"
    else:
        text = "🔥 Limited time offers!\n\nFind your favourite outfits at great prices."
    return text, InlineKeyboardMarkup(keyboard)


async def safe_edit(query, text, reply_markup=None):
    """Edit a text message. If the message is a photo (product card) or cannot be
    edited, send a new message instead so the user is never stuck."""
    msg = query.message
    if msg is not None and (msg.photo or not msg.text):
        await msg.reply_text(text, reply_markup=reply_markup)
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        print("EDIT ERROR:", e)
        if msg is not None:
            await msg.reply_text(text, reply_markup=reply_markup)


async def delete_message_safe(message):
    """Delete only the given message. Never raises, so navigation always continues."""
    if message is None:
        return False
    try:
        await message.delete()
        return True
    except Exception as e:
        print("DELETE ERROR:", e)
        return False


async def delete_by_id_safe(bot, chat_id, message_id):
    """Delete one message by ID. Never raises."""
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        print("DELETE BY ID ERROR:", e)
        return False


async def send_header(query, text):
    """Show the listing header WITHOUT any keyboard and return its message ID."""
    msg = query.message
    if msg is not None and not msg.photo and msg.text:
        try:
            edited = await query.edit_message_text(text)
            return getattr(edited, "message_id", msg.message_id)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return msg.message_id
            print("HEADER EDIT ERROR:", e)
    sent = await msg.reply_text(text)
    return sent.message_id


async def show_main_menu(query, is_admin):
    await safe_edit(query, MAIN_MENU_TEXT, main_menu(is_admin))


async def show_products_by_subcategory(query, subcategory, parent_key=None):
    if parent_key:
        back_markup = InlineKeyboardMarkup([[back_button(parent_key)]])
    else:
        back_markup = main_menu_button()

    try:
        info = get_category_info(parent_key) if parent_key else None
        category_id = info["id"] if info else None
        if category_is_hidden(info):
            await safe_edit(query, "🚫 This category is currently unavailable.", main_menu_button())
            return
        if category_id is not None and is_subcategory_hidden(category_id, subcategory):
            await safe_edit(query, "🚫 This subcategory is currently unavailable.", back_markup)
            return
        if category_id is None:
            products = []
        else:
            result = (
                supabase
                .table("products")
                .select("id,name,sale_price,platform,affiliate_link,image_url")
                .eq("category_id", category_id)
                .eq("subcategory", subcategory)
                .eq("is_active", True)
                .execute()
            )
            products = result.data or []
    except Exception as e:
        print("PRODUCT FETCH ERROR:", e)
        await safe_edit(query, "❌ Could not load products.", back_markup)
        return

    if not products:
        await safe_edit(
            query,
            f"🛍️ {subcategory}\n\nNo products have been added here yet.",
            back_markup
        )
        return

    header_id = await send_header(query, f"🛍️ {subcategory}\n\nFound {len(products)} product(s).")

    for product in products:
        name = product.get("name", "Product")
        price = product.get("sale_price", 0)
        platform = product.get("platform", "")
        link = product.get("affiliate_link")
        image_url = product.get("image_url")

        caption = f"🛍️ {name}\n\n💰 ₹{price}\n🏪 {platform}"

        keyboard = []
        if link:
            keyboard.append([InlineKeyboardButton("🛒 View Product", url=link)])
        keyboard.append([back_button(parent_key, header_id, "🔙 Back")] if parent_key else [InlineKeyboardButton("🔙 Main Menu", callback_data="main")])
        markup = InlineKeyboardMarkup(keyboard)

        try:
            if image_url:
                await query.message.reply_photo(photo=image_url, caption=caption, reply_markup=markup)
            else:
                await query.message.reply_text(caption, reply_markup=markup)
        except Exception as e:
            print("DISPLAY ERROR:", e)
            await query.message.reply_text(caption, reply_markup=markup)


async def handle_product_category(query):
    if query.data.startswith("sub_"):
        key, _sep, name = query.data[len("sub_"):].partition("_")
        if key in CATEGORY_ORDER and name:
            await show_products_by_subcategory(query, name, key)
            return True
        return False
    subcategory = PRODUCT_SUBCATEGORIES.get(query.data)
    if subcategory:
        await show_products_by_subcategory(query, subcategory, parent_from_callback(query.data))
        return True
    return False


async def main_callback(update, context):
    """Fallback inside the Add Product conversation: Main Menu ends it cleanly."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await show_main_menu(query, str(query.from_user.id) == str(ADMIN_ID))
    return ConversationHandler.END


async def button_handler(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    is_admin = str(query.from_user.id) == str(ADMIN_ID)

    # 1. Main menu FIRST - must never be intercepted by anything else
    if data == "main":
        await show_main_menu(query, is_admin)
        return

    # 2. Admin Manage Products (admin-only, checked inside the handler)
    if data == "admin_products" or data.startswith("mp_"):
        await handle_manage_callback(query, context, data)
        return

    # 2a. Admin Manage Categories (admin-only, checked inside the handler)
    if data == "admin_categories" or data.startswith("mc_"):
        await handle_category_admin_callback(query, context, data)
        return

    # 2b. Product subcategory screens
    if await handle_product_category(query):
        return

    # 3. Category screens (also used by the product "Back" buttons)
    category_key = None
    header_id = None
    if data in ("men", "women", "seasonal", "deals"):
        category_key = data
    elif data.startswith("back_"):
        parts = data.split("_")
        if len(parts) >= 2 and parts[1] in ("men", "women", "seasonal", "deals"):
            category_key = parts[1]
            if len(parts) >= 3 and parts[2].isdigit():
                header_id = int(parts[2])

    if category_key:
        if category_is_hidden(safe_category_info(category_key)):
            await safe_edit(query, "🚫 This category is currently unavailable.", main_menu_button())
            return
        text, markup = category_screen(category_key)
        msg = query.message
        if data.startswith("back_") and msg is not None:
            # Delete the listing header that belongs to this product (if known)
            if header_id is not None and header_id != msg.message_id:
                await delete_by_id_safe(context.bot, msg.chat_id, header_id)
            if msg.photo:
                # Delete ONLY this product message, then show the parent category
                await delete_message_safe(msg)
                await context.bot.send_message(chat_id=msg.chat_id, text=text, reply_markup=markup)
            else:
                await safe_edit(query, text, markup)
        else:
            await safe_edit(query, text, markup)
        return

    # 4. Admin screens (admin_add is handled ONLY by the ConversationHandler)
    if data == "admin":
        if not is_admin:
            await safe_edit(query, "⛔ You don't have permission to access the Admin Panel.")
            return
        await show_admin_panel(query)
    elif data == "admin_dashboard":
        await safe_edit(query, "📊 Dashboard\n\nDashboard will be added next.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]))
    else:
        await safe_edit(query, "🛍️ Products will appear here soon.", main_menu_button())


# ---------------------------------------------------------------------------
# ADMIN PANEL -> MANAGE PRODUCTS
# Callback prefixes: admin_products, mp_page_, mp_view_, mp_edit_, mp_editback_,
# mp_ef_ (edit field, ConversationHandler entry), mp_hide_, mp_show_, mp_move_,
# mp_mvc_, mp_mvs_, mp_delete_, mp_confirm_delete_, mp_cancel_delete_
# ---------------------------------------------------------------------------

EDIT_VALUE = 10
MANAGE_PAGE_SIZE = 5

MANAGE_CATEGORY_LABELS = [
    ("men", "👔 Men's Fashion"),
    ("women", "👗 Women's Fashion"),
    ("seasonal", "🌦️ Seasonal Wear"),
    ("deals", "🏷️ Deals & Offers"),
]

MANAGE_SUBCATEGORIES = {
    "men": [("👖 Jeans", "Jeans"), ("👔 Shirts", "Shirts"), ("👕 T-Shirts", "T-Shirts"),
            ("🧥 Jackets", "Jackets"), ("🧶 Sweaters & Hoodies", "Sweaters & Hoodies"), ("👟 Shoes", "Shoes")],
    "women": [("👕 Tops & T-Shirts", "Tops & T-Shirts"), ("👖 Jeans & Pants", "Jeans & Pants"),
              ("👗 Dresses", "Dresses"), ("🧥 Jackets & Coats", "Jackets & Coats"),
              ("🩳 Skirts & Shorts", "Skirts & Shorts"), ("👠 Shoes", "Shoes")],
    "seasonal": [("❄️ Winter Collection", "Winter Collection"), ("☀️ Summer Collection", "Summer Collection")],
    "deals": [("💰 Under ₹500", "Under ₹500"), ("💎 Under ₹1000", "Under ₹1000"),
              ("⭐ Top Rated", "Top Rated"), ("🆕 New Arrivals", "New Arrivals")],
}

# edit field -> (database column, label)
EDIT_FIELDS = {
    "photo": ("image_url", "Product photo"),
    "name": ("name", "Product name"),
    "price": ("sale_price", "Sale price"),
    "platform": ("platform", "Shopping platform"),
    "link": ("affiliate_link", "Affiliate/product link"),
}

PRODUCT_COLUMNS = "id,name,category_id,subcategory,image_url,platform,sale_price,affiliate_link,is_active"


def mp_back(callback_data):
    """Every Back button inside Manage Products says exactly "🔙 Back"."""
    return InlineKeyboardButton("🔙 Back", callback_data=callback_data)


def fmt_price(value):
    try:
        number = float(value)
        return str(int(number)) if number == int(number) else str(number)
    except (TypeError, ValueError):
        return str(value)


def get_category_names():
    """Return {category_id: category_name} from the existing categories table."""
    try:
        result = supabase.table("categories").select("id,name").execute()
        return {row["id"]: row["name"] for row in (result.data or [])}
    except Exception as e:
        print("CATEGORY NAMES ERROR:", e)
        return {}


def get_or_create_category_id(key):
    """Find the real category ID (by display_order, so renamed categories still match).
    Only creates the category if it does not exist yet, so duplicates are never created."""
    info = get_category_info(key, create=True)
    if not info:
        return None
    return info["id"]


def fetch_product(product_id):
    result = supabase.table("products").select(PRODUCT_COLUMNS).eq("id", product_id).limit(1).execute()
    return result.data[0] if result.data else None


def product_text(product, names):
    category = names.get(product.get("category_id"), "Unknown category")
    status = "🟢 Active" if product.get("is_active") else "🔴 Hidden"
    return (
        f"🛍️ {product.get('name', 'Product')}\n"
        f"💰 ₹{fmt_price(product.get('sale_price', 0))}\n"
        f"📂 {category} → {product.get('subcategory', '-')}\n"
        f"🏪 {product.get('platform', '-')}\n"
        f"{status}"
    )


async def deny_non_admin(query):
    await safe_edit(query, "⛔ You don't have permission to use this.")


async def show_manage_list(query, context, page=0, notice=None):
    page = max(0, page)
    context.user_data["mp_page"] = page
    offset = page * MANAGE_PAGE_SIZE
    prefix = f"{notice}\n\n" if notice else ""

    try:
        result = (
            supabase
            .table("products")
            .select(PRODUCT_COLUMNS)
            .order("id")
            .range(offset, offset + MANAGE_PAGE_SIZE)
            .execute()
        )
        rows = result.data or []
    except Exception as e:
        print("MANAGE LIST ERROR:", e)
        await safe_edit(query, prefix + "❌ Could not load products.", InlineKeyboardMarkup([[mp_back("admin")]]))
        return

    if not rows and page > 0:
        await show_manage_list(query, context, page - 1, notice)
        return

    if not rows:
        await safe_edit(query, prefix + "📦 Manage Products\n\nNo products found yet.", InlineKeyboardMarkup([[mp_back("admin")]]))
        return

    has_next = len(rows) > MANAGE_PAGE_SIZE
    rows = rows[:MANAGE_PAGE_SIZE]
    names = get_category_names()

    lines = [f"{prefix}📦 Manage Products (page {page + 1})\n"]
    keyboard = []
    for i, product in enumerate(rows):
        number = offset + i + 1
        lines.append(f"{number}. {product_text(product, names)}\n")
        icon = "🟢" if product.get("is_active") else "🔴"
        label = f"{number}. {str(product.get('name', 'Product'))[:30]} {icon}"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"mp_view_{product['id']}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"mp_page_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"mp_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([mp_back("admin")])

    lines.append("Tap a product to manage it 👇")
    await safe_edit(query, "\n".join(lines), InlineKeyboardMarkup(keyboard))


async def show_product_page(query, context, product_id, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    list_page = context.user_data.get("mp_page", 0)
    try:
        product = fetch_product(product_id)
    except Exception as e:
        print("PRODUCT LOAD ERROR:", e)
        product = None
    if not product:
        await show_manage_list(query, context, list_page, "❌ Product not found.")
        return

    names = get_category_names()
    toggle = (
        InlineKeyboardButton("👁️ Hide", callback_data=f"mp_hide_{product_id}")
        if product.get("is_active")
        else InlineKeyboardButton("👁️ Show", callback_data=f"mp_show_{product_id}")
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Edit", callback_data=f"mp_edit_{product_id}"), toggle],
        [InlineKeyboardButton("📂 Move", callback_data=f"mp_move_{product_id}"),
         InlineKeyboardButton("🗑️ Delete", callback_data=f"mp_delete_{product_id}")],
        [mp_back(f"mp_page_{list_page}")],
    ]
    await safe_edit(query, f"{prefix}📦 Manage Product\n\n{product_text(product, names)}", InlineKeyboardMarkup(keyboard))


async def show_edit_menu(query, context, product_id):
    try:
        product = fetch_product(product_id)
    except Exception as e:
        print("PRODUCT LOAD ERROR:", e)
        product = None
    if not product:
        await show_manage_list(query, context, context.user_data.get("mp_page", 0), "❌ Product not found.")
        return
    keyboard = [
        [InlineKeyboardButton("🖼️ Photo", callback_data=f"mp_ef_photo_{product_id}"),
         InlineKeyboardButton("📝 Name", callback_data=f"mp_ef_name_{product_id}")],
        [InlineKeyboardButton("💰 Price", callback_data=f"mp_ef_price_{product_id}"),
         InlineKeyboardButton("🏪 Platform", callback_data=f"mp_ef_platform_{product_id}")],
        [InlineKeyboardButton("🔗 Link", callback_data=f"mp_ef_link_{product_id}")],
        [mp_back(f"mp_view_{product_id}")],
    ]
    text = (
        "✏️ Edit Product\n\n"
        f"📝 Name: {product.get('name', '-')}\n"
        f"💰 Price: ₹{fmt_price(product.get('sale_price', 0))}\n"
        f"🏪 Platform: {product.get('platform', '-')}\n"
        f"🔗 Link: {product.get('affiliate_link', '-')}\n\n"
        "Which field do you want to edit?"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_move_categories(query, product_id):
    keyboard = []
    row = []
    for key, label in get_main_menu_labels(include_hidden=True):
        row.append(InlineKeyboardButton(label, callback_data=f"mp_mvc_{product_id}_{key}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([mp_back(f"mp_view_{product_id}")])
    await safe_edit(query, "📂 Move Product\n\nChoose the new main category:", InlineKeyboardMarkup(keyboard))


async def show_move_subcategories(query, product_id, key):
    if key not in CATEGORY_ORDER:
        await show_move_categories(query, product_id)
        return
    try:
        names = subcategory_choice_names(key)
    except Exception as e:
        print("SUBCATEGORY LOAD ERROR:", e)
        names = [value for _label, value in MANAGE_SUBCATEGORIES[key]]
    keyboard = []
    for i in range(0, len(names), 2):
        keyboard.append([
            InlineKeyboardButton(sub_label(key, name), callback_data=f"mp_mvs_{product_id}_{key}_{idx}")
            for idx, name in list(enumerate(names))[i:i + 2]
        ])
    keyboard.append([mp_back(f"mp_move_{product_id}")])
    await safe_edit(query, f"📂 {category_display_name(key)}\n\nChoose the new subcategory:", InlineKeyboardMarkup(keyboard))


async def handle_manage_callback(query, context, data):
    """Single entry point for every Manage Products callback. Admin-only."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return

    list_page = context.user_data.get("mp_page", 0)

    if data == "admin_products":
        await show_manage_list(query, context, 0)
    elif data.startswith("mp_page_"):
        page_text = data[len("mp_page_"):]
        await show_manage_list(query, context, int(page_text) if page_text.isdigit() else 0)
    elif data.startswith("mp_view_"):
        await show_product_page(query, context, data[len("mp_view_"):])
    elif data.startswith("mp_edit_"):
        await show_edit_menu(query, context, data[len("mp_edit_"):])
    elif data.startswith("mp_editback_"):
        await show_edit_menu(query, context, data[len("mp_editback_"):])
    elif data.startswith("mp_hide_") or data.startswith("mp_show_"):
        make_active = data.startswith("mp_show_")
        product_id = data.split("_", 2)[2]
        try:
            supabase.table("products").update({"is_active": make_active}).eq("id", product_id).execute()
        except Exception as e:
            print("VISIBILITY UPDATE ERROR:", e)
            await show_product_page(query, context, product_id, "❌ Could not update the product.")
            return
        notice = "✅ Product is visible again." if make_active else "🚫 Product hidden successfully."
        await show_product_page(query, context, product_id, notice)
    elif data.startswith("mp_move_"):
        await show_move_categories(query, data[len("mp_move_"):])
    elif data.startswith("mp_mvc_"):
        product_id, key = data[len("mp_mvc_"):].rsplit("_", 1)
        await show_move_subcategories(query, product_id, key)
    elif data.startswith("mp_mvs_"):
        try:
            product_id, key, idx_text = data[len("mp_mvs_"):].rsplit("_", 2)
            subcategory = subcategory_choice_names(key)[int(idx_text)]
        except Exception:
            await show_manage_list(query, context, list_page, "❌ Invalid subcategory.")
            return
        try:
            category_id = get_or_create_category_id(key)
            supabase.table("products").update(
                {"category_id": category_id, "subcategory": subcategory}
            ).eq("id", product_id).execute()
        except Exception as e:
            print("MOVE PRODUCT ERROR:", e)
            await show_product_page(query, context, product_id, "❌ Could not move the product.")
            return
        await show_product_page(query, context, product_id, "✅ Product moved successfully.")
    elif data.startswith("mp_delete_"):
        product_id = data[len("mp_delete_"):]
        try:
            product = fetch_product(product_id)
        except Exception as e:
            print("PRODUCT LOAD ERROR:", e)
            product = None
        if not product:
            await show_manage_list(query, context, list_page, "❌ Product not found.")
            return
        keyboard = [[
            InlineKeyboardButton("🗑️ Delete", callback_data=f"mp_confirm_delete_{product_id}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"mp_cancel_delete_{product_id}"),
        ]]
        await safe_edit(
            query,
            f"⚠️ Delete this product?\n\n🛍️ {product.get('name', 'Product')}\n"
            f"💰 ₹{fmt_price(product.get('sale_price', 0))}\n\nThis cannot be undone.",
            InlineKeyboardMarkup(keyboard)
        )
    elif data.startswith("mp_confirm_delete_"):
        product_id = data[len("mp_confirm_delete_"):]
        try:
            supabase.table("products").delete().eq("id", product_id).execute()
        except Exception as e:
            print("DELETE PRODUCT ERROR:", e)
            await show_product_page(query, context, product_id, "❌ Could not delete the product.")
            return
        await show_manage_list(query, context, list_page, "✅ Product deleted successfully.")
    elif data.startswith("mp_cancel_delete_"):
        await show_product_page(query, context, data[len("mp_cancel_delete_"):])


# ----- Edit one field at a time (ConversationHandler) -----

async def edit_field_start(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return ConversationHandler.END

    try:
        field, product_id = query.data[len("mp_ef_"):].split("_", 1)
    except ValueError:
        return ConversationHandler.END
    if field not in EDIT_FIELDS:
        return ConversationHandler.END

    try:
        product = fetch_product(product_id)
    except Exception as e:
        print("PRODUCT LOAD ERROR:", e)
        product = None
    if not product:
        await show_manage_list(query, context, context.user_data.get("mp_page", 0), "❌ Product not found.")
        return ConversationHandler.END

    column, label = EDIT_FIELDS[field]
    clear_category_input(context)
    context.user_data["mp_edit_field"] = field
    context.user_data["mp_edit_id"] = product_id

    if field == "photo":
        prompt = f"🖼️ Edit {label}\n\nSend the new product photo."
    elif field == "price":
        prompt = f"💰 Edit {label}\n\nCurrent: ₹{fmt_price(product.get(column))}\n\nSend the new price. Example: 499"
    elif field == "link":
        prompt = f"🔗 Edit {label}\n\nCurrent: {product.get(column)}\n\nSend the new link (must start with http:// or https://)."
    else:
        prompt = f"✏️ Edit {label}\n\nCurrent: {product.get(column)}\n\nSend the new value."
    prompt += "\n\nSend /cancel to stop."

    await safe_edit(query, prompt, InlineKeyboardMarkup([[mp_back(f"mp_editback_{product_id}")]]))
    return EDIT_VALUE


async def receive_edit_value(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    if context.user_data.get("mc_action"):
        return await receive_category_input(update, context)

    field = context.user_data.get("mp_edit_field")
    product_id = context.user_data.get("mp_edit_id")
    if field not in EDIT_FIELDS or not product_id:
        return ConversationHandler.END

    column, _label = EDIT_FIELDS[field]
    message = update.message

    if field == "photo":
        if not message.photo:
            await message.reply_text("⚠️ Please send a product photo.")
            return EDIT_VALUE
        value = message.photo[-1].file_id
    else:
        text = (message.text or "").strip()
        if not text:
            await message.reply_text("⚠️ Please send text for this field.")
            return EDIT_VALUE
        if field == "price":
            try:
                value = float(text)
                if value < 0:
                    raise ValueError
            except ValueError:
                await message.reply_text("⚠️ Please enter only a valid price.\n\nExample: 499")
                return EDIT_VALUE
        elif field == "link":
            if not text.startswith(("http://", "https://")):
                await message.reply_text("⚠️ Please send a valid link starting with http:// or https://")
                return EDIT_VALUE
            value = text
        else:
            value = text

    try:
        supabase.table("products").update({column: value}).eq("id", product_id).execute()
    except Exception as e:
        print("EDIT PRODUCT ERROR:", e)
        await message.reply_text("❌ Product could not be updated.\n\nPlease try again.")
        return EDIT_VALUE

    context.user_data.pop("mp_edit_field", None)
    context.user_data.pop("mp_edit_id", None)
    list_page = context.user_data.get("mp_page", 0)
    await message.reply_text(
        "✅ Product updated successfully.",
        reply_markup=InlineKeyboardMarkup([[mp_back(f"mp_page_{list_page}")]])
    )
    return ConversationHandler.END


async def cancel_edit_product(update, context):
    was_category = bool(context.user_data.get("mc_action"))
    context.user_data.pop("mp_edit_field", None)
    context.user_data.pop("mp_edit_id", None)
    clear_category_input(context)
    list_page = context.user_data.get("mp_page", 0)
    back = "mc_categories" if was_category else f"mp_page_{list_page}"
    await update.message.reply_text(
        "❌ Edit cancelled.",
        reply_markup=InlineKeyboardMarkup([[mp_back(back)]])
    )
    return ConversationHandler.END


async def edit_escape(update, context):
    """While editing, any navigation button ends the edit and then performs that action."""
    query = update.callback_query
    await query.answer()
    context.user_data.pop("mp_edit_field", None)
    context.user_data.pop("mp_edit_id", None)
    clear_category_input(context)
    data = query.data
    is_admin = str(query.from_user.id) == str(ADMIN_ID)

    if data == "main":
        await show_main_menu(query, is_admin)
    elif data == "admin":
        if is_admin:
            await show_admin_panel(query)
        else:
            await deny_non_admin(query)
    else:
        await dispatch_admin_callback(query, context, data)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# CATEGORY / SUBCATEGORY REGISTRY  +  ADMIN PANEL -> MANAGE CATEGORIES
#
# Main categories live in the existing `categories` table. Because a category can
# now be renamed, categories are identified by their existing `display_order`
# value (men=1, women=2, seasonal=3, deals=4), NOT by name. Rows created by the
# old code (display_order 0) are matched once by their default name and then
# updated to the right display_order automatically.
#
# Subcategories have no table in the current schema (only products.subcategory).
# Renaming / adding / hiding / deleting them needs a place to store them, so an
# OPTIONAL table is supported. Without it the bot behaves exactly as before
# (default subcategories + any subcategory names already used by products) and the
# subcategory editing screens are read-only.
#
#   create table if not exists public.subcategories (
#     id uuid primary key default gen_random_uuid(),
#     category_id uuid not null,          -- same type as categories.id
#     name text not null,
#     is_active boolean not null default true,
#     display_order integer not null default 0,
#     unique (category_id, name)
#   );
# ---------------------------------------------------------------------------

CATEGORY_ORDER = {"men": 1, "women": 2, "seasonal": 3, "deals": 4}
CATEGORY_EMOJI = {"men": "👔", "women": "👗", "seasonal": "🌦️", "deals": "🏷️"}
CATEGORY_NAME_MAX_LEN = 40
MC_ENTRY_PATTERN = r"^mc_(edit_|sub_edit_|sub_add_)"  # ConversationHandler entry callbacks
SUBCATEGORY_NAME_MAX_BYTES = 40  # keeps callback_data under Telegram's 64-byte limit

DEFAULT_SUBCATEGORY_LABELS = {
    (key, value): label
    for key, items in MANAGE_SUBCATEGORIES.items()
    for label, value in items
}

_SUB_TABLE_STATE = {"ok": None, "checked": 0.0}


# ----- main categories -----

def load_category_map(migrate=False):
    """Return {key: category_row} for the four main categories."""
    rows = supabase.table("categories").select("id,name,is_active,display_order").execute().data or []
    result = {}
    used = set()
    for key, order in CATEGORY_ORDER.items():
        match = next((r for r in rows if r.get("display_order") == order and r["id"] not in used), None)
        if match is None:
            default_name = PARENT_CATEGORY_NAMES[key]
            match = next((r for r in rows if r.get("name") == default_name and r["id"] not in used), None)
            if match is not None and migrate:
                try:
                    supabase.table("categories").update({"display_order": order}).eq("id", match["id"]).execute()
                    match["display_order"] = order
                except Exception as e:
                    print("CATEGORY ORDER MIGRATION ERROR:", e)
        if match is not None:
            used.add(match["id"])
            result[key] = match
    return result


def get_category_info(key, create=False):
    """Real category row (id, name, is_active) for a main category key."""
    if key not in CATEGORY_ORDER:
        return None
    info = load_category_map(migrate=True).get(key)
    if info or not create:
        return info
    created = supabase.table("categories").insert(
        {"name": PARENT_CATEGORY_NAMES[key], "display_order": CATEGORY_ORDER[key], "is_active": True}
    ).execute()
    if not created.data:
        raise Exception("Category was not returned by Supabase.")
    return created.data[0]


def safe_category_info(key):
    try:
        return get_category_info(key)
    except Exception as e:
        print("CATEGORY INFO ERROR:", e)
        return None


def category_display_name(key):
    info = safe_category_info(key)
    return info["name"] if info else PARENT_CATEGORY_NAMES.get(key, key)


def category_is_hidden(info):
    return bool(info) and info.get("is_active") is False


def get_main_menu_labels(include_hidden=False):
    """[(key, "👔 Name")] for the main categories. Hidden ones are skipped for customers."""
    try:
        cmap = load_category_map()
    except Exception as e:
        print("CATEGORY MAP ERROR:", e)
        cmap = {}
    labels = []
    for key in CATEGORY_ORDER:
        info = cmap.get(key)
        if category_is_hidden(info) and not include_hidden:
            continue
        name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
        labels.append((key, f"{CATEGORY_EMOJI[key]} {name}"))
    return labels


def add_flow_category_buttons():
    """Category buttons for the Add Product flow (all categories, callback addcat_<key>)."""
    buttons = []
    row = []
    for key, label in get_main_menu_labels(include_hidden=True):
        row.append(InlineKeyboardButton(label, callback_data=f"addcat_{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return buttons


# ----- subcategories -----

def sub_label(key, name):
    return DEFAULT_SUBCATEGORY_LABELS.get((key, name), f"📁 {name}")


def subcategory_table_ready():
    if _SUB_TABLE_STATE["ok"] is True:
        return True
    if _SUB_TABLE_STATE["ok"] is False and time.time() - _SUB_TABLE_STATE["checked"] < 60:
        return False
    try:
        supabase.table("subcategories").select("id").limit(1).execute()
        _SUB_TABLE_STATE["ok"] = True
    except Exception:
        _SUB_TABLE_STATE["ok"] = False
    _SUB_TABLE_STATE["checked"] = time.time()
    return _SUB_TABLE_STATE["ok"]


def product_subcategory_names(category_id):
    result = supabase.table("products").select("subcategory").eq("category_id", category_id).execute()
    names = []
    for row in result.data or []:
        name = row.get("subcategory")
        if name and name not in names:
            names.append(name)
    return names


def get_subcategory_rows(key, category_id):
    """Subcategories of a main category as [{id, name, is_active, display_order}] in a stable order."""
    defaults = [value for _label, value in MANAGE_SUBCATEGORIES[key]]

    def plain(names):
        return [{"id": None, "name": n, "is_active": True, "display_order": i} for i, n in enumerate(names)]

    if category_id is None:
        return plain(defaults)

    if subcategory_table_ready():
        try:
            def load():
                return (
                    supabase.table("subcategories")
                    .select("id,category_id,name,is_active,display_order")
                    .eq("category_id", category_id)
                    .order("display_order")
                    .order("name")
                    .execute()
                    .data or []
                )
            rows = load()
            if not rows:
                # first use of this category: register the defaults + names products already use
                names = defaults + [n for n in product_subcategory_names(category_id) if n not in defaults]
                try:
                    supabase.table("subcategories").insert(
                        [{"category_id": category_id, "name": n, "is_active": True, "display_order": i}
                         for i, n in enumerate(names)]
                    ).execute()
                except Exception as e:
                    print("SUBCATEGORY SEED ERROR:", e)
                rows = load()
            if rows:
                return rows
        except Exception as e:
            print("SUBCATEGORY LOAD ERROR:", e)

    try:
        extras = [n for n in product_subcategory_names(category_id) if n not in defaults]
    except Exception as e:
        print("SUBCATEGORY DERIVE ERROR:", e)
        extras = []
    return plain(defaults + extras)


def visible_subcategory_names(key, category_id):
    return [r["name"] for r in get_subcategory_rows(key, category_id) if r.get("is_active") is not False]


def subcategory_choice_names(key):
    """All subcategory names (including hidden ones) in stable order - used by admin pickers."""
    info = get_category_info(key, create=True)
    return [r["name"] for r in get_subcategory_rows(key, info["id"])]


def is_subcategory_hidden(category_id, name):
    if not subcategory_table_ready():
        return False
    try:
        result = (
            supabase.table("subcategories").select("is_active")
            .eq("category_id", category_id).eq("name", name).limit(1).execute()
        )
        return bool(result.data) and result.data[0].get("is_active") is False
    except Exception as e:
        print("SUBCATEGORY HIDDEN CHECK ERROR:", e)
        return False


def get_subcategory_row(sid):
    result = (
        supabase.table("subcategories").select("id,category_id,name,is_active")
        .eq("id", sid).limit(1).execute()
    )
    return result.data[0] if result.data else None


def key_for_category_id(category_id):
    for key, info in load_category_map().items():
        if info["id"] == category_id:
            return key
    return None


def category_has_products(category_id, subcategory=None):
    query = supabase.table("products").select("id").eq("category_id", category_id)
    if subcategory is not None:
        query = query.eq("subcategory", subcategory)
    return bool(query.limit(1).execute().data)


def validate_category_name(text):
    if not text:
        return "⚠️ Please send a category name."
    if len(text) > CATEGORY_NAME_MAX_LEN:
        return f"⚠️ Please use at most {CATEGORY_NAME_MAX_LEN} characters."
    return None


def validate_subcategory_name(text):
    if not text:
        return "⚠️ Please send a subcategory name."
    if "\n" in text:
        return "⚠️ Please send the name on a single line."
    if len(text.encode("utf-8")) > SUBCATEGORY_NAME_MAX_BYTES:
        return "⚠️ That name is too long. Please use a shorter name (about 25 characters)."
    return None


# ----- admin screens (each returns (text, InlineKeyboardMarkup)) -----

def status_text(active):
    return "🟢 Active" if active is not False else "🔴 Hidden"


def categories_admin_screen(notice=None):
    try:
        cmap = load_category_map()
    except Exception as e:
        print("CATEGORY MAP ERROR:", e)
        cmap = {}
    lines = [f"{notice}\n\n" if notice else "", "📂 Manage Categories\n"]
    keyboard = []
    for key in CATEGORY_ORDER:
        info = cmap.get(key)
        name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
        label = f"{CATEGORY_EMOJI[key]} {name}"
        status = status_text(info.get("is_active")) if info else "⚪ Not created yet"
        lines.append(f"{label}\n{status}\n")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"mc_view_{key}")])
    lines.append("Select a category to manage it 👇")
    keyboard.append([mp_back("admin")])
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def category_admin_screen(key, notice=None):
    info = get_category_info(key, create=True)
    prefix = f"{notice}\n\n" if notice else ""
    active = info.get("is_active") is not False
    toggle = (
        InlineKeyboardButton("👁️ Hide", callback_data=f"mc_hide_{key}")
        if active
        else InlineKeyboardButton("👁️ Show", callback_data=f"mc_show_{key}")
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Edit Category", callback_data=f"mc_edit_{key}"), toggle],
        [InlineKeyboardButton("📁 Manage Subcategories", callback_data=f"mc_subcategories_{key}")],
        [InlineKeyboardButton("🗑️ Delete Category", callback_data=f"mc_delete_{key}")],
        [mp_back("mc_categories")],
    ]
    text = f"{prefix}📂 {info['name']}\n\n{status_text(info.get('is_active'))}"
    return text, InlineKeyboardMarkup(keyboard)


def subcategories_admin_screen(key, notice=None):
    info = get_category_info(key, create=True)
    prefix = f"{notice}\n\n" if notice else ""
    rows = get_subcategory_rows(key, info["id"])
    table_ok = subcategory_table_ready()

    keyboard = []
    if table_ok and all(r.get("id") for r in rows):
        text = f"{prefix}📁 {info['name']} - Subcategories\n\nSelect a subcategory to manage it 👇"
        row = []
        for r in rows:
            label = sub_label(key, r["name"]) + ("" if r.get("is_active") is not False else " 🚫")
            row.append(InlineKeyboardButton(label, callback_data=f"mc_sub_view_{r['id']}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("➕ Add Subcategory", callback_data=f"mc_sub_add_{key}")])
    else:
        listing = "\n".join(f"• {sub_label(key, r['name'])}" for r in rows)
        text = (
            f"{prefix}📁 {info['name']} - Subcategories\n\n{listing}\n\n"
            "ℹ️ These subcategories are read-only for now. To add, rename, hide or delete "
            "subcategories, create the optional `subcategories` table in Supabase (see the setup note)."
        )
    keyboard.append([mp_back(f"mc_view_{key}")])
    return text, InlineKeyboardMarkup(keyboard)


def subcategory_admin_screen(sid, notice=None):
    row = get_subcategory_row(sid)
    if not row:
        return "❌ Subcategory not found.", InlineKeyboardMarkup([[mp_back("mc_categories")]])
    key = key_for_category_id(row["category_id"])
    prefix = f"{notice}\n\n" if notice else ""
    active = row.get("is_active") is not False
    toggle = (
        InlineKeyboardButton("👁️ Hide", callback_data=f"mc_sub_hide_{sid}")
        if active
        else InlineKeyboardButton("👁️ Show", callback_data=f"mc_sub_show_{sid}")
    )
    keyboard = [
        [InlineKeyboardButton("✏️ Edit", callback_data=f"mc_sub_edit_{sid}"), toggle],
        [InlineKeyboardButton("🗑️ Delete", callback_data=f"mc_sub_delete_{sid}")],
        [mp_back(f"mc_subcategories_{key}" if key else "mc_categories")],
    ]
    category_name = category_display_name(key) if key else "-"
    text = f"{prefix}📁 {row['name']}\n\n📂 {category_name}\n{status_text(row.get('is_active'))}"
    return text, InlineKeyboardMarkup(keyboard)


async def show_screen(query, builder, *args):
    try:
        text, markup = builder(*args)
    except Exception as e:
        print("MANAGE CATEGORIES ERROR:", e)
        text, markup = "❌ Something went wrong. Please try again.", InlineKeyboardMarkup([[mp_back("mc_categories")]])
    await safe_edit(query, text, markup)


async def reply_screen(message, builder, *args):
    try:
        text, markup = builder(*args)
    except Exception as e:
        print("MANAGE CATEGORIES ERROR:", e)
        text, markup = "❌ Something went wrong. Please try again.", InlineKeyboardMarkup([[mp_back("mc_categories")]])
    await message.reply_text(text, reply_markup=markup)


TABLE_MISSING_TEXT = (
    "ℹ️ This needs the optional `subcategories` table in Supabase, which is not available yet.\n\n"
    "Subcategories are read-only until it is created (see the setup note)."
)


async def handle_category_admin_callback(query, context, data):
    """Single entry point for every Manage Categories callback. Admin-only."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return

    if data in ("admin_categories", "mc_categories"):
        await show_screen(query, categories_admin_screen)

    elif data.startswith("mc_view_"):
        await show_screen(query, category_admin_screen, data[len("mc_view_"):])

    elif data.startswith("mc_hide_") or data.startswith("mc_show_"):
        make_active = data.startswith("mc_show_")
        key = data.split("_", 2)[2]
        try:
            info = get_category_info(key, create=True)
            supabase.table("categories").update({"is_active": make_active}).eq("id", info["id"]).execute()
            notice = "✅ Category is visible again." if make_active else "🚫 Category hidden successfully."
        except Exception as e:
            print("CATEGORY VISIBILITY ERROR:", e)
            notice = "❌ Could not update the category."
        await show_screen(query, category_admin_screen, key, notice)

    elif data.startswith("mc_subcategories_"):
        await show_screen(query, subcategories_admin_screen, data[len("mc_subcategories_"):])

    elif data.startswith("mc_sub_view_"):
        await show_screen(query, subcategory_admin_screen, data[len("mc_sub_view_"):])

    elif data.startswith("mc_sub_hide_") or data.startswith("mc_sub_show_"):
        make_active = data.startswith("mc_sub_show_")
        sid = data[len("mc_sub_hide_"):]
        try:
            supabase.table("subcategories").update({"is_active": make_active}).eq("id", sid).execute()
            notice = "✅ Subcategory is visible again." if make_active else "🚫 Subcategory hidden successfully."
        except Exception as e:
            print("SUBCATEGORY VISIBILITY ERROR:", e)
            notice = "❌ Could not update the subcategory."
        await show_screen(query, subcategory_admin_screen, sid, notice)

    elif data.startswith("mc_sub_delete_"):
        sid = data[len("mc_sub_delete_"):]
        try:
            row = get_subcategory_row(sid)
        except Exception as e:
            print("SUBCATEGORY LOAD ERROR:", e)
            row = None
        if not row:
            await show_screen(query, categories_admin_screen)
            return
        keyboard = [[
            InlineKeyboardButton("🗑️ Delete", callback_data=f"mc_sub_confirm_delete_{sid}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"mc_sub_cancel_delete_{sid}"),
        ]]
        await safe_edit(query, f"⚠️ Delete this subcategory?\n\n📁 {row['name']}", InlineKeyboardMarkup(keyboard))

    elif data.startswith("mc_sub_cancel_delete_"):
        await show_screen(query, subcategory_admin_screen, data[len("mc_sub_cancel_delete_"):])

    elif data.startswith("mc_sub_confirm_delete_"):
        sid = data[len("mc_sub_confirm_delete_"):]
        try:
            row = get_subcategory_row(sid)
            if not row:
                await show_screen(query, categories_admin_screen)
                return
            category_id = row["category_id"]
            key = key_for_category_id(category_id)
            back_to_sub = InlineKeyboardMarkup([[mp_back(f"mc_sub_view_{sid}")]])
            if category_has_products(category_id, row["name"]):
                await safe_edit(
                    query,
                    "⚠️ This subcategory contains products.\n\nMove those products to another subcategory before deleting it.",
                    back_to_sub
                )
                return
            siblings = supabase.table("subcategories").select("id").eq("category_id", category_id).execute().data or []
            if len(siblings) <= 1:
                await safe_edit(query, "⚠️ A category needs at least one subcategory, so the last one cannot be deleted.", back_to_sub)
                return
            supabase.table("subcategories").delete().eq("id", sid).execute()
        except Exception as e:
            print("SUBCATEGORY DELETE ERROR:", e)
            await safe_edit(query, "❌ Could not delete the subcategory.", InlineKeyboardMarkup([[mp_back("mc_categories")]]))
            return
        if key:
            await show_screen(query, subcategories_admin_screen, key, "✅ Subcategory deleted successfully.")
        else:
            await show_screen(query, categories_admin_screen)

    elif data.startswith("mc_delete_"):
        key = data[len("mc_delete_"):]
        info = safe_category_info(key)
        if not info:
            await show_screen(query, categories_admin_screen)
            return
        keyboard = [[
            InlineKeyboardButton("🗑️ Delete", callback_data=f"mc_confirm_delete_{key}"),
            InlineKeyboardButton("❌ Cancel", callback_data=f"mc_cancel_delete_{key}"),
        ]]
        await safe_edit(query, f"⚠️ Delete this category?\n\n📂 {info['name']}", InlineKeyboardMarkup(keyboard))

    elif data.startswith("mc_cancel_delete_"):
        await show_screen(query, category_admin_screen, data[len("mc_cancel_delete_"):])

    elif data.startswith("mc_confirm_delete_"):
        key = data[len("mc_confirm_delete_"):]
        try:
            info = get_category_info(key)
            if not info:
                await show_screen(query, categories_admin_screen)
                return
            if category_has_products(info["id"]):
                await safe_edit(
                    query,
                    "⚠️ This category contains products.\n\nPlease move or remove its products before deleting the category.",
                    InlineKeyboardMarkup([[mp_back(f"mc_view_{key}")]])
                )
                return
            if subcategory_table_ready():
                supabase.table("subcategories").delete().eq("category_id", info["id"]).execute()
            supabase.table("categories").delete().eq("id", info["id"]).execute()
        except Exception as e:
            print("CATEGORY DELETE ERROR:", e)
            await safe_edit(query, "❌ Could not delete the category.", InlineKeyboardMarkup([[mp_back("mc_categories")]]))
            return
        await show_screen(query, categories_admin_screen, "✅ Category deleted successfully.")
    # mc_edit_ / mc_sub_edit_ / mc_sub_add_ are ConversationHandler entry points


# ----- text input: rename category, rename subcategory, add subcategory -----

def clear_category_input(context):
    context.user_data.pop("mc_action", None)
    context.user_data.pop("mc_target", None)


async def category_input_start(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return ConversationHandler.END

    data = query.data
    context.user_data.pop("mp_edit_field", None)
    context.user_data.pop("mp_edit_id", None)

    try:
        if data.startswith("mc_sub_edit_"):
            sid = data[len("mc_sub_edit_"):]
            row = get_subcategory_row(sid)
            if not row:
                await show_screen(query, categories_admin_screen)
                return ConversationHandler.END
            action, target = "edit_sub", sid
            prompt = (
                f"✏️ Edit Subcategory\n\nCurrent subcategory:\n{row['name']}\n\n"
                "Send the new subcategory name.\nProducts in this category that use it will be updated too."
            )
            back = f"mc_sub_view_{sid}"
        elif data.startswith("mc_sub_add_"):
            key = data[len("mc_sub_add_"):]
            if not subcategory_table_ready():
                await safe_edit(query, TABLE_MISSING_TEXT, InlineKeyboardMarkup([[mp_back(f"mc_subcategories_{key}")]]))
                return ConversationHandler.END
            action, target = "add_sub", key
            prompt = f"➕ Add Subcategory\n\nCategory: {category_display_name(key)}\n\nSend the new subcategory name."
            back = f"mc_subcategories_{key}"
        else:
            key = data[len("mc_edit_"):]
            info = get_category_info(key, create=True)
            action, target = "edit_cat", key
            prompt = f"✏️ Edit Category\n\nCurrent category:\n{info['name']}\n\nSend the new category name."
            back = f"mc_view_{key}"
    except Exception as e:
        print("CATEGORY INPUT START ERROR:", e)
        await safe_edit(query, "❌ Something went wrong. Please try again.", InlineKeyboardMarkup([[mp_back("mc_categories")]]))
        return ConversationHandler.END

    context.user_data["mc_action"] = action
    context.user_data["mc_target"] = target
    await safe_edit(query, prompt + "\n\nSend /cancel to stop.", InlineKeyboardMarkup([[mp_back(back)]]))
    return EDIT_VALUE


async def receive_category_input(update, context):
    message = update.message
    action = context.user_data.get("mc_action")
    target = context.user_data.get("mc_target")
    text = (message.text or "").strip()
    if not text:
        await message.reply_text("⚠️ Please send the name as text.")
        return EDIT_VALUE

    try:
        if action == "edit_cat":
            error = validate_category_name(text)
            if error:
                await message.reply_text(error)
                return EDIT_VALUE
            info = get_category_info(target)
            if not info:
                clear_category_input(context)
                await reply_screen(message, categories_admin_screen)
                return ConversationHandler.END
            other_names = [i["name"].lower() for k, i in load_category_map().items() if k != target]
            if text.lower() in other_names:
                await message.reply_text("⚠️ Another category already uses this name. Please send a different name.")
                return EDIT_VALUE
            # only the name changes - the category id (and so every product link) stays the same
            supabase.table("categories").update({"name": text}).eq("id", info["id"]).execute()
            clear_category_input(context)
            await reply_screen(message, category_admin_screen, target, "✅ Category updated successfully.")
            return ConversationHandler.END

        if action == "edit_sub":
            error = validate_subcategory_name(text)
            if error:
                await message.reply_text(error)
                return EDIT_VALUE
            row = get_subcategory_row(target)
            if not row:
                clear_category_input(context)
                await reply_screen(message, categories_admin_screen)
                return ConversationHandler.END
            category_id, old_name = row["category_id"], row["name"]
            if text == old_name:
                clear_category_input(context)
                await reply_screen(message, subcategory_admin_screen, target, "ℹ️ The name was not changed.")
                return ConversationHandler.END
            siblings = supabase.table("subcategories").select("id,name").eq("category_id", category_id).execute().data or []
            if any(s["name"].lower() == text.lower() and s["id"] != target for s in siblings):
                await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                return EDIT_VALUE
            supabase.table("subcategories").update({"name": text}).eq("id", target).execute()
            try:
                # only products of THIS category with the old name are renamed
                updated = (
                    supabase.table("products").update({"subcategory": text})
                    .eq("category_id", category_id).eq("subcategory", old_name).execute()
                )
            except Exception:
                supabase.table("subcategories").update({"name": old_name}).eq("id", target).execute()
                raise
            count = len(updated.data or [])
            clear_category_input(context)
            await reply_screen(
                message, subcategory_admin_screen, target,
                f"✅ Subcategory updated successfully.\n📦 {count} product(s) updated."
            )
            return ConversationHandler.END

        if action == "add_sub":
            error = validate_subcategory_name(text)
            if error:
                await message.reply_text(error)
                return EDIT_VALUE
            if not subcategory_table_ready():
                clear_category_input(context)
                await message.reply_text(TABLE_MISSING_TEXT)
                return ConversationHandler.END
            info = get_category_info(target, create=True)
            rows = get_subcategory_rows(target, info["id"])
            if any(r["name"].lower() == text.lower() for r in rows):
                await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                return EDIT_VALUE
            next_order = max([r.get("display_order") or 0 for r in rows] + [0]) + 1
            supabase.table("subcategories").insert(
                {"category_id": info["id"], "name": text, "is_active": True, "display_order": next_order}
            ).execute()
            clear_category_input(context)
            await reply_screen(message, subcategories_admin_screen, target, "✅ Subcategory added successfully.")
            return ConversationHandler.END
    except Exception as e:
        print("CATEGORY INPUT ERROR:", e)
        await message.reply_text("❌ Could not save the change.\n\nPlease try again or send /cancel.")
        return EDIT_VALUE

    clear_category_input(context)
    return ConversationHandler.END


async def dispatch_admin_callback(query, context, data):
    if data == "admin_categories" or data.startswith("mc_"):
        await handle_category_admin_callback(query, context, data)
    else:
        await handle_manage_callback(query, context, data)


application = Application.builder().token(BOT_TOKEN).build()

add_product_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(add_product_start, pattern="^admin_add$")],
    states={
        PHOTO: [MessageHandler(filters.PHOTO, receive_photo)],
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
        PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
        CATEGORY: [
            CallbackQueryHandler(show_category_buttons, pattern="^addcat_"),
            CallbackQueryHandler(receive_subcategory, pattern="^addsub_"),
            CallbackQueryHandler(category_back, pattern="^add_category_back$")
        ],
        PLATFORM: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_platform)],
        LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_link)]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_add_product),
        CallbackQueryHandler(main_callback, pattern="^main$")
    ]
)

edit_product_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(edit_field_start, pattern="^mp_ef_"),
        CallbackQueryHandler(category_input_start, pattern=MC_ENTRY_PATTERN)
    ],
    states={
        EDIT_VALUE: [MessageHandler(filters.PHOTO | (filters.TEXT & ~filters.COMMAND), receive_edit_value)]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_edit_product),
        CallbackQueryHandler(edit_escape, pattern=r"^(main|admin|admin_products|admin_categories|mp_(?!ef_).*|mc_(?!edit_|sub_edit_|sub_add_).*)$")
    ],
    allow_reentry=True
)

# Order matters: the conversations must come BEFORE the generic button_handler
application.add_handler(add_product_conversation)
application.add_handler(edit_product_conversation)
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))

async def health(request):
    return web.Response(text="Outfit India Bot is running.")

async def telegram_webhook(request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="OK")

async def startup(app):
    await application.initialize()
    await application.start()
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing")
    webhook_url = f"{external_url}/telegram"
    await application.bot.set_webhook(url=webhook_url)
    print(f"Telegram webhook set: {webhook_url}")

async def shutdown(app):
    await application.stop()
    await application.shutdown()

web_app = web.Application()
web_app.router.add_get("/", health)
web_app.router.add_post("/telegram", telegram_webhook)
web_app.on_startup.append(startup)
web_app.on_cleanup.append(shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(web_app, host="0.0.0.0", port=port)
