import asyncio
import contextvars
import functools
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import quote
from aiohttp import web
from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, ConversationHandler, MessageHandler, filters
from config import BOT_TOKEN, ADMIN_ID, SUPABASE_URL, SUPABASE_KEY
from database import supabase

# Separate Support Bot (@OutfitIndiaHelp_Bot) — never hardcode the token.
SUPPORT_BOT_TOKEN = (os.environ.get("SUPPORT_BOT_TOKEN") or "").strip()
try:
    from config import SUPPORT_BOT_TOKEN as _CFG_SUPPORT_TOKEN  # optional
    if _CFG_SUPPORT_TOKEN:
        SUPPORT_BOT_TOKEN = str(_CFG_SUPPORT_TOKEN).strip()
except Exception:
    pass

# support_bot notification message_id -> customer telegram_id (in-memory)
_SUPPORT_REPLY_MAP = {}
_SUPPORT_MAP_MAX = 500
support_application = None  # set later when SUPPORT_BOT_TOKEN is present

# Premium catalogue visuals (customer side only). Files live next to bot.py under assets/.
# Mapping is presentation-only — navigation still comes from the database.
_ASSET_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")
_ASSET_FILE_IDS = {}  # path -> Telegram file_id (reuse after first successful upload)

# Main category key -> relative asset path
CATEGORY_ASSET_FILES = {
    "men": os.path.join("categories", "mens-fashion.png"),
    "women": os.path.join("categories", "womens-fashion.png"),
    "seasonal": os.path.join("categories", "seasonal-wear.png"),
    "deals": os.path.join("categories", "deals-offers.png"),
}

# Subcategory name (lowercase) -> relative asset path (Men's Fashion set from the reference)
SUBCATEGORY_ASSET_FILES = {
    "jeans": os.path.join("subcategories", "jeans.png"),
    "shirts": os.path.join("subcategories", "shirts.png"),
    "t-shirts": os.path.join("subcategories", "t-shirts.png"),
    "tshirts": os.path.join("subcategories", "t-shirts.png"),
    "jackets": os.path.join("subcategories", "jackets.png"),
    "sweaters & hoodies": os.path.join("subcategories", "sweaters-hoodies.png"),
    "sweaters and hoodies": os.path.join("subcategories", "sweaters-hoodies.png"),
    "shoes": os.path.join("subcategories", "shoes.png"),
}

def _asset_abs(relative_path):
    if not relative_path:
        return None
    path = os.path.join(_ASSET_ROOT, relative_path)
    return path if os.path.isfile(path) else None


def category_asset_path(key):
    """Local path for a main category visual, or None (safe fallback)."""
    return _asset_abs(CATEGORY_ASSET_FILES.get(key))


def subcategory_asset_path(name):
    """Local path for a subcategory visual by name, or None."""
    if not name:
        return None
    key = str(name).strip().lower()
    path = _asset_abs(SUBCATEGORY_ASSET_FILES.get(key))
    if path:
        return path
    # Light fuzzy match for renamed DB values (e.g. "Men's Jeans")
    for alias, rel in SUBCATEGORY_ASSET_FILES.items():
        if alias in key or key in alias:
            return _asset_abs(rel)
    return None


def _asset_media_source(path):
    """Prefer cached Telegram file_id; otherwise open the local file for upload."""
    file_id = _ASSET_FILE_IDS.get(path)
    if file_id:
        return file_id
    return open(path, "rb")


def _cache_file_id_from_message(path, message):
    """Store file_id after Telegram accepts an uploaded photo."""
    if not message or not path or path in _ASSET_FILE_IDS:
        return
    try:
        photos = getattr(message, "photo", None) or []
        if photos:
            _ASSET_FILE_IDS[path] = photos[-1].file_id
    except Exception:
        pass


async def send_catalogue_photo(bot, chat_id, path, caption, reply_markup=None):
    """Send a catalogue visual. Never raises into the caller on I/O failure."""
    try:
        media = _asset_media_source(path)
        try:
            msg = await bot.send_photo(
                chat_id=chat_id,
                photo=media,
                caption=caption,
                reply_markup=reply_markup,
            )
        finally:
            if hasattr(media, "close"):
                try:
                    media.close()
                except Exception:
                    pass
        _cache_file_id_from_message(path, msg)
        return msg
    except Exception as e:
        print(f"CATALOGUE PHOTO ERROR: {type(e).__name__}: {e}")
        return None


async def show_customer_category(query, context, key):
    """Customer category screen: premium visual (when available) + subcategory keyboard."""
    text, markup = category_screen(key)
    if text is None:
        await safe_edit(query, "🚫 This category is currently unavailable.", main_menu_button())
        return
    path = category_asset_path(key)
    msg = query.message
    chat_id = msg.chat_id if msg is not None else query.from_user.id

    if path:
        # Prefer editing in place when the current message is already a photo
        if msg is not None and msg.photo:
            try:
                media_src = _asset_media_source(path)
                try:
                    media = InputMediaPhoto(media=media_src, caption=text)
                    edited = await query.edit_message_media(media=media, reply_markup=markup)
                finally:
                    if hasattr(media_src, "close"):
                        try:
                            media_src.close()
                        except Exception:
                            pass
                _cache_file_id_from_message(path, edited if edited is not None else msg)
                return
            except BadRequest as e:
                if "not modified" in str(e).lower():
                    return
                print(f"CATEGORY MEDIA EDIT ERROR: {e}")
            except Exception as e:
                print(f"CATEGORY MEDIA EDIT ERROR: {e}")
            # Fall through: delete old message and send a fresh photo
            await delete_message_safe(msg)
            sent = await send_catalogue_photo(context.bot, chat_id, path, text, markup)
            if sent is not None:
                return
            await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
            return

        # Current message is text — try replace with photo, else reply photo
        try:
            media_src = _asset_media_source(path)
            try:
                media = InputMediaPhoto(media=media_src, caption=text)
                edited = await query.edit_message_media(media=media, reply_markup=markup)
            finally:
                if hasattr(media_src, "close"):
                    try:
                        media_src.close()
                    except Exception:
                        pass
            _cache_file_id_from_message(path, edited)
            return
        except Exception as e:
            print(f"CATEGORY TEXT->PHOTO ERROR: {e}")
        sent = await send_catalogue_photo(context.bot, chat_id, path, text, markup)
        if sent is not None:
            # Remove the old text menu when we successfully sent a photo reply
            if msg is not None and msg.text:
                await delete_message_safe(msg)
            return

    # No asset or photo send failed — classic text UI
    await safe_edit(query, text, markup)

PHOTO, NAME, PRICE, CATEGORY, PLATFORM, LINK = range(6)
BROADCAST_TEXT = 20
SEARCH_CUSTOMER = 30
SEARCH_ADMIN = 31
SUPPORT_MESSAGE = 40

HELP_TEXT = (
    "✨ OUTFIT INDIA — HELP\n\n"
    "Welcome to Outfit India! 🛍️\n\n"
    "Here’s how to use the bot:\n\n"
    "🛍️ Browse Products\n"
    "Explore Men’s Fashion, Women’s Fashion, Seasonal Wear, Deals & Offers, and Other categories.\n\n"
    "🔍 Search Products\n"
    "Can’t find what you’re looking for? Use Search Products and type the product name.\n\n"
    "❤️ Save Items\n"
    "Found something you like but don’t want to buy right now? Tap ♡ Save Item. "
    "You can find it anytime from ❤️ Saved Items.\n\n"
    "🛒 Buy a Product\n"
    "Open any product and tap 🛒 Buy Now to visit the shopping link.\n\n"
    "📌 Important\n"
    "Outfit India helps you discover products and redirects you to the respective "
    "shopping platform to complete your purchase."
)

CONTACT_US_PROMPT = (
    "💬 CONTACT US\n\n"
    "Please describe your problem or question below. Our support team will review your message."
)

def admin_nav_rows(back_callback):
    """Admin Panel navigation block, used ONLY on admin management screens:
        [ 🔙 Back ]        -> the screen's existing Back target
        [ 🏠 Main Menu ]   -> the normal customer Main Menu (existing "main" callback)
    Never add this to customer-facing screens."""
    return [
        [InlineKeyboardButton("🔙 Back", callback_data=back_callback)],
        [InlineKeyboardButton("🏠 Main Menu", callback_data="main")],
    ]


# Shared admin navigation keyboard (Back returns to the Admin Panel via the existing "admin" callback)
ADMIN_BACK_MARKUP = InlineKeyboardMarkup(admin_nav_rows("admin"))

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
    buttons.append([InlineKeyboardButton("🔍 Search Products", callback_data="search_c")])
    buttons.append([InlineKeyboardButton("❤️ Saved Items", callback_data="saved_home")])
    if is_admin:
        buttons.append([InlineKeyboardButton("⚙️ Admin Panel", callback_data="admin")])
    return InlineKeyboardMarkup(buttons)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    is_admin = str(user.id) == str(ADMIN_ID)
    await register_or_touch_user(user)
    await update.message.reply_text(
        f"Hi {user.first_name}!👋\n\n"
        "Welcome to OUTFIT INDIA! ✦\n\n"
        "Find your next favourite.",
        reply_markup=main_menu(is_admin),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Customer /help — exact Outfit India help text + Contact Us button."""
    await register_or_touch_user(update.effective_user)
    await update.message.reply_text(
        HELP_TEXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Us", callback_data="contact_us")],
        ]),
    )


async def show_help_screen(query, context):
    """Show help from a callback (e.g. after Cancel on Contact Us)."""
    await safe_edit(
        query,
        HELP_TEXT,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("💬 Contact Us", callback_data="contact_us")],
        ]),
    )


# ---------------------------------------------------------------------------
# CUSTOMER ANTI-DOUBLE-CLICK GUARD + VISIBLE LOADING STATE
# Telegram has no disabled InlineKeyboardButton, so the closest reliable behaviour is:
#   1. acknowledge the tap at once (stops Telegram's spinner),
#   2. take the customer's ONE global action lock, scoped to (user id, chat id) - while it is held EVERY
#      other customer tap from that user in that chat (same button or a different one) is ignored: no
#      screens, DB writes or tracking run,
#   3. temporarily swap ONLY the tapped button's label for "⏳ Loading..." (every other button, URL
#      buttons included, stays exactly as it was),
#   4. run the existing handler untouched,
#   5. if the handler did not itself edit/delete the tapped message (error, or the destination opened
#      as a new message), put the original keyboard back; if it did, the destination already replaced it.
# The lock is released by try/finally as soon as the handler (and any restore) ends, even on errors.
# Scope: (user id, chat id) - one active customer action per customer per chat. It is never a bot-wide lock,
# so different users (and the same user in different chats) never block each other. Admin callbacks are
# not guarded. message id + callback data are still used to find the tapped button for loading/restore.
# PAGE-AWARE RELEASE: the lock protects the CURRENT page only. The moment the action has successfully rendered
# a NEW page by editing the tapped message with a keyboard (edit_message_text/caption/media + reply_markup), the
# lock is released at once - the new page's buttons never inherit the old page's lock and nothing waits on a
# timer. Actions that do not replace the page (Save/Remove, errors, new-message destinations) keep the lock until
# the handler ends, plus the short _RECENT_DONE duplicate window.
# A tap queued from the OLD page that arrives after the page changed is rejected only if its button no longer
# exists on the page now shown (see _RECENT_PAGE); a button of the new page is always accepted.
# ---------------------------------------------------------------------------
_ACTIVE_CALLBACKS = {}  # (user_id, chat_id) -> (owner token, start time): the per-customer action lock
CALLBACK_LOCK_STALE_SECONDS = 60  # safety net only; the normal unlock is the handler finishing
# Ownership of the lock for the CURRENT action. A ContextVar is inherited by every task the action spawns, so a
# guarded handler that (directly or through a helper task) calls another guarded handler re-enters instead of
# deadlocking against its own lock. A different tap runs in its own context and never sees this token.
_LOCK_OWNER = contextvars.ContextVar("outfit_customer_lock_owner", default=None)


class _Action:
    """One guarded customer action: identifies the tapped page/message and tracks lock state."""
    __slots__ = ("key", "chat_id", "message_id", "released", "finished")

    def __init__(self, key, chat_id, message_id):
        self.key, self.chat_id, self.message_id = key, chat_id, message_id
        self.released = False  # lock freed because the new page was rendered
        self.finished = False  # handler (and cleanup) ended


# Page-replacing calls: edits of the tapped message that carry a keyboard = a new clickable page is on screen.
_PAGE_RENDER_METHODS = ("edit_message_text", "edit_message_caption", "edit_message_media")
# (user_id, chat_id) -> (time.monotonic() of the page change, id of the message whose page changed)
_RECENT_PAGE = {}


def _release_page_lock(action):
    """The action rendered its new page: free the customer's lock NOW (no timer). Idempotent; only ever removes
    this action's own lock entry."""
    if action.released or action.finished:
        return
    action.released = True
    current = _ACTIVE_CALLBACKS.get(action.key)
    if current is not None and current[0] is action:
        del _ACTIVE_CALLBACKS[action.key]
    t = time.monotonic()
    _RECENT_PAGE[action.key] = (t, action.message_id)
    if len(_RECENT_PAGE) > 500:
        for k in [k for k, v in _RECENT_PAGE.items() if t - v[0] > 30]:
            _RECENT_PAGE.pop(k, None)


def _button_on_shown_page(query, data):
    """True if `data` is a button of the keyboard the tapped message shows now (or if that is unknown)."""
    markup = getattr(getattr(query, "message", None), "reply_markup", None)
    rows = getattr(markup, "inline_keyboard", None)
    if not rows:
        return True  # cannot tell -> never reject on a guess
    return any(getattr(b, "callback_data", None) == data for row in rows for b in row)
# Taps already queued in the user's client/Telegram when the first action finished arrive as separate webhook
# requests AFTER the lock is free. For one short window the last COMPLETED action therefore remembers which
# messages it owned (the tapped message + every message it edited/deleted); a later tap coming from one of those
# now-replaced messages is a stale tap from the old screen - whatever button it is - and is rejected. A tap on a
# brand-new message (a different message id) is a genuine new action and is accepted. Nothing waits on this; it
# is never recorded for a failed action (retry must work at once).
_RECENT_DONE = {}  # (user_id, chat_id) -> (time.monotonic() of successful completion, frozenset of message ids)
CALLBACK_DUPLICATE_WINDOW_SECONDS = 1.5
_ADMIN_CALLBACK_PREFIXES = ("admin", "mp_", "mc_", "dashboard_", "pt_", "ub_", "search_a_")
LOADING_BUTTON_LABEL = "⏳ Loading..."
# These callbacks answer the query themselves with a toast, so they must not be pre-acknowledged.
_TOAST_CALLBACK_PREFIXES = ("saved_toggle_",)

# Query ids already acknowledged by the guard: a later query.answer() for the same tap becomes a no-op
# (a second answer would otherwise raise). Only taps the guard acknowledged are affected.
_PREANSWERED = {}  # callback query id -> time.monotonic()
# (chat_id, message_id) pairs the running handler edited or deleted (set per guarded tap).
_TOUCHED = contextvars.ContextVar("outfit_touched_messages", default=None)
_TRACKED_BOT_METHODS = (
    "edit_message_text", "edit_message_caption", "edit_message_media",
    "edit_message_reply_markup", "delete_message",
)
_ORIGINAL_CQ_ANSWER = CallbackQuery.answer


def _install_callback_hooks():
    """One-time, idempotent wrappers (class level, so every existing edit/delete/answer call site is covered
    without touching any handler). They only record or skip - they never change what is sent."""
    if not getattr(CallbackQuery.answer, "_oi_answer_once", False):
        original_answer = CallbackQuery.answer

        @functools.wraps(original_answer)
        async def answer_once(self, *args, **kwargs):
            if self.id in _PREANSWERED:
                return True  # the guard already acknowledged this tap
            return await original_answer(self, *args, **kwargs)

        answer_once._oi_answer_once = True
        CallbackQuery.answer = answer_once

    for name in _TRACKED_BOT_METHODS:
        original = getattr(Bot, name, None)
        if original is None or getattr(original, "_oi_touch_wrapped", False):
            continue

        def make_wrapper(orig, method_name):
            @functools.wraps(orig)
            async def wrapper(self, *args, **kwargs):
                result = await orig(self, *args, **kwargs)
                touched = _TOUCHED.get()
                if touched is not None:  # recorded only after the call succeeded
                    chat_id = kwargs.get("chat_id", args[0] if method_name == "delete_message" and args else None)
                    message_id = kwargs.get("message_id", args[1] if method_name == "delete_message" and len(args) > 1 else None)
                    if message_id is not None:
                        touched.add((chat_id, message_id))
                    action = _LOCK_OWNER.get()
                    if (action is not None and method_name in _PAGE_RENDER_METHODS
                            and kwargs.get("reply_markup") is not None
                            and message_id == action.message_id and chat_id == action.chat_id):
                        _release_page_lock(action)  # new page is on screen: its buttons must work immediately
                return result
            wrapper._oi_touch_wrapped = True
            return wrapper

        setattr(Bot, name, make_wrapper(original, name))


_install_callback_hooks()


async def _ack_callback(query, data):
    """Acknowledge the tap immediately (unless the handler answers with its own toast)."""
    if data.startswith(_TOAST_CALLBACK_PREFIXES):
        return
    try:
        await _ORIGINAL_CQ_ANSWER(query)
        now = time.monotonic()
        _PREANSWERED[query.id] = now
        if len(_PREANSWERED) > 200:
            for qid in [k for k, t in _PREANSWERED.items() if now - t > 120]:
                _PREANSWERED.pop(qid, None)
    except Exception:
        pass  # the handler's own answer (if any) still gets its chance


def _loading_markup(markup, data):
    """Copy of `markup` with only the button(s) carrying callback `data` relabelled; None if not found."""
    rows, found = [], False
    for row in getattr(markup, "inline_keyboard", None) or ():
        new_row = []
        for button in row:
            if getattr(button, "callback_data", None) == data:
                new_row.append(InlineKeyboardButton(LOADING_BUTTON_LABEL, callback_data=data))
                found = True
            else:
                new_row.append(button)  # untouched: other callbacks and URL buttons
        rows.append(new_row)
    return InlineKeyboardMarkup(rows) if found else None


async def _show_loading(bot, query, data):
    """Show the loading label on the tapped button. Returns (chat_id, message_id, original_markup) or None."""
    try:
        msg = query.message
        original = getattr(msg, "reply_markup", None) if msg is not None else None
        if original is None:
            return None
        loading = _loading_markup(original, data)
        if loading is None:
            return None
        await bot.edit_message_reply_markup(
            chat_id=msg.chat_id, message_id=msg.message_id, reply_markup=loading
        )
        return (msg.chat_id, msg.message_id, original)
    except Exception as e:
        print(f"LOADING STATE ERROR: {type(e).__name__}: {e}")
        return None


async def _restore_keyboard(bot, state):
    """Put the original keyboard back (the message may be gone by now - that is fine)."""
    chat_id, message_id, original = state
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=original)
    except Exception as e:
        print(f"LOADING RESTORE SKIPPED: {type(e).__name__}: {e}")


def single_flight(handler):
    """Decorator for (update, context) callback handlers: run ONE customer action at a time per
    (user, chat), with a temporary loading label on the tapped button."""
    @functools.wraps(handler)
    async def guarded(update, context, *args, **kwargs):
        query = getattr(update, "callback_query", None)
        data = (query.data or "") if query is not None else ""
        if query is None or not data or query.from_user is None or data.startswith(_ADMIN_CALLBACK_PREFIXES):
            return await handler(update, context, *args, **kwargs)
        chat_id = query.message.chat_id if query.message is not None else query.from_user.id
        message_id = query.message.message_id if query.message is not None else None
        key = (query.from_user.id, chat_id)  # the per-customer action lock (NOT per button, NOT bot-wide)
        # ---- atomic check-and-acquire: NO await between the lookups and the registration below, so on the
        # single asyncio thread no other callback can interleave. Nothing awaits before the lock exists.
        now = time.monotonic()
        running = _LOCK_OWNER.get()
        if running is not None and not running.finished:
            # a guarded handler called from inside the SAME action (even after its page lock was released): re-enter
            return await handler(update, context, *args, **kwargs)
        held = _ACTIVE_CALLBACKS.get(key)
        if held is not None:
            owner, started = held
            reject = now - started < CALLBACK_LOCK_STALE_SECONDS  # else: stale lock from a lost task, take over
        else:
            reject = False
            page = _RECENT_PAGE.get(key)
            if page is not None and now - page[0] < CALLBACK_DUPLICATE_WINDOW_SECONDS and page[1] == message_id:
                # the page of this message just changed: reject only a queued tap of the OLD page's button
                reject = not _button_on_shown_page(query, data)
            done = _RECENT_DONE.get(key)
            if not reject and done is not None:
                done_at, done_messages = done
                reject = now - done_at < CALLBACK_DUPLICATE_WINDOW_SECONDS and message_id in done_messages
        if reject:
            try:
                await _ORIGINAL_CQ_ANSWER(query)  # stop this tap's spinner only; handler is NOT run
            except Exception:
                pass
            return None
        owner = _Action(key, chat_id, message_id)
        _ACTIVE_CALLBACKS[key] = (owner, now)  # lock acquired (still no await since the check)
        owner_token = _LOCK_OWNER.set(owner)  # synchronous: marks this action (and tasks it spawns) as the owner
        touched, touched_token, loading = set(), None, None
        succeeded = False
        try:
            try:
                await _ack_callback(query, data)
                loading = await _show_loading(context.bot, query, data)
                touched_token = _TOUCHED.set(touched)
                result = await handler(update, context, *args, **kwargs)
                succeeded = True
                return result
            finally:
                try:
                    if touched_token is not None:
                        _TOUCHED.reset(touched_token)
                    _PREANSWERED.pop(query.id, None)
                    if loading is not None and (loading[0], loading[1]) not in touched:
                        await _restore_keyboard(context.bot, loading)
                except Exception as e:
                    print(f"LOADING CLEANUP ERROR: {type(e).__name__}: {e}")
        finally:
            try:
                _LOCK_OWNER.reset(owner_token)
            except ValueError:
                pass
            owner.finished = True
            current = _ACTIVE_CALLBACKS.get(key)
            if current is not None and current[0] is owner:
                del _ACTIVE_CALLBACKS[key]
            if succeeded and not owner.released:  # a released action already changed the page: no old-page memory
                t = time.monotonic()
                owned = {message_id} if message_id is not None else set()
                owned.update(m for (c, m) in touched if c == chat_id)
                _RECENT_DONE[key] = (t, frozenset(owned))
                if len(_RECENT_DONE) > 500:
                    for k in [k for k, v in _RECENT_DONE.items() if t - v[0] > 30]:
                        _RECENT_DONE.pop(k, None)
    return guarded


@single_flight
async def contact_us_start(update, context):
    """Entry: customer tapped 💬 Contact Us."""
    query = update.callback_query
    await query.answer()
    await register_or_touch_user(query.from_user)
    await safe_edit(
        query,
        CONTACT_US_PROMPT,
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Cancel", callback_data="contact_cancel")],
        ]),
    )
    return SUPPORT_MESSAGE


def _remember_support_notification(message_id, customer_telegram_id):
    """Map Support Bot notification message_id → customer for admin Reply routing."""
    if message_id is None or customer_telegram_id is None:
        return
    _SUPPORT_REPLY_MAP[int(message_id)] = int(customer_telegram_id)
    # Bound memory growth
    if len(_SUPPORT_REPLY_MAP) > _SUPPORT_MAP_MAX:
        for key in list(_SUPPORT_REPLY_MAP.keys())[: len(_SUPPORT_REPLY_MAP) - _SUPPORT_MAP_MAX]:
            _SUPPORT_REPLY_MAP.pop(key, None)


def _lookup_customer_from_support_reply(origin_message):
    """Resolve customer id from map first, then Telegram ID: line in the notification text."""
    if origin_message is None:
        return None
    mid = getattr(origin_message, "message_id", None)
    if mid is not None and int(mid) in _SUPPORT_REPLY_MAP:
        return _SUPPORT_REPLY_MAP[int(mid)]
    origin_text = origin_message.text or origin_message.caption or ""
    match = re.search(r"Telegram ID:\s*(\d+)", origin_text)
    if match:
        return int(match.group(1))
    return None


async def _send_support_notification_via_support_bot(admin_text, customer_telegram_id):
    """
    Send the admin notification FROM the Support Bot (@OutfitIndiaHelp_Bot).
    Returns the sent Message on success, raises on failure.
    """
    if not SUPPORT_BOT_TOKEN:
        raise RuntimeError("SUPPORT_BOT_TOKEN is not configured")
    # Prefer the shared support Application bot when running; fall back to a one-off Bot.
    bot = None
    if support_application is not None:
        bot = support_application.bot
    if bot is None:
        from telegram import Bot
        bot = Bot(token=SUPPORT_BOT_TOKEN)
        await bot.initialize()
        try:
            sent = await bot.send_message(chat_id=int(ADMIN_ID), text=admin_text)
        finally:
            try:
                await bot.shutdown()
            except Exception:
                pass
    else:
        sent = await bot.send_message(chat_id=int(ADMIN_ID), text=admin_text)
    if sent is not None:
        _remember_support_notification(sent.message_id, customer_telegram_id)
    return sent


async def receive_support_message(update, context):
    """Customer typed a support message — notify admin via Support Bot, confirm to customer."""
    user = update.effective_user
    await register_or_touch_user(user)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text(
            "⚠️ Please type your message as text, or tap 🔙 Cancel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Cancel", callback_data="contact_cancel")],
            ]),
        )
        return SUPPORT_MESSAGE

    name = (user.full_name or user.first_name or "Customer").strip()
    username = f"@{user.username}" if user.username else "Not Available"
    tid = user.id
    admin_text = (
        "💬 NEW SUPPORT MESSAGE\n\n"
        "Customer:\n"
        f"Name: {name}\n"
        f"Username: {username}\n"
        f"Telegram ID: {tid}\n\n"
        "Message:\n"
        f"{text}"
    )
    try:
        await _send_support_notification_via_support_bot(admin_text, tid)
    except Exception as e:
        print(f"SUPPORT FORWARD ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await update.message.reply_text(
            "❌ Sorry, we could not send your message right now. Please try again later.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🏠 Main Menu", callback_data="main")],
            ]),
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "✅ Message Sent\n\n"
        "Your message has been sent to Outfit India Support. "
        "We'll review it and get back to you.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🏠 Main Menu", callback_data="main")],
        ]),
    )
    return ConversationHandler.END


@single_flight
async def cancel_support(update, context):
    """Cancel support input — return to Help screen."""
    query = update.callback_query
    if query:
        await query.answer()
        await show_help_screen(query, context)
        return ConversationHandler.END
    if update.message:
        await update.message.reply_text(
            HELP_TEXT,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("💬 Contact Us", callback_data="contact_us")],
            ]),
        )
    return ConversationHandler.END


