import os
from aiohttp import web

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import BOT_TOKEN, ADMIN_ID
from database import supabase


# =========================
# ADD PRODUCT STATES
# =========================

PHOTO, NAME, PRICE, CATEGORY, PLATFORM, LINK = range(6)


# =========================
# MAIN MENU
# =========================

def main_menu(is_admin=False):
    buttons = [
        [
            InlineKeyboardButton(
                "👔 Men's Fashion",
                callback_data="men"
            ),
            InlineKeyboardButton(
                "👗 Women's Fashion",
                callback_data="women"
            ),
        ],
        [
            InlineKeyboardButton(
                "🌦️ Seasonal Wear",
                callback_data="seasonal"
            ),
            InlineKeyboardButton(
                "🏷️ Deals & Offers",
                callback_data="deals"
            ),
        ],
    ]

    if is_admin:
        buttons.append([
            InlineKeyboardButton(
                "⚙️ Admin Panel",
                callback_data="admin"
            )
        ])

    return InlineKeyboardMarkup(buttons)


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = str(user.id) == str(ADMIN_ID)

    text = (
        f"Hi {user.first_name}! 👋\n\n"
        "Welcome to Outfit India! ✨\n\n"
        "Discover stylish outfits, seasonal clothing "
        "and great fashion deals from trusted shopping platforms.\n\n"
        "What are you looking for today?"
    )

    await update.message.reply_text(
        text,
        reply_markup=main_menu(is_admin)
    )


# =========================
# ADMIN PANEL
# =========================

async def show_admin_panel(query):
    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Add Product",
                callback_data="admin_add"
            )
        ],
        [
            InlineKeyboardButton(
                "📦 Manage Products",
                callback_data="admin_products"
            )
        ],
        [
            InlineKeyboardButton(
                "📂 Manage Categories",
                callback_data="admin_categories"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 Dashboard",
                callback_data="admin_dashboard"
            )
        ],
        [
            InlineKeyboardButton(
                "🔙 Back to Main Menu",
                callback_data="main"
            )
        ],
    ]

    await query.edit_message_text(
        "⚙️ Outfit India Admin Panel\n\n"
        "Manage your products and categories from Telegram.",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


# =========================
# ADD PRODUCT
# =========================

async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text(
            "⛔ You don't have permission to use this."
        )
        return ConversationHandler.END

    context.user_data.clear()

    await query.edit_message_text(
        "➕ Add Product\n\n"
        "Step 1/6\n\n"
        "🖼️ Send the product photo.\n\n"
        "Send /cancel anytime to stop."
    )

    return PHOTO


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text(
            "⚠️ Please send a product photo."
        )
        return PHOTO

    photo = update.message.photo[-1]

    # Telegram file_id is enough for us to reuse the image.
    context.user_data["image_url"] = photo.file_id

    await update.message.reply_text(
        "✅ Photo received.\n\n"
        "Step 2/6\n\n"
        "📝 Now send the product name."
    )

    return NAME


async def receive_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    name = update.message.text.strip()

    if not name:
        await update.message.reply_text(
            "⚠️ Please enter a valid product name."
        )
        return NAME

    context.user_data["name"] = name

    await update.message.reply_text(
        "Step 3/6\n\n"
        "💰 Send the sale price.\n\n"
        "Example: 499"
    )

    return PRICE


async def receive_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    price_text = update.message.text.strip()

    try:
        price = float(price_text)
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "⚠️ Please enter only a valid price.\n\n"
            "Example: 499"
        )
        return PRICE

    context.user_data["sale_price"] = price

    await update.message.reply_text(
        "Step 4/6\n\n"
        "📂 Send the category name.\n\n"
        "Example:\n"
        "Men's Fashion\n"
        "Women's Fashion\n"
        "Winter Collection"
    )

    return CATEGORY


