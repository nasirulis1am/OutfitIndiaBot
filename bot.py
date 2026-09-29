import os
from aiohttp import web
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from config import BOT_TOKEN, ADMIN_ID
from database import supabase

PHOTO, NAME, PRICE, CATEGORY, PLATFORM, LINK = range(6)

def main_menu(is_admin=False):
    buttons = [
        [InlineKeyboardButton("👔 Men's Fashion", callback_data="men"), InlineKeyboardButton("👗 Women's Fashion", callback_data="women")],
        [InlineKeyboardButton("🌦️ Seasonal Wear", callback_data="seasonal"), InlineKeyboardButton("🏷️ Deals & Offers", callback_data="deals")]
    ]
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
    buttons = [
        [InlineKeyboardButton("👔 Men's Fashion", callback_data="addcat_men"), InlineKeyboardButton("👗 Women's Fashion", callback_data="addcat_women")],
        [InlineKeyboardButton("🌦️ Seasonal Wear", callback_data="addcat_seasonal"), InlineKeyboardButton("🏷️ Deals & Offers", callback_data="addcat_deals")]
    ]
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
        existing = supabase.table("categories").select("id").eq("name", category_name).limit(1).execute()
        if existing.data:
            category_id = existing.data[0]["id"]
        else:
            created = supabase.table("categories").insert({"name":category_name,"display_order":0,"is_active":True}).execute()
            if not created.data: raise Exception("Category was not returned by Supabase.")
            category_id = created.data[0]["id"]
    except Exception as e:
        print("CATEGORY LOAD ERROR:", e)
        await query.edit_message_text("❌ Could not save this category.\n\nPlease choose the category again.")
        return CATEGORY

    context.user_data["category_id"] = category_id
    context.user_data["category_name"] = category_name
    context.user_data["category_key"] = key

    subcategories = {
        "men":[("👖 Jeans","Jeans"),("👔 Shirts","Shirts"),("👕 T-Shirts","T-Shirts"),("🧥 Jackets","Jackets"),("🧶 Sweaters & Hoodies","Sweaters & Hoodies"),("👟 Shoes","Shoes")],
        "women":[("👕 Tops & T-Shirts","Tops & T-Shirts"),("👖 Jeans & Pants","Jeans & Pants"),("👗 Dresses","Dresses"),("🧥 Jackets & Coats","Jackets & Coats"),("🩳 Skirts & Shorts","Skirts & Shorts"),("👠 Shoes","Shoes")],
        "seasonal":[("❄️ Winter Collection","Winter Collection"),("☀️ Summer Collection","Summer Collection")],
        "deals":[("💰 Under ₹500","Under ₹500"),("💎 Under ₹1000","Under ₹1000"),("⭐ Top Rated","Top Rated"),("🆕 New Arrivals","New Arrivals")]
    }
    items = subcategories[key]
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
    buttons = [
        [InlineKeyboardButton("👔 Men's Fashion", callback_data="addcat_men"), InlineKeyboardButton("👗 Women's Fashion", callback_data="addcat_women")],
        [InlineKeyboardButton("🌦️ Seasonal Wear", callback_data="addcat_seasonal"), InlineKeyboardButton("🏷️ Deals & Offers", callback_data="addcat_deals")]
    ]
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
    category_name = PARENT_CATEGORY_NAMES.get(parent_key)
    if not category_name:
        return None
    result = (
        supabase
        .table("categories")
        .select("id")
        .eq("name", category_name)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    return None


def back_button(parent_key, header_id=None, label=None):
    """Category-specific Back button (never goes to main).
    Product messages use the short label "🔙 Back" and carry the listing header's
    message ID in the callback data, so the header can be deleted too."""
    callback = f"back_{parent_key}" if header_id is None else f"back_{parent_key}_{header_id}"
    return InlineKeyboardButton(label or PARENT_BACK_LABELS[parent_key], callback_data=callback)


def category_screen(key):
    """Return (text, keyboard) for a main category's subcategory screen."""
    if key == "men":
        keyboard = [
            [InlineKeyboardButton("👖 Jeans", callback_data="men_jeans"), InlineKeyboardButton("👔 Shirts", callback_data="men_shirts")],
            [InlineKeyboardButton("👕 T-Shirts", callback_data="men_tshirts"), InlineKeyboardButton("🧥 Jackets", callback_data="men_jackets")],
            [InlineKeyboardButton("🧶 Sweaters & Hoodies", callback_data="men_sweaters"), InlineKeyboardButton("👟 Shoes", callback_data="men_shoes")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")]
        ]
        return "Here's what you can explore in Men's Fashion 👇", InlineKeyboardMarkup(keyboard)
    if key == "women":
        keyboard = [
            [InlineKeyboardButton("👕 Tops & T-Shirts", callback_data="women_tops"), InlineKeyboardButton("👖 Jeans & Pants", callback_data="women_pants")],
            [InlineKeyboardButton("👗 Dresses", callback_data="women_dresses"), InlineKeyboardButton("🧥 Jackets & Coats", callback_data="women_jackets")],
            [InlineKeyboardButton("🩳 Skirts & Shorts", callback_data="women_skirts"), InlineKeyboardButton("👠 Shoes", callback_data="women_shoes")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")]
        ]
        return "Explore Women's Fashion 👇", InlineKeyboardMarkup(keyboard)
    if key == "seasonal":
        keyboard = [
            [InlineKeyboardButton("❄️ Winter Collection", callback_data="winter")],
            [InlineKeyboardButton("☀️ Summer Collection", callback_data="summer")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")]
        ]
        return "Choose your season 👇", InlineKeyboardMarkup(keyboard)
    if key == "deals":
        keyboard = [
            [InlineKeyboardButton("💰 Under ₹500", callback_data="deal_500")],
            [InlineKeyboardButton("💎 Under ₹1000", callback_data="deal_1000")],
            [InlineKeyboardButton("⭐ Top Rated", callback_data="top_rated")],
            [InlineKeyboardButton("🆕 New Arrivals", callback_data="new_arrivals")],
            [InlineKeyboardButton("🔙 Back to Main Menu", callback_data="main")]
        ]
        return "🔥 Limited time offers!\n\nFind your favourite outfits at great prices.", InlineKeyboardMarkup(keyboard)
    return None, None


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
        category_id = get_category_id(parent_key)
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

    # 2. Product subcategory screens
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
    elif data == "admin_products":
        await safe_edit(query, "📦 Manage Products\n\nProduct management will be added next.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]))
    elif data == "admin_categories":
        await safe_edit(query, "📂 Manage Categories\n\nCategory management will be added next.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]))
    elif data == "admin_dashboard":
        await safe_edit(query, "📊 Dashboard\n\nDashboard will be added next.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Admin Panel", callback_data="admin")]]))
    else:
        await safe_edit(query, "🛍️ Products will appear here soon.", main_menu_button())


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

# Order matters: the conversation must come BEFORE the generic button_handler
application.add_handler(add_product_conversation)
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