@single_flight
async def support_escape(update, context):
    """Exit support conversation on other customer callbacks."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    await register_or_touch_user(query.from_user)
    if data == "contact_cancel":
        await show_help_screen(query, context)
        return ConversationHandler.END
    if data == "main":
        await show_main_menu(query, is_admin)
        return ConversationHandler.END
    if data == "contact_us":
        return await contact_us_start(update, context)
    # Hand off to normal routing
    if data == "saved_home" or data.startswith("saved_"):
        await handle_saved_callback(query, context, data)
    elif data in CATEGORY_ORDER:
        await show_customer_category(query, context, data)
    elif data == "search_c":
        # Let customer search conversation take over via re-entry on next handler cycle —
        # end support and show search prompt here.
        await safe_edit(
            query,
            CUSTOMER_SEARCH_PROMPT,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main")]]),
            parse_mode="HTML",
        )
        _store_search_prompt_message(context, query.message)
        # End support; user can open Search again from menu if needed
    else:
        await show_main_menu(query, is_admin)
    return ConversationHandler.END


async def support_bot_admin_reply(update, context):
    """
    Runs on the SUPPORT bot Application.
    Admin replies (Telegram Reply) to a Support Bot notification → deliver via MAIN bot.
    Non-admin users are ignored. Customer never talks to the Support Bot.
    """
    if str(update.effective_user.id) != str(ADMIN_ID):
        print(f"SUPPORT BOT: ignored non-admin sender {update.effective_user.id}")
        return
    msg = update.message
    if msg is None or not msg.reply_to_message:
        return
    origin = msg.reply_to_message
    origin_text = origin.text or origin.caption or ""
    if "NEW SUPPORT MESSAGE" not in origin_text:
        return

    customer_id = _lookup_customer_from_support_reply(origin)
    if customer_id is None:
        print("SUPPORT REPLY: could not resolve customer Telegram ID")
        try:
            await msg.reply_text("❌ Could not identify the customer for this ticket.")
        except Exception:
            pass
        return

    reply_body = (msg.text or msg.caption or "").strip()
    if not reply_body:
        try:
            await msg.reply_text("⚠️ Reply must include text to send to the customer.")
        except Exception:
            pass
        return

    # Deliver using the MAIN Outfit India bot — never the Support Bot
    try:
        await application.bot.send_message(
            chat_id=customer_id,
            text=f"💬 OUTFIT INDIA SUPPORT\n\n{reply_body}",
        )
        try:
            await msg.reply_text("✅ Reply sent to customer.")
        except Exception:
            pass
        print(f"SUPPORT REPLY SUCCESS: customer={customer_id}")
    except Exception as e:
        print(f"SUPPORT REPLY ERROR: customer={customer_id} {type(e).__name__}: {safe_error_text(e)}")
        try:
            await msg.reply_text("❌ Could not deliver reply to the customer.")
        except Exception:
            pass


async def show_admin_panel(query):
    keyboard = [
        [InlineKeyboardButton("📦 Manage Products", callback_data="admin_products"),
         InlineKeyboardButton("📂 Manage Categories", callback_data="admin_categories")],
        [InlineKeyboardButton("➕ Add Product", callback_data="admin_add")],
        [InlineKeyboardButton("📊 Product Tracking", callback_data="admin_tracking")],
        [InlineKeyboardButton("📈 Dashboard", callback_data="admin_dashboard")],
        [InlineKeyboardButton("👥 Users & Broadcast", callback_data="admin_users")],
        *admin_nav_rows("main"),
    ]
    await safe_edit(
        query,
        "⚙️ OUTFIT INDIA ADMIN PANEL\n\nManage your catalogue from here.",
        InlineKeyboardMarkup(keyboard)
    )

_ALLOWED_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
_ALLOWED_IMAGE_MIMES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/webp",
}


def _document_is_image(document) -> bool:
    if document is None:
        return False
    mime = (getattr(document, "mime_type", None) or "").lower().strip()
    if mime in _ALLOWED_IMAGE_MIMES or mime.startswith("image/"):
        return True
    name = (getattr(document, "file_name", None) or "").lower()
    return any(name.endswith(ext) for ext in _ALLOWED_IMAGE_EXTS)


async def send_product_image(bot, chat_id, image_url, caption=None, reply_markup=None, message=None):
    """
    Display a product image stored in image_url (Telegram file_id).
    Supports both photo file_ids and document file_ids (original-quality uploads).
    Falls back to temporary download if file_id cannot be used with send_photo.
    Never permanently stores downloaded files.
    """
    if not image_url:
        return None

    async def _send_photo_obj(photo_obj):
        if message is not None:
            return await message.reply_photo(
                photo=photo_obj, caption=caption, reply_markup=reply_markup
            )
        return await bot.send_photo(
            chat_id=chat_id, photo=photo_obj, caption=caption, reply_markup=reply_markup
        )

    # 1) Try as Telegram photo/file_id (works for normal photo file_ids)
    try:
        return await _send_photo_obj(image_url)
    except Exception as e1:
        print(f"PRODUCT IMAGE send_photo file_id failed: {type(e1).__name__}: {e1}")

    # 2) Download by file_id and resend as photo bytes (document file_ids)
    tmp_path = None
    try:
        tg_file = await bot.get_file(image_url)
        import tempfile

        suffix = ".jpg"
        file_path = getattr(tg_file, "file_path", None) or ""
        lower = file_path.lower()
        for ext in _ALLOWED_IMAGE_EXTS:
            if lower.endswith(ext):
                suffix = ext
                break
        fd, tmp_path = tempfile.mkstemp(prefix="oi_img_", suffix=suffix)
        os.close(fd)
        await tg_file.download_to_drive(custom_path=tmp_path)
        with open(tmp_path, "rb") as f:
            return await _send_photo_obj(f)
    except Exception as e2:
        print(f"PRODUCT IMAGE download fallback failed: {type(e2).__name__}: {e2}")
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


_AP_KEY = "_add_product_msgs"  # lives in context.user_data (per admin) -> {"chat_id": int, "ids": [message ids]}


def _ap_track(context, chat_id, *message_ids):
    """Remember Telegram message ids of THIS Add Product session (stored in the admin's own user_data)."""
    session = context.user_data.setdefault(_AP_KEY, {"chat_id": chat_id, "ids": []})
    for mid in message_ids:
        if mid is not None and mid not in session["ids"]:
            session["ids"].append(mid)


async def _ap_reply(update, context, text, **kwargs):
    """reply_text + track both the admin's message and the bot's reply for later cleanup."""
    msg = update.message
    _ap_track(context, msg.chat_id, msg.message_id)
    sent = await msg.reply_text(text, **kwargs)
    _ap_track(context, msg.chat_id, getattr(sent, "message_id", None))
    return sent


async def _ap_cleanup(context):
    """Delete every tracked message of this Add Product session, then forget them. A failed deletion is
    logged by delete_by_id_safe and skipped; it never stops the rest."""
    session = context.user_data.pop(_AP_KEY, None)
    if not session:
        return
    for mid in session["ids"]:
        await delete_by_id_safe(context.bot, session["chat_id"], mid)


async def add_product_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    context.user_data.clear()
    if query.message is not None:
        _ap_track(context, query.message.chat_id, query.message.message_id)
    await query.edit_message_text(
        "➕ ADD PRODUCT\n\nStep 1/6\n\n"
        "🖼️ Send the product image as a FILE/DOCUMENT to preserve original quality.\n\n"
        "Send /cancel anytime to stop."
    )
    return PHOTO

async def receive_photo(update, context):
    """Step 1: accept image as DOCUMENT only (original quality). Reject compressed photos."""
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END

    message = update.message
    # Compressed Telegram photo — reject and stay on Step 1
    if message.photo and not message.document:
        await _ap_reply(update, context, 
            "⚠️ Please send the image as a FILE/DOCUMENT to preserve original quality."
        )
        return PHOTO

    document = message.document
    if document is None:
        await _ap_reply(update, context, 
            "⚠️ Please send the image as a FILE/DOCUMENT to preserve original quality."
        )
        return PHOTO

    if not _document_is_image(document):
        await _ap_reply(update, context, 
            "⚠️ Please send a JPG, PNG, or WEBP image as a FILE/DOCUMENT."
        )
        return PHOTO

    # Store document file_id in existing image_url field (no download / no re-upload)
    context.user_data["image_url"] = document.file_id
    await _ap_reply(update, context, 
        "✅ Image received (original quality).\n\nStep 2/6\n\n📝 Now send the product name."
    )
    return NAME

async def receive_name(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    name = update.message.text.strip()
    if not name:
        await _ap_reply(update, context, "⚠️ Please enter a valid product name.")
        return NAME
    context.user_data["name"] = name
    await _ap_reply(update, context, "Step 3/6\n\n💰 Send the sale price.\n\nExample: 499")
    return PRICE

async def receive_price(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    try:
        price = float(update.message.text.strip())
        if price < 0: raise ValueError
    except ValueError:
        await _ap_reply(update, context, "⚠️ Please enter only a valid price.\n\nExample: 499")
        return PRICE
    context.user_data["sale_price"] = price
    buttons = add_flow_category_buttons()
    if not buttons:
        await _ap_reply(update, context, 
            "⚠️ No categories are available.\n\nRestore a category in Admin Panel → Manage Categories, then start Add Product again."
        )
        context.user_data.clear()
        return ConversationHandler.END
    await _ap_reply(update, context, "Step 4/6\n\n📂 Choose the main category:", reply_markup=add_flow_markup(buttons))
    return CATEGORY

async def show_category_buttons(update, context):
    query = update.callback_query
    await query.answer()
    if query.message is not None:
        _ap_track(context, query.message.chat_id, query.message.message_id)
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END

    key = query.data.replace("addcat_", "")
    if key not in CATEGORY_ORDER:
        await query.edit_message_text("❌ Invalid category.")
        return CATEGORY

    try:
        info = get_category_info(key)
    except Exception as e:
        print("CATEGORY LOAD ERROR:", e)
        await query.edit_message_text("❌ Could not load this category.\n\nPlease choose the category again.")
        return CATEGORY
    if not info:
        await query.edit_message_text(
            "⚠️ This category no longer exists.\n\nRestore it in Admin Panel → Manage Categories first.",
            reply_markup=add_flow_markup(add_flow_category_buttons())
        )
        return CATEGORY

    context.user_data["category_id"] = info["id"]
    context.user_data["category_name"] = info["name"]
    context.user_data["category_key"] = key

    # ACTIVE subcategories only, loaded dynamically from the subcategories table
    sub_names = active_subcategory_names(key, info["id"])
    items = [(sub_label(key, n), n) for n in sub_names if fits_callback(f"addsub_{key}_{n}")]
    buttons = []
    for i in range(0, len(items), 2):
        buttons.append([InlineKeyboardButton(label, callback_data=f"addsub_{key}_{value}") for label, value in items[i:i+2]])
    buttons.extend(admin_nav_rows("add_category_back"))
    text = f"📂 {info['name']}\n\nChoose a subcategory:"
    if not items:
        text = f"📂 {info['name']}\n\n⚠️ This category has no active subcategories.\nAdd or show one in Manage Categories → Manage Subcategories, or pick another category."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    return CATEGORY

async def receive_subcategory(update, context):
    query = update.callback_query
    await query.answer()
    if query.message is not None:
        _ap_track(context, query.message.chat_id, query.message.message_id)
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    parts = query.data.split("_", 2)
    if len(parts) < 3:
        await query.edit_message_text("❌ Invalid subcategory.")
        return CATEGORY
    key, name = parts[1], parts[2]
    # never trust callback_data alone: the subcategory must still be an ACTIVE one of the chosen category
    if key != context.user_data.get("category_key") or name not in active_subcategory_names(key, context.user_data.get("category_id")):
        await query.edit_message_text(
            "⚠️ That subcategory is not available.\n\nPlease choose again.",
            reply_markup=add_flow_markup(add_flow_category_buttons())
        )
        return CATEGORY
    context.user_data["subcategory"] = name
    context.user_data.pop("sub_subcategory", None)
    context.user_data.pop("subcategory_id", None)

    # Third level: shown ONLY when this subcategory has active child categories (loaded from the database)
    try:
        sub_row, children = active_children_by_name(context.user_data.get("category_id"), name)
    except Exception as e:
        print("ADD PRODUCT CHILD LOAD ERROR:", e)
        sub_row, children = None, []
    if sub_row and children:
        context.user_data["subcategory_id"] = sub_row["id"]
        buttons = []
        for i in range(0, len(children), 2):
            buttons.append([
                InlineKeyboardButton(f"📁 {c['name']}"[:TELEGRAM_BUTTON_TEXT_MAX], callback_data=f"addss_{c['id']}")
                for c in children[i:i + 2]
            ])
        buttons.append([InlineKeyboardButton("⏭️ Skip (no sub-subcategory)", callback_data="addss_skip")])
        buttons.extend(admin_nav_rows(f"addcat_{key}"))
        await query.edit_message_text(
            f"Step 4/6\n\n🗂️ {name}\n\nChoose a sub-subcategory, or skip:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
        return CATEGORY
    await query.edit_message_text("Step 5/6\n\n🛍️ Send the shopping platform.\n\nExample:\nAmazon\nFlipkart\nMyntra")
    return PLATFORM

async def receive_sub_subcategory(update, context):
    query = update.callback_query
    await query.answer()
    if query.message is not None:
        _ap_track(context, query.message.chat_id, query.message.message_id)
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    value = query.data[len("addss_"):]
    if value == "skip":
        context.user_data.pop("sub_subcategory", None)
    else:
        chosen = None
        sub_id = context.user_data.get("subcategory_id")
        try:
            if sub_id is not None:
                # never trust callback_data alone: must be an ACTIVE child of the chosen subcategory
                chosen = next((c for c in load_child_rows(sub_id, active_only=True) if str(c["id"]) == value), None)
        except Exception as e:
            print("ADD PRODUCT CHILD CHECK ERROR:", e)
        if not chosen:
            await query.edit_message_text(
                "⚠️ That sub-subcategory is not available.\n\nPlease choose again.",
                reply_markup=add_flow_markup(add_flow_category_buttons())
            )
            return CATEGORY
        context.user_data["sub_subcategory"] = chosen["name"]
    await query.edit_message_text("Step 5/6\n\n🛍️ Send the shopping platform.\n\nExample:\nAmazon\nFlipkart\nMyntra")
    return PLATFORM

async def category_back(update, context):
    query = update.callback_query
    await query.answer()
    if query.message is not None:
        _ap_track(context, query.message.chat_id, query.message.message_id)
    if str(query.from_user.id) != str(ADMIN_ID):
        await query.edit_message_text("⛔ You don't have permission to use this.")
        return ConversationHandler.END
    buttons = add_flow_category_buttons()
    await query.edit_message_text("Step 4/6\n\n📂 Choose the main category:", reply_markup=add_flow_markup(buttons))
    return CATEGORY

async def receive_platform(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    platform = update.message.text.strip()
    if not platform:
        await _ap_reply(update, context, "⚠️ Please enter the platform name.")
        return PLATFORM
    context.user_data["platform"] = platform
    await _ap_reply(update, context, "Step 6/6\n\n🔗 Now send the affiliate/product link.")
    return LINK

async def receive_link(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID): return ConversationHandler.END
    link = update.message.text.strip()
    if not link.startswith(("http://","https://")):
        await _ap_reply(update, context, "⚠️ Please send a valid link starting with http:// or https://")
        return LINK
    _ap_track(context, update.message.chat_id, update.message.message_id)
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
    if context.user_data.get("sub_subcategory"):
        data["sub_subcategory"] = context.user_data["sub_subcategory"]  # only when chosen
    try:
        result = supabase.table("products").insert(data).execute()
        if not result.data: raise Exception("Product was not returned by Supabase.")
        # Success only: remove this session's step/prompt/answer messages, then show just the confirmation
        await _ap_cleanup(context)
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ PRODUCT PUBLISHED!\n\n"
            f"🛍️ {context.user_data['name']}\n"
            f"💰 ₹{context.user_data['sale_price']}\n"
            f"📂 {context.user_data['category_name']}\n"
            f"🗂️ {context.user_data['subcategory']}"
            + (f" › {context.user_data['sub_subcategory']}" if context.user_data.get("sub_subcategory") else "")
            + "\n"
            f"🏪 {context.user_data['platform']}\n\n"
            "The product is now saved in your catalogue.",
            reply_markup=ADMIN_BACK_MARKUP
        )
    except Exception as e:
        print("PRODUCT INSERT ERROR:", e)
        await update.message.reply_text("❌ Product could not be saved.\n\nPlease try again.", reply_markup=ADMIN_BACK_MARKUP)
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_add_product(update, context):
    # Remove only this session's tracked messages (and the /cancel command itself), keep the cancel notice
    _ap_track(context, update.message.chat_id, update.message.message_id)
    await _ap_cleanup(context)
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id, text="❌ Add Product cancelled.", reply_markup=ADMIN_BACK_MARKUP
    )
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

MAIN_MENU_TEXT = (
    "OUTFIT INDIA\n"
    "Your Perfect Outfit, Just a Message Away.\n\n"
    "What are you looking for today?"
)


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


# Same category names that the Add Product flow stores in the "categories" table
PARENT_CATEGORY_NAMES = {
    "men": "Men's Fashion",
    "women": "Women's Fashion",
    "seasonal": "Seasonal Wear",
    "deals": "Deals & Offers",
    "other": "Other",
}


def back_button(parent_key, header_id=None, label=None):
    """Parent-category Back button (never goes to main). The visible text is ALWAYS just "🔙 Back".
    Product messages carry the listing header's message ID in the callback data,
    so the header can be deleted too."""
    callback = f"back_{parent_key}" if header_id is None else f"back_{parent_key}_{header_id}"
    return InlineKeyboardButton(label or "🔙 Back", callback_data=callback)


def category_screen(key):
    """Return (text, keyboard) for a main category's subcategory screen (customer side)."""
    if key not in CATEGORY_ORDER:
        return None, None
    info = safe_category_info(key)
    name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
    try:
        names = active_subcategory_names(key, info["id"] if info else None)
    except Exception as e:
        print("SUBCATEGORY LOAD ERROR:", e)
        names = [value for _label, value in MANAGE_SUBCATEGORIES[key]]

    # Full-width catalogue rows (one button per row), matching the premium menu layout.
    buttons = [
        [InlineKeyboardButton(customer_sub_label(key, n), callback_data=f"sub_{key}_{n}")]
        for n in names if fits_callback(f"sub_{key}_{n}")
    ]
    buttons.append([InlineKeyboardButton("←  Back to Main Menu", callback_data="main")])
    keyboard = buttons

    if key == "men":
        text = f"Here's what you can explore in\n{name}"
    elif key == "women":
        text = f"Explore {name}"
    elif key == "seasonal":
        text = "Choose your season"
    elif key == "other":
        text = f"🛍️  {name}\nBrowse everyday essentials"
    else:
        text = "Limited time offers!\nGrab your favourite outfits at the best prices."
    if not keyboard or len(keyboard) == 1:
        # Only the back button → no subcategory rows
        text = f"{name}\n\nNo collections available here yet."
    return text, InlineKeyboardMarkup(keyboard)


async def safe_edit(query, text, reply_markup=None, parse_mode=None):
    """Edit a text message. If the message is a photo (product card) or cannot be
    edited, send a new message instead so the user is never stuck."""
    msg = query.message
    if msg is not None and (msg.photo or not msg.text):
        await msg.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return
    try:
        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "not modified" in str(e).lower():
            return
        print("EDIT ERROR:", e)
        if msg is not None:
            await msg.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)


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


# ---------------------------------------------------------------------------
# CUSTOMER PRODUCT LIST / PRODUCT DETAILS / SAVED ITEMS  (compact cards)
# Approved design: horizontal card = product image LEFT, name + price RIGHT, ">" at the
# right edge. Telegram cannot lay out text beside an image inside one message, so each
# card is ONE generated picture (the stored product image is only read, never changed
# or re-saved). Everything shown comes from the existing products row: name,
# sale_price and platform. There is no original price / rating / review / discount
# column in the database, so those parts of the design are simply not drawn.
# Callbacks: pd_<product_id> opens Product Details (a new message; the tapped card is removed);
#            pdb closes that details message and brings the removed card back into the list.
# Each card carries ONE slim "View Details ›" button as its Details action (a drawn arrow inside a
# photo cannot be tapped in Telegram). The list's Back button sits under the LAST card - no extra
# text message. If the last card is the one opened, Back moves to the card above it.
# If Pillow or the image download is unavailable, a card falls back to the product
# photo with a text caption, so the list always works.
# ---------------------------------------------------------------------------

try:
    from io import BytesIO
    from PIL import Image, ImageDraw, ImageFont, ImageOps
    PIL_AVAILABLE = True
except Exception:  # Pillow not installed: cards fall back to photo + caption
    PIL_AVAILABLE = False

# Card geometry is measured from the approved reference (Product List card = 258 x 83 px on the
# reference sheet), then scaled x3. Every value below is that measurement x3 (rounded).
CARD_SCALE = 3
CARD_W, CARD_H = 774, 246          # reference ratio 3.1 : 1 (compact, not a banner)
CARD_RADIUS = 24                   # rounded card corners
CARD_OUTER_BG = (0, 12, 19)        # reference screen background behind the card
CARD_BG = (6, 24, 33)              # card panel colour sampled from the reference
CARD_TEXT = (255, 255, 255)
CARD_MUTED = (148, 170, 181)
CARD_THUMB_BG = (200, 205, 210)
CARD_THUMB_INSET = 6               # image sits 2px inside the card edge in the reference
CARD_THUMB_RADIUS = 15
CARD_TEXT_LEFT = 270               # text column starts 90px from the card's left edge
CARD_TEXT_RIGHT_PAD = 84           # room kept free for the ">" indicator
CARD_ARROW_CX = CARD_W - 42        # ">" centre: 14px from the right edge in the reference
CARD_NAME_SIZE, CARD_PRICE_SIZE, CARD_INFO_SIZE = 30, 38, 28   # name 32 -> 30: a bold, wider face at 30 reads like the old condensed 32
CARD_NAME_PITCH, CARD_PRICE_PITCH, CARD_INFO_PITCH = 40, 62, 56
# Product-name typography (readability only; card size/colours unchanged): letter + word spacing, up to 3 lines
CARD_NAME_TRACKING = 1             # extra px between letters
CARD_NAME_WORD_GAP = 5             # extra px added to every word space
CARD_NAME_STROKE = 0               # the old 1px faux-bold stroke filled the dot of "i" (so "i" looked like "l"): a true bold face is used instead
CARD_NAME_SIZE_SMALL, CARD_NAME_PITCH_SMALL, CARD_NAME_MAX_LINES = 27, 35, 3  # used only when 2 lines are not enough
CARD_RENDER_VERSION = "v4"         # bump to invalidate cached generated cards
CARD_RENDER_CONCURRENCY = 4
CARD_FONT_CANDIDATES = {
    "bold": [
        os.path.join(_ASSET_ROOT, "fonts", "bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    # Product name only: optional assets/fonts/name.ttf first (drop in a hyperlegible face such as Atkinson
    # Hyperlegible to improve i / l / I / 1 further), then bold faces, the wider ones BEFORE the condensed one.
    # _card_font("name") additionally skips any face whose "I" and "l" are drawn identically (e.g. Arial/Liberation).
    "name": [
        os.path.join(_ASSET_ROOT, "fonts", "name.ttf"),
        os.path.join(_ASSET_ROOT, "fonts", "bold.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ],
    "regular": [
        os.path.join(_ASSET_ROOT, "fonts", "regular.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    ],
}
_CARD_FONTS = {}
_CARD_FILE_IDS = {}       # (product id, name, price, image) -> Telegram file_id of the generated card
_CARD_FILE_IDS_MAX = 2000
# (chat_id, id of the message carrying the list's Back = normally the LAST card) -> card list record:
#   {"chat_id", "products" (in order), "ids" (message id per product, None while its card is removed),
#    "back_row", "detail_id"/"detail_index" (the open Product Details message and its product index)}
# When no card is left to carry Back (single product opened) the key is (chat_id, "details:<id>").
_LIST_CARD_MESSAGES = {}
_LIST_CARD_MESSAGES_MAX = 300


def _font_separates_il(font):
    """True when capital I and lowercase l are drawn as different shapes (many sans faces draw them alike)."""
    try:
        def shape(c):
            img = Image.new("L", (96, 128), 0)
            ImageDraw.Draw(img).text((20, 10), c, font=font, fill=255)
            return img.getbbox(), img.tobytes()
        (bi, di), (bl, dl) = shape("I"), shape("l")
        return di != dl and (bi is None or bl is None or (bi[3] - bi[1], bi[2] - bi[0]) != (bl[3] - bl[1], bl[2] - bl[0]))
    except Exception:
        return False


def _card_font(kind, size):
    key = (kind, size)
    if key in _CARD_FONTS:
        return _CARD_FONTS[key]
    font, fallback = None, None
    for path in CARD_FONT_CANDIDATES[kind]:
        if os.path.exists(path):
            try:
                candidate = ImageFont.truetype(path, size)
            except Exception:
                continue
            if kind != "name" or _font_separates_il(candidate):
                font = candidate
                break
            if fallback is None:
                fallback = candidate  # readable enough only if nothing better exists
    if font is None:
        font = fallback
    if font is None:
        font = ImageFont.load_default(size)
    _CARD_FONTS[key] = font
    return font


def _font_has_glyph(font, char):
    """True when the font draws `char` differently from its "missing glyph" box."""
    try:
        def render(c):
            img = Image.new("L", (96, 96), 0)
            ImageDraw.Draw(img).text((8, 8), c, font=font, fill=255)
            return img.tobytes()
        return render(char) != render("\uFFFF")
    except Exception:
        return False


def _fit_text(draw, text, font, max_width):
    if draw.textlength(text, font=font) <= max_width:
        return text
    while text and draw.textlength(text + "…", font=font) > max_width:
        text = text[:-1]
    return text.rstrip() + "…"


def _wrap_two_lines(draw, text, font, max_width):
    words, lines, current = (text or "").split(), [], ""
    for i, word in enumerate(words):
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=font) <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
        if len(lines) == 2:
            break
    if len(lines) < 2 and current:
        lines.append(current)
    consumed = " ".join(lines).split()
    if len(consumed) < len(words) and lines:  # text did not fit in two lines
        lines[-1] = _fit_text(draw, lines[-1] + " …", font, max_width)
    return [_fit_text(draw, line, font, max_width) for line in lines[:2]] or [""]


def _tracked_width(draw, text, font, tracking=CARD_NAME_TRACKING, word_gap=CARD_NAME_WORD_GAP):
    """Pixel width of `text` drawn with extra letter spacing and extra word gaps."""
    if not text:
        return 0
    return draw.textlength(text, font=font) + tracking * (len(text) - 1) + word_gap * text.count(" ")


def _tracked_x_positions(draw, text, font, left, tracking=CARD_NAME_TRACKING, word_gap=CARD_NAME_WORD_GAP):
    """x of every character of `text` (kerning-aware prefix widths + tracking + word gaps)."""
    return [left + draw.textlength(text[:i], font=font) + tracking * i + word_gap * text[:i].count(" ")
            for i in range(len(text))]


def _draw_tracked(draw, left, y, text, font, fill, stroke_width=0, tracking=CARD_NAME_TRACKING, word_gap=CARD_NAME_WORD_GAP):
    for ch, x in zip(text, _tracked_x_positions(draw, text, font, left, tracking, word_gap)):
        if ch != " ":
            draw.text((x, y), ch, font=font, fill=fill, anchor="lm", stroke_width=stroke_width, stroke_fill=fill)


def _wrap_name(draw, text, font, max_width, max_lines):
    """Word-wrap a product name into at most `max_lines` lines of tracked width <= max_width.
    A single word wider than a line is split by characters (never glued to its neighbour, never clipped).
    Returns (lines, complete); complete=False means the name did not fit and the last line ends with "…"."""
    words = (text or "").split() or ["Product"]
    lines, current = [], ""
    for word in words:
        while _tracked_width(draw, word, font) > max_width:  # one over-long word: break it by characters
            if current:
                lines.append(current)
                current = ""
            cut = len(word) - 1
            while cut > 1 and _tracked_width(draw, word[:cut], font) > max_width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        trial = f"{current} {word}".strip()
        if not current or _tracked_width(draw, trial, font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) <= max_lines:
        return lines, True
    last_words = " ".join(lines[max_lines - 1:]).split()
    while len(last_words) > 1 and _tracked_width(draw, " ".join(last_words) + "…", font) > max_width:
        last_words.pop()  # drop whole trailing words, never cut a word in half (unless a single word is left)
    last = " ".join(last_words)
    while last and _tracked_width(draw, last + "…", font) > max_width:
        last = last[:-1]
    return lines[:max_lines - 1] + [last.rstrip() + "…"], False


def _name_layout(draw, name, max_width):
    """(font, lines, pitch): normal size on up to 2 lines, otherwise a slightly smaller size on up to 3 lines."""
    text = str(name or "Product")
    font = _card_font("name", CARD_NAME_SIZE)
    lines, complete = _wrap_name(draw, text, font, max_width, 2)
    if complete:
        return font, lines, CARD_NAME_PITCH
    font = _card_font("name", CARD_NAME_SIZE_SMALL)
    lines, _complete = _wrap_name(draw, text, font, max_width, CARD_NAME_MAX_LINES)
    return font, lines, CARD_NAME_PITCH_SMALL


def render_product_card(image_bytes, name, price_value, platform):
    """Return PNG bytes of one compact horizontal product card (needs Pillow).
    Layout = approved reference: image left, name / price / platform right, ">" at the edge."""
    outer = Image.new("RGB", (CARD_W, CARD_H), CARD_OUTER_BG)
    panel = Image.new("RGB", (CARD_W, CARD_H), CARD_BG)
    panel_mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(panel_mask).rounded_rectangle((0, 0, CARD_W - 1, CARD_H - 1), radius=CARD_RADIUS, fill=255)
    outer.paste(panel, (0, 0), panel_mask)
    card = outer
    draw = ImageDraw.Draw(card)

    thumb = CARD_H - 2 * CARD_THUMB_INSET
    if image_bytes:
        try:
            photo = ImageOps.exif_transpose(Image.open(BytesIO(image_bytes))).convert("RGB")
            photo = ImageOps.fit(photo, (thumb, thumb), method=Image.LANCZOS, centering=(0.5, 0.35))
        except Exception:
            photo = Image.new("RGB", (thumb, thumb), CARD_THUMB_BG)
    else:
        photo = Image.new("RGB", (thumb, thumb), CARD_THUMB_BG)
    thumb_mask = Image.new("L", (thumb, thumb), 0)
    ImageDraw.Draw(thumb_mask).rounded_rectangle((0, 0, thumb - 1, thumb - 1), radius=CARD_THUMB_RADIUS, fill=255)
    card.paste(photo, (CARD_THUMB_INSET, CARD_THUMB_INSET), thumb_mask)

    left = CARD_TEXT_LEFT
    max_width = CARD_W - CARD_TEXT_RIGHT_PAD - left
    # Reference text is medium weight (between regular and bold): regular face + a 1px stroke.
    price_font = _card_font("regular", CARD_PRICE_SIZE)
    info_font = _card_font("regular", CARD_INFO_SIZE)

    name_font, name_lines, name_pitch = _name_layout(draw, name, max_width)
    symbol = "₹" if _font_has_glyph(price_font, "₹") else "Rs."
    price_line = f"{symbol}{fmt_price(price_value)}"
    platform = str(platform or "").strip()

    # rows: (text, font, colour, pitch = vertical distance from this row's centre to the next row's centre)
    # rows: (text, font, colour, stroke, pitch); pitch = distance from this row's centre to the next row's
    # (the product name rows are flagged in `tracked`: they are drawn with letter/word spacing)
    rows = [(line, name_font, CARD_TEXT, CARD_NAME_STROKE, name_pitch) for line in name_lines]
    rows[-1] = rows[-1][:4] + (CARD_PRICE_PITCH,)
    tracked = set(range(len(name_lines)))
    rows.append((price_line, price_font, CARD_TEXT, 1, CARD_INFO_PITCH if platform else 0))
    if platform:
        rows.append((_fit_text(draw, platform, info_font, max_width), info_font, CARD_MUTED, 0, 0))

    block = sum(r[4] for r in rows)                  # first centre -> last centre
    y = (CARD_H - block) // 2                        # centre of the first row
    for index, (text, font, colour, stroke, pitch) in enumerate(rows):
        if index in tracked:
            _draw_tracked(draw, left, y, text, font, colour, stroke_width=stroke)
        else:
            draw.text((left, y), text, font=font, fill=colour, anchor="lm", stroke_width=stroke, stroke_fill=colour)
        y += pitch

    cx, cy = CARD_ARROW_CX, CARD_H // 2              # ">" indicator
    draw.line([(cx - 7, cy - 15), (cx + 7, cy), (cx - 7, cy + 15)], fill=CARD_MUTED, width=4, joint="curve")
    out = BytesIO()
    card.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def _download_image_bytes(bot, file_id):
    tg_file = await bot.get_file(file_id)
    return bytes(await tg_file.download_as_bytearray())


async def _prepare_card(bot, product, semaphore):
    """Card PNG bytes for one product, or None (no Pillow / failure -> caller falls back)."""
    if not PIL_AVAILABLE:
        return None
    async with semaphore:
        try:
            image_bytes = None
            if product.get("image_url"):
                try:
                    image_bytes = await _download_image_bytes(bot, product["image_url"])
                except Exception as e:
                    print(f"PRODUCT CARD IMAGE DOWNLOAD ERROR: {type(e).__name__}: {e}")
            return await asyncio.to_thread(
                render_product_card, image_bytes, product.get("name"),
                product.get("sale_price", 0), product.get("platform"),
            )
        except Exception as e:
            print(f"PRODUCT CARD RENDER ERROR: {type(e).__name__}: {e}")
            return None


def detail_open_markup(product_id, back_row=None):
    """Slim "View Details ›" button for one card (+ the list's Back row when this is the last card)."""
    rows = []
    cb = f"pd_{product_id}"
    if product_id is not None and fits_callback(cb):
        rows.append([InlineKeyboardButton("View Details ›", callback_data=cb)])
    if back_row:
        rows.append(list(back_row))
    return InlineKeyboardMarkup(rows) if rows else None


def _card_cache_key(product):
    return (CARD_RENDER_VERSION, str(product.get("id")), str(product.get("name")), str(product.get("sale_price")),
            str(product.get("platform")), str(product.get("image_url")))


def _register_list_session(chat_id, carrier, session):
    """File a card list under the message that carries its Back button (oldest entry dropped when full)."""
    if len(_LIST_CARD_MESSAGES) >= _LIST_CARD_MESSAGES_MAX:
        _LIST_CARD_MESSAGES.pop(next(iter(_LIST_CARD_MESSAGES)))
    _LIST_CARD_MESSAGES[(chat_id, carrier)] = session


def _carrier_of(session):
    """Id of the message currently carrying this list's Back button."""
    for (_chat_id, carrier), value in _LIST_CARD_MESSAGES.items():
        if value is session:
            return carrier
    return None


def _rekey_list_session(session, carrier):
    for key in [k for k, v in _LIST_CARD_MESSAGES.items() if v is session]:
        _LIST_CARD_MESSAGES.pop(key, None)
    _register_list_session(session["chat_id"], carrier, session)


def _find_list_session(chat_id, message_id):
    """The card list that owns this message (one of its cards, or its open Details message)."""
    for session in _LIST_CARD_MESSAGES.values():
        if session["chat_id"] == chat_id and (message_id in session["ids"] or message_id == session.get("detail_id")):
            return session
    return None


def _details_open_for_product(chat_id, product_id):
    for session in _LIST_CARD_MESSAGES.values():
        index = session.get("detail_index")
        if (session["chat_id"] == chat_id and index is not None
                and str(session["products"][index].get("id")) == str(product_id)):
            return True
    return False


async def send_product_cards(query, products, back_row=None, session=None, offset=0, header_id=None, footer=None):
    """Send one compact card per product (in the given order). back_row (the list's Back button) is
    attached to the LAST card, so no extra text message is needed. Returns the sent message ids.
    session/offset are used only when Product Details closes: the cards from position `offset` on
    are re-sent into the existing list record instead of creating a new one."""
    bot = query.get_bot()
    chat_id = query.message.chat_id
    semaphore = asyncio.Semaphore(CARD_RENDER_CONCURRENCY)
    todo = [p for p in products if _card_cache_key(p) not in _CARD_FILE_IDS]
    rendered = dict(zip(
        (_card_cache_key(p) for p in todo),
        await asyncio.gather(*[_prepare_card(bot, p, semaphore) for p in todo]),
    ))
    sent_ids = []
    card_ids = []  # message id per product (None when that card could not be sent)
    last_index = len(products) - 1
    last_has_back = False
    for index, product in enumerate(products):
        markup = detail_open_markup(product.get("id"), back_row if index == last_index else None)
        key = _card_cache_key(product)
        sent = None
        try:
            photo = _CARD_FILE_IDS.get(key) or rendered.get(key)
            if photo is not None:
                sent = await bot.send_photo(chat_id=chat_id, photo=photo, reply_markup=markup)
                if key not in _CARD_FILE_IDS and getattr(sent, "photo", None):
                    if len(_CARD_FILE_IDS) >= _CARD_FILE_IDS_MAX:
                        _CARD_FILE_IDS.clear()
                    _CARD_FILE_IDS[key] = sent.photo[-1].file_id
        except Exception as e:
            print(f"PRODUCT CARD SEND ERROR: {type(e).__name__}: {e}")
            sent = None
        if sent is None:  # fallback: product photo + text caption, still opens Details
            caption = f"{product.get('name', 'Product')}\n₹{fmt_price(product.get('sale_price', 0))}"
            try:
                if product.get("image_url"):
                    sent = await send_product_image(
                        bot, chat_id, product["image_url"], caption=caption,
                        reply_markup=markup, message=query.message,
                    )
                if sent is None:
                    sent = await query.message.reply_text(caption, reply_markup=markup)
            except Exception as e:
                print(f"PRODUCT CARD FALLBACK ERROR: {type(e).__name__}: {e}")
        if sent is not None:
            sent_ids.append(sent.message_id)
            card_ids.append(sent.message_id)
            if index == last_index:
                last_has_back = True
        else:
            card_ids.append(None)
    if session is not None:  # Details closed: put the re-sent cards back into the existing list record
        for position, message_id in enumerate(card_ids):
            session["ids"][offset + position] = message_id
        if back_row:
            if last_has_back:
                _rekey_list_session(session, sent_ids[-1])
            else:  # the last card could not be sent: never leave the customer without a Back button
                await send_list_footer(query, session)
    elif back_row:
        session = {
            "chat_id": chat_id, "products": list(products), "ids": card_ids,
            "back_row": list(back_row), "detail_id": None, "detail_index": None,
            "header_id": header_id,  # Product List header: stays while Details is open, removed on list Back
            "footer": footer, "footer_id": None,  # optional (text, markup) message kept BELOW the cards (Search pagination)
        }
        if last_has_back:
            _register_list_session(chat_id, sent_ids[-1], session)
        else:  # the last card could not be sent: never leave the customer without a Back button
            await send_list_footer(query, session)
    return sent_ids


async def send_list_footer(query, session, text="🔙"):
    """Failure fallback only: a minimal message that carries Back when the last card failed to send."""
    sent = await query.message.reply_text(text, reply_markup=InlineKeyboardMarkup([session["back_row"]]))
    if sent is not None and any(message_id is not None for message_id in session["ids"]):
        _rekey_list_session(session, sent.message_id)
    return sent


class _ChatQueryShim:
    """Lets send_product_cards run from a plain text-message update (typed search): only what it uses --
    get_bot() and message.chat_id / reply_text / reply_photo -- sending to the chat, nothing quoted."""

    class _Msg:
        def __init__(self, bot, chat_id):
            self._bot, self.chat_id = bot, chat_id
            self.photo, self.text, self.message_id = None, None, None

        async def reply_text(self, text, **kwargs):
            return await self._bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

        async def reply_photo(self, photo, **kwargs):
            return await self._bot.send_photo(chat_id=self.chat_id, photo=photo, **kwargs)

    def __init__(self, bot, chat_id):
        self._bot = bot
        self.message = self._Msg(bot, chat_id)

    def get_bot(self):
        return self._bot


async def send_list_session_footer(query, session):
    """Search only: (re)send the pagination footer so it stays UNDER the cards. Replaces the old footer."""
    footer = session.get("footer")
    if not footer:
        return None
    if session.get("footer_id") is not None:
        await delete_by_id_safe(query.get_bot(), session["chat_id"], session["footer_id"])
        session["footer_id"] = None
    try:
        sent = await query.get_bot().send_message(chat_id=session["chat_id"], text=footer[0], reply_markup=footer[1])
        session["footer_id"] = sent.message_id if sent is not None else None
    except Exception as e:
        print(f"SEARCH FOOTER SEND ERROR: {type(e).__name__}: {e}")
    return session.get("footer_id")


async def purge_list_cards(bot, query):
    """Back pressed under a card list: remove the list's other cards (and its open Details) too (best effort)."""
    msg = query.message
    if msg is None:
        return
    session = _LIST_CARD_MESSAGES.pop((msg.chat_id, msg.message_id), None)
    if not session:
        return
    for message_id in session["ids"]:
        if message_id is not None and message_id != msg.message_id:
            await delete_by_id_safe(bot, msg.chat_id, message_id)
    if session.get("detail_id") is not None:
        await delete_by_id_safe(bot, msg.chat_id, session["detail_id"])
    if session.get("header_id") is not None and session["header_id"] != msg.message_id:
        await delete_by_id_safe(bot, msg.chat_id, session["header_id"])
    if session.get("footer_id") is not None:
        await delete_by_id_safe(bot, msg.chat_id, session["footer_id"])


async def hide_list_card(query, session, index, card_id, details_id):
    """View Details: remove ONLY the opened product's card (its "View Details ›" button goes with it).
    If that card carried the list's Back button, Back moves to the nearest card above it."""
    bot = query.get_bot()
    chat_id = session["chat_id"]
    ids, products = session["ids"], session["products"]
    if _carrier_of(session) == card_id:
        above = next((i for i in range(index - 1, -1, -1) if ids[i] is not None), None)
        if above is None:  # no card left to carry Back: the Details message's own Back returns to the list
            _rekey_list_session(session, f"details:{details_id}")
        else:
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=ids[above],
                    reply_markup=detail_open_markup(products[above].get("id"), session["back_row"]),
                )
                _rekey_list_session(session, ids[above])
            except Exception as e:
                print(f"PRODUCT LIST BACK MOVE ERROR: {type(e).__name__}: {e}")
                await send_list_footer(query, session)
    await delete_by_id_safe(bot, chat_id, card_id)
    ids[index] = None
    session["detail_id"] = details_id
    session["detail_index"] = index


async def restore_list_card(query, session):
    """Details closed: bring back the card removed when Details opened, in its original position.
    Telegram cannot insert a message into the middle of a chat, so the cards after it are re-sent too
    (the cards before it are untouched). Order is preserved, Back ends up under the last card, and
    nothing is duplicated."""
    bot = query.get_bot()
    chat_id = session["chat_id"]
    index = session["detail_index"]
    products, ids = session["products"], session["ids"]
    old_carrier = _carrier_of(session)
    session["detail_id"] = None
    session["detail_index"] = None
    deleted = set()
    for i in range(index + 1, len(ids)):
        if ids[i] is not None:
            await delete_by_id_safe(bot, chat_id, ids[i])
            deleted.add(ids[i])
            ids[i] = None
    await send_product_cards(query, products[index:], session["back_row"], session=session, offset=index)
    await send_list_session_footer(query, session)  # no-op for normal Product Lists
    new_carrier = _carrier_of(session)
    if isinstance(old_carrier, int) and old_carrier != new_carrier and old_carrier not in deleted:
        if old_carrier in ids:  # a card above the restored one carried Back: it only keeps its Details button
            try:
                await bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=old_carrier,
                    reply_markup=detail_open_markup(products[ids.index(old_carrier)].get("id")),
                )
            except Exception as e:
                print(f"PRODUCT LIST BACK RESET ERROR: {type(e).__name__}: {e}")
        else:  # an old footer message
            await delete_by_id_safe(bot, chat_id, old_carrier)


PRICE_DISCLAIMER_TEXT = "The price may not be exactly the same."  # shown under the price on EVERY Product Details


def count_text(count):
    return f"{count} product{'s' if count != 1 else ''} found"


async def show_product_details(query, product_id):
    """Product Details (approved design #5) as a new message under the list. The tapped product card
    is removed from the list (hide_list_card) and comes back when Details closes (restore_list_card)."""
    back_row = [InlineKeyboardButton("🔙 Back", callback_data="pdb")]
    msg = query.message
    session, index, card_id = None, None, None
    if msg is not None:
        session = _find_list_session(msg.chat_id, msg.message_id)
        if session is not None and msg.message_id in session["ids"]:
            index = session["ids"].index(msg.message_id)
            card_id = msg.message_id
        else:
            session = None
            if _details_open_for_product(msg.chat_id, product_id):
                return  # double tap on a card whose Details is already open: never open a second one
    if session is not None and session.get("detail_index") is not None:
        # one Details per list: close the open one first (its card comes back), then open this one
        other_details_id = session["detail_id"]
        await restore_list_card(query, session)
        await delete_by_id_safe(query.get_bot(), msg.chat_id, other_details_id)
        card_id = session["ids"][index]  # cards after the restored one were re-sent: may be a new message
        if card_id is None:
            session = None
    try:
        product = await asyncio.to_thread(fetch_product, product_id)
    except Exception as e:
        print(f"PRODUCT DETAILS FETCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await query.message.reply_text("❌ Could not load this product.", reply_markup=InlineKeyboardMarkup([back_row]))
        return
    if not product or product.get("is_active") is False:
        await query.message.reply_text("This product is no longer available.", reply_markup=InlineKeyboardMarkup([back_row]))
        return
    user_id = query.from_user.id
    pid = product.get("id")
    try:
        saved = await asyncio.to_thread(is_product_saved, user_id, pid)
    except Exception as e:
        print(f"PRODUCT DETAILS SAVED CHECK ERROR: {type(e).__name__}: {safe_error_text(e)}")
        saved = False
    lines = [str(product.get("name") or "Product"), f"₹{fmt_price(product.get('sale_price', 0))}", PRICE_DISCLAIMER_TEXT]
    platform = str(product.get("platform") or "").strip()
    if platform:
        lines.append(f"Available on {platform}")
    caption = "\n".join(lines)
    markup = product_card_markup(pid, user_id, product.get("affiliate_link"), is_saved=saved, back_row=back_row, detail=True)
    sent = None
    try:
        if product.get("image_url"):
            sent = await send_product_image(
                query.get_bot(), query.message.chat_id, product["image_url"],
                caption=caption, reply_markup=markup, message=query.message,
            )
        if sent is None:
            sent = await query.message.reply_text(caption, reply_markup=markup)
    except Exception as e:
        print(f"PRODUCT DETAILS DISPLAY ERROR: {type(e).__name__}: {e}")
        sent = await query.message.reply_text(caption, reply_markup=markup)
    if session is not None and sent is not None:
        try:
            await hide_list_card(query, session, index, card_id, sent.message_id)
        except Exception as e:
            print(f"PRODUCT LIST CARD HIDE ERROR: {type(e).__name__}: {e}")


async def close_product_details(query):
    """Details Back: remove the details message and bring the opened product's card back into the list
    it was opened from (same position, same order, Back under the last card)."""
    msg = query.message
    session = _find_list_session(msg.chat_id, msg.message_id) if msg is not None else None
    if session is not None and session.get("detail_id") == msg.message_id and session.get("detail_index") is not None:
        try:
            await restore_list_card(query, session)
        except Exception as e:
            print(f"PRODUCT LIST RESTORE ERROR: {type(e).__name__}: {e}")
    if not await delete_message_safe(query.message):
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass


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
        if parent_key and (info is None or category_is_hidden(info)):
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
            f"{subcategory}\n\nNo products have been added here yet.",
            back_markup
        )
        return

    header_caption = f"{subcategory}\n{count_text(len(products))}"
    sub_path = subcategory_asset_path(subcategory)
    header_id = None
    if sub_path:
        # Prefer a premium subcategory visual as the listing header
        try:
            bot = query.get_bot() if hasattr(query, "get_bot") else query.message.get_bot()
            msg = query.message
            if msg is not None and msg.photo:
                media_src = _asset_media_source(sub_path)
                try:
                    media = InputMediaPhoto(media=media_src, caption=header_caption)
                    edited = await query.edit_message_media(media=media)
                finally:
                    if hasattr(media_src, "close"):
                        try:
                            media_src.close()
                        except Exception:
                            pass
                _cache_file_id_from_message(sub_path, edited if edited is not None else msg)
                header_id = msg.message_id
            else:
                banner = await send_catalogue_photo(
                    bot, msg.chat_id, sub_path, header_caption
                )
                if banner is not None:
                    header_id = banner.message_id
                    if msg is not None and msg.text:
                        await delete_message_safe(msg)
        except Exception as e:
            print(f"SUBCATEGORY BANNER ERROR: {e}")
            header_id = None
    if header_id is None:
        header_id = await send_header(query, header_caption)

    # Compact cards (approved design #4); each opens Product Details. Back sits under the list.
    back_row = (
        [back_button(parent_key, header_id, "🔙 Back")]
        if parent_key
        else [InlineKeyboardButton("🔙 Main Menu", callback_data="main")]
    )
    await send_product_cards(query, products, back_row, header_id=header_id)
    await attach_sort_button(query, header_id, sort_callback_subcategory(parent_key, subcategory), len(products))

    # Product Tracking: one "view" per product shown; query.id makes Telegram retries count once
    await record_tracking_events([
        tracking_row(p["id"], EVENT_VIEW, query.from_user.id, f"{query.id}:{p['id']}:{EVENT_VIEW}")
        for p in products if p.get("id") is not None
    ])


# ---------------------------------------------------------------------------
# Generic hierarchy (customer side): Category -> Subcategory -> Sub-subcategory -> Products
# Child options come from the subcategories table (parent_id). Nothing is hard-coded.
# Callbacks: ss_<child row id> (products of one child), sso_<subcategory row id> (products of a
# subcategory that have no third-level value).
# ---------------------------------------------------------------------------

def subcategory_icon(key, name):
    known = DEFAULT_SUBCATEGORY_LABELS.get((key, name))
    if known and known.split():
        return known.split()[0]
    return fashion_icon_for_name(name)


async def show_child_selection(query, key, category_name, sub_row, children):
    """Child categories: one full-width button per row, exact DB name + ›, Back."""
    sub_name = sub_row["name"]
    text = f"{sub_name}\n\nChoose an option:"

    # Full-width catalogue rows (same style as Men's Fashion subcategory screen)
    rows = []
    for child in children:
        cb = f"ss_{child['id']}"
        label = customer_nav_label(child.get("name"))
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    # products of this subcategory that were never given a child stay reachable
    try:
        if await asyncio.to_thread(has_unassigned_products, sub_row["category_id"], sub_name):
            other_label = customer_nav_label(f"Other {sub_name}")
            rows.append([InlineKeyboardButton(other_label, callback_data=f"sso_{sub_row['id']}")])
    except Exception as e:
        print("UNASSIGNED PRODUCTS CHECK ERROR:", e)
    rows.append([InlineKeyboardButton(f"← Back to {category_name}", callback_data=f"back_{key}")])
    markup = InlineKeyboardMarkup(rows)

    msg = query.message
    if msg is not None and msg.photo:
        # Coming from a photo screen: show the text screen and remove the old photo message
        await msg.reply_text(text, reply_markup=markup)
        await delete_message_safe(msg)
        return
    await safe_edit(query, text, markup)


async def show_children_if_any(query, key, name):
    """Customer: show the child screen of a subcategory when it has active children.
    Returns True if shown. False means 'no children' -> the caller lists products directly."""
    try:
        info = get_category_info(key)
        if not info or category_is_hidden(info):
            return False
        sub_row, children = await asyncio.to_thread(active_children_by_name, info["id"], name)
    except Exception as e:
        print("CHILD CATEGORY LOAD ERROR:", e)
        return False
    if not sub_row or not children:
        return False
    await show_child_selection(query, key, info["name"], sub_row, children)
    return True


def fetch_products_for_child(category_id, subcategory_name, child_name):
    """Active products matching the full hierarchy (child_name None = no third-level value)."""
    query = (
        supabase.table("products")
        .select("id,name,sale_price,platform,affiliate_link,image_url")
        .eq("category_id", category_id)
        .eq("subcategory", subcategory_name)
        .eq("is_active", True)
    )
    query = query.is_("sub_subcategory", "null") if child_name is None else query.eq("sub_subcategory", child_name)
    return query.execute().data or []


async def show_products_by_child(query, row_id, other=False):
    """Products of one third-level category (ss_<id>) or of a subcategory's unassigned products (sso_<id>)."""
    try:
        row = get_subcategory_row(row_id)
        if other:
            sub_row, child_name = row, None
        else:
            sub_row = get_subcategory_row(row["parent_id"]) if row and row.get("parent_id") is not None else None
            child_name = row["name"] if row else None
        if not row or not sub_row:
            await safe_edit(query, "⚠️ This section is no longer available.", main_menu_button())
            return
        key = key_for_category_id(sub_row["category_id"])
        info = get_category_info(key) if key else None
    except Exception as e:
        print("CHILD CATEGORY LOOKUP ERROR:", e)
        await safe_edit(query, "❌ Could not load products.", main_menu_button())
        return

    category_id = sub_row["category_id"]
    base_subcategory = sub_row["name"]
    back_cb = f"sub_{key}_{base_subcategory}"
    if not key or not fits_callback(back_cb):
        back_cb = f"back_{key}" if key else "main"
    back_to_parent = InlineKeyboardMarkup([[
        InlineKeyboardButton(f"🔙 Back to {base_subcategory}", callback_data=back_cb)
    ]])

    if not info or category_is_hidden(info):
        await safe_edit(query, "🚫 This category is currently unavailable.", main_menu_button())
        return
    if sub_row.get("is_active") is False or (not other and row.get("is_active") is False):
        await safe_edit(query, "🚫 This section is currently unavailable.", back_to_parent)
        return

    try:
        products = await asyncio.to_thread(fetch_products_for_child, category_id, base_subcategory, child_name)
    except Exception as e:
        print("CHILD PRODUCT FETCH ERROR:", e)
        await safe_edit(query, "❌ Could not load products.", back_to_parent)
        return

    title = f"{base_subcategory}" + (f" · {child_name}" if child_name else " · Other")
    if not products:
        await safe_edit(query, f"{title}\n\nNo products available here yet.", back_to_parent)
        return

    header_id = await send_header(query, f"{title}\n{count_text(len(products))}")

    user_id = query.from_user.id
    await send_product_cards(query, products, [InlineKeyboardButton("🔙 Back", callback_data=back_cb)], header_id=header_id)
    await attach_sort_button(query, header_id, sort_callback_child(row_id, other), len(products))

    await record_tracking_events([
        tracking_row(
            p["id"], EVENT_VIEW, user_id,
            f"child:{key}:{base_subcategory}:{child_name or '-'}:{p['id']}:{EVENT_VIEW}",
        )
        for p in products if p.get("id") is not None
    ])


# ---------------------------------------------------------------------------
# PRODUCT LIST -> "💰 Price: Low to High"
# The button sits on the list's header message (the one above the cards). Callback data is self-contained
# (psort_s_<category key>_<subcategory> / psort_c_<child row id> / psort_o_<subcategory row id>), so it works
# for every category/subcategory/child list without any state and survives a restart.
# Sorting re-reads ONLY the products of that one list (same filters as the list itself), sorts them in Python
# by their numeric price, sends the new cards, then releases the customer's page lock RIGHT AWAY (the existing
# single_flight mechanism: no sleep, no timer, no second lock) and finally deletes the old cards.
# Search results are not sorted here: they are paginated by the database (see limitations).
# ---------------------------------------------------------------------------
SORT_BUTTON_LABEL = "💰 Price: Low to High"
SORT_CALLBACK_PREFIX = "psort_"


def parse_price(value):
    """Numeric Decimal for a stored price (int/float/Decimal or text such as "₹1,099", "Rs. 499/-", "999.00");
    None when missing, not a number, negative or not finite."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float, Decimal)):
            number = Decimal(str(value))
        else:
            text = re.sub(r"^(?:₹|rs\.?|inr)\s*", "", str(value).strip(), flags=re.IGNORECASE)
            text = re.sub(r"\s*/-\s*$", "", text).replace(",", "").replace(" ", "")
            if not re.fullmatch(r"\d+(?:\.\d*)?|\.\d+", text):
                return None
            number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite() or number < 0:
        return None
    return number


def sort_products_by_price(products):
    """New list, cheapest first by NUMERIC price. Missing/invalid prices go last; ties keep their order."""
    def key(product):
        price = parse_price(product.get("sale_price")) if isinstance(product, dict) else None
        return (1, Decimal(0)) if price is None else (0, price)
    return sorted(products or [], key=key)


def sort_callback_subcategory(parent_key, subcategory):
    if not parent_key or not subcategory:
        return None
    return f"{SORT_CALLBACK_PREFIX}s_{parent_key}_{subcategory}"


def sort_callback_child(row_id, other=False):
    return f"{SORT_CALLBACK_PREFIX}{'o' if other else 'c'}_{row_id}"


async def attach_sort_button(query, header_id, callback_data, product_count):
    """Put the sort button on the list header once the cards are rendered (2+ products). Never raises."""
    if header_id is None or product_count < 2 or not callback_data or not fits_callback(callback_data):
        return
    try:
        await query.get_bot().edit_message_reply_markup(
            chat_id=query.message.chat_id, message_id=header_id,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(SORT_BUTTON_LABEL, callback_data=callback_data)]]),
        )
    except Exception as e:
        print(f"SORT BUTTON ATTACH ERROR: {type(e).__name__}: {e}")
        return
    # The list is fully rendered and its header now carries a NEW keyboard (the sort button): that is a new page,
    # so free this action's page lock now (same single_flight mechanism, no timer). Without this the header would
    # inherit the "old screen" duplicate window and a Sort tap right after the list appeared would be swallowed.
    # A queued tap of the OLD menu button is still rejected: that button is no longer on the header (_RECENT_PAGE).
    action = _LOCK_OWNER.get()
    if action is not None and action.message_id == header_id:
        _release_page_lock(action)


def fetch_products_for_subcategory(category_id, subcategory):
    """Active products of one subcategory: the same query the Product List uses."""
    return (
        supabase.table("products")
        .select("id,name,sale_price,platform,affiliate_link,image_url")
        .eq("category_id", category_id)
        .eq("subcategory", subcategory)
        .eq("is_active", True)
        .execute()
    ).data or []


def _load_sort_scope(data):
    """Blocking part of a sort tap: returns {"state": "ok"|"unavailable"|"error", "products", "back" (callback data
    of the list's Back button), "back_key"} for the ONE list the tapped header belongs to. Same visibility rules
    as the list itself (hidden category / subcategory / section are not shown)."""
    rest = data[len(SORT_CALLBACK_PREFIX):]
    if rest.startswith("s_"):
        key, _sep, name = rest[2:].partition("_")
        if key not in CATEGORY_ORDER or not name:
            return {"state": "unavailable"}
        info = get_category_info(key)
        if info is None or category_is_hidden(info) or is_subcategory_hidden(info["id"], name):
            return {"state": "unavailable"}
        return {"state": "ok", "products": fetch_products_for_subcategory(info["id"], name), "back_key": key}
    if rest[:2] in ("c_", "o_"):
        other, row_id = rest.startswith("o_"), rest[2:]
        row = get_subcategory_row(row_id)
        if other:
            sub_row, child_name = row, None
        else:
            sub_row = get_subcategory_row(row["parent_id"]) if row and row.get("parent_id") is not None else None
            child_name = row["name"] if row else None
        if not row or not sub_row:
            return {"state": "unavailable"}
        key = key_for_category_id(sub_row["category_id"])
        info = get_category_info(key) if key else None
        if not info or category_is_hidden(info):
            return {"state": "unavailable"}
        if sub_row.get("is_active") is False or (not other and row.get("is_active") is False):
            return {"state": "unavailable"}
        back_cb = f"sub_{key}_{sub_row['name']}"
        if not key or not fits_callback(back_cb):
            back_cb = f"back_{key}" if key else "main"
        return {
            "state": "ok", "back": back_cb,
            "products": fetch_products_for_child(sub_row["category_id"], sub_row["name"], child_name),
        }
    return {"state": "unavailable"}


def _session_for_header(chat_id, header_id):
    for session in _LIST_CARD_MESSAGES.values():
        if session.get("chat_id") == chat_id and session.get("header_id") == header_id:
            return session
    return None


async def show_sorted_products(query, data):
    """Price: Low to High. Runs inside the existing single_flight guard (rapid taps are rejected there)."""
    msg = query.message
    if msg is None:
        return
    chat_id, header_id = msg.chat_id, msg.message_id
    old_session = _session_for_header(chat_id, header_id)
    if old_session is not None and old_session.get("sorted"):
        return  # this list is already sorted (e.g. a tap queued behind the one that sorted it): nothing to redo
    try:
        scope = await asyncio.to_thread(_load_sort_scope, data)
    except Exception as e:
        print(f"SORT LOAD ERROR: {type(e).__name__}: {safe_error_text(e)}")
        scope = {"state": "error"}
    if scope["state"] != "ok":
        text = "🚫 This section is currently unavailable." if scope["state"] == "unavailable" else "❌ Could not sort products."
        await msg.reply_text(text, reply_markup=main_menu_button())
        return
    products = sort_products_by_price(scope["products"])
    if not products:
        await msg.reply_text("No products available here yet.", reply_markup=main_menu_button())
        return
    if scope.get("back_key"):
        back_row = [back_button(scope["back_key"], header_id, "🔙 Back")]
    else:
        back_row = [InlineKeyboardButton("🔙 Back", callback_data=scope["back"])]

    # Take the old list out of the session table FIRST (nothing awaits before this), render the new list,
    # and only then remove the old cards - if rendering fails the old list is put back untouched.
    old_carrier = _carrier_of(old_session) if old_session is not None else None
    if old_session is not None:
        for key in [k for k, v in _LIST_CARD_MESSAGES.items() if v is old_session]:
            _LIST_CARD_MESSAGES.pop(key, None)
    new_ids = await send_product_cards(query, products, back_row, header_id=header_id)
    if not new_ids:
        if old_session is not None and old_carrier is not None:
            _register_list_session(chat_id, old_carrier, old_session)
        return
    new_session = _session_for_header(chat_id, header_id)
    if new_session is not None:
        new_session["sorted"] = True
    # The NEW sorted list is on screen: free this action's page lock now (idempotent, only its own entry).
    action = _LOCK_OWNER.get()
    if action is not None:
        _release_page_lock(action)
    if old_session is not None:
        old_ids = [m for m in old_session["ids"] if m is not None]
        old_ids += [m for m in (old_session.get("detail_id"), old_session.get("footer_id")) if m is not None]
        await asyncio.gather(*[delete_by_id_safe(query.get_bot(), chat_id, m) for m in old_ids])


async def handle_product_category(query):
    data = query.data or ""

    # Third-level product listing: sso_<subcategory id> (unassigned) / ss_<child id>
    if data.startswith("sso_"):
        await show_products_by_child(query, data[len("sso_"):], other=True)
        return True
    if data.startswith("ss_"):
        await show_products_by_child(query, data[len("ss_"):])
        return True

    if data.startswith("sub_"):
        key, _sep, name = data[len("sub_"):].partition("_")
        if key in CATEGORY_ORDER and name:
            # Generic: children first when the subcategory has any, otherwise its products
            if not await show_children_if_any(query, key, name):
                await show_products_by_subcategory(query, name, key)
            return True
        return False
    subcategory = PRODUCT_SUBCATEGORIES.get(data)
    if subcategory:
        await show_products_by_subcategory(query, subcategory, parent_from_callback(data))
        return True
    return False


async def main_callback(update, context):
    """Fallback inside the Add Product conversation: Main Menu ends it cleanly."""
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await show_main_menu(query, str(query.from_user.id) == str(ADMIN_ID))
    return ConversationHandler.END


@single_flight
async def button_handler(update, context):
    """Original handler + cleanup: a product list keeps its Back button on the LAST card; once Back
    has been handled, that card is removed too (back_/saved_back_ handlers delete it themselves)."""
    query = update.callback_query
    data = (query.data or "") if query is not None else ""
    msg = query.message if query is not None else None
    owner = msg if (msg is not None and not data.startswith("pd_")
                    and (msg.chat_id, msg.message_id) in _LIST_CARD_MESSAGES) else None
    await _button_handler_core(update, context)
    if owner is not None and owner.photo and not data.startswith(("back_", "saved_back_")):
        await delete_message_safe(owner)


async def _button_handler_core(update, context):
    query = update.callback_query
    data = query.data or ""
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    await register_or_touch_user(query.from_user)

    # Save/Unsave toggle: do NOT answer here — handle_saved_toggle answers with a toast.
    if data.startswith("saved_toggle_"):
        product_id = data[len("saved_toggle_"):]
        if product_id:
            await handle_saved_toggle(query, context, product_id)
        else:
            try:
                await query.answer()
            except Exception:
                pass
        return

    # Saved-items product Back: delete product + header + main menu
    if data.startswith("saved_back_"):
        await purge_list_cards(context.bot, query)
        rest = data[len("saved_back_"):]
        product_id, header_id = rest, None
        if "_" in rest:
            pid_part, _, maybe_hdr = rest.rpartition("_")
            if maybe_hdr.isdigit() and pid_part:
                product_id, header_id = pid_part, int(maybe_hdr)
        await handle_saved_back(query, context, product_id, header_id)
        return

    # Customer search product Back: delete search result messages + main menu
    if data == "search_c_back" or data.startswith("search_c_back_"):
        await handle_search_c_back(query, context, data)
        return

    # Answer all other callbacks immediately so Telegram stops the loading spinner
    try:
        await query.answer()
    except Exception:
        pass

    # Product Details (open / Back). Customer-safe: reads one active product only.
    if data == "pdb":
        await close_product_details(query)
        return
    if data.startswith("pd_") and len(data) > 3:
        await show_product_details(query, data[len("pd_"):])
        return

    # Product List: "💰 Price: Low to High" (sits on the list header; must NOT purge the list as a Back would)
    if data.startswith(SORT_CALLBACK_PREFIX):
        await show_sorted_products(query, data)
        return

    # Any other button pressed on a card list's last card is its Back: remove the list's other cards
    await purge_list_cards(context.bot, query)

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

    # 2a-2. Admin Dashboard (admin-only, checked inside the handler)
    if data == "admin_dashboard" or data.startswith("dashboard_"):
        await handle_dashboard_callback(query, context, data)
        return

    # 2a-3. Admin Product Tracking (admin-only, checked inside the handler)
    if data == "admin_tracking" or data.startswith("pt_"):
        await handle_tracking_callback(query, context, data)
        return

    # 2a-4. Admin Users & Broadcast (admin-only, checked inside the handler)
    if data == "admin_users" or data.startswith("ub_"):
        await handle_users_broadcast_callback(query, context, data)
        return

    # 2a-5. Product Search results pagination (customer + admin)
    if data.startswith("search_c_page_") or data.startswith("search_a_page_"):
        try:
            await query.answer()  # stop the loading spinner (these routes return before the generic answer)
        except Exception:
            pass
        await handle_search_callback(query, context, data)
        return

    # 2a-6. Saved Items list / pagination (customer)
    if data == "saved_home" or data.startswith("saved_page_"):
        try:
            await query.answer()
        except Exception:
            pass
        await handle_saved_callback(query, context, data)
        return

    # 2b. Product subcategory screens
    if await handle_product_category(query):
        return

    # 3. Category screens (also used by the product "Back" buttons)
    category_key = None
    header_id = None
    if data in CATEGORY_ORDER:
        category_key = data
    elif data.startswith("back_"):
        parts = data.split("_")
        if len(parts) >= 2 and parts[1] in CATEGORY_ORDER:
            category_key = parts[1]
            if len(parts) >= 3 and parts[2].isdigit():
                header_id = int(parts[2])

    if category_key:
        _info, state = category_state(category_key)
        if state in ("hidden", "deleted"):
            await safe_edit(query, "🚫 This category is currently unavailable.", main_menu_button())
            return
        msg = query.message
        if data.startswith("back_") and msg is not None:
            # Delete the listing header that belongs to this product (if known)
            if header_id is not None and header_id != msg.message_id:
                await delete_by_id_safe(context.bot, msg.chat_id, header_id)
            if msg.photo:
                # Delete ONLY this product message, then show the parent category visual
                await delete_message_safe(msg)
        await show_customer_category(query, context, category_key)
        return

    # 4. Admin screens (admin_add is handled ONLY by the ConversationHandler)
    if data == "admin":
        if not is_admin:
            await safe_edit(query, "⛔ You don't have permission to access the Admin Panel.")
            return
        await show_admin_panel(query)
    else:
        await safe_edit(query, "🛍️ Products will appear here soon.", main_menu_button())


# ---------------------------------------------------------------------------
# ADMIN PANEL -> DASHBOARD
# Callbacks: admin_dashboard (open), dashboard_refresh, dashboard_back.
# Read-only: every number comes live from the existing products, categories and
# subcategories tables. Nothing is written, no new table, no click tracking.
# Convention (same as the rest of the bot): a row is Hidden only when
# is_active is False; everything else counts as Active, so Active + Hidden = Total.
# ---------------------------------------------------------------------------

DASHBOARD_PAGE = 1000  # rows per request (Supabase returns at most 1000 per call)
DASHBOARD_MAX_PLATFORMS = 15
DASHBOARD_NAME_MAX = 40


def dashboard_fetch_all(table, columns):
    """Every row of a table, read in pages so counts are never cut off at 1000 rows."""
    rows = []
    start = 0
    while True:
        chunk = (
            supabase.table(table).select(columns)
            .order("id").range(start, start + DASHBOARD_PAGE - 1)
            .execute().data or []
        )
        rows.extend(chunk)
        if len(chunk) < DASHBOARD_PAGE:
            return rows
        start += DASHBOARD_PAGE


def dashboard_status_counts(rows):
    hidden = sum(1 for r in rows if r.get("is_active") is False)
    return len(rows), len(rows) - hidden, hidden


def dashboard_price(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # drops NaN


def dashboard_money(number):
    if number == int(number):
        return f"₹{int(number):,}"
    return f"₹{number:,.2f}"


def dashboard_recent_products():
    """Latest 5 products. Uses products.created_at (the column the bot already sorts by
    elsewhere). Returns (rows, order_note); order_note is None on the normal path."""
    try:
        rows = (
            supabase.table("products").select("id,name,sale_price")
            .order("created_at", desc=True).limit(5).execute().data or []
        )
        return rows, None
    except Exception as e:
        print(f"DASHBOARD RECENT (created_at) ERROR: {type(e).__name__}: {safe_error_text(e)}")
    rows = (
        supabase.table("products").select("id,name,sale_price")
        .order("id", desc=True).limit(5).execute().data or []
    )
    return rows, "ordered by product id (no usable created_at)"


def build_dashboard_text():
    """Query Supabase and return the dashboard text. Raises on any database error."""
    products = dashboard_fetch_all("products", "id,name,category_id,platform,sale_price,is_active")
    categories = dashboard_fetch_all("categories", "id,name,is_active,display_order")
    subcategories = dashboard_fetch_all("subcategories", "id,is_active")
    recent, order_note = dashboard_recent_products()

    p_total, p_active, p_hidden = dashboard_status_counts(products)
    c_total, c_active, c_hidden = dashboard_status_counts(categories)
    s_total, s_active, s_hidden = dashboard_status_counts(subcategories)

    # platforms: group by the actual value (ignoring only case/extra spaces)
    groups = {}
    for p in products:
        raw = (p.get("platform") or "").strip()
        label = raw or "Not set"
        bucket = groups.setdefault(label.lower(), {"count": 0, "spellings": {}})
        bucket["count"] += 1
        bucket["spellings"][label] = bucket["spellings"].get(label, 0) + 1
    platform_rows = sorted(
        ((max(b["spellings"], key=lambda k: (b["spellings"][k], k[:1].isupper())), b["count"]) for b in groups.values()),
        key=lambda item: (-item[1], item[0].lower())
    )

    # prices: sale_price of every product that has a valid number
    prices = [n for n in (dashboard_price(p.get("sale_price")) for p in products) if n is not None]

    # products per category: products.category_id -> categories.id
    per_category = {}
    for p in products:
        per_category[p.get("category_id")] = per_category.get(p.get("category_id"), 0) + 1
    emoji_by_order = {order: CATEGORY_EMOJI[key] for key, order in CATEGORY_ORDER.items()}  # keyed by slot

    lines = ["📊 OUTFIT INDIA DASHBOARD", ""]
    lines += ["🛍️ PRODUCTS", f"Total: {p_total}", f"🟢 Active: {p_active}", f"🔴 Hidden: {p_hidden}", ""]
    lines += ["📂 CATEGORIES", f"Total: {c_total}", f"🟢 Active: {c_active}", f"🔴 Hidden: {c_hidden}", ""]
    lines += ["🗂️ SUBCATEGORIES", f"Total: {s_total}", f"🟢 Active: {s_active}", f"🔴 Hidden: {s_hidden}", ""]

    lines.append("🏪 PLATFORMS")
    if platform_rows:
        for name, count in platform_rows[:DASHBOARD_MAX_PLATFORMS]:
            lines.append(f"{name}: {count}")
        extra = platform_rows[DASHBOARD_MAX_PLATFORMS:]
        if extra:
            lines.append(f"+ {len(extra)} more platform(s): {sum(c for _n, c in extra)} product(s)")
    else:
        lines.append("No products yet")
    lines.append("")

    lines.append("💰 PRICE")
    if prices:
        lines += [
            f"Lowest: {dashboard_money(min(prices))}",
            f"Highest: {dashboard_money(max(prices))}",
            f"Average: {dashboard_money(sum(prices) / len(prices))}",
        ]
    else:
        lines.append("No prices yet")
    lines.append("")

    lines.append("📈 PRODUCTS BY CATEGORY")
    known_ids = set()
    for c in sorted(categories, key=lambda r: (r.get("display_order") is None, r.get("display_order") or 0, r["id"])):
        known_ids.add(c["id"])
        emoji = emoji_by_order.get(category_slot(c.get("display_order")), "📁")
        hidden_mark = " 🚫" if c.get("is_active") is False else ""
        lines.append(f"{emoji} {c.get('name', '-')}{hidden_mark}: {per_category.get(c['id'], 0)}")
    orphaned = sum(n for cid, n in per_category.items() if cid not in known_ids)
    if orphaned:
        lines.append(f"❔ No matching category: {orphaned}")
    if not categories:
        lines.append("No categories yet")
    lines.append("")

    lines.append("🆕 RECENT PRODUCTS" + (f" ({order_note})" if order_note else ""))
    if recent:
        for r in recent:
            name = str(r.get("name") or "-")
            if len(name) > DASHBOARD_NAME_MAX:
                name = name[:DASHBOARD_NAME_MAX - 1] + "…"
            price = dashboard_price(r.get("sale_price"))
            lines.append(f"• {name} — {dashboard_money(price) if price is not None else '₹-'}")
    else:
        lines.append("No products yet")

    lines += ["", "🕒 Updated: " + datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")]
    return "\n".join(lines)


def dashboard_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Refresh", callback_data="dashboard_refresh")],
        *admin_nav_rows("dashboard_back"),
    ])


async def handle_dashboard_callback(query, context, data):
    """Admin Panel -> Dashboard. Admin-only; Refresh re-queries Supabase every time."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return

    if data == "dashboard_back":
        await show_admin_panel(query)
        return

    if data not in ("admin_dashboard", "dashboard_refresh"):
        return

    print(f"DASHBOARD QUERY START ({data})")
    try:
        text = build_dashboard_text()
        print("DASHBOARD QUERY SUCCESS")
    except Exception as e:
        print(f"DASHBOARD ERROR: {type(e).__name__}: {safe_error_text(e)}")
        text = (
            "📊 OUTFIT INDIA DASHBOARD\n\n"
            "❌ Could not load dashboard data.\n\n"
            "Please try again."
        )
    await safe_edit(query, text, dashboard_keyboard())


# ---------------------------------------------------------------------------
# ADMIN PANEL -> MANAGE PRODUCTS
# Callback prefixes: admin_products, mp_page_, mp_view_, mp_edit_, mp_editback_,
# mp_ef_ (edit field, ConversationHandler entry), mp_hide_, mp_show_, mp_move_,
# mp_mvc_, mp_mvs_, mp_delete_, mp_confirm_delete_, mp_cancel_delete_
# ---------------------------------------------------------------------------

EDIT_VALUE = 10
MANAGE_PAGE_SIZE = 5

MANAGE_CATEGORY_LABELS = [
    ("men", "♂  Men's Fashion"),
    ("women", "♀  Women's Fashion"),
    ("seasonal", "◇  Seasonal Wear"),
    ("deals", "★  Deals & Offers"),
    ("other", "🛍️  Other"),
]

# Subcategory labels: original clothing icons (admin compact labels). Customer menu uses customer_sub_label().
MANAGE_SUBCATEGORIES = {
    "men": [("👖 Jeans", "Jeans"), ("👔 Shirts", "Shirts"), ("👕 T-Shirts", "T-Shirts"),
            ("🧥 Jackets", "Jackets"), ("🧶 Sweaters & Hoodies", "Sweaters & Hoodies"), ("👟 Shoes", "Shoes")],
    "women": [("👚 Tops & T-Shirts", "Tops & T-Shirts"), ("👖 Jeans & Pants", "Jeans & Pants"),
              ("👗 Dresses", "Dresses"), ("🧥 Jackets & Coats", "Jackets & Coats"),
              ("🩳 Skirts & Shorts", "Skirts & Shorts"), ("👠 Shoes", "Shoes")],
    "seasonal": [("❄️ Winter Collection", "Winter Collection"), ("☀️ Summer Collection", "Summer Collection")],
    "deals": [("💰 Under ₹500", "Under ₹500"), ("💎 Under ₹1000", "Under ₹1000"),
              ("⭐ Top Rated", "Top Rated"), ("🆕 New Arrivals", "New Arrivals")],
    "other": [("🧴 Face Wash", "Face Wash"), ("🧢 Caps", "Caps"), ("🕶️ Sunglasses", "Sunglasses"),
              ("👝 Wallets", "Wallets"), ("🧷 Belts", "Belts"), ("🎒 Bags", "Bags"),
              ("⌚ Watches", "Watches"), ("🌸 Perfumes", "Perfumes"), ("💍 Accessories", "Accessories"),
              ("🧴 Personal Care", "Personal Care")],
}

# edit field -> (database column, label)
EDIT_FIELDS = {
    "photo": ("image_url", "Product photo"),
    "name": ("name", "Product name"),
    "price": ("sale_price", "Sale price"),
    "platform": ("platform", "Shopping platform"),
    "link": ("affiliate_link", "Affiliate/product link"),
}

PRODUCT_COLUMNS = "id,name,category_id,subcategory,sub_subcategory,image_url,platform,sale_price,affiliate_link,is_active"


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


def fetch_product(product_id):
    result = supabase.table("products").select(PRODUCT_COLUMNS).eq("id", product_id).limit(1).execute()
    return result.data[0] if result.data else None


def product_text(product, names):
    category = names.get(product.get("category_id"), "Unknown category")
    status = "🟢 Active" if product.get("is_active") else "🔴 Hidden"
    path = f"{category} → {product.get('subcategory', '-')}"
    sub_sub = (product.get("sub_subcategory") or "").strip()
    if sub_sub:  # third level shown only when the product has one
        path += f" → {sub_sub}"
    return (
        f"🛍️ {product.get('name', 'Product')}\n"
        f"💰 ₹{fmt_price(product.get('sale_price', 0))}\n"
        f"📂 {path}\n"
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
        await safe_edit(query, prefix + "❌ Could not load products.\n\nPlease try again.", InlineKeyboardMarkup(admin_nav_rows("admin")))
        return

    if not rows and page > 0:
        await show_manage_list(query, context, page - 1, notice)
        return

    if not rows:
        await safe_edit(
            query,
            prefix + "📦 MANAGE PRODUCTS\n\nNo products found yet.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Products", callback_data="mp_search")],
                *admin_nav_rows("admin"),
            ]),
        )
        return

    has_next = len(rows) > MANAGE_PAGE_SIZE
    rows = rows[:MANAGE_PAGE_SIZE]
    names = get_category_names()

    lines = [f"{prefix}📦 MANAGE PRODUCTS (Page {page + 1})\n\nChoose a product to manage.\n"]
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
    keyboard.append([InlineKeyboardButton("🔍 Search Products", callback_data="mp_search")])
    keyboard.extend(admin_nav_rows("admin"))

    await safe_edit(query, "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard))


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
        *admin_nav_rows(f"mp_page_{list_page}"),
    ]
    await safe_edit(query, f"{prefix}📦 MANAGE PRODUCT\n\n{product_text(product, names)}", InlineKeyboardMarkup(keyboard))


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
        *admin_nav_rows(f"mp_view_{product_id}"),
    ]
    text = (
        "✏️ EDIT PRODUCT\n\n"
        f"📝 Name: {product.get('name', '-')}\n"
        f"💰 Price: ₹{fmt_price(product.get('sale_price', 0))}\n"
        f"🏪 Platform: {product.get('platform', '-')}\n"
        f"🔗 Link: {product.get('affiliate_link', '-')}\n\n"
        "Choose a field to edit."
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
    keyboard.extend(admin_nav_rows(f"mp_view_{product_id}"))
    await safe_edit(query, "📂 MOVE PRODUCT\n\nChoose the new main category.", InlineKeyboardMarkup(keyboard))


async def show_move_subcategories(query, product_id, key):
    if key not in CATEGORY_ORDER:
        await show_move_categories(query, product_id)
        return
    info = safe_category_info(key)
    if not info:
        await show_move_categories(query, product_id)
        return
    names = active_subcategory_names(key, info["id"])
    keyboard = []
    for i in range(0, len(names), 2):
        keyboard.append([
            InlineKeyboardButton(sub_label(key, name), callback_data=f"mp_mvs_{product_id}_{key}_{idx}")
            for idx, name in list(enumerate(names))[i:i + 2]
        ])
    keyboard.extend(admin_nav_rows(f"mp_move_{product_id}"))
    text = f"📂 {info['name']}\n\nChoose the new subcategory."
    if not names:
        text = f"📂 {info['name']}\n\n⚠️ This category has no active subcategories.\nAdd or show one in Manage Categories first."
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def apply_product_move(query, context, product_id, category_id, subcategory, sub_subcategory):
    """Save a product's category / subcategory (and sub-subcategory when the column exists)."""
    update = {"category_id": category_id, "subcategory": subcategory}
    if _hier_flags()["subsub"]:
        update["sub_subcategory"] = sub_subcategory  # None clears a stale third-level value
    try:
        supabase.table("products").update(update).eq("id", product_id).execute()
    except Exception as e:
        print("MOVE PRODUCT ERROR:", e)
        await show_product_page(query, context, product_id, "❌ Could not move the product.")
        return
    await show_product_page(query, context, product_id, "✅ Product moved successfully.")


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
            info = get_category_info(key)
            if not info:
                raise ValueError("category missing")
            subcategory = active_subcategory_names(key, info["id"])[int(idx_text)]
        except Exception:
            await show_manage_list(query, context, list_page, "❌ Invalid subcategory.")
            return
        # Third level: ask only when the chosen subcategory has active child categories
        try:
            _sub_row, children = active_children_by_name(info["id"], subcategory)
        except Exception as e:
            print("MOVE PRODUCT CHILD LOAD ERROR:", e)
            children = []
        if children:
            sidx = int(idx_text)
            keyboard = []
            for i in range(0, len(children), 2):
                keyboard.append([
                    InlineKeyboardButton(f"📁 {c['name']}"[:TELEGRAM_BUTTON_TEXT_MAX],
                                         callback_data=f"mp_mvt_{product_id}_{key}_{sidx}_{cidx}")
                    for cidx, c in list(enumerate(children))[i:i + 2]
                ])
            keyboard.append([InlineKeyboardButton("⏭️ No sub-subcategory", callback_data=f"mp_mvt_{product_id}_{key}_{sidx}_x")])
            keyboard.extend(admin_nav_rows(f"mp_mvc_{product_id}_{key}"))
            await safe_edit(
                query,
                f"📂 {info['name']} › {subcategory}\n\nChoose the sub-subcategory.",
                InlineKeyboardMarkup(keyboard),
            )
            return
        await apply_product_move(query, context, product_id, info["id"], subcategory, None)
    elif data.startswith("mp_mvt_"):
        try:
            product_id, key, sidx, cidx = data[len("mp_mvt_"):].rsplit("_", 3)
            info = get_category_info(key)
            if not info:
                raise ValueError("category missing")
            subcategory = active_subcategory_names(key, info["id"])[int(sidx)]
            sub_subcategory = None
            if cidx != "x":
                _sub_row, children = active_children_by_name(info["id"], subcategory)
                sub_subcategory = children[int(cidx)]["name"]
        except Exception:
            await show_manage_list(query, context, list_page, "❌ Invalid sub-subcategory.")
            return
        await apply_product_move(query, context, product_id, info["id"], subcategory, sub_subcategory)
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

    await safe_edit(query, prompt, InlineKeyboardMarkup(admin_nav_rows(f"mp_editback_{product_id}")))
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
        # Prefer document (original quality); reject compressed photo-only uploads
        if message.photo and not message.document:
            await message.reply_text(
                "⚠️ Please send the image as a FILE/DOCUMENT to preserve original quality."
            )
            return EDIT_VALUE
        document = message.document
        if document is not None and _document_is_image(document):
            value = document.file_id
        elif message.photo:
            # Backward-compatible if somehow photo arrives without document
            value = message.photo[-1].file_id
        else:
            await message.reply_text(
                "⚠️ Please send a JPG, PNG, or WEBP image as a FILE/DOCUMENT."
            )
            return EDIT_VALUE
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
        reply_markup=InlineKeyboardMarkup(admin_nav_rows(f"mp_page_{list_page}"))
    )
    return ConversationHandler.END


async def cancel_edit_product(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
    was_category = bool(context.user_data.get("mc_action"))
    context.user_data.pop("mp_edit_field", None)
    context.user_data.pop("mp_edit_id", None)
    clear_category_input(context)
    list_page = context.user_data.get("mp_page", 0)
    back = "mc_categories" if was_category else f"mp_page_{list_page}"
    await update.message.reply_text(
        "❌ Edit cancelled.",
        reply_markup=InlineKeyboardMarkup(admin_nav_rows(back))
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
# Callback prefixes: admin_categories, mc_categories, mc_view_, mc_hide_, mc_show_,
# mc_edit_ (ConversationHandler entry), mc_delete_, mc_confirm_delete_,
# mc_cancel_delete_, mc_restore_, mc_subcategories_, mc_sub_view_, mc_sub_add_
# (entry), mc_sub_edit_ (entry), mc_sub_hide_, mc_sub_show_, mc_sub_delete_,
# mc_sub_confirm_delete_, mc_sub_cancel_delete_
# Reorder (display_order): mc_ro_t (top level), mc_ro_c_<key> (subcategories), mc_ro_p_<id>
# (sub-subcategories), mc_mtu_/mc_mtd_<key> (move a main category), mc_mu_/mc_md_<id> (move a
# subcategory / sub-subcategory)
#
# MAIN CATEGORIES live in the existing `categories` table. A category can be
# renamed, so the four categories are identified by their `display_order`
# (men=1, women=2, seasonal=3, deals=4), NOT by name. A row that still has the
# default name but another display_order is matched once by name and its
# display_order is corrected automatically. Renaming only changes `name`; the
# category id (and so every products.category_id link) never changes.
#
# A DELETED category (allowed only when no product uses it) disappears from the
# customer menu and from the Add/Move pickers. It is never re-created behind the
# admin's back; Manage Categories shows it as "Deleted" with a Restore button.
#
# SUBCATEGORIES live in the `subcategories` table (see the SQL migration file):
#   id, category_id -> categories.id, name, display_order, is_active
# with a unique (category_id, lower(name)) rule. Products still store the
# subcategory NAME in products.subcategory, always used together with
# products.category_id. If the table does not exist yet (migration not run) the
# customer menus keep working from the built-in defaults + names already used by
# products, and the admin subcategory screens tell the admin to run the SQL.
# ---------------------------------------------------------------------------

CATEGORY_ORDER = {"men": 1, "women": 2, "seasonal": 3, "deals": 4, "other": 5}
# Monochrome geometric / text symbols for main categories.
CATEGORY_EMOJI = {"men": "♂", "women": "♀", "seasonal": "◇", "deals": "★", "other": "🛍️"}
CATEGORY_NAME_MAX_LEN = 40
MC_ENTRY_PATTERN = r"^mc_(edit_|sub_edit_|sub_add_|ss_add_)"  # ConversationHandler entry callbacks
SUBCATEGORY_NAME_MAX_BYTES = 40  # keeps callback_data under Telegram's 64-byte limit
CALLBACK_DATA_MAX_BYTES = 64

DEFAULT_SUBCATEGORY_LABELS = {
    (key, value): label
    for key, items in MANAGE_SUBCATEGORIES.items()
    for label, value in items
}

# Keyword → clothing / category icon for unknown or renamed subcategory names.
# Order matters: more specific phrases first. Keeps the original icon set.
_FASHION_ICON_KEYWORDS = (
    ("t-shirt", "👕"), ("tshirt", "👕"), ("tee", "👕"), ("tops &", "👚"),
    ("shirt", "👔"),
    ("jean", "👖"), ("denim", "👖"), ("pant", "👖"),
    ("jacket", "🧥"), ("coat", "🧥"),
    ("sweater", "🧶"), ("hoodie", "🧶"), ("knit", "🧶"),
    ("shoe", "👟"), ("sneaker", "👟"), ("boot", "👟"), ("footwear", "👟"), ("heel", "👠"),
    ("dress", "👗"),
    ("skirt", "🩳"), ("short", "🩳"),
    ("winter", "❄️"),
    ("summer", "☀️"),
    ("under ₹500", "💰"), ("under 500", "💰"), ("under ₹1000", "💎"), ("under 1000", "💎"), ("under", "💰"),
    ("top rated", "⭐"), ("rated", "⭐"),
    ("new arrival", "🆕"), ("arrival", "🆕"),
    ("deal", "🏷️"), ("offer", "🏷️"),
    ("tops", "👚"), ("top", "👚"),
    # Other category
    ("face wash", "🧴"), ("personal care", "🧴"),
    ("cap", "🧢"),
    ("sunglass", "🕶️"),
    ("wallet", "👝"),
    ("belt", "🧷"),
    ("bag", "🎒"),
    ("watch", "⌚"),
    ("perfume", "🌸"),
    ("accessor", "💍"),
)


def fashion_icon_for_name(name):
    """Icon for a subcategory name. Uses the original clothing set; safe fallback for unknowns."""
    n = (name or "").strip().lower()
    if not n:
        return "🛍️"
    for keyword, icon in _FASHION_ICON_KEYWORDS:
        if keyword in n:
            return icon
    return "🛍️"


# Short taglines for the customer catalogue button second line (Telegram button text ≤ 64 chars).
SUBCATEGORY_SUBTITLES = {
    "Jeans": "Online never goes out of style",
    "Shirts": "Smart looks, every day",
    "T-Shirts": "Comfort meets style",
    "Jackets": "Stay stylish, stay warm",
    "Sweaters & Hoodies": "For cozy vibes",
    "Shoes": "Step into confidence",
    "Tops & T-Shirts": "Everyday essentials",
    "Jeans & Pants": "Denim that defines you",
    "Dresses": "For every occasion",
    "Jackets & Coats": "Layer in style",
    "Skirts & Shorts": "Light and breezy",
    "Winter Collection": "Stay warm, stay stylish",
    "Summer Collection": "Light looks for sunny days",
    "Under ₹500": "Budget-friendly styles",
    "Under ₹1000": "Great value picks",
    "Top Rated": "Customer favourites",
    "New Arrivals": "Fresh styles, just in",
}

TELEGRAM_BUTTON_TEXT_MAX = 64


def customer_nav_label(name):
    """Full-width nav button label: exact DB name + ›. Never auto-adds emojis."""
    raw = (name or "").strip()
    text = f"{raw} ›" if raw else "›"
    if len(text) > TELEGRAM_BUTTON_TEXT_MAX:
        text = text[:TELEGRAM_BUTTON_TEXT_MAX]
    return text


def customer_sub_label(key, name):
    """Full-width customer catalogue button: exact DB name + ›. No auto emoji."""
    return customer_nav_label(name)

_SUB_TABLE_STATE = {"ok": None, "checked": 0.0, "status": None, "error": ""}
SUBCATEGORY_COLUMNS = "id,category_id,name,is_active,display_order"

SETUP_TEXT = (
    "⚠️ Subcategory storage is not set up yet.\n\n"
    "Run the SQL migration file (outfit_india_subcategories.sql) in the Supabase SQL Editor, "
    "then open this screen again."
)


def fits_callback(data):
    """Telegram rejects callback_data longer than 64 bytes."""
    return len(data.encode("utf-8")) <= CALLBACK_DATA_MAX_BYTES


# ----- main categories -----
#
# TOP-LEVEL ORDER (uses only the existing categories.display_order column)
# The five main categories are identified by their "slot" (men=1 ... other=5) and ordered by
# their "position". Both live in the one display_order number:
#     display_order = position * 10 + slot
# Never-reordered data (display_order 1..5) is position 0 + slot 1..5, so existing databases keep
# working unchanged. Moving a category only rewrites these numbers; the slot (last digit) never
# changes, so a category keeps its own name, image, subcategories and products when it moves.
CATEGORY_SLOT_STEP = 10


def category_slot(value):
    """Slot (1..5) encoded in a categories.display_order value, or None."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number % CATEGORY_SLOT_STEP if number > 0 else None


def category_position(value):
    """Position (0, 1, 2 ...) encoded in a categories.display_order value."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number // CATEGORY_SLOT_STEP if number > 0 else 0


def ordered_category_keys(cmap):
    """Keys of the main categories that exist, in customer display order."""
    keys = [k for k in CATEGORY_ORDER if k in cmap]
    keys.sort(key=lambda k: (category_position(cmap[k].get("display_order")), CATEGORY_ORDER[k]))
    return keys


def admin_category_keys(cmap):
    """Existing main categories in display order, then deleted ones (in their default order)."""
    present = ordered_category_keys(cmap)
    return present + [k for k in CATEGORY_ORDER if k not in present]


def load_category_map(migrate=False):
    """Return {key: category_row} for the main categories that currently exist."""
    rows = (
        supabase.table("categories").select("id,name,is_active,display_order")
        .order("id").execute().data or []
    )
    result = {}
    used = set()
    for key, order in CATEGORY_ORDER.items():
        match = next((r for r in rows if category_slot(r.get("display_order")) == order and r["id"] not in used), None)
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


def get_category_info(key):
    """Real category row (id, name, is_active, display_order) or None if it does not exist.
    Never creates anything."""
    if key not in CATEGORY_ORDER:
        return None
    return load_category_map(migrate=True).get(key)


def create_category(key):
    """Explicitly (re)create a main category with its default name. Used only by Restore."""
    value = CATEGORY_ORDER[key]
    try:
        positions = [category_position(r.get("display_order")) for r in load_category_map().values()]
        if positions and max(positions) > 0:  # categories were reordered before: restore at the end
            value = (max(positions) + 1) * CATEGORY_SLOT_STEP + CATEGORY_ORDER[key]
    except Exception as e:
        print("CATEGORY RESTORE ORDER ERROR:", e)
    created = supabase.table("categories").insert(
        {"name": PARENT_CATEGORY_NAMES[key], "display_order": value, "is_active": True}
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


def category_state(key):
    """(info, state) with state in 'ok', 'hidden', 'deleted' or 'error' (database problem)."""
    try:
        info = get_category_info(key)
    except Exception as e:
        print("CATEGORY INFO ERROR:", e)
        return None, "error"
    if info is None:
        return None, "deleted"
    if info.get("is_active") is False:
        return info, "hidden"
    return info, "ok"


def category_display_name(key):
    info = safe_category_info(key)
    return info["name"] if info else PARENT_CATEGORY_NAMES.get(key, key)


def category_is_hidden(info):
    return bool(info) and info.get("is_active") is False


def get_main_menu_labels(include_hidden=False):
    """[(key, "👔 Name")] for the categories that exist. Hidden ones are skipped for
    customers; deleted ones are always skipped."""
    try:
        cmap = load_category_map()
    except Exception as e:
        print("CATEGORY MAP ERROR:", e)
        cmap = None  # database problem: fall back to the default labels so the menu still works
    labels = []
    for key in (ordered_category_keys(cmap) if cmap is not None else CATEGORY_ORDER):
        if cmap is None:
            info = None
        else:
            info = cmap.get(key)
            if info is None:
                continue
            if category_is_hidden(info) and not include_hidden:
                continue
        name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
        labels.append((key, f"{CATEGORY_EMOJI[key]} {name}"))
    return labels


def add_flow_markup(buttons):
    """Add Product category keyboard + the admin 🏠 Main Menu button (admin flow only)."""
    return InlineKeyboardMarkup(list(buttons) + [[InlineKeyboardButton("🏠 Main Menu", callback_data="main")]])


def add_flow_category_buttons():
    """Category buttons for the Add Product flow (callback addcat_<key>)."""
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
    """Compact label for admin flows (Add Product / Manage). Customer catalogue uses
    customer_sub_label() instead for the full-width card-style rows."""
    known = DEFAULT_SUBCATEGORY_LABELS.get((key, name))
    if known:
        return known
    return f"{fashion_icon_for_name(name)}  {name}"


def safe_error_text(error):
    """Error text for logs / admin screens with any secret value removed."""
    text = str(error)
    for secret in (SUPABASE_KEY, BOT_TOKEN):
        if secret:
            text = text.replace(str(secret), "***")
    return text[:300]


def supabase_host():
    """Project host only (never the key) - shows which Supabase project the bot talks to."""
    try:
        return SUPABASE_URL.split("//", 1)[-1].split("/", 1)[0]
    except Exception:
        return "unknown"


def is_missing_table_error(error):
    """True only when the database itself says the table does not exist."""
    text = str(error).lower()
    code = str(getattr(error, "code", "") or "")
    return (
        code in ("42P01", "PGRST205", "PGRST200")
        or "could not find the table" in text
        or ("relation" in text and "does not exist" in text)
    )


def subcategory_table_ready(force=False):
    """True if public.subcategories can be read with the columns the bot uses.
    The REAL exception is logged and remembered (status 'missing' or 'error') -
    it is never silently converted into 'not set up'."""
    if not force:
        if _SUB_TABLE_STATE["ok"] is True:
            return True
        if _SUB_TABLE_STATE["ok"] is False and time.time() - _SUB_TABLE_STATE["checked"] < 60:
            return False
    print(f"SUBCATEGORY QUERY START (probe) host={supabase_host()} table=subcategories columns={SUBCATEGORY_COLUMNS}")
    try:
        result = supabase.table("subcategories").select(SUBCATEGORY_COLUMNS).limit(1).execute()
        _SUB_TABLE_STATE.update(ok=True, status="ok", error="")
        print(f"SUBCATEGORY QUERY SUCCESS (probe) rows_returned={len(result.data or [])}")
    except Exception as e:
        detail = safe_error_text(e)
        _SUB_TABLE_STATE.update(
            ok=False,
            status="missing" if is_missing_table_error(e) else "error",
            error=f"{type(e).__name__}: {detail}",
        )
        print(f"SUBCATEGORY QUERY ERROR (probe): {type(e).__name__}: {detail}")
    _SUB_TABLE_STATE["checked"] = time.time()
    return _SUB_TABLE_STATE["ok"]


def subcategory_unavailable_text():
    """Admin message for when the probe failed. Setup text ONLY for a genuinely missing table."""
    if _SUB_TABLE_STATE.get("status") == "missing":
        return SETUP_TEXT
    return (
        "❌ Could not load subcategories.\n\n"
        "Please try again."
    )


def product_subcategory_names(category_id):
    result = supabase.table("products").select("subcategory").eq("category_id", category_id).execute()
    names = []
    for row in result.data or []:
        name = row.get("subcategory")
        if name and name not in names:
            names.append(name)
    return names


def load_subcategory_rows(category_id):
    """Authoritative list from the subcategories table (raises on error)."""
    print(f"SUBCATEGORY QUERY START category_id={category_id!r} ({type(category_id).__name__})")
    try:
        query = (
            supabase.table("subcategories")
            .select(SUBCATEGORY_COLUMNS)
            .eq("category_id", category_id)
        )
        if _hier_flags()["parent"]:
            query = query.is_("parent_id", "null")  # children are not subcategories
        rows = query.order("display_order").order("name").execute().data or []
    except Exception as e:
        print(f"SUBCATEGORY QUERY ERROR: {type(e).__name__}: {safe_error_text(e)}")
        raise
    print("SUBCATEGORY QUERY SUCCESS")
    print(f"SUBCATEGORY ROW COUNT: {len(rows)}")
    return rows


def get_subcategory_rows(key, category_id):
    """Subcategories of a main category as [{id, name, is_active, display_order}].
    The subcategories table is the source of truth. Only if it is unavailable (SQL
    migration not run yet) does this fall back to the built-in defaults plus the
    names products already use, so customers can still browse."""
    if category_id is not None and subcategory_table_ready():
        try:
            return load_subcategory_rows(category_id)
        except Exception as e:
            print("SUBCATEGORY LOAD ERROR:", e)

    defaults = [value for _label, value in MANAGE_SUBCATEGORIES[key]]
    extras = []
    if category_id is not None:
        try:
            extras = [n for n in product_subcategory_names(category_id) if n not in defaults]
        except Exception as e:
            print("SUBCATEGORY DERIVE ERROR:", e)
    return [{"id": None, "name": n, "is_active": True, "display_order": i} for i, n in enumerate(defaults + extras)]


def active_subcategory_names(key, category_id):
    """Names of the ACTIVE subcategories, in display order (customer menus, Add Product, Move)."""
    return [r["name"] for r in get_subcategory_rows(key, category_id) if r.get("is_active") is not False]


def is_subcategory_hidden(category_id, name):
    if not subcategory_table_ready():
        return False
    try:
        query = (
            supabase.table("subcategories").select("is_active")
            .eq("category_id", category_id).eq("name", name)
        )
        if _hier_flags()["parent"]:
            query = query.is_("parent_id", "null")
        result = query.limit(1).execute()
        return bool(result.data) and result.data[0].get("is_active") is False
    except Exception as e:
        print("SUBCATEGORY HIDDEN CHECK ERROR:", e)
        return False


def get_subcategory_row(sid):
    result = (
        supabase.table("subcategories").select("*")
        .eq("id", sid).limit(1).execute()
    )
    return result.data[0] if result.data else None


def seed_default_subcategories(key, category_id):
    """Insert the default subcategories of a (re)created category."""
    defaults = [value for _label, value in MANAGE_SUBCATEGORIES[key]]
    supabase.table("subcategories").insert(
        [{"category_id": category_id, "name": n, "is_active": True, "display_order": i}
         for i, n in enumerate(defaults, start=1)]
    ).execute()


def key_for_category_id(category_id):
    for key, info in load_category_map().items():
        if info["id"] == category_id:
            return key
    return None


def category_has_products(category_id, subcategory=None, sub_subcategory=None):
    """True if any product (active or hidden) uses the category (and subcategory / sub-subcategory, if given)."""
    query = supabase.table("products").select("id").eq("category_id", category_id)
    if subcategory is not None:
        query = query.eq("subcategory", subcategory)
    if sub_subcategory is not None:
        query = query.eq("sub_subcategory", sub_subcategory)
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


def is_duplicate_error(error):
    text = str(error).lower()
    return "23505" in text or "duplicate key" in text or "already exists" in text


# ----- generic category hierarchy: Category -> Subcategory -> Sub-subcategory -----
# Storage (see outfit_india_hierarchy.sql):
#   subcategories.parent_id   NULL = subcategory of a main category;
#                             <id of another subcategories row> = child of that row (any depth)
#   products.sub_subcategory  name of the chosen third level (NULL = none), used together with
#                             products.category_id + products.subcategory
# Until the SQL has been run the bot behaves exactly as before (two levels).
_HIER_FLAGS = {"parent": None, "subsub": None, "checked": 0.0}
HIERARCHY_SETUP_TEXT = (
    "⚠️ Sub-subcategories are not set up yet.\n\n"
    "Run the SQL file (outfit_india_hierarchy.sql) in the Supabase SQL Editor, "
    "then open this screen again."
)


def _hier_flags(force=False):
    """{'parent': bool, 'subsub': bool}: do subcategories.parent_id / products.sub_subcategory exist?
    A True result is remembered; a False result is re-checked after 60 seconds."""
    if not force and (
        (_HIER_FLAGS["parent"] and _HIER_FLAGS["subsub"])
        or time.time() - _HIER_FLAGS["checked"] < 60
    ) and _HIER_FLAGS["parent"] is not None:
        return _HIER_FLAGS
    parent_ok = subsub_ok = False
    try:
        supabase.table("subcategories").select("id,parent_id").limit(1).execute()
        parent_ok = True
    except Exception as e:
        print(f"HIERARCHY PROBE subcategories.parent_id unavailable: {type(e).__name__}: {safe_error_text(e)}")
    try:
        supabase.table("products").select("id,sub_subcategory").limit(1).execute()
        subsub_ok = True
    except Exception as e:
        print(f"HIERARCHY PROBE products.sub_subcategory unavailable: {type(e).__name__}: {safe_error_text(e)}")
    _HIER_FLAGS.update(parent=parent_ok, subsub=subsub_ok, checked=time.time())
    return _HIER_FLAGS


def hierarchy_ready(force=False):
    """True only when BOTH columns exist, i.e. the third level can be used end to end."""
    flags = _hier_flags(force)
    return bool(flags["parent"] and flags["subsub"])


def load_child_rows(parent_id, active_only=False):
    """Child rows (any level) of one subcategories row, in display order."""
    rows = (
        supabase.table("subcategories").select("*")
        .eq("parent_id", parent_id)
        .order("display_order").order("name")
        .execute().data or []
    )
    if active_only:
        rows = [r for r in rows if r.get("is_active") is not False]
    return rows


def active_children_by_name(category_id, subcategory_name):
    """(subcategory_row, active_child_rows) for a subcategory name. Children are [] when the
    hierarchy is not set up, the subcategory is hidden or it simply has none."""
    if category_id is None or not hierarchy_ready():
        return None, []
    found = (
        supabase.table("subcategories").select("*")
        .eq("category_id", category_id).eq("name", subcategory_name)
        .is_("parent_id", "null").limit(1).execute().data
    )
    if not found:
        return None, []
    sub = found[0]
    if sub.get("is_active") is False:
        return sub, []
    return sub, load_child_rows(sub["id"], active_only=True)


def has_unassigned_products(category_id, subcategory_name):
    """Active products of a subcategory that have no third-level value (so they stay reachable)."""
    result = (
        supabase.table("products").select("id")
        .eq("category_id", category_id).eq("subcategory", subcategory_name)
        .eq("is_active", True).is_("sub_subcategory", "null").limit(1).execute()
    )
    return bool(result.data)


# ----- admin screens (each returns (text, InlineKeyboardMarkup)) -----

def status_text(active):
    return "🟢 Active" if active is not False else "🔴 Hidden"


def categories_admin_screen(notice=None):
    try:
        cmap = load_category_map()
    except Exception as e:
        print("CATEGORY MAP ERROR:", e)
        cmap = {}
    lines = [f"{notice}\n\n" if notice else "", "📂 MANAGE CATEGORIES\n\nChoose a category to manage.\n"]
    keyboard = []
    for key in admin_category_keys(cmap):
        info = cmap.get(key)
        name = info["name"] if info else PARENT_CATEGORY_NAMES[key]
        label = f"{CATEGORY_EMOJI[key]} {name}"
        status = status_text(info.get("is_active")) if info else "⚪ Deleted"
        lines.append(f"{label}\n{status}\n")
        keyboard.append([InlineKeyboardButton(label, callback_data=f"mc_view_{key}")])
    keyboard.append([InlineKeyboardButton("↕️ Reorder", callback_data="mc_ro_t")])
    keyboard.extend(admin_nav_rows("admin"))
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard)


def category_admin_screen(key, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    if key not in CATEGORY_ORDER:
        return categories_admin_screen("❌ Invalid category.")
    info = get_category_info(key)
    if not info:
        text = (
            f"{prefix}{CATEGORY_EMOJI[key]} {PARENT_CATEGORY_NAMES[key]}\n\n⚪ This category was deleted.\n\n"
            "Restore it to use it again. It comes back empty, with its default subcategories."
        )
        keyboard = [
            [InlineKeyboardButton("♻️ Restore Category", callback_data=f"mc_restore_{key}")],
            *admin_nav_rows("mc_categories"),
        ]
        return text, InlineKeyboardMarkup(keyboard)
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
        *admin_nav_rows("mc_categories"),
    ]
    text = f"{prefix}📂 {info['name']}\n\n{status_text(info.get('is_active'))}"
    return text, InlineKeyboardMarkup(keyboard)


def subcategories_admin_screen(key, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    info = get_category_info(key)
    if not info:
        return categories_admin_screen("⚠️ This category no longer exists.")

    if not subcategory_table_ready(force=True):
        return (
            f"{prefix}📁 {info['name']} - Subcategories\n\n{subcategory_unavailable_text()}",
            InlineKeyboardMarkup(admin_nav_rows(f"mc_view_{key}"))
        )

    try:
        rows = load_subcategory_rows(info["id"])
    except Exception as e:
        print(f"SUBCATEGORY LOAD ERROR: {type(e).__name__}: {safe_error_text(e)}")
        return (
            f"{prefix}📁 {info['name']} - Subcategories\n\n"
            "❌ Could not load subcategories.\n\nPlease try again.",
            InlineKeyboardMarkup(admin_nav_rows(f"mc_view_{key}"))
        )
    if rows:
        text = f"{prefix}📁 {info['name']} - Subcategories\n\nChoose a subcategory to manage."
    else:
        text = (
            f"{prefix}📁 {info['name']} - Subcategories\n\nNo subcategories found for this category. Tap ➕ Add Subcategory.\n\n"
            "If you expected existing subcategories here, check that the bot's SUPABASE_KEY is allowed to "
            "read public.subcategories (Row Level Security)."
        )
    # read-only warning: product subcategory names that have no matching row (exact match), so they
    # would be invisible to customers. Nothing is changed automatically.
    try:
        listed = {r["name"] for r in rows}
        unlisted = [n for n in product_subcategory_names(info["id"]) if n not in listed]
    except Exception as e:
        print("UNLISTED SUBCATEGORY CHECK ERROR:", e)
        unlisted = []
    if unlisted:
        shown = ", ".join(unlisted[:5]) + (" …" if len(unlisted) > 5 else "")
        text += (
            f"\n\n⚠️ Some products use subcategory names that are not listed here: {shown}\n"
            "Add a subcategory with exactly that name, or move those products (Manage Products → Move)."
        )
    keyboard = []
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
    if len(rows) >= 2:
        keyboard.append([InlineKeyboardButton("↕️ Reorder", callback_data=f"mc_ro_c_{key}")])
    keyboard.extend(admin_nav_rows(f"mc_view_{key}"))
    return text, InlineKeyboardMarkup(keyboard)


def subcategory_admin_screen(sid, notice=None):
    row = get_subcategory_row(sid)
    if not row:
        return "❌ Subcategory not found.", InlineKeyboardMarkup(admin_nav_rows("mc_categories"))
    key = key_for_category_id(row["category_id"])
    parent_id = row.get("parent_id")
    prefix = f"{notice}\n\n" if notice else ""
    active = row.get("is_active") is not False
    toggle = (
        InlineKeyboardButton("👁️ Hide", callback_data=f"mc_sub_hide_{sid}")
        if active
        else InlineKeyboardButton("👁️ Show", callback_data=f"mc_sub_show_{sid}")
    )
    keyboard = [[InlineKeyboardButton("✏️ Edit", callback_data=f"mc_sub_edit_{sid}"), toggle]]
    category_name = category_display_name(key) if key else "-"
    if parent_id is None:
        # subcategory: can have sub-subcategories
        keyboard.append([InlineKeyboardButton("📂 Manage Sub-subcategories", callback_data=f"mc_ssl_{sid}")])
        back = f"mc_subcategories_{key}" if key else "mc_categories"
        path = category_name
    else:
        parent = get_subcategory_row(parent_id)
        back = f"mc_ssl_{parent_id}"
        path = f"{category_name} › {parent['name']}" if parent else category_name
    keyboard.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"mc_sub_delete_{sid}")])
    keyboard.extend(admin_nav_rows(back))
    text = f"{prefix}📁 {row['name']}\n\n📂 {path}\n{status_text(row.get('is_active'))}"
    return text, InlineKeyboardMarkup(keyboard)


def children_admin_screen(parent_sid, notice=None):
    """Manage the sub-subcategories (children) of one subcategory."""
    prefix = f"{notice}\n\n" if notice else ""
    parent = get_subcategory_row(parent_sid)
    if not parent:
        return categories_admin_screen("⚠️ This subcategory no longer exists.")
    back_rows = admin_nav_rows(f"mc_sub_view_{parent_sid}")
    if not hierarchy_ready(force=True):
        return (
            f"{prefix}📂 {parent['name']} - Sub-subcategories\n\n{HIERARCHY_SETUP_TEXT}",
            InlineKeyboardMarkup(back_rows),
        )
    try:
        children = load_child_rows(parent["id"])
    except Exception as e:
        print(f"CHILD LOAD ERROR: {type(e).__name__}: {safe_error_text(e)}")
        return (
            f"{prefix}📂 {parent['name']} - Sub-subcategories\n\n❌ Could not load sub-subcategories.\n\nPlease try again.",
            InlineKeyboardMarkup(back_rows),
        )
    if children:
        text = f"{prefix}📂 {parent['name']} - Sub-subcategories\n\nChoose one to manage."
    else:
        text = (
            f"{prefix}📂 {parent['name']} - Sub-subcategories\n\nNo sub-subcategories yet. Tap ➕ Add Sub-subcategory.\n\n"
            f"Customers see the products of {parent['name']} directly until you add one."
        )
    keyboard = []
    pair = []
    for c in children:
        label = f"📁 {c['name']}" + ("" if c.get("is_active") is not False else " 🚫")
        pair.append(InlineKeyboardButton(label, callback_data=f"mc_sub_view_{c['id']}"))
        if len(pair) == 2:
            keyboard.append(pair)
            pair = []
    if pair:
        keyboard.append(pair)
    keyboard.append([InlineKeyboardButton("➕ Add Sub-subcategory", callback_data=f"mc_ss_add_{parent_sid}")])
    if len(children) >= 2:
        keyboard.append([InlineKeyboardButton("↕️ Reorder", callback_data=f"mc_ro_p_{parent_sid}")])
    keyboard.extend(back_rows)
    return text, InlineKeyboardMarkup(keyboard)


# ----- reorder: Admin Panel -> Manage Categories -> ↕️ Reorder -----
# Generic for every level. Top level uses reorder_top_category(); everything below it
# (subcategories, sub-subcategories, any deeper child) uses reorder_subcategory_row(), which finds
# the row's own siblings from the row itself (same category + no parent, or same parent_id).
# Any category/subcategory/sub-subcategory created later is therefore reorderable automatically.
# Only display_order is written - never names, parent_id, products, links or images.

def load_sibling_rows(row):
    """Every row that shares this row's parent, in customer display order (display_order, name)."""
    query = supabase.table("subcategories").select("*")
    if row.get("parent_id") is not None:
        query = query.eq("parent_id", row["parent_id"])
    else:
        query = query.eq("category_id", row["category_id"])
        if _hier_flags()["parent"]:
            query = query.is_("parent_id", "null")
    return query.order("display_order").order("name").execute().data or []


def reorder_top_category(key, direction):
    """Move one main category one place (direction -1 = up, +1 = down). Returns (moved, notice)."""
    cmap = load_category_map(migrate=True)
    ordered = ordered_category_keys(cmap)
    if key not in ordered:
        return False, "⚠️ This category no longer exists."
    name = cmap[key]["name"]
    i = ordered.index(key)
    j = i + direction
    if j < 0:
        return False, f"ℹ️ {name} is already first."
    if j >= len(ordered):
        return False, f"ℹ️ {name} is already last."
    ordered[i], ordered[j] = ordered[j], ordered[i]
    for position, k in enumerate(ordered):
        value = position * CATEGORY_SLOT_STEP + CATEGORY_ORDER[k]  # the slot (last digit) never changes
        if cmap[k].get("display_order") != value:
            supabase.table("categories").update({"display_order": value}).eq("id", cmap[k]["id"]).execute()
    return True, f"{'⬆️' if direction < 0 else '⬇️'} {name} moved {'up' if direction < 0 else 'down'}."


def reorder_subcategory_row(sid, direction):
    """Move one subcategories row (subcategory, sub-subcategory or deeper) one place among its siblings.
    Siblings are renumbered 1..N so no two share a display_order. Returns (row, notice)."""
    row = get_subcategory_row(sid)
    if not row:
        return None, "⚠️ This item no longer exists."
    siblings = load_sibling_rows(row)
    ids = [str(r["id"]) for r in siblings]
    if str(row["id"]) not in ids:
        return row, "⚠️ This item no longer exists."
    i = ids.index(str(row["id"]))
    j = i + direction
    name = row["name"]
    if j < 0:
        return row, f"ℹ️ {name} is already first."
    if j >= len(siblings):
        return row, f"ℹ️ {name} is already last."
    siblings[i], siblings[j] = siblings[j], siblings[i]
    for number, sibling in enumerate(siblings, start=1):
        if sibling.get("display_order") != number:
            supabase.table("subcategories").update({"display_order": number}).eq("id", sibling["id"]).execute()
    return row, f"{'⬆️' if direction < 0 else '⬇️'} {name} moved {'up' if direction < 0 else 'down'}."


REORDER_LABEL_MAX = 16


def build_reorder_screen(title, items, back_callback, notice=None):
    """items = [(name, is_active, up_callback, down_callback)] in display order.
    Names are shown exactly as stored (no automatic emojis)."""
    prefix = f"{notice}\n\n" if notice else ""
    if len(items) < 2:
        text = f"{prefix}↕️ REORDER - {title}\n\nAdd at least two items to change their order."
        return text, InlineKeyboardMarkup(admin_nav_rows(back_callback))
    lines = [f"{prefix}↕️ REORDER - {title}", "", "Tap ⬆️ or ⬇️ to move an item one place.", ""]
    keyboard = []
    last = len(items) - 1
    for n, (name, active, up_cb, down_cb) in enumerate(items):
        lines.append(f"{n + 1}. {name}" + ("" if active else " 🚫"))
        short = name if len(name) <= REORDER_LABEL_MAX else name[:REORDER_LABEL_MAX - 1] + "…"
        row = []
        if n > 0:
            row.append(InlineKeyboardButton(f"⬆️ {short}", callback_data=up_cb))
        if n < last:
            row.append(InlineKeyboardButton(f"⬇️ {short}", callback_data=down_cb))
        keyboard.append(row)
    keyboard.extend(admin_nav_rows(back_callback))
    return "\n".join(lines), InlineKeyboardMarkup(keyboard)


def reorder_top_screen(notice=None):
    cmap = load_category_map()
    items = [
        (cmap[k]["name"], cmap[k].get("is_active") is not False, f"mc_mtu_{k}", f"mc_mtd_{k}")
        for k in ordered_category_keys(cmap)
    ]
    return build_reorder_screen("Categories", items, "mc_categories", notice)


def reorder_subcategories_screen(key, notice=None):
    info = get_category_info(key)
    if not info:
        return categories_admin_screen("⚠️ This category no longer exists.")
    back = f"mc_subcategories_{key}"
    if not subcategory_table_ready(force=True):
        return f"{subcategory_unavailable_text()}", InlineKeyboardMarkup(admin_nav_rows(back))
    rows = load_subcategory_rows(info["id"])
    items = [(r["name"], r.get("is_active") is not False, f"mc_mu_{r['id']}", f"mc_md_{r['id']}") for r in rows]
    return build_reorder_screen(f"{info['name']}", items, back, notice)


def reorder_children_screen(parent_sid, notice=None):
    parent = get_subcategory_row(parent_sid)
    if not parent:
        return categories_admin_screen("⚠️ This subcategory no longer exists.")
    back = f"mc_ssl_{parent_sid}"
    if not hierarchy_ready(force=True):
        return HIERARCHY_SETUP_TEXT, InlineKeyboardMarkup(admin_nav_rows(back))
    rows = load_child_rows(parent["id"])
    items = [(r["name"], r.get("is_active") is not False, f"mc_mu_{r['id']}", f"mc_md_{r['id']}") for r in rows]
    return build_reorder_screen(f"{parent['name']}", items, back, notice)


async def show_screen(query, builder, *args):
    try:
        text, markup = builder(*args)
    except Exception as e:
        print("MANAGE CATEGORIES ERROR:", e)
        text, markup = "❌ Something went wrong. Please try again.", InlineKeyboardMarkup(admin_nav_rows("mc_categories"))
    await safe_edit(query, text, markup)


async def reply_screen(message, builder, *args):
    try:
        text, markup = builder(*args)
    except Exception as e:
        print("MANAGE CATEGORIES ERROR:", e)
        text, markup = "❌ Something went wrong. Please try again.", InlineKeyboardMarkup(admin_nav_rows("mc_categories"))
    await message.reply_text(text, reply_markup=markup)


async def handle_category_admin_callback(query, context, data):
    """Single entry point for every Manage Categories callback. Admin-only."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return

    if data in ("admin_categories", "mc_categories"):
        await show_screen(query, categories_admin_screen)

    elif data == "mc_ro_t":
        await show_screen(query, reorder_top_screen)

    elif data.startswith("mc_ro_c_"):
        await show_screen(query, reorder_subcategories_screen, data[len("mc_ro_c_"):])

    elif data.startswith("mc_ro_p_"):
        await show_screen(query, reorder_children_screen, data[len("mc_ro_p_"):])

    elif data.startswith("mc_mtu_") or data.startswith("mc_mtd_"):
        direction = -1 if data.startswith("mc_mtu_") else 1
        try:
            _moved, notice = reorder_top_category(data[len("mc_mtu_"):], direction)
        except Exception as e:
            print("CATEGORY REORDER ERROR:", e)
            notice = "❌ Could not change the order."
        await show_screen(query, reorder_top_screen, notice)

    elif data.startswith("mc_mu_") or data.startswith("mc_md_"):
        direction = -1 if data.startswith("mc_mu_") else 1
        try:
            moved_row, notice = reorder_subcategory_row(data[len("mc_mu_"):], direction)
        except Exception as e:
            print("SUBCATEGORY REORDER ERROR:", e)
            await show_screen(query, categories_admin_screen, "❌ Could not change the order.")
            return
        if moved_row is None:
            await show_screen(query, categories_admin_screen, notice)
        elif moved_row.get("parent_id") is not None:
            await show_screen(query, reorder_children_screen, moved_row["parent_id"], notice)
        else:
            parent_key = key_for_category_id(moved_row["category_id"])
            if parent_key:
                await show_screen(query, reorder_subcategories_screen, parent_key, notice)
            else:
                await show_screen(query, categories_admin_screen, notice)

    elif data.startswith("mc_view_"):
        await show_screen(query, category_admin_screen, data[len("mc_view_"):])

    elif data.startswith("mc_hide_") or data.startswith("mc_show_"):
        make_active = data.startswith("mc_show_")
        key = data.split("_", 2)[2]
        try:
            info = get_category_info(key)
            if not info:
                await show_screen(query, category_admin_screen, key)
                return
            supabase.table("categories").update({"is_active": make_active}).eq("id", info["id"]).execute()
            notice = "✅ Category is visible again." if make_active else "🚫 Category hidden successfully."
        except Exception as e:
            print("CATEGORY VISIBILITY ERROR:", e)
            notice = "❌ Could not update the category."
        await show_screen(query, category_admin_screen, key, notice)

    elif data.startswith("mc_restore_"):
        key = data[len("mc_restore_"):]
        if key not in CATEGORY_ORDER:
            await show_screen(query, categories_admin_screen)
            return
        try:
            if get_category_info(key) is None:
                info = create_category(key)
                if subcategory_table_ready(force=True):
                    try:
                        seed_default_subcategories(key, info["id"])
                    except Exception as e:
                        print("SUBCATEGORY SEED ERROR:", e)
            notice = "✅ Category restored successfully."
        except Exception as e:
            print("CATEGORY RESTORE ERROR:", e)
            notice = "❌ Could not restore the category."
        await show_screen(query, category_admin_screen, key, notice)

    elif data.startswith("mc_subcategories_"):
        await show_screen(query, subcategories_admin_screen, data[len("mc_subcategories_"):])

    elif data.startswith("mc_sub_view_"):
        await show_screen(query, subcategory_admin_screen, data[len("mc_sub_view_"):])

    elif data.startswith("mc_ssl_"):
        await show_screen(query, children_admin_screen, data[len("mc_ssl_"):])

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
            parent_id = row.get("parent_id")
            if parent_id is not None:
                # sub-subcategory: blocked while products use it under its parent
                parent = get_subcategory_row(parent_id)
                if parent and category_has_products(category_id, parent["name"], row["name"]):
                    await safe_edit(
                        query,
                        "⚠️ This sub-subcategory contains products.\n\nMove those products before deleting it.",
                        InlineKeyboardMarkup(admin_nav_rows(f"mc_sub_view_{sid}"))
                    )
                    return
            else:
                if hierarchy_ready() and load_child_rows(sid):
                    await safe_edit(
                        query,
                        "⚠️ This subcategory has sub-subcategories.\n\nDelete or move them before deleting it.",
                        InlineKeyboardMarkup(admin_nav_rows(f"mc_sub_view_{sid}"))
                    )
                    return
                # category-specific check: same category_id AND same subcategory name
                if category_has_products(category_id, row["name"]):
                    await safe_edit(
                        query,
                        "⚠️ This subcategory contains products.\n\nMove those products to another subcategory before deleting it.",
                        InlineKeyboardMarkup(admin_nav_rows(f"mc_sub_view_{sid}"))
                    )
                    return
            supabase.table("subcategories").delete().eq("id", sid).execute()
        except Exception as e:
            print("SUBCATEGORY DELETE ERROR:", e)
            await safe_edit(query, "❌ Could not delete the subcategory.", InlineKeyboardMarkup(admin_nav_rows("mc_categories")))
            return
        if parent_id is not None:
            await show_screen(query, children_admin_screen, parent_id, "✅ Sub-subcategory deleted successfully.")
        elif key:
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
        await safe_edit(
            query,
            f"⚠️ Delete this category?\n\n📂 {info['name']}\n\nIts subcategories will be deleted too. "
            "A category that still has products cannot be deleted.",
            InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("mc_cancel_delete_"):
        await show_screen(query, category_admin_screen, data[len("mc_cancel_delete_"):])

    elif data.startswith("mc_confirm_delete_"):
        key = data[len("mc_confirm_delete_"):]
        try:
            info = get_category_info(key)
            if not info:
                await show_screen(query, categories_admin_screen)
                return
            # never delete a category (or cascade) while ANY product still uses it
            if category_has_products(info["id"]):
                await safe_edit(
                    query,
                    "⚠️ This category contains products.\n\nPlease move or remove its products before deleting the category.",
                    InlineKeyboardMarkup(admin_nav_rows(f"mc_view_{key}"))
                )
                return
            saved_subs = []
            if subcategory_table_ready():
                saved_query = supabase.table("subcategories").select(
                    "category_id,name,is_active,display_order"
                ).eq("category_id", info["id"])
                if _hier_flags()["parent"]:
                    saved_query = saved_query.is_("parent_id", "null")  # children are removed with their parent
                saved_subs = saved_query.execute().data or []
                supabase.table("subcategories").delete().eq("category_id", info["id"]).execute()
            try:
                supabase.table("categories").delete().eq("id", info["id"]).execute()
            except Exception:
                if saved_subs:  # put the subcategories back so nothing is half-deleted
                    supabase.table("subcategories").insert(saved_subs).execute()
                raise
        except Exception as e:
            print("CATEGORY DELETE ERROR:", e)
            await safe_edit(query, "❌ Could not delete the category.", InlineKeyboardMarkup(admin_nav_rows("mc_categories")))
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
            info = get_category_info(key)
            if not info:
                await show_screen(query, categories_admin_screen, "⚠️ This category no longer exists.")
                return ConversationHandler.END
            if not subcategory_table_ready(force=True):
                await safe_edit(query, subcategory_unavailable_text(), InlineKeyboardMarkup(admin_nav_rows(f"mc_subcategories_{key}")))
                return ConversationHandler.END
            action, target = "add_sub", key
            prompt = f"➕ Add Subcategory\n\nCategory: {info['name']}\n\nSend the new subcategory name."
            back = f"mc_subcategories_{key}"
        elif data.startswith("mc_ss_add_"):
            psid = data[len("mc_ss_add_"):]
            parent = get_subcategory_row(psid)
            if not parent:
                await show_screen(query, categories_admin_screen, "⚠️ This subcategory no longer exists.")
                return ConversationHandler.END
            if not hierarchy_ready(force=True):
                await safe_edit(query, HIERARCHY_SETUP_TEXT, InlineKeyboardMarkup(admin_nav_rows(f"mc_sub_view_{psid}")))
                return ConversationHandler.END
            action, target = "add_ss", psid
            prompt = f"➕ Add Sub-subcategory\n\nSubcategory: {parent['name']}\n\nSend the new sub-subcategory name."
            back = f"mc_ssl_{psid}"
        else:
            key = data[len("mc_edit_"):]
            info = get_category_info(key)
            if not info:
                await show_screen(query, category_admin_screen, key)
                return ConversationHandler.END
            action, target = "edit_cat", key
            prompt = f"✏️ Edit Category\n\nCurrent category:\n{info['name']}\n\nSend the new category name."
            back = f"mc_view_{key}"
    except Exception as e:
        print("CATEGORY INPUT START ERROR:", e)
        await safe_edit(query, "❌ Something went wrong. Please try again.", InlineKeyboardMarkup(admin_nav_rows("mc_categories")))
        return ConversationHandler.END

    context.user_data["mc_action"] = action
    context.user_data["mc_target"] = target
    await safe_edit(query, prompt + "\n\nSend /cancel to stop.", InlineKeyboardMarkup(admin_nav_rows(back)))
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
            parent_id = row.get("parent_id")
            if text == old_name:
                clear_category_input(context)
                await reply_screen(message, subcategory_admin_screen, target, "ℹ️ The name was not changed.")
                return ConversationHandler.END
            sibling_query = supabase.table("subcategories").select("id,name").eq("category_id", category_id)
            if _hier_flags()["parent"]:
                # compare only with rows on the same level / under the same parent
                sibling_query = sibling_query.is_("parent_id", "null") if parent_id is None else sibling_query.eq("parent_id", parent_id)
            siblings = sibling_query.execute().data or []
            if any(s["name"].lower() == text.lower() and str(s["id"]) != str(target) for s in siblings):
                await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                return EDIT_VALUE
            try:
                supabase.table("subcategories").update({"name": text}).eq("id", target).execute()
            except Exception as e:
                if is_duplicate_error(e):
                    await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                    return EDIT_VALUE
                raise
            try:
                if parent_id is None:
                    # only products of THIS category with the old name are renamed
                    updated = (
                        supabase.table("products").update({"subcategory": text})
                        .eq("category_id", category_id).eq("subcategory", old_name).execute()
                    )
                else:
                    parent = get_subcategory_row(parent_id)
                    updated = (
                        supabase.table("products").update({"sub_subcategory": text})
                        .eq("category_id", category_id).eq("subcategory", parent["name"])
                        .eq("sub_subcategory", old_name).execute()
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

        if action == "add_ss":
            error = validate_subcategory_name(text)
            if error:
                await message.reply_text(error.replace("subcategory name", "sub-subcategory name"))
                return EDIT_VALUE
            if not hierarchy_ready(force=True):
                clear_category_input(context)
                await message.reply_text(HIERARCHY_SETUP_TEXT)
                return ConversationHandler.END
            parent = get_subcategory_row(target)
            if not parent:
                clear_category_input(context)
                await reply_screen(message, categories_admin_screen, "⚠️ This subcategory no longer exists.")
                return ConversationHandler.END
            rows = load_child_rows(parent["id"])
            dup_text = "⚠️ This subcategory already has a sub-subcategory with that name. Please send a different name."
            if any(r["name"].lower() == text.lower() for r in rows):
                await message.reply_text(dup_text)
                return EDIT_VALUE
            next_order = max([r.get("display_order") or 0 for r in rows] + [0]) + 1
            try:
                supabase.table("subcategories").insert(
                    {"category_id": parent["category_id"], "name": text, "is_active": True,
                     "display_order": next_order, "parent_id": parent["id"]}
                ).execute()
            except Exception as e:
                if is_duplicate_error(e):
                    await message.reply_text(dup_text)
                    return EDIT_VALUE
                raise
            clear_category_input(context)
            await reply_screen(message, children_admin_screen, target, "✅ Sub-subcategory added successfully.")
            return ConversationHandler.END

        if action == "add_sub":
            error = validate_subcategory_name(text)
            if error:
                await message.reply_text(error)
                return EDIT_VALUE
            if not subcategory_table_ready(force=True):
                clear_category_input(context)
                await message.reply_text(subcategory_unavailable_text())
                return ConversationHandler.END
            info = get_category_info(target)
            if not info:
                clear_category_input(context)
                await reply_screen(message, categories_admin_screen, "⚠️ This category no longer exists.")
                return ConversationHandler.END
            rows = load_subcategory_rows(info["id"])
            if any(r["name"].lower() == text.lower() for r in rows):
                await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                return EDIT_VALUE
            next_order = max([r.get("display_order") or 0 for r in rows] + [0]) + 1
            try:
                supabase.table("subcategories").insert(
                    {"category_id": info["id"], "name": text, "is_active": True, "display_order": next_order}
                ).execute()
            except Exception as e:
                if is_duplicate_error(e):
                    await message.reply_text("⚠️ This category already has a subcategory with that name. Please send a different name.")
                    return EDIT_VALUE
                raise
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
    elif data == "admin_dashboard" or data.startswith("dashboard_"):
        await handle_dashboard_callback(query, context, data)
    elif data == "admin_tracking" or data.startswith("pt_"):
        await handle_tracking_callback(query, context, data)
    elif data == "admin_users" or data.startswith("ub_"):
        await handle_users_broadcast_callback(query, context, data)
    else:
        await handle_manage_callback(query, context, data)


# ---------------------------------------------------------------------------
# ADMIN PANEL -> PRODUCT TRACKING  (separate from the Dashboard)
# Callbacks: admin_tracking, pt_all_<page>, pt_top_<page>, pt_recent_<page>,
#            pt_prod_<product_id>_<range>_<page>      (range: t | 7 | 30 | a)
# Data: table `product_tracking` (see product_tracking.sql). Events:
#   view          - a product card was shown to a customer
#   product_click - customer tapped "🛒 View Product" (bot-side redirect hit)
#   link_click    - the bot redirected that tap to the external product URL
# Telegram URL buttons cannot call the bot, so "View Product" points to
# <RENDER_EXTERNAL_URL>/go/<product_id>?u=<telegram_user_id>; that route records
# the events and immediately 302-redirects to the product's saved link. The bot
# cannot know whether the external page finished loading, so "link clicks" means
# "redirects issued", and it is always equal to "product clicks" for one tap.
# Date filters are UTC: Today = since 00:00 UTC today; Last 7 Days = today plus
# the 6 previous UTC days; Last 30 Days = today plus the 29 previous UTC days.
# ---------------------------------------------------------------------------

TRACK_TABLE = "product_tracking"
TRACK_PAGE_SIZE = 5
TRACK_RECENT_PAGE_SIZE = 10
TRACK_FETCH_PAGE = 1000
TRACK_REDIRECT_BUCKET_SECONDS = 5  # same user + product within this window counts once
EVENT_VIEW = "view"
EVENT_PRODUCT_CLICK = "product_click"
EVENT_LINK_CLICK = "link_click"
TRACK_EVENT_LABELS = {
    EVENT_VIEW: "👁️ View",
    EVENT_PRODUCT_CLICK: "🛒 Product click",
    EVENT_LINK_CLICK: "🔗 Link click",
}
TRACK_RANGES = {
    "t": ("Today", 0),
    "7": ("Last 7 Days", 6),
    "30": ("Last 30 Days", 29),
    "a": ("All Time", None),
}


def tracking_back(callback_data):
    return InlineKeyboardButton("🔙 Back", callback_data=callback_data)


def tracking_since(range_key, now=None):
    """UTC start boundary (inclusive) for a range, or None for All Time."""
    days_back = TRACK_RANGES.get(range_key, TRACK_RANGES["a"])[1]
    if days_back is None:
        return None
    now = now or datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=days_back)


def tracked_link(product_id, user_id, link):
    """URL for the customer's "View Product" button. Falls back to the direct link
    if the public URL is unknown, so the button can never break."""
    base = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not base or product_id is None:
        return link
    url = f"{base}/go/{quote(str(product_id), safe='')}"
    if user_id is not None:
        url += f"?u={int(user_id)}"
    return url


def tracking_row(product_id, event_type, user_id, dedup_key):
    return {
        "product_id": product_id,
        "event_type": event_type,
        "telegram_user_id": int(user_id) if user_id is not None else None,
        "dedup_key": dedup_key,
    }


def _insert_tracking_rows(rows):
    # dedup_key is UNIQUE: a repeated key (Telegram retry) is silently ignored
    supabase.table(TRACK_TABLE).upsert(rows, on_conflict="dedup_key", ignore_duplicates=True).execute()


async def record_tracking_events(rows):
    """Write events without ever raising: tracking must never break a customer flow."""
    if not rows:
        return
    try:
        await asyncio.to_thread(_insert_tracking_rows, rows)
    except Exception as e:
        print(f"TRACKING WRITE ERROR: {type(e).__name__}: {safe_error_text(e)}")


def _lookup_product_link(product_id):
    result = supabase.table("products").select("id,affiliate_link").eq("id", product_id).limit(1).execute()
    return result.data[0] if result.data else None


async def track_redirect(request):
    """GET /go/<product_id>?u=<telegram_user_id>: record the click, then redirect."""
    product_id = request.match_info["product_id"]
    raw_user = request.query.get("u", "")
    user_id = int(raw_user) if raw_user.isdigit() and len(raw_user) <= 18 else None
    try:
        product = await asyncio.to_thread(_lookup_product_link, product_id)
    except Exception as e:
        print(f"TRACKING REDIRECT LOOKUP ERROR: {type(e).__name__}: {safe_error_text(e)}")
        product = None
    link = str((product or {}).get("affiliate_link") or "").strip()
    # only ever redirect to the http(s) link stored for this product (no open redirect)
    if not link.lower().startswith(("http://", "https://")):
        return web.Response(status=404, text="This link is no longer available.")
    if request.method == "GET":
        bucket = int(time.time() // TRACK_REDIRECT_BUCKET_SECONDS)
        base_key = f"go:{product_id}:{user_id if user_id is not None else 'anon'}:{bucket}"
        await record_tracking_events([
            tracking_row(product_id, EVENT_PRODUCT_CLICK, user_id, f"{base_key}:{EVENT_PRODUCT_CLICK}"),
            tracking_row(product_id, EVENT_LINK_CLICK, user_id, f"{base_key}:{EVENT_LINK_CLICK}"),
        ])
    raise web.HTTPFound(link)


# ----- statistics queries -----

def _empty_counts():
    return {EVENT_VIEW: 0, EVENT_PRODUCT_CLICK: 0, EVENT_LINK_CLICK: 0}


def _counts_via_rpc(since):
    result = supabase.rpc("product_tracking_counts", {"p_since": since.isoformat() if since else None}).execute()
    counts = {}
    for row in result.data or []:
        counts[str(row["product_id"])] = {
            EVENT_VIEW: int(row.get("views") or 0),
            EVENT_PRODUCT_CLICK: int(row.get("product_clicks") or 0),
            EVENT_LINK_CLICK: int(row.get("link_clicks") or 0),
        }
    return counts


def _counts_via_paging(since):
    counts = {}
    start = 0
    while True:
        query = supabase.table(TRACK_TABLE).select("product_id,event_type")
        if since is not None:
            query = query.gte("created_at", since.isoformat())
        chunk = query.order("id").range(start, start + TRACK_FETCH_PAGE - 1).execute().data or []
        for row in chunk:
            bucket = counts.setdefault(str(row["product_id"]), _empty_counts())
            if row.get("event_type") in bucket:
                bucket[row["event_type"]] += 1
        if len(chunk) < TRACK_FETCH_PAGE:
            return counts
        start += TRACK_FETCH_PAGE


def fetch_tracking_counts(since=None):
    """{product_id(str): {event_type: count}} for every product that has events,
    in ONE grouped query (SQL function), or a paged read if the function is missing."""
    try:
        return _counts_via_rpc(since)
    except Exception as e:
        print(f"TRACKING RPC UNAVAILABLE, using paged read: {type(e).__name__}: {safe_error_text(e)}")
        return _counts_via_paging(since)


def fetch_product_counts(product_id, since=None):
    """Exact counts for ONE product (three count-only queries)."""
    counts = {}
    for event_type in (EVENT_VIEW, EVENT_PRODUCT_CLICK, EVENT_LINK_CLICK):
        query = (
            supabase.table(TRACK_TABLE).select("id", count="exact")
            .eq("product_id", product_id).eq("event_type", event_type)
        )
        if since is not None:
            query = query.gte("created_at", since.isoformat())
        result = query.limit(1).execute()
        counts[event_type] = int(result.count or 0)
    return counts


def fetch_products_by_ids(ids):
    """{product_id(str): product_row} for the given ids, in one query."""
    ids = list(ids)
    if not ids:
        return {}
    result = supabase.table("products").select(PRODUCT_COLUMNS).in_("id", ids).execute()
    return {str(row["id"]): row for row in (result.data or [])}


def total_interactions(counts):
    return counts[EVENT_VIEW] + counts[EVENT_PRODUCT_CLICK] + counts[EVENT_LINK_CLICK]


def tracking_time_text(value):
    text = str(value or "")
    return f"{text[:10]} {text[11:16]} UTC" if len(text) >= 16 else "-"


def paging_row(prefix, page, has_next):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"{prefix}_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"{prefix}_{page + 1}"))
    return nav


# ----- screens -----

TRACKING_TITLE = "📊 PRODUCT TRACKING"


async def show_tracking_menu(query):
    keyboard = [
        [InlineKeyboardButton("📊 All Products", callback_data="pt_all_0")],
        [InlineKeyboardButton("🔥 Top Products", callback_data="pt_top_0")],
        [InlineKeyboardButton("📅 Recent Activity", callback_data="pt_recent_0")],
        *admin_nav_rows("admin"),
    ]
    await safe_edit(query, f"{TRACKING_TITLE}\n\nChoose what you want to view.", InlineKeyboardMarkup(keyboard))


async def show_tracking_all(query, page):
    page = max(0, page)
    offset = page * TRACK_PAGE_SIZE
    back = InlineKeyboardMarkup(admin_nav_rows("admin_tracking"))
    try:
        rows = (
            supabase.table("products").select(PRODUCT_COLUMNS).order("id")
            .range(offset, offset + TRACK_PAGE_SIZE).execute().data or []
        )
    except Exception as e:
        print(f"TRACKING LIST ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(query, f"{TRACKING_TITLE}\n\n❌ Could not load products.\n\nPlease try again.", back)
        return
    if not rows and page > 0:
        await show_tracking_all(query, page - 1)
        return
    if not rows:
        await safe_edit(query, f"{TRACKING_TITLE}\n\nNo products found yet.", back)
        return
    has_next = len(rows) > TRACK_PAGE_SIZE
    keyboard = []
    for product in rows[:TRACK_PAGE_SIZE]:
        callback = f"pt_prod_{product['id']}_a_{page}"
        if not fits_callback(callback):
            continue
        icon = "" if product.get("is_active") else " 🔴"
        label = f"🛍️ {str(product.get('name') or 'Product')[:32]}{icon}"
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    nav = paging_row("pt_all", page, has_next)
    if nav:
        keyboard.append(nav)
    keyboard.extend(admin_nav_rows("admin_tracking"))
    await safe_edit(
        query,
        f"{TRACKING_TITLE} (Page {page + 1})\n\nChoose a product. 🔴 = hidden from customers.",
        InlineKeyboardMarkup(keyboard),
    )


async def show_tracking_product(query, product_id, range_key, page):
    if range_key not in TRACK_RANGES:
        range_key = "a"

    # Always keep both actions available, including on database/API errors.
    back = InlineKeyboardMarkup([
        *admin_nav_rows(f"pt_all_{page}"),
    ])
    refresh = InlineKeyboardButton(
        "🔄 Refresh",
        callback_data=f"pt_prod_{product_id}_{range_key}_{page}",
    )
    error_markup = InlineKeyboardMarkup([
        [refresh],
        *admin_nav_rows(f"pt_all_{page}"),
    ])

    try:
        print(
            f"TRACKING PRODUCT QUERY START: product_id={str(product_id)[:120]} "
            f"range={range_key} page={page}",
            flush=True,
        )
        product = fetch_product(product_id)
        if product is None:
            print("TRACKING PRODUCT NOT FOUND", flush=True)
            await safe_edit(query, f"{TRACKING_TITLE}\n\nThis product no longer exists.", back)
            return

        # Use the ID returned by Supabase instead of the callback string. This keeps
        # integer/UUID product-id types consistent with the database query.
        real_product_id = product.get("id", product_id)
        since = tracking_since(range_key)
        print(
            f"TRACKING COUNTS QUERY START: product_id={str(real_product_id)[:120]} "
            f"since={since.isoformat() if since else 'all'}",
            flush=True,
        )
        counts = fetch_product_counts(real_product_id, since)
        print(
            f"TRACKING COUNTS QUERY SUCCESS: product_id={str(real_product_id)[:120]} "
            f"views={counts.get(EVENT_VIEW, 0)} "
            f"product_clicks={counts.get(EVENT_PRODUCT_CLICK, 0)} "
            f"link_clicks={counts.get(EVENT_LINK_CLICK, 0)}",
            flush=True,
        )
        category = get_category_names().get(product.get("category_id"), "Unknown category")
    except Exception as e:
        print(
            f"TRACKING PRODUCT ERROR: {type(e).__name__}: {safe_error_text(e)}",
            flush=True,
        )
        await safe_edit(
            query,
            f"{TRACKING_TITLE}\n\n❌ Could not load tracking data.\n\n"
            "The database request failed.\nPlease tap Refresh to try again.",
            error_markup,
        )
        return
    hidden = "" if product.get("is_active") else "\n🔴 Hidden from customers"
    text = (
        f"{TRACKING_TITLE}\n\n{product.get('name', 'Product')}{hidden}\n\n"
        f"📁 Category: {category}\n"
        f"📂 Subcategory: {product.get('subcategory') or '-'}\n"
        f"🛍️ Platform: {product.get('platform') or '-'}\n\n"
        f"🗓️ Period: {TRACK_RANGES[range_key][0]} (UTC)\n\n"
        f"👁️ Views: {counts[EVENT_VIEW]}\n"
        f"🛒 Product Clicks: {counts[EVENT_PRODUCT_CLICK]}\n"
        f"🔗 Link Clicks: {counts[EVENT_LINK_CLICK]}\n"
        f"📈 Total Interactions: {total_interactions(counts)}"
    )
    def btn(key, label):
        return InlineKeyboardButton(label, callback_data=f"pt_prod_{product_id}_{key}_{page}")
    keyboard = [
        [btn("t", "📅 Today"), btn("7", "📆 Last 7 Days")],
        [btn("30", "📆 Last 30 Days"), btn("a", "📊 All Time")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"pt_prod_{product_id}_{range_key}_{page}")],
        *admin_nav_rows(f"pt_all_{page}"),
    ]
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_tracking_top(query, page):
    page = max(0, page)
    back = InlineKeyboardMarkup(admin_nav_rows("admin_tracking"))
    try:
        counts = fetch_tracking_counts(None)
        ranked = sorted(counts.items(), key=lambda item: (-total_interactions(item[1]), item[0]))
        offset = page * TRACK_PAGE_SIZE
        if not ranked:
            await safe_edit(query, f"{TRACKING_TITLE}\n\n🔥 TOP PRODUCTS\n\nNo tracking data yet.", back)
            return
        if offset >= len(ranked):
            await show_tracking_top(query, max(0, (len(ranked) - 1) // TRACK_PAGE_SIZE))
            return
        chunk = ranked[offset:offset + TRACK_PAGE_SIZE]
        products = fetch_products_by_ids([pid for pid, _c in chunk])
    except Exception as e:
        print(f"TRACKING TOP ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(query, f"{TRACKING_TITLE}\n\n❌ Could not load tracking data.\n\nPlease try again.", back)
        return
    lines = [f"{TRACKING_TITLE}\n\n🔥 TOP PRODUCTS (Page {page + 1})\nRanked by total interactions.\n"]
    for i, (pid, c) in enumerate(chunk):
        name = products[pid].get("name", "Product") if pid in products else "Deleted product"
        lines.append(
            f"{offset + i + 1}. {name}\n"
            f"   👁️ {c[EVENT_VIEW]} · 🛒 {c[EVENT_PRODUCT_CLICK]} · 🔗 {c[EVENT_LINK_CLICK]} · 📈 {total_interactions(c)}\n"
        )
    keyboard = []
    nav = paging_row("pt_top", page, offset + TRACK_PAGE_SIZE < len(ranked))
    if nav:
        keyboard.append(nav)
    keyboard.extend(admin_nav_rows("admin_tracking"))
    await safe_edit(query, "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard))


async def show_tracking_recent(query, page):
    page = max(0, page)
    back = InlineKeyboardMarkup(admin_nav_rows("admin_tracking"))
    offset = page * TRACK_RECENT_PAGE_SIZE
    try:
        events = (
            supabase.table(TRACK_TABLE).select("id,product_id,event_type,created_at")
            .order("created_at", desc=True).order("id", desc=True)
            .range(offset, offset + TRACK_RECENT_PAGE_SIZE).execute().data or []
        )
        if not events and page > 0:
            await show_tracking_recent(query, page - 1)
            return
        if not events:
            await safe_edit(query, f"{TRACKING_TITLE}\n\n📅 RECENT ACTIVITY\n\nNo tracking data yet.", back)
            return
        has_next = len(events) > TRACK_RECENT_PAGE_SIZE
        events = events[:TRACK_RECENT_PAGE_SIZE]
        products = fetch_products_by_ids({str(e["product_id"]) for e in events})
    except Exception as e:
        print(f"TRACKING RECENT ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(query, f"{TRACKING_TITLE}\n\n❌ Could not load tracking data.\n\nPlease try again.", back)
        return
    lines = [f"{TRACKING_TITLE}\n\n📅 RECENT ACTIVITY (Page {page + 1})\nNewest first.\n"]
    for event in events:
        pid = str(event["product_id"])
        name = products[pid].get("name", "Product") if pid in products else "Deleted product"
        label = TRACK_EVENT_LABELS.get(event.get("event_type"), str(event.get("event_type")))
        lines.append(f"{label} — {name}\n   🕒 {tracking_time_text(event.get('created_at'))}\n")
    keyboard = []
    nav = paging_row("pt_recent", page, has_next)
    if nav:
        keyboard.append(nav)
    keyboard.extend(admin_nav_rows("admin_tracking"))
    await safe_edit(query, "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard))


def _page_number(text):
    return int(text) if text.isdigit() else 0


async def handle_tracking_callback(query, context, data):
    """Single entry point for every Product Tracking callback. Admin-only."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return
    if data == "admin_tracking":
        await show_tracking_menu(query)
    elif data.startswith("pt_all_"):
        await show_tracking_all(query, _page_number(data[len("pt_all_"):]))
    elif data.startswith("pt_top_"):
        await show_tracking_top(query, _page_number(data[len("pt_top_"):]))
    elif data.startswith("pt_recent_"):
        await show_tracking_recent(query, _page_number(data[len("pt_recent_"):]))
    elif data.startswith("pt_prod_"):
        parts = data[len("pt_prod_"):].rsplit("_", 2)
        if len(parts) == 3 and parts[0]:
            await show_tracking_product(query, parts[0], parts[1], _page_number(parts[2]))
        else:
            await show_tracking_menu(query)
    else:
        await show_tracking_menu(query)


# ---------------------------------------------------------------------------
# ADMIN PANEL -> USERS & BROADCAST
# Callbacks: admin_users, ub_users_<page>, ub_user_<telegram_id>,
#            ub_disable_<telegram_id>, ub_enable_<telegram_id>,
#            ub_broadcast, ub_bc_create, ub_bc_send, ub_bc_edit, ub_bc_cancel
# Data: table `users` (see users_broadcast.sql).
# is_active is an admin flag only; disabled users are still allowed to use the bot.
# Total Users  = every row in public.users
# Active Users = rows where is_active is not False
# ---------------------------------------------------------------------------

USERS_TABLE = "users"
USERS_PAGE_SIZE = 10
USERS_COLUMNS = "id,telegram_id,first_name,last_name,username,first_seen,last_seen,is_active"
BROADCAST_DELAY_SECONDS = 0.05  # gentle pacing between outbound messages


def ub_back(callback_data):
    return InlineKeyboardButton("🔙 Back", callback_data=callback_data)


def _user_upsert_sync(user):
    """Insert or update a Telegram user. Never overwrites is_active or first_seen."""
    now = datetime.now(timezone.utc).isoformat()
    row = {
        "telegram_id": int(user.id),
        "first_name": (user.first_name or None),
        "last_name": (user.last_name or None),
        "username": (user.username or None),
        "last_seen": now,
        "updated_at": now,
    }
    # On INSERT, first_seen / is_active use table defaults.
    # On UPDATE, only the columns listed above are changed.
    supabase.table(USERS_TABLE).upsert(row, on_conflict="telegram_id").execute()


async def register_or_touch_user(user):
    """Fail-safe: never raise into customer or admin flows."""
    if user is None or getattr(user, "id", None) is None:
        return
    try:
        await asyncio.to_thread(_user_upsert_sync, user)
    except Exception as e:
        print(f"USER UPSERT ERROR: {type(e).__name__}: {safe_error_text(e)}")


def _users_status_counts():
    """Return (total, active). Active = is_active is not False."""
    total_result = supabase.table(USERS_TABLE).select("id", count="exact").limit(1).execute()
    active_result = (
        supabase.table(USERS_TABLE).select("id", count="exact")
        .eq("is_active", True).limit(1).execute()
    )
    total = int(total_result.count or 0)
    active = int(active_result.count or 0)
    return total, active


def _fetch_users_page(page):
    page = max(0, page)
    offset = page * USERS_PAGE_SIZE
    rows = (
        supabase.table(USERS_TABLE).select(USERS_COLUMNS)
        .order("last_seen", desc=True)
        .range(offset, offset + USERS_PAGE_SIZE)
        .execute().data or []
    )
    has_next = len(rows) > USERS_PAGE_SIZE
    return rows[:USERS_PAGE_SIZE], has_next, page


def _fetch_user_by_telegram_id(telegram_id):
    result = (
        supabase.table(USERS_TABLE).select(USERS_COLUMNS)
        .eq("telegram_id", telegram_id).limit(1).execute()
    )
    return result.data[0] if result.data else None


def _set_user_active(telegram_id, make_active):
    now = datetime.now(timezone.utc).isoformat()
    supabase.table(USERS_TABLE).update({
        "is_active": make_active,
        "updated_at": now,
    }).eq("telegram_id", telegram_id).execute()


def _active_recipient_ids():
    """All telegram_id values for users with is_active = true (paged)."""
    ids = []
    start = 0
    page = 1000
    while True:
        chunk = (
            supabase.table(USERS_TABLE).select("telegram_id")
            .eq("is_active", True)
            .order("id")
            .range(start, start + page - 1)
            .execute().data or []
        )
        for row in chunk:
            tid = row.get("telegram_id")
            if tid is not None:
                ids.append(int(tid))
        if len(chunk) < page:
            return ids
        start += page


def _fmt_user_time(value):
    text = str(value or "")
    if len(text) >= 16:
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return dt.astimezone(timezone.utc).strftime("%d %b %Y, %H:%M UTC")
        except Exception:
            return f"{text[:10]} {text[11:16]} UTC"
    return "-"


def _user_display_name(row):
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    name = f"{first} {last}".strip()
    return name or "User"


def _user_list_line(row):
    name = _user_display_name(row)
    username = row.get("username")
    handle = f"@{username}" if username else "—"
    status = "🟢 Active" if row.get("is_active") is not False else "🔴 Disabled"
    return (
        f"👤 {name}\n"
        f"{handle}\n"
        f"ID: {row.get('telegram_id')}\n"
        f"{status}\n"
        f"Last seen: {_fmt_user_time(row.get('last_seen'))}"
    )


async def show_users_broadcast_home(query, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    try:
        total, active = await asyncio.to_thread(_users_status_counts)
        text = (
            f"{prefix}👥 USERS & BROADCAST\n\n"
            "Manage users and send announcements.\n\n"
            f"👥 Users: {total}\n"
            f"🟢 Active: {active}"
        )
    except Exception as e:
        print(f"USERS HOME ERROR: {type(e).__name__}: {safe_error_text(e)}")
        text = (
            f"{prefix}👥 USERS & BROADCAST\n\n"
            "❌ Could not load user counts.\n\n"
            "If this is the first time, run users_broadcast.sql in Supabase, then try again."
        )
    keyboard = [
        [InlineKeyboardButton("👥 Manage Users", callback_data="ub_users_0")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="ub_broadcast")],
        *admin_nav_rows("admin"),
    ]
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_manage_users(query, page):
    back = InlineKeyboardMarkup(admin_nav_rows("admin_users"))
    try:
        rows, has_next, page = await asyncio.to_thread(_fetch_users_page, page)
    except Exception as e:
        print(f"USERS LIST ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(
            query,
            "👥 MANAGE USERS\n\n❌ Could not load users.\n\nPlease try again.",
            back,
        )
        return
    if not rows and page > 0:
        await show_manage_users(query, page - 1)
        return
    if not rows:
        await safe_edit(query, "👥 MANAGE USERS\n\nNo users registered yet.", back)
        return
    lines = [f"👥 MANAGE USERS (Page {page + 1})\n\nChoose a user.\n"]
    keyboard = []
    for row in rows:
        tid = row.get("telegram_id")
        callback = f"ub_user_{tid}"
        if not fits_callback(callback):
            continue
        icon = "🟢" if row.get("is_active") is not False else "🔴"
        label = f"{icon} {_user_display_name(row)[:28]}"
        lines.append(_user_list_line(row) + "\n")
        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"ub_users_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"ub_users_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.extend(admin_nav_rows("admin_users"))
    await safe_edit(query, "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard))


async def show_user_detail(query, telegram_id, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    back = InlineKeyboardMarkup(admin_nav_rows("ub_users_0"))
    try:
        row = await asyncio.to_thread(_fetch_user_by_telegram_id, int(telegram_id))
    except Exception as e:
        print(f"USER DETAIL ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(query, f"{prefix}❌ Could not load this user.", back)
        return
    if not row:
        await safe_edit(query, f"{prefix}❌ User not found.", back)
        return
    active = row.get("is_active") is not False
    username = row.get("username")
    handle = f"@{username}" if username else "—"
    text = (
        f"{prefix}👤 USER DETAILS\n\n"
        f"Name: {_user_display_name(row)}\n"
        f"Username: {handle}\n"
        f"Telegram ID: {row.get('telegram_id')}\n\n"
        f"Status: {'🟢 Active' if active else '🔴 Disabled'}\n\n"
        f"First seen: {_fmt_user_time(row.get('first_seen'))}\n"
        f"Last seen: {_fmt_user_time(row.get('last_seen'))}"
    )
    toggle = (
        InlineKeyboardButton("🚫 Disable User", callback_data=f"ub_disable_{telegram_id}")
        if active
        else InlineKeyboardButton("🟢 Enable User", callback_data=f"ub_enable_{telegram_id}")
    )
    keyboard = [[toggle], *admin_nav_rows("ub_users_0")]
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_broadcast_home(query, notice=None):
    prefix = f"{notice}\n\n" if notice else ""
    text = (
        f"{prefix}📢 BROADCAST\n\n"
        "Send a message to your registered users.\n\n"
        "You can send text announcements to all active users."
    )
    keyboard = [
        [InlineKeyboardButton("✉️ Create Broadcast", callback_data="ub_bc_create")],
        *admin_nav_rows("admin_users"),
    ]
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def show_broadcast_preview(query, context):
    message = (context.user_data.get("ub_broadcast_text") or "").strip()
    if not message:
        await show_broadcast_home(query, "⚠️ No message to preview.")
        return
    try:
        recipients = await asyncio.to_thread(_active_recipient_ids)
        count = len(recipients)
    except Exception as e:
        print(f"BROADCAST RECIPIENTS ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(
            query,
            "📢 BROADCAST PREVIEW\n\n❌ Could not load recipient count.\n\nPlease try again.",
            InlineKeyboardMarkup(admin_nav_rows("ub_broadcast")),
        )
        return
    preview = message if len(message) <= 900 else message[:897] + "…"
    text = (
        f"📢 BROADCAST PREVIEW\n\n{preview}\n\n"
        f"👥 Recipients: {count:,}"
    )
    keyboard = [
        [InlineKeyboardButton("📤 Send Broadcast", callback_data="ub_bc_send")],
        [InlineKeyboardButton("✏️ Edit Message", callback_data="ub_bc_edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="ub_bc_cancel")],
    ]
    await safe_edit(query, text, InlineKeyboardMarkup(keyboard))


async def run_broadcast(query, context):
    message = (context.user_data.get("ub_broadcast_text") or "").strip()
    context.user_data.pop("ub_broadcast_text", None)
    if not message:
        await show_broadcast_home(query, "⚠️ Broadcast cancelled — empty message.")
        return
    try:
        recipients = await asyncio.to_thread(_active_recipient_ids)
    except Exception as e:
        print(f"BROADCAST SEND LIST ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(
            query,
            "📢 BROADCAST\n\n❌ Could not load recipients.\n\nPlease try again.",
            InlineKeyboardMarkup(admin_nav_rows("admin_users")),
        )
        return
    total = len(recipients)
    await safe_edit(
        query,
        f"📢 BROADCAST\n\nSending to {total:,} active user(s)…\nPlease wait.",
        None,
    )
    sent = 0
    failed = 0
    bot = context.bot
    for tid in recipients:
        try:
            await bot.send_message(chat_id=tid, text=message)
            sent += 1
        except RetryAfter as e:
            wait = float(getattr(e, "retry_after", 1) or 1)
            await asyncio.sleep(wait)
            try:
                await bot.send_message(chat_id=tid, text=message)
                sent += 1
            except Exception as e2:
                failed += 1
                print(f"BROADCAST SEND ERROR (retry): {type(e2).__name__}: {safe_error_text(e2)}")
        except (Forbidden, BadRequest, TelegramError) as e:
            failed += 1
            print(f"BROADCAST SEND ERROR: {type(e).__name__}: {safe_error_text(e)}")
        except Exception as e:
            failed += 1
            print(f"BROADCAST SEND ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)
    text = (
        f"✅ BROADCAST COMPLETE\n\n"
        f"👥 Total: {total:,}\n"
        f"✅ Sent: {sent:,}\n"
        f"❌ Failed: {failed:,}"
    )
    await safe_edit(query, text, InlineKeyboardMarkup(admin_nav_rows("admin_users")))


async def handle_users_broadcast_callback(query, context, data):
    """Single entry for Users & Broadcast callbacks. Admin-only."""
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return

    if data == "admin_users":
        await show_users_broadcast_home(query)
    elif data.startswith("ub_users_"):
        page_text = data[len("ub_users_"):]
        await show_manage_users(query, int(page_text) if page_text.isdigit() else 0)
    elif data.startswith("ub_user_"):
        tid = data[len("ub_user_"):]
        if tid.isdigit():
            await show_user_detail(query, tid)
        else:
            await show_manage_users(query, 0)
    elif data.startswith("ub_disable_"):
        tid = data[len("ub_disable_"):]
        try:
            await asyncio.to_thread(_set_user_active, int(tid), False)
            notice = "🚫 User disabled."
        except Exception as e:
            print(f"USER DISABLE ERROR: {type(e).__name__}: {safe_error_text(e)}")
            notice = "❌ Could not update the user."
        await show_user_detail(query, tid, notice)
    elif data.startswith("ub_enable_"):
        tid = data[len("ub_enable_"):]
        try:
            await asyncio.to_thread(_set_user_active, int(tid), True)
            notice = "✅ User enabled."
        except Exception as e:
            print(f"USER ENABLE ERROR: {type(e).__name__}: {safe_error_text(e)}")
            notice = "❌ Could not update the user."
        await show_user_detail(query, tid, notice)
    elif data == "ub_broadcast":
        await show_broadcast_home(query)
    elif data == "ub_bc_cancel":
        context.user_data.pop("ub_broadcast_text", None)
        await show_users_broadcast_home(query, "❌ Broadcast cancelled.")
    elif data == "ub_bc_send":
        await run_broadcast(query, context)
    elif data in ("ub_bc_create", "ub_bc_edit"):
        # Handled by ConversationHandler entry; should not reach here normally.
        await show_broadcast_home(query)
    else:
        await show_users_broadcast_home(query)


# ----- Broadcast text input (ConversationHandler) -----

async def broadcast_start(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return ConversationHandler.END
    if query.data == "ub_bc_edit":
        prompt = (
            "✏️ EDIT BROADCAST\n\n"
            "Send the new message text.\n\n"
            "Send /cancel to stop."
        )
    else:
        context.user_data.pop("ub_broadcast_text", None)
        prompt = (
            "📢 CREATE BROADCAST\n\n"
            "Send the message you want to broadcast to all active users.\n\n"
            "Text only for now.\n\n"
            "Send /cancel to stop."
        )
    await safe_edit(
        query,
        prompt,
        InlineKeyboardMarkup(admin_nav_rows("ub_bc_cancel")),
    )
    return BROADCAST_TEXT


async def receive_broadcast_text(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Please send the broadcast as text.")
        return BROADCAST_TEXT
    if len(text) > 4000:
        await update.message.reply_text("⚠️ Message is too long. Please keep it under 4000 characters.")
        return BROADCAST_TEXT
    context.user_data["ub_broadcast_text"] = text
    try:
        recipients = await asyncio.to_thread(_active_recipient_ids)
        count = len(recipients)
    except Exception as e:
        print(f"BROADCAST PREVIEW ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await update.message.reply_text(
            "❌ Could not load recipient count.\n\nPlease try again.",
            reply_markup=InlineKeyboardMarkup(admin_nav_rows("ub_broadcast")),
        )
        return ConversationHandler.END
    preview = text if len(text) <= 900 else text[:897] + "…"
    body = (
        f"📢 BROADCAST PREVIEW\n\n{preview}\n\n"
        f"👥 Recipients: {count:,}"
    )
    keyboard = [
        [InlineKeyboardButton("📤 Send Broadcast", callback_data="ub_bc_send")],
        [InlineKeyboardButton("✏️ Edit Message", callback_data="ub_bc_edit")],
        [InlineKeyboardButton("❌ Cancel", callback_data="ub_bc_cancel")],
    ]
    await update.message.reply_text(body, reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def cancel_broadcast(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
    context.user_data.pop("ub_broadcast_text", None)
    await update.message.reply_text(
        "❌ Broadcast cancelled.",
        reply_markup=InlineKeyboardMarkup(admin_nav_rows("admin_users")),
    )
    return ConversationHandler.END


async def broadcast_escape(update, context):
    """Leave the broadcast conversation and run the requested admin action."""
    query = update.callback_query
    await query.answer()
    data = query.data
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    if data == "ub_bc_cancel":
        context.user_data.pop("ub_broadcast_text", None)
        if is_admin:
            await show_users_broadcast_home(query, "❌ Broadcast cancelled.")
        else:
            await deny_non_admin(query)
        return ConversationHandler.END
    if data == "main":
        context.user_data.pop("ub_broadcast_text", None)
        await show_main_menu(query, is_admin)
        return ConversationHandler.END
    if data == "admin":
        context.user_data.pop("ub_broadcast_text", None)
        if is_admin:
            await show_admin_panel(query)
        else:
            await deny_non_admin(query)
        return ConversationHandler.END
    # Keep draft if navigating to preview-related callbacks; clear otherwise
    if data not in ("ub_bc_send", "ub_bc_edit"):
        context.user_data.pop("ub_broadcast_text", None)
    if is_admin:
        await dispatch_admin_callback(query, context, data)
    else:
        await deny_non_admin(query)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# SAVED ITEMS  (customer)
# Callbacks: saved_home, saved_page_<n>, saved_toggle_<product_id>
# Table: public.saved_items (see saved_items.sql)
# Per Telegram user; unique (telegram_id, product_id). Only active products
# are listed for customers; hidden products keep their saved rows.
# ---------------------------------------------------------------------------

SAVED_TABLE = "saved_items"
SAVED_PAGE_SIZE = 5


def product_card_markup(product_id, user_id, link, is_saved, back_row, detail=False):
    """Existing product card keyboard + Save Item toggle. back_row is a list of buttons.
    detail=True is the Product Details layout: Buy Now, then Save and Back side by side."""
    if detail:
        keyboard = []
        if link:
            keyboard.append([InlineKeyboardButton("🛒 Buy Now", url=tracked_link(product_id, user_id, link))])
        row = []
        toggle_cb = f"saved_toggle_{product_id}" if product_id is not None else ""
        if toggle_cb and fits_callback(toggle_cb):
            row.append(InlineKeyboardButton("♥ Saved" if is_saved else "♡ Save", callback_data=toggle_cb))
        row.extend(back_row or [])
        if row:
            keyboard.append(row)
        return InlineKeyboardMarkup(keyboard)
    keyboard = []
    if link:
        keyboard.append([
            InlineKeyboardButton(
                "🛒 View Product",
                url=tracked_link(product_id, user_id, link),
            )
        ])
    if product_id is not None:
        toggle_cb = f"saved_toggle_{product_id}"
        if fits_callback(toggle_cb):
            label = "♥ Saved" if is_saved else "♡ Save Item"
            keyboard.append([InlineKeyboardButton(label, callback_data=toggle_cb)])
    if back_row:
        keyboard.append(back_row)
    return InlineKeyboardMarkup(keyboard)


def _saved_pid_str(product_id):
    """Normalise product id for storage / comparison (always text)."""
    if product_id is None:
        return None
    return str(product_id).strip()


def _product_ids_for_in_filter(ids):
    """
    Build a list for products.id .in_() that works for both bigint and uuid PKs.
    Prefer native ints when the stored value is all digits; otherwise keep string (uuid).
    """
    out = []
    for pid in ids:
        s = _saved_pid_str(pid)
        if not s:
            continue
        if s.isdigit():
            try:
                out.append(int(s))
                continue
            except (TypeError, ValueError):
                pass
        out.append(s)
    return out


def saved_ids_for_user(telegram_id, product_ids):
    """Return set of product_id strings this user has saved among product_ids."""
    if not product_ids:
        return set()
    try:
        ids = [_saved_pid_str(pid) for pid in product_ids if pid is not None]
        ids = [i for i in ids if i]
        if not ids:
            return set()
        tid = int(telegram_id)
        print(f"SAVED IDS QUERY: telegram_id={tid} count={len(ids)}")
        result = (
            supabase.table(SAVED_TABLE)
            .select("product_id")
            .eq("telegram_id", tid)
            .in_("product_id", ids)
            .execute()
        )
        found = {str(r["product_id"]) for r in (result.data or []) if r.get("product_id") is not None}
        print(f"SAVED IDS QUERY OK: found={len(found)}")
        return found
    except Exception as e:
        print(f"SAVED IDS ERROR: {type(e).__name__}: {safe_error_text(e)}")
        return set()


def is_product_saved(telegram_id, product_id):
    try:
        tid = int(telegram_id)
        pid = _saved_pid_str(product_id)
        if not pid:
            return False
        result = (
            supabase.table(SAVED_TABLE)
            .select("id")
            .eq("telegram_id", tid)
            .eq("product_id", pid)
            .limit(1)
            .execute()
        )
        return bool(result.data)
    except Exception as e:
        print(f"SAVED CHECK ERROR: {type(e).__name__}: {safe_error_text(e)}")
        return False


def _resolve_canonical_product_id(product_id):
    """
    Resolve product_id against public.products so we store the same value shape
    the rest of the bot uses. Returns (canonical_str, native_value, products_id_type).
    """
    raw = product_id
    pid_str = _saved_pid_str(product_id)
    if not pid_str:
        raise ValueError("product_id is required")

    # Try as given, then as int when digits-only (bigint PKs)
    candidates = [pid_str]
    if pid_str.isdigit():
        try:
            candidates.append(int(pid_str))
        except (TypeError, ValueError):
            pass

    last_err = None
    for candidate in candidates:
        try:
            result = (
                supabase.table("products")
                .select("id")
                .eq("id", candidate)
                .limit(1)
                .execute()
            )
            if result.data:
                native = result.data[0]["id"]
                return str(native), native, type(native).__name__
        except Exception as e:
            last_err = e
            print(
                f"SAVE ITEM RESOLVE TRY: candidate={candidate!r} "
                f"type={type(candidate).__name__} err={type(e).__name__}: {safe_error_text(e)}"
            )

    if last_err is not None:
        raise RuntimeError(
            f"could not resolve product id {pid_str!r}: {safe_error_text(last_err)}"
        ) from last_err
    # Product not found — still allow save using the callback string so the
    # row is stored; listing will hide inactive/missing products.
    print(f"SAVE ITEM RESOLVE: product not in products table, using callback id={pid_str!r}")
    return pid_str, pid_str, type(pid_str).__name__


def toggle_saved_product(telegram_id, product_id):
    """
    Insert or delete a saved_items row.
    Returns True if now saved, False if now unsaved.
    Raises on hard failure (caller shows a friendly Telegram message).
    """
    import traceback

    tid = int(telegram_id)
    raw_pid = product_id
    print(
        f"SAVE ITEM START: telegram_id={tid} product_id={raw_pid!r} "
        f"type={type(raw_pid).__name__}"
    )

    try:
        pid_str, native_id, native_type = _resolve_canonical_product_id(raw_pid)
    except Exception as e:
        print(
            f"SAVE ITEM ERROR: resolve failed telegram_id={tid} "
            f"product_id={raw_pid!r} {type(e).__name__}: {safe_error_text(e)}"
        )
        traceback.print_exc()
        raise

    print(
        f"SAVE ITEM RESOLVED: telegram_id={tid} product_id={pid_str!r} "
        f"native={native_id!r} native_type={native_type}"
    )

    # --- check existing (product_id column is text per saved_items.sql) ---
    try:
        existing = (
            supabase.table(SAVED_TABLE)
            .select("id,product_id,telegram_id")
            .eq("telegram_id", tid)
            .eq("product_id", pid_str)
            .limit(1)
            .execute()
            .data
            or []
        )
        print(f"SAVE ITEM EXISTING CHECK: count={len(existing)}")
    except Exception as e:
        print(
            f"SAVE ITEM ERROR: existing-check failed telegram_id={tid} "
            f"product_id={pid_str!r} {type(e).__name__}: {safe_error_text(e)}"
        )
        traceback.print_exc()
        raise

    # --- UNSAVE ---
    if existing:
        print(f"UNSAVE ITEM START: telegram_id={tid} product_id={pid_str!r}")
        try:
            supabase.table(SAVED_TABLE).delete().eq("telegram_id", tid).eq(
                "product_id", pid_str
            ).execute()
            print(f"UNSAVE ITEM SUCCESS: telegram_id={tid} product_id={pid_str!r}")
            return False
        except Exception as e:
            print(
                f"UNSAVE ITEM ERROR: telegram_id={tid} product_id={pid_str!r} "
                f"{type(e).__name__}: {safe_error_text(e)}"
            )
            traceback.print_exc()
            raise

    # --- SAVE (INSERT) ---
    # Do not send saved_at — column has DEFAULT now(). Avoids timestamp format issues.
    # product_id stored as text (matches migration). telegram_id as bigint.
    row = {"telegram_id": tid, "product_id": pid_str}
    print(f"SAVE ITEM INSERT: row={row!r}")
    try:
        result = supabase.table(SAVED_TABLE).insert(row).execute()
        print(
            f"SAVE ITEM SUCCESS: telegram_id={tid} product_id={pid_str!r} "
            f"response={getattr(result, 'data', None)!r}"
        )
        return True
    except Exception as e:
        err = str(e)
        err_l = err.lower()
        # Duplicate race — already saved
        if "duplicate" in err_l or "unique" in err_l or "23505" in err_l:
            print(
                f"SAVE ITEM SUCCESS (already saved / race): telegram_id={tid} "
                f"product_id={pid_str!r}"
            )
            return True
        # Common Supabase issue: table INSERT granted, sequence not granted
        if "sequence" in err_l or "saved_items_id_seq" in err_l or "permission denied" in err_l:
            print(
                "SAVE ITEM ERROR HINT: If error mentions sequence/permission, run once in Supabase SQL:\n"
                "  GRANT USAGE, SELECT ON SEQUENCE public.saved_items_id_seq TO service_role;"
            )
        print(
            f"SAVE ITEM ERROR: telegram_id={tid} product_id={pid_str!r} "
            f"row={row!r} {type(e).__name__}: {safe_error_text(e)}"
        )
        traceback.print_exc()

        # Retry with native type if text insert failed on a non-text column
        if native_id is not None and native_id != pid_str:
            alt_row = {"telegram_id": tid, "product_id": native_id}
            print(f"SAVE ITEM RETRY INSERT: row={alt_row!r}")
            try:
                result = supabase.table(SAVED_TABLE).insert(alt_row).execute()
                print(
                    f"SAVE ITEM SUCCESS (retry native): telegram_id={tid} "
                    f"product_id={native_id!r} response={getattr(result, 'data', None)!r}"
                )
                return True
            except Exception as e2:
                err2_l = str(e2).lower()
                if "duplicate" in err2_l or "unique" in err2_l or "23505" in err2_l:
                    print(
                        f"SAVE ITEM SUCCESS (retry race): telegram_id={tid} "
                        f"product_id={native_id!r}"
                    )
                    return True
                print(
                    f"SAVE ITEM ERROR (retry): telegram_id={tid} "
                    f"{type(e2).__name__}: {safe_error_text(e2)}"
                )
                traceback.print_exc()
                raise
        raise


def fetch_saved_product_page(telegram_id, page):
    """
    Return (products, has_next, page) for active saved products, newest first.
    Hidden/inactive products are omitted but their saved rows remain.
    """
    page = max(0, page)
    tid = int(telegram_id)
    start = page * SAVED_PAGE_SIZE
    # Over-fetch so filtering inactive still fills a page when possible
    end = start + (SAVED_PAGE_SIZE * 3)

    print(f"SAVED LIST QUERY START: telegram_id={tid} page={page} range={start}..{end}")
    try:
        # Same style as working users/tracking queries (order + range)
        result = (
            supabase.table(SAVED_TABLE)
            .select("product_id,saved_at")
            .eq("telegram_id", tid)
            .order("saved_at", desc=True)
            .range(start, end)
            .execute()
        )
        rows = result.data or []
        print(f"SAVED LIST QUERY OK: rows={len(rows)}")
    except Exception as e:
        # Fallback: no order/range (covers older PostgREST / schema-cache quirks)
        print(f"SAVED LIST QUERY ERROR: {type(e).__name__}: {safe_error_text(e)}")
        try:
            result = (
                supabase.table(SAVED_TABLE)
                .select("product_id,saved_at")
                .eq("telegram_id", tid)
                .limit(200)
                .execute()
            )
            rows = result.data or []
            # Newest first when saved_at is present
            rows = sorted(
                rows,
                key=lambda r: r.get("saved_at") or "",
                reverse=True,
            )
            rows = rows[start:end + 1]
            print(f"SAVED LIST FALLBACK OK: rows={len(rows)}")
        except Exception as e2:
            print(f"SAVED LIST FALLBACK ERROR: {type(e2).__name__}: {safe_error_text(e2)}")
            raise RuntimeError(
                f"saved_items select failed: {safe_error_text(e2)}"
            ) from e2

    if not rows and page > 0:
        return fetch_saved_product_page(telegram_id, page - 1)

    ordered_ids = []
    seen = set()
    for r in rows:
        pid = r.get("product_id")
        if pid is None:
            continue
        key = _saved_pid_str(pid)
        if not key or key in seen:
            continue
        seen.add(key)
        ordered_ids.append(key)

    if not ordered_ids:
        return [], False, page

    products_by_id = {}
    query_ids = _product_ids_for_in_filter(ordered_ids)
    print(f"SAVED PRODUCTS QUERY: ids={query_ids[:10]!r} total={len(query_ids)}")
    try:
        result = (
            supabase.table("products")
            .select("id,name,sale_price,platform,affiliate_link,image_url,is_active")
            .in_("id", query_ids)
            .execute()
        )
        for p in result.data or []:
            if p.get("id") is not None:
                products_by_id[_saved_pid_str(p["id"])] = p
        print(f"SAVED PRODUCTS QUERY OK: matched={len(products_by_id)}")
    except Exception as e:
        print(f"SAVED PRODUCTS FETCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        # Second try: all string ids (uuid columns)
        try:
            str_ids = [_saved_pid_str(x) for x in ordered_ids]
            result = (
                supabase.table("products")
                .select("id,name,sale_price,platform,affiliate_link,image_url,is_active")
                .in_("id", str_ids)
                .execute()
            )
            for p in result.data or []:
                if p.get("id") is not None:
                    products_by_id[_saved_pid_str(p["id"])] = p
            print(f"SAVED PRODUCTS RETRY OK: matched={len(products_by_id)}")
        except Exception as e2:
            print(f"SAVED PRODUCTS RETRY ERROR: {type(e2).__name__}: {safe_error_text(e2)}")
            return [], False, page

    active = []
    for pid in ordered_ids:
        p = products_by_id.get(_saved_pid_str(pid))
        if p and p.get("is_active") is not False:
            active.append(p)

    page_rows = active[:SAVED_PAGE_SIZE]
    has_next = len(active) > SAVED_PAGE_SIZE or len(rows) > SAVED_PAGE_SIZE
    return page_rows, has_next, page


async def show_saved_home(query, context, page=0, notice=None):
    """List the customer's saved active products as product cards.
    Header is text-only (no Back under '❤️ SAVED ITEMS')."""
    prefix = f"{notice}\n\n" if notice else ""
    user_id = query.from_user.id
    empty_back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main")]])
    try:
        products, has_next, page = await asyncio.to_thread(
            fetch_saved_product_page, user_id, page
        )
    except Exception as e:
        err_text = safe_error_text(e)
        print(f"SAVED LIST ERROR: {type(e).__name__}: {err_text}")
        await safe_edit(
            query,
            f"{prefix}Saved Items\n\n❌ Could not load saved items.\n\n"
            "Please try again in a moment.",
            empty_back,
        )
        return

    if not products and page == 0:
        # Empty state may include Back so the user is not stuck
        await safe_edit(
            query,
            f"{prefix}Saved Items\n\nYou haven't saved any products yet.",
            empty_back,
        )
        return

    # Text-only header — no keyboard under "❤️ SAVED ITEMS"
    await safe_edit(query, f"{prefix}Saved Items\nYour favorite products", None)
    # Actual Telegram message_id of the header (same message that was edited)
    header_id = query.message.message_id if query.message is not None else None

    # Compact cards (approved design #6); each opens the same Product Details screen.
    first_pid = products[0].get("id")
    # Back reuses the existing saved_back_<product_id>_<header_id> handler: it removes the last card
    # (which carries Back) + the header and shows the Main Menu (other cards via purge_list_cards).
    back_cb = f"saved_back_{first_pid}_{header_id}" if header_id is not None else f"saved_back_{first_pid}"
    if not fits_callback(back_cb):
        back_cb = "main"
    await send_product_cards(query, products, [InlineKeyboardButton("🔙 Back", callback_data=back_cb)])

    # Tracking views (same as category browsing)
    await record_tracking_events([
        tracking_row(
            p["id"], EVENT_VIEW, user_id, f"saved:{query.id}:{p['id']}:{EVENT_VIEW}"
        )
        for p in products if p.get("id") is not None
    ])


def _extract_product_card_bits(message, product_id, user_id):
    """Recover View Product URL and Back row from the current product card keyboard."""
    link = None
    back_row = None
    if message is None or not message.reply_markup:
        return link, back_row
    for row in message.reply_markup.inline_keyboard:
        for btn in row:
            url = getattr(btn, "url", None)
            if url and not link:
                # Prefer original affiliate link from tracked redirect if present
                link = url
            cb = getattr(btn, "callback_data", None) or ""
            if cb == "pdb":  # Product Details layout: Save and Back share a row, keep only Back
                back_row = [btn]
                continue
            if (
                cb in ("main", "saved_home", "search_c_back")
                or cb.startswith("back_")
                or cb.startswith("saved_back_")
            ):
                back_row = list(row)
    if link and "/go/" in str(link):
        # tracked_link was used — resolve real affiliate URL for the button
        try:
            product = fetch_product(product_id)
            if product and product.get("affiliate_link"):
                link = product.get("affiliate_link")
        except Exception as e:
            print(f"SAVE ITEM LINK RESOLVE ERROR: {type(e).__name__}: {safe_error_text(e)}")
    return link, back_row


async def handle_saved_toggle(query, context, product_id):
    """Toggle save for one product and refresh the message keyboard only.
    Always answers the callback query so Telegram stops the loading spinner."""
    import traceback

    user_id = query.from_user.id
    pid = _saved_pid_str(product_id)
    print(
        f"SAVE ITEM HANDLER: user={user_id} product_id={pid!r} "
        f"raw={product_id!r} type={type(product_id).__name__}"
    )

    if not pid:
        try:
            await query.answer("Invalid product.", show_alert=True)
        except Exception:
            pass
        print("SAVE ITEM ERROR: empty product_id from callback")
        return

    try:
        now_saved = await asyncio.to_thread(toggle_saved_product, user_id, pid)
        if now_saved:
            print(f"SAVE ITEM HANDLER DONE (saved): user={user_id} product_id={pid!r}")
        else:
            print(f"SAVE ITEM HANDLER DONE (unsaved): user={user_id} product_id={pid!r}")
    except Exception as e:
        print(
            f"SAVE ITEM HANDLER ERROR: user={user_id} product_id={pid!r} "
            f"{type(e).__name__}: {safe_error_text(e)}"
        )
        traceback.print_exc()
        try:
            await query.answer("Could not update Saved Items. Try again.", show_alert=True)
        except Exception:
            pass
        return

    # Rebuild keyboard: keep View Product + Back, flip Save/Unsave label
    link, back_row = _extract_product_card_bits(query.message, pid, user_id)
    if back_row is None:
        back_row = [InlineKeyboardButton("🔙 Back", callback_data="main")]

    is_detail = any(getattr(b, "callback_data", None) == "pdb" for b in back_row)
    markup = product_card_markup(
        pid, user_id, link, is_saved=now_saved, back_row=back_row, detail=is_detail
    )
    try:
        await query.edit_message_reply_markup(reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            print(f"SAVE ITEM MARKUP ERROR: {e}")
    except Exception as e:
        print(f"SAVE ITEM MARKUP ERROR: {type(e).__name__}: {e}")

    try:
        await query.answer("♥ Saved" if now_saved else "Removed from Saved Items")
    except Exception as e:
        print(f"SAVE ITEM ANSWER ERROR: {e}")


def unsave_product_only(telegram_id, product_id):
    """Remove a saved_items row for this user+product. Never deletes from products."""
    tid = int(telegram_id)
    pid_str, _native, _t = _resolve_canonical_product_id(product_id)
    print(f"SAVED BACK UNSAVE: telegram_id={tid} product_id={pid_str!r}")
    try:
        supabase.table(SAVED_TABLE).delete().eq("telegram_id", tid).eq(
            "product_id", pid_str
        ).execute()
    except Exception as e:
        # Retry with native type if needed
        print(f"SAVED BACK UNSAVE ERROR: {type(e).__name__}: {safe_error_text(e)}")
        try:
            supabase.table(SAVED_TABLE).delete().eq("telegram_id", tid).eq(
                "product_id", str(product_id)
            ).execute()
        except Exception as e2:
            print(f"SAVED BACK UNSAVE RETRY ERROR: {type(e2).__name__}: {safe_error_text(e2)}")
            raise
    print(f"SAVED BACK UNSAVE SUCCESS: telegram_id={tid} product_id={pid_str!r}")


async def handle_saved_back(query, context, product_id, header_id=None):
    """Product opened from Saved Items → Back (navigation only):
    1) delete this product Telegram message
    2) delete the ❤️ SAVED ITEMS header message (by stored message_id)
    3) send Main Menu
    Does NOT unsave — only ♥ Saved / ♡ Save Item changes saved_items.
    """
    user_id = query.from_user.id
    is_admin = str(user_id) == str(ADMIN_ID)
    try:
        await query.answer()
    except Exception:
        pass

    msg = query.message
    chat_id = msg.chat_id if msg is not None else None
    bot = context.bot

    # 1) Remove the product card that was just tapped
    await delete_message_safe(msg)

    # 2) Remove the text-only "❤️ SAVED ITEMS" header (exact message_id from callback)
    if chat_id is not None and header_id is not None:
        try:
            await delete_by_id_safe(bot, chat_id, int(header_id))
        except Exception as e:
            print(f"SAVED BACK HEADER DELETE ERROR: {e}")

    # 3) Fresh Main Menu (cannot edit a deleted message)
    if chat_id is None:
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=MAIN_MENU_TEXT,
            reply_markup=main_menu(is_admin),
        )
    except Exception as e:
        print(f"SAVED BACK MAIN MENU ERROR: {e}")


async def handle_saved_callback(query, context, data):
    """Entry for saved_home / saved_page_* / saved_toggle_* / saved_back_*."""
    if data == "saved_home":
        await show_saved_home(query, context, 0)
        return
    if data.startswith("saved_page_"):
        page_text = data[len("saved_page_"):]
        await show_saved_home(
            query, context, int(page_text) if page_text.isdigit() else 0
        )
        return
    if data.startswith("saved_toggle_"):
        product_id = data[len("saved_toggle_"):]
        if product_id:
            await handle_saved_toggle(query, context, product_id)
        return
    if data.startswith("saved_back_"):
        rest = data[len("saved_back_"):]
        product_id, header_id = rest, None
        if "_" in rest:
            pid_part, _, maybe_hdr = rest.rpartition("_")
            if maybe_hdr.isdigit() and pid_part:
                product_id, header_id = pid_part, int(maybe_hdr)
        await handle_saved_back(query, context, product_id, header_id)
        return


# ---------------------------------------------------------------------------
# PRODUCT SEARCH  (customer + admin)
# Callbacks: search_c (ConversationHandler entry), search_c_page_<n>,
#            mp_search (ConversationHandler entry), search_a_page_<n>
# Search runs in Supabase with ilike on name / subcategory / platform and
# category name (via category_id list). No new tables.
# Customer: only active products in active categories; hidden subcategories
# are dropped after fetch. Admin: all products, status shown, opens mp_view_.
# ---------------------------------------------------------------------------

SEARCH_PAGE_SIZE = 5
SEARCH_ADMIN_PAGE_SIZE = 5
SEARCH_MAX_TOKENS = 5
SEARCH_QUERY_MAX_LEN = 80


def _search_tokens(raw):
    """Split and sanitize user input for safe ilike filters (no %, _, quotes, commas)."""
    text = (raw or "").strip()
    if not text:
        return []
    text = text[:SEARCH_QUERY_MAX_LEN]
    cleaned = []
    for part in text.split():
        token = "".join(ch for ch in part if ch not in "%_,\"'()\\")
        token = token.strip()
        if token:
            cleaned.append(token)
    # de-dupe while preserving order
    seen = set()
    tokens = []
    for t in cleaned:
        key = t.lower()
        if key not in seen:
            seen.add(key)
            tokens.append(t)
        if len(tokens) >= SEARCH_MAX_TOKENS:
            break
    return tokens


def _category_ids_matching_tokens(tokens):
    """Category ids whose name matches any token (case-insensitive)."""
    if not tokens:
        return []
    ids = set()
    for token in tokens:
        pattern = f"%{token}%"
        try:
            rows = (
                supabase.table("categories").select("id")
                .ilike("name", pattern).execute().data or []
            )
            for r in rows:
                if r.get("id") is not None:
                    ids.add(r["id"])
        except Exception as e:
            print(f"SEARCH CATEGORY MATCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
    return list(ids)


def _active_category_ids():
    rows = (
        supabase.table("categories").select("id")
        .eq("is_active", True).execute().data or []
    )
    return [r["id"] for r in rows if r.get("id") is not None]


def _hidden_subcategory_pairs():
    """Set of (category_id, subcategory_name) that are hidden."""
    if not subcategory_table_ready():
        return set()
    try:
        query = (
            supabase.table("subcategories").select("category_id,name")
            .eq("is_active", False)
        )
        if _hier_flags()["parent"]:
            query = query.is_("parent_id", "null")  # only real subcategories, not their children
        rows = query.execute().data or []
        return {(r.get("category_id"), r.get("name")) for r in rows}
    except Exception as e:
        print(f"SEARCH HIDDEN SUB ERROR: {type(e).__name__}: {safe_error_text(e)}")
        return set()


def _build_token_or_filter(token, category_ids):
    """PostgREST or_ clause: token matches name OR platform OR subcategory OR category_id."""
    pattern = f"%{token}%"
    parts = [
        f"name.ilike.{pattern}",
        f"platform.ilike.{pattern}",
        f"subcategory.ilike.{pattern}",
    ]
    for cid in category_ids:
        parts.append(f"category_id.eq.{cid}")
    return ",".join(parts)


def search_products_db(raw_query, *, customer_only, page, page_size):
    """
    Return (rows, has_next, page).
    Database-side filtering with ilike. Multi-word queries require every token
    to match at least one of: name, platform, subcategory, or category name.
    customer_only: is_active products, active categories only; hidden
    subcategories removed from the page.
    """
    tokens = _search_tokens(raw_query)
    page = max(0, page)
    if not tokens:
        return [], False, page

    cat_ids_for_tokens = _category_ids_matching_tokens(tokens)
    # Per-token category ids (same list is fine — token still must match some field)
    query = supabase.table("products").select(PRODUCT_COLUMNS).order("id")

    if customer_only:
        query = query.eq("is_active", True)
        active_cats = _active_category_ids()
        if not active_cats:
            return [], False, page
        query = query.in_("category_id", active_cats)

    for token in tokens:
        # Categories matching this specific token
        token_cats = []
        pattern = f"%{token}%"
        try:
            rows = (
                supabase.table("categories").select("id")
                .ilike("name", pattern).execute().data or []
            )
            token_cats = [r["id"] for r in rows if r.get("id") is not None]
        except Exception as e:
            print(f"SEARCH TOKEN CAT ERROR: {type(e).__name__}: {safe_error_text(e)}")
        query = query.or_(_build_token_or_filter(token, token_cats))

    offset = page * page_size
    # Fetch one extra to detect a next page
    result = query.range(offset, offset + page_size).execute()
    rows = result.data or []
    has_next = len(rows) > page_size
    rows = rows[:page_size]

    if customer_only and rows:
        hidden = _hidden_subcategory_pairs()
        if hidden:
            rows = [
                r for r in rows
                if (r.get("category_id"), r.get("subcategory")) not in hidden
            ]

    return rows, has_next, page


async def show_customer_search_results(query, context, page):
    raw = (context.user_data.get("cs_query") or "").strip()
    back = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main")]])
    if not raw:
        await safe_edit(query, "🔍 SEARCH RESULTS\n\nNo search query. Try again.", back)
        return
    try:
        rows, has_next, page = await asyncio.to_thread(
            search_products_db, raw, customer_only=True, page=page, page_size=SEARCH_PAGE_SIZE
        )
    except Exception as e:
        print(f"CUSTOMER SEARCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(
            query,
            "🔍 SEARCH RESULTS\n\n❌ Could not search products right now.\n\nPlease try again.",
            back,
        )
        return

    if not rows and page > 0:
        await show_customer_search_results(query, context, page - 1)
        return

    if not rows:
        display_q = raw if len(raw) <= 60 else raw[:57] + "…"
        await safe_edit(
            query,
            f"🔍 SEARCH RESULTS\n\nNo products found for:\n\"{display_q}\"\n\nTry another search.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="search_c")],
                [InlineKeyboardButton("🔙 Back", callback_data="main")],
            ]),
        )
        return

    header_id = await send_header(
        query,
        f"🔍 SEARCH RESULTS\n\nFound products for \"{raw if len(raw) <= 40 else raw[:37] + '…'}\" (page {page + 1}).",
    )

    search_user_id = query.from_user.id
    search_back = [InlineKeyboardButton("🔙 Back", callback_data="search_c_back")]
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"search_c_page_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"search_c_page_{page + 1}"))
    nav_row = []
    if nav:
        nav_row.append(nav)
    nav_row.append([InlineKeyboardButton("🔍 Search Again", callback_data="search_c")])
    footer = (f"📄 Page {page + 1}", InlineKeyboardMarkup(nav_row))
    # Same compact product cards as the normal Product List ("View Details ›" -> pd_<id>)
    product_msg_ids = [
        m for m in await send_product_cards(query, rows, search_back, header_id=header_id, footer=footer)
        if m is not None
    ]

    # Product Tracking views (same path as category browsing)
    await record_tracking_events([
        tracking_row(p["id"], EVENT_VIEW, query.from_user.id, f"{query.id}:{p['id']}:{EVENT_VIEW}")
        for p in rows if p.get("id") is not None
    ])

    # Pagination footer sits under the cards (tracked in the card session so it stays below on restore)
    chat_id0 = query.message.chat_id if query.message else None
    footer_session = _find_list_session(chat_id0, product_msg_ids[-1]) if product_msg_ids else None
    if footer_session is None:
        footer_session = {"chat_id": chat_id0, "footer": footer, "footer_id": None}
    else:
        footer_session["footer"] = footer
    await send_list_session_footer(query, footer_session)
    footer_id = footer_session.get("footer_id")

    # Exact Telegram message ids for this search-result screen (used by search_c_back)
    chat_id = query.message.chat_id if query.message else None
    context.user_data["cs_result_header_id"] = header_id
    context.user_data["cs_result_footer_id"] = footer_id
    context.user_data["cs_result_product_ids"] = product_msg_ids
    context.user_data["cs_result_chat_id"] = chat_id


async def show_admin_search_results(query, context, page):
    raw = (context.user_data.get("as_query") or "").strip()
    back = InlineKeyboardMarkup(admin_nav_rows("admin_products"))
    if not raw:
        await safe_edit(query, "🔍 PRODUCT SEARCH\n\nNo search query. Try again.", back)
        return
    try:
        rows, has_next, page = await asyncio.to_thread(
            search_products_db, raw, customer_only=False, page=page, page_size=SEARCH_ADMIN_PAGE_SIZE
        )
    except Exception as e:
        print(f"ADMIN SEARCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await safe_edit(
            query,
            "🔍 PRODUCT SEARCH\n\n❌ Could not search products.\n\nPlease try again.",
            back,
        )
        return

    if not rows and page > 0:
        await show_admin_search_results(query, context, page - 1)
        return

    if not rows:
        display_q = raw if len(raw) <= 60 else raw[:57] + "…"
        await safe_edit(
            query,
            f"🔍 PRODUCT SEARCH\n\nNo products found for:\n\"{display_q}\"\n\nTry another search.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="mp_search")],
                *admin_nav_rows("admin_products"),
            ]),
        )
        return

    names = get_category_names()
    lines = [
        f"🔍 PRODUCT SEARCH (Page {page + 1})\n\n"
        f"Query: \"{raw if len(raw) <= 40 else raw[:37] + '…'}\"\n"
        "Choose a product to manage.\n"
    ]
    keyboard = []
    for product in rows:
        lines.append(product_text(product, names) + "\n")
        icon = "🟢" if product.get("is_active") else "🔴"
        label = f"{str(product.get('name', 'Product'))[:28]} {icon}"
        callback = f"mp_view_{product['id']}"
        if fits_callback(callback):
            keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"search_a_page_{page - 1}"))
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"search_a_page_{page + 1}"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔍 Search Again", callback_data="mp_search")])
    keyboard.extend(admin_nav_rows("admin_products"))
    await safe_edit(query, "\n".join(lines).rstrip(), InlineKeyboardMarkup(keyboard))


async def _drop_previous_search_page(query, context):
    """Next/Prev: delete the previous page's header and cards so pages never stack up. The footer that
    was tapped is NOT deleted: it is edited into the new page's header."""
    msg = query.message
    if msg is None:
        return
    bot, chat_id = context.bot, msg.chat_id
    to_delete = set()
    for key, sess in list(_LIST_CARD_MESSAGES.items()):
        if sess.get("footer_id") == msg.message_id:
            sess["footer_id"] = None
            _LIST_CARD_MESSAGES.pop(key, None)
            to_delete.update(m for m in sess["ids"] if m is not None)
            for extra in (sess.get("detail_id"), sess.get("header_id")):
                if extra is not None:
                    to_delete.add(extra)
    if not to_delete and context.user_data.get("cs_result_footer_id") == msg.message_id:
        to_delete.update(int(m) for m in (context.user_data.get("cs_result_product_ids") or []) if m is not None)
        if context.user_data.get("cs_result_header_id") is not None:
            to_delete.add(int(context.user_data["cs_result_header_id"]))
    to_delete.discard(msg.message_id)
    for mid in to_delete:
        await delete_by_id_safe(bot, chat_id, mid)


async def handle_search_callback(query, context, data):
    if data.startswith("search_c_page_"):
        await _drop_previous_search_page(query, context)
        page_text = data[len("search_c_page_"):]
        await show_customer_search_results(
            query, context, int(page_text) if page_text.isdigit() else 0
        )
    elif data.startswith("search_a_page_"):
        if str(query.from_user.id) != str(ADMIN_ID):
            await deny_non_admin(query)
            return
        page_text = data[len("search_a_page_"):]
        await show_admin_search_results(
            query, context, int(page_text) if page_text.isdigit() else 0
        )


# ----- Search text input (ConversationHandler) -----

CUSTOMER_SEARCH_PROMPT = (
    "🔍 Search Products\n\n"
    "Just send me the name of any <b>PRODUCT</b> you're looking for! 📸\n"
    "I'll help you find it quickly."
)


def _store_search_prompt_message(context, message):
    """Remember the Search Products instruction message so it can be deleted on query."""
    if message is None:
        context.user_data.pop("cs_prompt_msg_id", None)
        context.user_data.pop("cs_prompt_chat_id", None)
        return
    context.user_data["cs_prompt_msg_id"] = message.message_id
    context.user_data["cs_prompt_chat_id"] = message.chat_id


async def _delete_search_prompt_message(context, bot):
    """Delete the active Search Products instruction message, if any."""
    prompt_id = context.user_data.pop("cs_prompt_msg_id", None)
    prompt_chat = context.user_data.pop("cs_prompt_chat_id", None)
    if prompt_id is None or prompt_chat is None:
        return
    try:
        await delete_by_id_safe(bot, prompt_chat, int(prompt_id))
    except Exception as e:
        print(f"SEARCH PROMPT DELETE ERROR: {e}")


@single_flight
async def customer_search_start(update, context):
    query = update.callback_query
    await query.answer()
    await register_or_touch_user(query.from_user)
    context.user_data.pop("cs_query", None)
    await safe_edit(
        query,
        CUSTOMER_SEARCH_PROMPT,
        InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main")]]),
        parse_mode="HTML",
    )
    # The edited message is the instruction prompt — store its id for later deletion
    _store_search_prompt_message(context, query.message)
    return SEARCH_CUSTOMER


async def receive_customer_search(update, context):
    await register_or_touch_user(update.effective_user)
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Please send a search term as text.")
        return SEARCH_CUSTOMER
    context.user_data["cs_query"] = text[:SEARCH_QUERY_MAX_LEN]
    chat_id = update.effective_chat.id
    bot = context.bot

    # Delete the "🔍 Search Products" instruction message before results
    await _delete_search_prompt_message(context, bot)

    # Keep the chat clean: delete the customer's search message when Telegram allows it.
    # Failure is silent — search continues either way.
    await delete_message_safe(update.message)

    raw = context.user_data["cs_query"]
    try:
        rows, has_next, page = await asyncio.to_thread(
            search_products_db, raw, customer_only=True, page=0, page_size=SEARCH_PAGE_SIZE
        )
    except Exception as e:
        print(f"CUSTOMER SEARCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await bot.send_message(
            chat_id=chat_id,
            text="🔍 SEARCH RESULTS\n\n❌ Could not search products right now.\n\nPlease try again.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="search_c")],
                [InlineKeyboardButton("🔙 Back", callback_data="main")],
            ]),
        )
        return ConversationHandler.END

    if not rows:
        display_q = raw if len(raw) <= 60 else raw[:57] + "…"
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"🔍 SEARCH RESULTS\n\nNo products found for:\n\"{display_q}\"\n\nTry another search."
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="search_c")],
                [InlineKeyboardButton("🔙 Back", callback_data="main")],
            ]),
        )
        return ConversationHandler.END

    header_msg = await bot.send_message(
        chat_id=chat_id,
        text=(
            f"🔍 SEARCH RESULTS\n\nFound products for "
            f"\"{raw if len(raw) <= 40 else raw[:37] + '…'}\" (page 1)."
        ),
    )
    header_id = header_msg.message_id if header_msg is not None else None
    search_back = [InlineKeyboardButton("🔙 Back", callback_data="search_c_back")]
    nav_row = []
    if has_next:
        nav_row.append([InlineKeyboardButton("Next ➡️", callback_data="search_c_page_1")])
    nav_row.append([InlineKeyboardButton("🔍 Search Again", callback_data="search_c")])
    footer = ("📄 Page 1", InlineKeyboardMarkup(nav_row))
    shim = _ChatQueryShim(bot, chat_id)
    # Same compact product cards as the normal Product List ("View Details ›" -> pd_<id>)
    product_msg_ids = [
        m for m in await send_product_cards(shim, rows, search_back, header_id=header_id, footer=footer)
        if m is not None
    ]

    await record_tracking_events([
        tracking_row(
            p["id"], EVENT_VIEW, update.effective_user.id,
            f"search:{update.effective_user.id}:{p['id']}:{EVENT_VIEW}:{int(time.time())}",
        )
        for p in rows if p.get("id") is not None
    ])

    footer_session = _find_list_session(chat_id, product_msg_ids[-1]) if product_msg_ids else None
    if footer_session is None:
        footer_session = {"chat_id": chat_id, "footer": footer, "footer_id": None}
    await send_list_session_footer(shim, footer_session)
    footer_id = footer_session.get("footer_id")

    context.user_data["cs_result_header_id"] = header_id
    context.user_data["cs_result_footer_id"] = footer_id
    context.user_data["cs_result_product_ids"] = product_msg_ids
    context.user_data["cs_result_chat_id"] = chat_id
    return ConversationHandler.END


async def handle_search_c_back(query, context, data=None):
    """Search product Back: delete ALL messages of this search-result screen, then Main Menu.
    Uses exact message_ids stored when results were sent. Failed deletes are ignored.
    """
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    try:
        await query.answer()
    except Exception:
        pass

    msg = query.message
    chat_id = (
        msg.chat_id
        if msg is not None
        else context.user_data.get("cs_result_chat_id")
    )
    bot = context.bot
    header_id = context.user_data.get("cs_result_header_id")
    footer_id = context.user_data.get("cs_result_footer_id")
    product_ids = list(context.user_data.get("cs_result_product_ids") or [])

    # Collect every message id belonging to this search result block
    to_delete = set()
    if msg is not None:
        to_delete.add(msg.message_id)
    for mid in product_ids:
        if mid is not None:
            to_delete.add(int(mid))
    if header_id is not None:
        to_delete.add(int(header_id))
    if footer_id is not None:
        to_delete.add(int(footer_id))

    # Card-list registry first: cards re-sent after Product Details have new ids, so the stored ones are stale
    if msg is not None:
        try:
            await purge_list_cards(bot, query)
        except Exception as e:
            print(f"SEARCH LIST PURGE ERROR: {type(e).__name__}: {e}")

    # Delete current product first, then the rest (ignore already-gone messages)
    await delete_message_safe(msg)
    if chat_id is not None:
        for mid in to_delete:
            if msg is not None and mid == msg.message_id:
                continue
            await delete_by_id_safe(bot, chat_id, mid)

    context.user_data.pop("cs_result_header_id", None)
    context.user_data.pop("cs_result_footer_id", None)
    context.user_data.pop("cs_result_product_ids", None)
    context.user_data.pop("cs_result_chat_id", None)

    if chat_id is None:
        return
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=MAIN_MENU_TEXT,
            reply_markup=main_menu(is_admin),
        )
    except Exception as e:
        print(f"SEARCH C BACK MAIN MENU ERROR: {e}")


async def cancel_customer_search(update, context):
    context.user_data.pop("cs_query", None)
    context.user_data.pop("cs_prompt_msg_id", None)
    context.user_data.pop("cs_prompt_chat_id", None)
    context.user_data.pop("cs_result_header_id", None)
    context.user_data.pop("cs_result_footer_id", None)
    context.user_data.pop("cs_result_product_ids", None)
    context.user_data.pop("cs_result_chat_id", None)
    is_admin = str(update.effective_user.id) == str(ADMIN_ID)
    await update.message.reply_text(
        "❌ Search cancelled.",
        reply_markup=main_menu(is_admin),
    )
    return ConversationHandler.END


@single_flight
async def customer_search_escape(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("cs_query", None)
    data = query.data
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    await register_or_touch_user(query.from_user)
    if data == "main":
        context.user_data.pop("cs_prompt_msg_id", None)
        context.user_data.pop("cs_prompt_chat_id", None)
        await show_main_menu(query, is_admin)
    elif data == "admin" and is_admin:
        context.user_data.pop("cs_prompt_msg_id", None)
        context.user_data.pop("cs_prompt_chat_id", None)
        await show_admin_panel(query)
    elif data == "search_c":
        await safe_edit(
            query,
            CUSTOMER_SEARCH_PROMPT,
            InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back", callback_data="main")]]),
            parse_mode="HTML",
        )
        _store_search_prompt_message(context, query.message)
        return SEARCH_CUSTOMER
    else:
        # Hand off to normal button routing
        if data == "admin_products" or data.startswith("mp_"):
            if is_admin:
                await handle_manage_callback(query, context, data)
        elif data == "admin_categories" or data.startswith("mc_"):
            if is_admin:
                await handle_category_admin_callback(query, context, data)
        elif data == "admin_dashboard" or data.startswith("dashboard_"):
            if is_admin:
                await handle_dashboard_callback(query, context, data)
        elif data == "admin_tracking" or data.startswith("pt_"):
            if is_admin:
                await handle_tracking_callback(query, context, data)
        elif data == "admin_users" or data.startswith("ub_"):
            if is_admin:
                await handle_users_broadcast_callback(query, context, data)
        elif await handle_product_category(query):
            pass
        elif data in CATEGORY_ORDER:
            await show_customer_category(query, context, data)
        elif data.startswith("saved_toggle_"):
            product_id = data[len("saved_toggle_"):]
            if product_id:
                await handle_saved_toggle(query, context, product_id)
        elif data == "saved_home" or data.startswith("saved_page_"):
            await handle_saved_callback(query, context, data)
        else:
            await show_main_menu(query, is_admin)
    return ConversationHandler.END


async def admin_search_start(update, context):
    query = update.callback_query
    await query.answer()
    if str(query.from_user.id) != str(ADMIN_ID):
        await deny_non_admin(query)
        return ConversationHandler.END
    context.user_data.pop("as_query", None)
    await safe_edit(
        query,
        "🔍 PRODUCT SEARCH\n\n"
        "Send the product name, category, subcategory or platform to search.\n\n"
        "Hidden products are included.\n\n"
        "Send /cancel to stop.",
        InlineKeyboardMarkup(admin_nav_rows("admin_products")),
    )
    return SEARCH_ADMIN


async def receive_admin_search(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if not text:
        await update.message.reply_text("⚠️ Please send a search term as text.")
        return SEARCH_ADMIN
    context.user_data["as_query"] = text[:SEARCH_QUERY_MAX_LEN]
    raw = context.user_data["as_query"]
    try:
        rows, has_next, page = await asyncio.to_thread(
            search_products_db, raw, customer_only=False, page=0, page_size=SEARCH_ADMIN_PAGE_SIZE
        )
    except Exception as e:
        print(f"ADMIN SEARCH ERROR: {type(e).__name__}: {safe_error_text(e)}")
        await update.message.reply_text(
            "🔍 PRODUCT SEARCH\n\n❌ Could not search products.\n\nPlease try again.",
            reply_markup=InlineKeyboardMarkup(admin_nav_rows("admin_products")),
        )
        return ConversationHandler.END

    if not rows:
        display_q = raw if len(raw) <= 60 else raw[:57] + "…"
        await update.message.reply_text(
            f"🔍 PRODUCT SEARCH\n\nNo products found for:\n\"{display_q}\"\n\nTry another search.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔍 Search Again", callback_data="mp_search")],
                *admin_nav_rows("admin_products"),
            ]),
        )
        return ConversationHandler.END

    names = get_category_names()
    lines = [
        f"🔍 PRODUCT SEARCH (Page 1)\n\n"
        f"Query: \"{raw if len(raw) <= 40 else raw[:37] + '…'}\"\n"
        "Choose a product to manage.\n"
    ]
    keyboard = []
    for product in rows:
        lines.append(product_text(product, names) + "\n")
        icon = "🟢" if product.get("is_active") else "🔴"
        label = f"{str(product.get('name', 'Product'))[:28]} {icon}"
        callback = f"mp_view_{product['id']}"
        if fits_callback(callback):
            keyboard.append([InlineKeyboardButton(label, callback_data=callback)])
    nav = []
    if has_next:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data="search_a_page_1"))
    if nav:
        keyboard.append(nav)
    keyboard.append([InlineKeyboardButton("🔍 Search Again", callback_data="mp_search")])
    keyboard.extend(admin_nav_rows("admin_products"))
    await update.message.reply_text("\n".join(lines).rstrip(), reply_markup=InlineKeyboardMarkup(keyboard))
    return ConversationHandler.END