async def receive_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    category_name = update.message.text.strip()

    if not category_name:
        await update.message.reply_text(
            "⚠️ Please enter a category name."
        )
        return CATEGORY

    # Find existing category
    existing = (
        supabase
        .table("categories")
        .select("id")
        .eq("name", category_name)
        .limit(1)
        .execute()
    )

    if existing.data:
        category_id = existing.data[0]["id"]
    else:
        # Create category automatically if it doesn't exist
        created = (
            supabase
            .table("categories")
            .insert({
                "name": category_name,
                "display_order": 0,
                "is_active": True,
            })
            .execute()
        )

        category_id = created.data[0]["id"]

    context.user_data["category_id"] = category_id
    context.user_data["category_name"] = category_name

    await update.message.reply_text(
        "Step 5/6\n\n"
        "🛍️ Send the shopping platform.\n\n"
        "Example: Amazon\n"
        "Or: Flipkart\n"
        "Or: Myntra"
    )

    return PLATFORM


async def receive_platform(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    platform = update.message.text.strip()

    if not platform:
        await update.message.reply_text(
            "⚠️ Please enter the platform name."
        )
        return PLATFORM

    context.user_data["platform"] = platform

    await update.message.reply_text(
        "Step 6/6\n\n"
        "🔗 Now send the affiliate/product link."
    )

    return LINK


async def receive_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    link = update.message.text.strip()

    if not link.startswith(("http://", "https://")):
        await update.message.reply_text(
            "⚠️ Please send a valid link starting with http:// or https://"
        )
        return LINK

    data = {
        "name": context.user_data["name"],
        "category_id": context.user_data["category_id"],
        "image_url": context.user_data["image_url"],
        "platform": context.user_data["platform"],
        "sale_price": context.user_data["sale_price"],
        "affiliate_link": link,
        "is_active": True,
    }

    try:
        result = (
            supabase
            .table("products")
            .insert(data)
            .execute()
        )

        if not result.data:
            raise Exception("Product was not returned by Supabase.")

        await update.message.reply_text(
            "✅ PRODUCT PUBLISHED!\n\n"
            f"🛍️ {context.user_data['name']}\n"
            f"💰 ₹{context.user_data['sale_price']}\n"
            f"📂 {context.user_data['category_name']}\n"
            f"🏪 {context.user_data['platform']}\n\n"
            "The product is now saved in your catalogue."
        )

    except Exception as e:
        print("PRODUCT INSERT ERROR:", e)

        await update.message.reply_text(
            "❌ Product could not be saved.\n\n"
            "Please try again."
        )

    context.user_data.clear()

    return ConversationHandler.END


async def cancel_add_product(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    context.user_data.clear()

    await update.message.reply_text(
        "❌ Add Product cancelled."
    )

    return ConversationHandler.END


# =========================
# BUTTON HANDLER
# =========================

async def button_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    is_admin = str(user.id) == str(ADMIN_ID)

    if query.data == "men":
        keyboard = [
            [
                InlineKeyboardButton("👖 Jeans", callback_data="men_jeans"),
                InlineKeyboardButton("👔 Shirts", callback_data="men_shirts"),
            ],
            [
                InlineKeyboardButton("👕 T-Shirts", callback_data="men_tshirts"),
                InlineKeyboardButton("🧥 Jackets", callback_data="men_jackets"),
            ],
            [
                InlineKeyboardButton(
                    "🧶 Sweaters & Hoodies",
                    callback_data="men_sweaters"
                ),
                InlineKeyboardButton("👟 Shoes", callback_data="men_shoes"),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Main Menu",
                    callback_data="main"
                )
            ],
        ]

        await query.edit_message_text(
            "Here's what you can explore in Men's Fashion 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "women":
        keyboard = [
            [
                InlineKeyboardButton(
                    "👕 Tops & T-Shirts",
                    callback_data="women_tops"
                ),
                InlineKeyboardButton(
                    "👖 Jeans & Pants",
                    callback_data="women_pants"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👗 Dresses",
                    callback_data="women_dresses"
                ),
                InlineKeyboardButton(
                    "🧥 Jackets & Coats",
                    callback_data="women_jackets"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🩳 Skirts & Shorts",
                    callback_data="women_skirts"
                ),
                InlineKeyboardButton(
                    "👠 Shoes",
                    callback_data="women_shoes"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Main Menu",
                    callback_data="main"
                )
            ],
        ]

        await query.edit_message_text(
            "Explore Women's Fashion 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "seasonal":
        keyboard = [
            [
                InlineKeyboardButton(
                    "❄️ Winter Collection",
                    callback_data="winter"
                )
            ],
            [
                InlineKeyboardButton(
                    "☀️ Summer Collection",
                    callback_data="summer"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Main Menu",
                    callback_data="main"
                )
            ],
        ]

        await query.edit_message_text(
            "Choose your season 👇",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "deals":
        keyboard = [
            [
                InlineKeyboardButton(
                    "💰 Under ₹500",
                    callback_data="deal_500"
                )
            ],
            [
                InlineKeyboardButton(
                    "💎 Under ₹1000",
                    callback_data="deal_1000"
                )
            ],
            [
                InlineKeyboardButton(
                    "⭐ Top Rated",
                    callback_data="top_rated"
                )
            ],
            [
                InlineKeyboardButton(
                    "🆕 New Arrivals",
                    callback_data="new_arrivals"
                )
            ],
            [
                InlineKeyboardButton(
                    "🔙 Back to Main Menu",
                    callback_data="main"
                )
            ],
        ]

        await query.edit_message_text(
            "🔥 Limited time offers!\n\n"
            "Find your favourite outfits at great prices.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "admin":
        if not is_admin:
            await query.edit_message_text(
                "⛔ You don't have permission to access the Admin Panel."
            )
            return

        await show_admin_panel(query)

    elif query.data == "admin_products":
        await query.edit_message_text(
            "📦 Manage Products\n\n"
            "Product management will be added next.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Admin Panel",
                        callback_data="admin"
                    )
                ]
            ])
        )

    elif query.data == "admin_categories":
        await query.edit_message_text(
            "📂 Manage Categories\n\n"
            "Category management will be added next.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Admin Panel",
                        callback_data="admin"
                    )
                ]
            ])
        )

    elif query.data == "admin_dashboard":
        await query.edit_message_text(
            "📊 Dashboard\n\n"
            "Dashboard will be added next.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Admin Panel",
                        callback_data="admin"
                    )
                ]
            ])
        )

    elif query.data == "main":
        await query.edit_message_text(
            "What are you looking for today? 👇",
            reply_markup=main_menu(is_admin)
        )

    else:
        await query.edit_message_text(
            "🛍️ Products will appear here soon.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back to Main Menu",
                        callback_data="main"
                    )
                ]
            ])
        )


