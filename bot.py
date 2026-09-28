from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)
from config import BOT_TOKEN, ADMIN_ID


# ---------- MAIN MENU ----------

def main_menu(is_admin=False):
    buttons = [
        [
            InlineKeyboardButton("👔 Men's Fashion", callback_data="men"),
            InlineKeyboardButton("👗 Women's Fashion", callback_data="women"),
        ],
        [
            InlineKeyboardButton("🌦️ Seasonal Wear", callback_data="seasonal"),
            InlineKeyboardButton("🏷️ Deals & Offers", callback_data="deals"),
        ],
    ]

    if is_admin:
        buttons.append([
            InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")
        ])

    return InlineKeyboardMarkup(buttons)


# ---------- START ----------

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


# ---------- BUTTON HANDLER ----------

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
                InlineKeyboardButton(
                    "👖 Jeans",
                    callback_data="men_jeans"
                ),
                InlineKeyboardButton(
                    "👔 Shirts",
                    callback_data="men_shirts"
                ),
            ],
            [
                InlineKeyboardButton(
                    "👕 T-Shirts",
                    callback_data="men_tshirts"
                ),
                InlineKeyboardButton(
                    "🧥 Jackets",
                    callback_data="men_jackets"
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧶 Sweaters & Hoodies",
                    callback_data="men_sweaters"
                ),
                InlineKeyboardButton(
                    "👟 Shoes",
                    callback_data="men_shoes"
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
            "🔥 Limited time offers!\n"
            "Find your favourite outfits at great prices.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif query.data == "admin":

        if not is_admin:
            await query.edit_message_text(
                "⛔ You don't have permission to access the Admin Panel."
            )
            return

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

    elif query.data == "main":

        await query.edit_message_text(
            "What are you looking for today? 👇",
            reply_markup=main_menu(is_admin)
        )

    else:
        await query.edit_message_text(
            "🛍️ Products will appear here soon.\n\n"
            "We are setting up the product catalogue.",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 Back to Main Menu",
                        callback_data="main"
                    )
                ]
            ])
        )


# ---------- RUN BOT ----------

def main():
    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(
        CallbackQueryHandler(button_handler)
    )

    print("Outfit India Bot is running...")

    application.run_polling()


if __name__ == "__main__":
    main()