async def cancel_admin_search(update, context):
    if str(update.effective_user.id) != str(ADMIN_ID):
        return ConversationHandler.END
    context.user_data.pop("as_query", None)
    await update.message.reply_text(
        "❌ Search cancelled.",
        reply_markup=InlineKeyboardMarkup(admin_nav_rows("admin_products")),
    )
    return ConversationHandler.END


async def admin_search_escape(update, context):
    query = update.callback_query
    await query.answer()
    context.user_data.pop("as_query", None)
    data = query.data
    is_admin = str(query.from_user.id) == str(ADMIN_ID)
    if not is_admin:
        await deny_non_admin(query)
        return ConversationHandler.END
    if data == "main":
        await show_main_menu(query, True)
    elif data == "admin":
        await show_admin_panel(query)
    elif data == "admin_products" or data.startswith("mp_"):
        await handle_manage_callback(query, context, data)
    else:
        await dispatch_admin_callback(query, context, data)
    return ConversationHandler.END


application = Application.builder().token(BOT_TOKEN).build()

# Support Bot Application (optional until SUPPORT_BOT_TOKEN is set)
support_application = None
if SUPPORT_BOT_TOKEN:
    support_application = Application.builder().token(SUPPORT_BOT_TOKEN).build()
    support_application.add_handler(
        MessageHandler(
            filters.REPLY & filters.TEXT & ~filters.COMMAND,
            support_bot_admin_reply,
        )
    )
    print("Support Bot Application created (SUPPORT_BOT_TOKEN set)")