# =========================
# WEBHOOK
# =========================

application = (
    Application.builder()
    .token(BOT_TOKEN)
    .build()
)


add_product_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(
            add_product_start,
            pattern="^admin_add$"
        )
    ],
    states={
        PHOTO: [
            MessageHandler(
                filters.PHOTO,
                receive_photo
            )
        ],
        NAME: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                receive_name
            )
        ],
        PRICE: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                receive_price
            )
        ],
        CATEGORY: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                receive_category
            )
        ],
        PLATFORM: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                receive_platform
            )
        ],
        LINK: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                receive_link
            )
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_add_product)
    ],
)


application.add_handler(add_product_conversation)
application.add_handler(CommandHandler("start", start))
application.add_handler(CallbackQueryHandler(button_handler))


# =========================
# WEB SERVER
# =========================

async def health(request):
    return web.Response(
        text="Outfit India Bot is running."
    )


async def telegram_webhook(request):
    data = await request.json()

    update = Update.de_json(
        data,
        application.bot
    )

    await application.process_update(update)

    return web.Response(text="OK")


async def startup(app):
    await application.initialize()
    await application.start()

    external_url = os.environ.get(
        "RENDER_EXTERNAL_URL"
    )

    if not external_url:
        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing"
        )

    webhook_url = (
        f"{external_url}/telegram"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    print(
        f"Telegram webhook set: {webhook_url}"
    )


async def shutdown(app):
    await application.stop()
    await application.shutdown()


web_app = web.Application()

web_app.router.add_get(
    "/",
    health
)

web_app.router.add_post(
    "/telegram",
    telegram_webhook
)

web_app.on_startup.append(startup)
web_app.on_cleanup.append(shutdown)


if __name__ == "__main__":
    port = int(
        os.environ.get("PORT", "10000")
    )

    web.run_app(
        web_app,
        host="0.0.0.0",
        port=port
        )