else:
    print("WARNING: SUPPORT_BOT_TOKEN not set — Contact Us notifications will fail until configured")

add_product_conversation = ConversationHandler(
    entry_points=[CallbackQueryHandler(add_product_start, pattern="^admin_add$")],
    states={
        PHOTO: [MessageHandler(filters.Document.ALL | filters.PHOTO, receive_photo)],
        NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_name)],
        PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_price)],
        CATEGORY: [
            CallbackQueryHandler(show_category_buttons, pattern="^addcat_"),
            CallbackQueryHandler(receive_subcategory, pattern="^addsub_"),
            CallbackQueryHandler(receive_sub_subcategory, pattern="^addss_"),
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
        EDIT_VALUE: [MessageHandler(
            filters.PHOTO | filters.Document.ALL | (filters.TEXT & ~filters.COMMAND),
            receive_edit_value,
        )]
    },
    fallbacks=[
        CommandHandler("cancel", cancel_edit_product),
        CallbackQueryHandler(edit_escape, pattern=r"^(main|admin|admin_products|admin_categories|admin_dashboard|admin_tracking|admin_users|dashboard_.*|pt_.*|ub_.*|search_.*|mp_(?!ef_).*|mc_(?!edit_|sub_edit_|sub_add_|ss_add_).*)$")
    ],
    allow_reentry=True
)

broadcast_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(broadcast_start, pattern=r"^ub_bc_(create|edit)$"),
    ],
    states={
        BROADCAST_TEXT: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_broadcast_text),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_broadcast),
        CallbackQueryHandler(
            broadcast_escape,
            pattern=r"^(main|admin|admin_products|admin_categories|admin_dashboard|admin_tracking|admin_users|dashboard_.*|pt_.*|ub_.*|search_.*|mp_.*|mc_.*)$",
        ),
    ],
    allow_reentry=True,
)

customer_search_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(customer_search_start, pattern="^search_c$"),
    ],
    states={
        SEARCH_CUSTOMER: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_customer_search),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_customer_search),
        CallbackQueryHandler(
            customer_search_escape,
            pattern=r"^(main|admin|admin_products|admin_categories|admin_dashboard|admin_tracking|admin_users|search_c|men|women|seasonal|deals|other|saved_.*|dashboard_.*|pt_.*|ub_.*|mp_.*|mc_.*|sub_.*)$",
        ),
    ],
    allow_reentry=True,
)

admin_search_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(admin_search_start, pattern="^mp_search$"),
    ],
    states={
        SEARCH_ADMIN: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_admin_search),
        ],
    },
    fallbacks=[
        CommandHandler("cancel", cancel_admin_search),
        CallbackQueryHandler(
            admin_search_escape,
            pattern=r"^(main|admin|admin_products|admin_categories|admin_dashboard|admin_tracking|admin_users|dashboard_.*|pt_.*|ub_.*|search_.*|mp_(?!search$).*|mc_.*)$",
        ),
    ],
    allow_reentry=True,
)

@single_flight
async def saved_toggle_entry(update, context):
    """Dedicated entry so Save/Unsave is never swallowed by ConversationHandlers."""
    query = update.callback_query
    data = query.data or ""
    product_id = data[len("saved_toggle_"):] if data.startswith("saved_toggle_") else ""
    await register_or_touch_user(query.from_user)
    if product_id:
        await handle_saved_toggle(query, context, product_id)
    else:
        try:
            await query.answer()
        except Exception:
            pass


@single_flight
async def search_c_back_entry(update, context):
    """Dedicated entry so search result Back is never swallowed by ConversationHandlers."""
    query = update.callback_query
    await register_or_touch_user(query.from_user)
    await handle_search_c_back(query, context, query.data or "")


support_conversation = ConversationHandler(
    entry_points=[
        CallbackQueryHandler(contact_us_start, pattern="^contact_us$"),
    ],
    states={
        SUPPORT_MESSAGE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, receive_support_message),
        ],
    },
    fallbacks=[
        CallbackQueryHandler(cancel_support, pattern="^contact_cancel$"),
        CallbackQueryHandler(
            support_escape,
            pattern=r"^(main|contact_cancel|contact_us|search_c|men|women|seasonal|deals|other|saved_.*|admin|admin_.*)$",
        ),
        CommandHandler("cancel", cancel_support),
    ],
    allow_reentry=True,
)

# Order matters: conversations before generic button_handler.
# saved_toggle / search_c_back registered first so they win over ConversationHandler fallbacks.
application.add_handler(CallbackQueryHandler(saved_toggle_entry, pattern=r"^saved_toggle_"))
application.add_handler(CallbackQueryHandler(search_c_back_entry, pattern=r"^search_c_back"))
application.add_handler(add_product_conversation)
application.add_handler(edit_product_conversation)
application.add_handler(broadcast_conversation)
application.add_handler(customer_search_conversation)
application.add_handler(admin_search_conversation)
application.add_handler(support_conversation)
application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("help", help_command))
application.add_handler(CallbackQueryHandler(button_handler))

async def health(request):
    return web.Response(text="Outfit India Bot is running.")

async def telegram_webhook(request):
    """Main Outfit India bot webhook."""
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="OK")

async def support_telegram_webhook(request):
    """Support Bot (@OutfitIndiaHelp_Bot) webhook — admin replies only."""
    if support_application is None:
        return web.Response(text="Support bot not configured", status=503)
    data = await request.json()
    update = Update.de_json(data, support_application.bot)
    await support_application.process_update(update)
    return web.Response(text="OK")

async def startup(app):
    await application.initialize()
    await application.start()
    external_url = os.environ.get("RENDER_EXTERNAL_URL")
    if not external_url:
        raise RuntimeError("RENDER_EXTERNAL_URL is missing")
    webhook_url = f"{external_url.rstrip('/')}/telegram"
    await application.bot.set_webhook(url=webhook_url)
    print(f"Main bot webhook set: {webhook_url}")

    if support_application is not None:
        await support_application.initialize()
        await support_application.start()
        support_webhook_url = f"{external_url.rstrip('/')}/support-telegram"
        await support_application.bot.set_webhook(url=support_webhook_url)
        print(f"Support bot webhook set: {support_webhook_url}")
    else:
        print("Support bot not started (SUPPORT_BOT_TOKEN missing)")

async def shutdown(app):
    if support_application is not None:
        try:
            await support_application.stop()
            await support_application.shutdown()
        except Exception as e:
            print(f"SUPPORT BOT SHUTDOWN ERROR: {e}")
    await application.stop()
    await application.shutdown()

web_app = web.Application()
web_app.router.add_get("/", health)
web_app.router.add_post("/telegram", telegram_webhook)
web_app.router.add_post("/support-telegram", support_telegram_webhook)
web_app.router.add_get("/go/{product_id}", track_redirect)
web_app.on_startup.append(startup)
web_app.on_cleanup.append(shutdown)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    web.run_app(web_app, host="0.0.0.0", port=port)
