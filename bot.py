import os
import requests
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from pymongo import MongoClient
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask
from threading import Thread

# --- RENDER KEEP-ALIVE SERVER ---
app = Flask('')
@app.route('/')
def home(): return "Bot is running and healthy!"

def run_web():
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    Thread(target=run_web).start()

# --- CONFIGURATION (Environment Variables) ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
MONGO_URI = os.getenv('MONGO_URI')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
UPI_ID = os.getenv('UPI_ID')
CONTACT_USERNAME = os.getenv('CONTACT_USERNAME')
BHARATPE_TOKEN = os.getenv('BHARATPE_TOKEN')
WELCOME_IMAGE_URL = os.getenv('WELCOME_IMAGE_URL')  # optional — shown on /start; falls back to text-only if not set

bot = telebot.TeleBot(BOT_TOKEN)
client = MongoClient(MONGO_URI)
db = client['sub_management']
channels_col = db['channels']
users_col = db['users']
used_utrs_col = db['used_utrs']  # permanent record of spent UTRs, never touched by kick_expired_users
settings_col = db['settings']  # small key/value store for things like the welcome image

# In-memory tracker: user_id -> {"ch_id":.., "mins":.., "price":..}
pending_payments = {}

# Tracks the last "navigation" message per chat: {"message_id": ..., "has_photo": bool}
# so a button tap can edit it in place, while a typed command still deletes and sends fresh.
# Payment confirmations, admin notices, and grant/removal messages are sent directly with
# bot.send_message/send_photo (NOT send_page) so they always persist.
last_page_msg = {}

def send_page(chat_id, text, photo=None, reply_markup=None, parse_mode=None, force_new=False):
    info = last_page_msg.get(chat_id)

    if not force_new and info:
        try:
            if photo and info.get('has_photo'):
                # Same type (photo -> photo): edit the image, caption, and buttons in place
                media = InputMediaPhoto(media=photo, caption=text, parse_mode=parse_mode)
                bot.edit_message_media(media=media, chat_id=chat_id, message_id=info['message_id'], reply_markup=reply_markup)
                last_page_msg[chat_id] = {"message_id": info['message_id'], "has_photo": True}
                return
            elif not photo and not info.get('has_photo'):
                # Same type (text -> text): edit the text and buttons in place
                bot.edit_message_text(text, chat_id=chat_id, message_id=info['message_id'], reply_markup=reply_markup, parse_mode=parse_mode)
                last_page_msg[chat_id] = {"message_id": info['message_id'], "has_photo": False}
                return
            # else: type mismatch (text<->photo) — Telegram can't convert message type via edit,
            # fall through to delete + send new below
        except Exception:
            pass  # edit failed (e.g. "message not modified", too old to edit, etc.) — fall back to delete+resend

    if info:
        try:
            bot.delete_message(chat_id, info['message_id'])
        except Exception:
            pass  # already gone / too old to delete — fine, just move on

    if photo:
        sent = bot.send_photo(chat_id, photo, caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
    else:
        sent = bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
    last_page_msg[chat_id] = {"message_id": sent.message_id, "has_photo": bool(photo)}
    return sent

# --- ADMIN LOGIC ---

import html as _html

def esc(s):
    return _html.escape(str(s))

def disp_name(ch_data):
    """Returns the admin-set display name if one is configured, otherwise the real Telegram channel title."""
    return ch_data.get('display_name') or ch_data['name']

def plan_info(p_val):
    """A plan value can be a plain price string (legacy / no custom label) or a dict {'price':..,'label':..}.
    Returns (price, label_or_None) either way."""
    if isinstance(p_val, dict):
        return p_val.get('price'), p_val.get('label')
    return p_val, None

def plan_button_text(p_time, p_val):
    price, custom_label = plan_info(p_val)
    if str(p_time).strip().lower() == "lifetime":
        duration_str = "Lifetime"
    elif int(p_time) < 60:
        duration_str = f"{p_time} Min"
    else:
        days = int(p_time) // 1440
        duration_str = f"{days} Day" if days == 1 else f"{days} Days"

    if custom_label:
        return f"{custom_label} - ₹{price} ({duration_str})"
    return f"💳 {duration_str} - ₹{price}"

def show_plans(chat_id, ch_id, user_id=None, skip_active_check=False, force_new=False):
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        return

    if user_id and not skip_active_check:
        now = datetime.now().timestamp()
        existing = users_col.find_one({"user_id": user_id, "channel_id": ch_id})
        if existing and (existing.get("lifetime") or existing.get("expiry", 0) > now):
            if existing.get("lifetime"):
                status_line = "Lifetime ♾️ — you already have unlimited access."
            else:
                remaining_sec = existing["expiry"] - now
                days = int(remaining_sec // 86400)
                hours = int((remaining_sec % 86400) // 3600)
                status_line = f"{days}d {hours}h remaining"

            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("✅ Yes, Extend", callback_data=f"extendyes_{ch_id}"))
            markup.add(InlineKeyboardButton("❌ No", callback_data="extendno"))
            markup.add(InlineKeyboardButton("⬅️ Back", callback_data="backtolist"))
            send_page(chat_id,
                f"ℹ️ You already have an active plan for <b>{esc(disp_name(ch_data))}</b>.\n\nStatus: {status_line}\n\nDo you want to buy more time and extend it?",
                reply_markup=markup, parse_mode="HTML", force_new=force_new)
            return

    markup = InlineKeyboardMarkup()
    for p_time, p_val in ch_data['plans'].items():
        markup.add(InlineKeyboardButton(plan_button_text(p_time, p_val), callback_data=f"select_{ch_id}_{p_time}"))
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="backtolist"))

    if ch_data.get('description'):
        caption = (f"📋 <b>SELECTED CHANNEL DETAILS</b>\n\n"
                   f"<blockquote>{esc(ch_data['description'])}</blockquote>\n\n"
                   f"Please select a subscription plan below:")
    else:
        caption = f"Welcome!\n\nYou are joining: <b>{esc(disp_name(ch_data))}</b>.\n\nPlease select a subscription plan below:"

    send_page(chat_id, caption, photo=ch_data.get('image'), reply_markup=markup, parse_mode="HTML", force_new=force_new)

@bot.callback_query_handler(func=lambda call: call.data.startswith('extendyes_'))
def extendyes(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    show_plans(call.message.chat.id, ch_id, user_id=call.from_user.id, skip_active_check=True)

@bot.callback_query_handler(func=lambda call: call.data == "extendno")
def extendno(call):
    bot.answer_callback_query(call.id)
    show_channel_list(call.message.chat.id)

def show_channel_list(chat_id):
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({})
    count = 0
    for ch in cursor:
        markup.add(InlineKeyboardButton(f"📢 {disp_name(ch)}", callback_data=f"viewch_{ch['channel_id']}"))
        count += 1
    markup.add(InlineKeyboardButton("⬅️ Back", callback_data="backtostart"))

    if count == 0:
        send_page(chat_id, "No channels are available for subscription right now. Please check back later.", reply_markup=markup)
    else:
        list_image_setting = settings_col.find_one({"key": "channel_list_image"})
        list_image = list_image_setting["value"] if list_image_setting and list_image_setting.get("value") else None
        send_page(chat_id, "📢 *SELECT A CHANNEL*\n\nChoose one of our premium channels from below to view plans and pricing:",
                   photo=list_image, reply_markup=markup, parse_mode="Markdown")

def get_welcome_image():
    """Returns a Telegram file_id or URL to use for the /start image, or None if none is set.
    A photo set via the admin panel (stored as a file_id) takes priority over WELCOME_IMAGE_URL."""
    setting = settings_col.find_one({"key": "welcome_image"})
    if setting and setting.get("value"):
        return setting["value"]
    return WELCOME_IMAGE_URL

def show_welcome(chat_id, first_name, force_new=False):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("💎 BUY MEMBERSHIP", callback_data="buy_membership"))
    caption = (f"👋 Welcome, {first_name}!\n\n"
               f"I am your Premium Subscription Bot. 🤖\n"
               f"I can help you get instant access to our exclusive premium channels.\n\n"
               f"👇 Click on Buy Membership button below to browse our premium channel plans!")
    welcome_image = get_welcome_image()
    send_page(chat_id, caption, photo=welcome_image, reply_markup=markup, force_new=force_new)

@bot.message_handler(commands=['start'])
def start_handler(message):
    text = message.text.split()

    if len(text) > 1:
        try:
            ch_id = int(text[1])
            ch_data = channels_col.find_one({"channel_id": ch_id})
            if ch_data:
                show_plans(message.chat.id, ch_id, user_id=message.from_user.id, force_new=True)
                return
        except: pass

    show_welcome(message.chat.id, message.from_user.first_name, force_new=True)

@bot.callback_query_handler(func=lambda call: call.data == "buy_membership")
def buy_membership(call):
    bot.answer_callback_query(call.id)
    show_channel_list(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data == "backtostart")
def backtostart(call):
    bot.answer_callback_query(call.id)
    show_welcome(call.message.chat.id, call.from_user.first_name)

@bot.callback_query_handler(func=lambda call: call.data == "backtolist")
def backtolist(call):
    bot.answer_callback_query(call.id)
    show_channel_list(call.message.chat.id)

@bot.message_handler(commands=['myplan'])
def myplan_handler(message):
    user_id = message.from_user.id
    now = datetime.now().timestamp()
    subs = list(users_col.find({"user_id": user_id}))
    active_subs = [s for s in subs if s.get("lifetime") or s.get("expiry", 0) > now]

    if not active_subs:
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("💎 Buy Membership", callback_data="buy_membership"))
        send_page(message.chat.id, "You have no plan history. Click below button to purchase a plan.", reply_markup=markup, force_new=True)
        return

    lines = ["📋 <b>Your Active Plans</b>\n"]
    for s in active_subs:
        ch_data = channels_col.find_one({"channel_id": s['channel_id']})
        ch_name = esc(disp_name(ch_data)) if ch_data else str(s['channel_id'])
        if s.get("lifetime"):
            lines.append(f"• {ch_name} — Lifetime ♾️")
        else:
            remaining_sec = s['expiry'] - now
            days = int(remaining_sec // 86400)
            hours = int((remaining_sec % 86400) // 3600)
            mins = int((remaining_sec % 3600) // 60)
            if days > 0:
                remain_str = f"{days}d {hours}h remaining"
            elif hours > 0:
                remain_str = f"{hours}h {mins}m remaining"
            else:
                remain_str = f"{mins}m remaining"
            lines.append(f"• {ch_name} — {remain_str}")

    send_page(message.chat.id, "\n".join(lines), parse_mode="HTML", force_new=True)

@bot.message_handler(commands=['help'])
def help_handler(message):
    if message.from_user.id == ADMIN_ID:
        text = ("🤖 <b>Admin Commands:</b>\n\n"
                "/start – Start the bot (same view as users)\n"
                "/admin – Open Admin Panel (add channels, edit prices, stats, grant/remove access, images, and more)\n"
                "/myplan – Check your plans\n"
                "/help – Show this help")
    else:
        text = ("🤖 <b>User Commands:</b>\n\n"
                "/start – Start the bot\n"
                "/myplan – Check your plans\n"
                "/help – Show this help")
    send_page(message.chat.id, text, parse_mode="HTML", force_new=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith('viewch_'))
def view_channel(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    show_plans(call.message.chat.id, ch_id, user_id=call.from_user.id)

@bot.message_handler(commands=['channels'], func=lambda m: m.from_user.id == ADMIN_ID)
def list_channels(message):
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0
    for ch in cursor:
        markup.add(InlineKeyboardButton(f"Channel: {ch['name']}", callback_data=f"manage_{ch['channel_id']}"))
        count += 1
    
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    
    if count == 0:
        bot.send_message(ADMIN_ID, "No channels found. Click below to add one.", reply_markup=markup)
    else:
        bot.send_message(ADMIN_ID, "Your Managed Channels:", reply_markup=markup)

@bot.message_handler(commands=['add'], func=lambda m: m.from_user.id == ADMIN_ID)
def add_channel_start(message):
    msg = bot.send_message(ADMIN_ID, "Please ensure the bot is an Admin in your channel, then FORWARD any message from that channel here.")
    bot.register_next_step_handler(msg, get_plans)

@bot.callback_query_handler(func=lambda call: call.data == "add_new")
def cb_add_new(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(ADMIN_ID, "Please FORWARD any message from your channel here.")
    bot.register_next_step_handler(msg, get_plans)

def get_plans(message):
    if message.forward_from_chat:
        ch_id = message.forward_from_chat.id
        ch_name = message.forward_from_chat.title
        msg = bot.send_message(ADMIN_ID, 
            f"Channel Detected: *{ch_name}*\n\nEnter plans in format:\n`Min:Price` or `Min:Price:Label`\n\n"
            "Separate multiple plans with commas. Use `lifetime` instead of minutes for a lifetime plan. A custom label is optional — leave it out to use the default \"X Days - ₹Y\" style.\n\n"
            "Example:\n`1440:39, 10080:69:🥈 Silver Plan, lifetime:199:🥇 Gold Plan`", parse_mode="Markdown")
        bot.register_next_step_handler(msg, finalize_channel, ch_id, ch_name)
    else:
        bot.send_message(ADMIN_ID, "❌ Error: Message was not forwarded. Use /add to try again.")

def finalize_channel(message, ch_id, ch_name):
    try:
        raw_plans = message.text.split(',')
        plans_dict = {}
        for p in raw_plans:
            parts = p.strip().split(':', 2)
            t = parts[0].strip()
            t = t.lower() if t.lower() == "lifetime" else t
            pr = parts[1].strip()
            label = parts[2].strip() if len(parts) == 3 else None
            plans_dict[t] = {"price": pr, "label": label} if label else pr

        channels_col.update_one({"channel_id": ch_id}, {"$set": {"name": ch_name, "plans": plans_dict, "admin_id": ADMIN_ID}}, upsert=True)
        bot_username = bot.get_me().username
        bot.send_message(ADMIN_ID, f"✅ Setup Successful!\n\nInvite Link for users:\n`https://t.me/{bot_username}?start={ch_id}`", parse_mode="Markdown")
    except:
        bot.send_message(ADMIN_ID, "❌ Invalid format. Please use `Min:Price` or `Min:Price:Label`. Use /add to retry.")

# --- ADMIN PANEL ---

@bot.message_handler(commands=['admin'], func=lambda m: m.from_user.id == ADMIN_ID)
def admin_panel(message):
    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("➕ Add New Channel", callback_data="add_new"))
    markup.add(InlineKeyboardButton("📊 Stats", callback_data="adm_stats"))
    markup.add(InlineKeyboardButton("👥 Active Subscribers", callback_data="adm_active"))
    markup.add(InlineKeyboardButton("🎁 Manually Grant Access", callback_data="adm_grant"))
    markup.add(InlineKeyboardButton("🚫 Remove Membership", callback_data="adm_remove"))
    markup.add(InlineKeyboardButton("📢 Broadcast Message", callback_data="adm_broadcast"))
    markup.add(InlineKeyboardButton("📋 Manage Channels", callback_data="adm_channels"))
    markup.add(InlineKeyboardButton("🖼 Set Welcome Image", callback_data="adm_setimg"))
    markup.add(InlineKeyboardButton("🖼 Set Channel List Image", callback_data="adm_setlistimg"))
    bot.send_message(message.chat.id, "🛠 *Admin Panel*\n\nChoose an option:", reply_markup=markup, parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "adm_stats")
def adm_stats(call):
    bot.answer_callback_query(call.id)
    total_channels = channels_col.count_documents({"admin_id": ADMIN_ID})
    active_subs = users_col.count_documents({"$or": [{"expiry": {"$gt": datetime.now().timestamp()}}, {"lifetime": True}]})
    total_approvals = used_utrs_col.count_documents({})
    total_revenue = sum(a.get("amount", 0) for a in used_utrs_col.find({}))

    since = datetime.now() - timedelta(hours=24)
    today_approvals = list(used_utrs_col.find({"used_at": {"$gte": since}}))
    today_revenue = sum(a.get("amount", 0) for a in today_approvals)

    bot.send_message(call.message.chat.id,
        f"📊 *Bot Stats*\n\n"
        f"Channels: {total_channels}\n"
        f"Active subscribers: {active_subs}\n\n"
        f"*All-time*\nApprovals: {total_approvals}\nRevenue: ₹{total_revenue}\n\n"
        f"*Last 24 hours*\nApprovals: {len(today_approvals)}\nRevenue: ₹{today_revenue}",
        parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "adm_active")
def adm_active(call):
    bot.answer_callback_query(call.id)
    now = datetime.now().timestamp()
    active = list(users_col.find({"$or": [{"expiry": {"$gt": now}}, {"lifetime": True}]}).sort("expiry", 1))

    if not active:
        bot.send_message(call.message.chat.id, "No active subscribers right now.")
        return

    lines = [f"👥 *Active Subscribers* ({len(active)})\n"]
    for u in active[:25]:
        ch_data = channels_col.find_one({"channel_id": u['channel_id']})
        ch_name = ch_data['name'] if ch_data else str(u['channel_id'])
        if u.get("lifetime"):
            lines.append(f"• User {u['user_id']} — {ch_name} — Lifetime ♾️")
        else:
            remaining_min = int((u['expiry'] - now) / 60)
            lines.append(f"• User {u['user_id']} — {ch_name} — expires in {remaining_min} min")
    if len(active) > 25:
        lines.append(f"... and {len(active) - 25} more")

    bot.send_message(call.message.chat.id, "\n".join(lines), parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data == "adm_channels")
def adm_channels(call):
    bot.answer_callback_query(call.id)
    list_channels(call.message)

@bot.callback_query_handler(func=lambda call: call.data == "adm_broadcast")
def adm_broadcast(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id, "📢 Send the message you want to broadcast to all active subscribers.\n\nSend /cancel to abort.")
    bot.register_next_step_handler(msg, do_broadcast)

def do_broadcast(message):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Broadcast cancelled.")
        return

    recipients = users_col.distinct("user_id")
    sent, failed = 0, 0
    for uid in recipients:
        try:
            bot.send_message(uid, message.text)
            sent += 1
        except Exception:
            failed += 1
    bot.send_message(ADMIN_ID, f"📢 Broadcast complete.\n\nSent: {sent}\nFailed (blocked/inactive): {failed}")

@bot.callback_query_handler(func=lambda call: call.data == "adm_grant")
def adm_grant(call):
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0
    for ch in cursor:
        markup.add(InlineKeyboardButton(f"{ch['name']}", callback_data=f"grantch_{ch['channel_id']}"))
        count += 1
    if count == 0:
        bot.send_message(call.message.chat.id, "No channels found. Add one first with /add.")
        return
    bot.send_message(call.message.chat.id, "🎁 Which channel do you want to grant access to?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('grantch_'))
def grantch(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    msg = bot.send_message(call.message.chat.id,
        "Send the user's Telegram ID and duration in minutes, separated by a space.\n\nExample: `123456789 1440` (grants 1440 minutes / 1 day)\nOr for lifetime: `123456789 lifetime`",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_grant, ch_id)

def process_grant(message, ch_id):
    try:
        parts = message.text.strip().split()
        target_id = int(parts[0])
        mins = parts[1]  # keep as string — may be "lifetime" or a number
        if mins.lower() != "lifetime":
            int(mins)  # validate it's numeric if not lifetime
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Invalid format. Use: `user_id minutes` or `user_id lifetime`. Try /admin again to retry.", parse_mode="Markdown")
        return

    try:
        link, is_lifetime = create_access(target_id, ch_id, mins)

        if is_lifetime:
            msg_text = f"🎁 <b>Access Granted by Admin!</b>\n\nSubscription: Lifetime Membership ♾️\n\nJoin Link: {link.invite_link}\n\n✅ This is a lifetime membership — no expiry!"
        else:
            msg_text = f"🎁 <b>Access Granted by Admin!</b>\n\nSubscription: {mins} Minutes\n\nJoin Link: {link.invite_link}\n\n⚠️ Note: This link and your access will expire in {mins} minutes."

        bot.send_message(target_id, msg_text, parse_mode="HTML")
        bot.send_message(ADMIN_ID, f"✅ Granted user {target_id} {'Lifetime' if is_lifetime else mins + ' mins'} access manually.")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error granting access: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "adm_remove")
def adm_remove(call):
    bot.answer_callback_query(call.id)
    markup = InlineKeyboardMarkup()
    cursor = channels_col.find({"admin_id": ADMIN_ID})
    count = 0
    for ch in cursor:
        markup.add(InlineKeyboardButton(f"{ch['name']}", callback_data=f"removech_{ch['channel_id']}"))
        count += 1
    if count == 0:
        bot.send_message(call.message.chat.id, "No channels found.")
        return
    bot.send_message(call.message.chat.id, "🚫 Which channel do you want to remove a member from?", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('removech_'))
def removech(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    msg = bot.send_message(call.message.chat.id, "Send the Telegram user ID to remove from this channel.")
    bot.register_next_step_handler(msg, process_remove, ch_id)

def process_remove(message, ch_id):
    try:
        target_id = int(message.text.strip())
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Invalid user ID. Try /admin again to retry.")
        return

    try:
        bot.ban_chat_member(ch_id, target_id)
        bot.unban_chat_member(ch_id, target_id)
        users_col.delete_one({"user_id": target_id, "channel_id": ch_id})
        bot.send_message(target_id, "⚠️ Your membership has been removed by the admin.")
        bot.send_message(ADMIN_ID, f"🚫 Removed user {target_id} from channel.")
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error removing member: {e}\n\n(Note removed from database anyway if they were tracked.)")
        users_col.delete_one({"user_id": target_id, "channel_id": ch_id})

@bot.callback_query_handler(func=lambda call: call.data == "adm_setimg")
def adm_setimg(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        "🖼 Send the photo you want to use as the /start welcome image.\n\nSend /cancel to abort, or /remove to go back to no image.")
    bot.register_next_step_handler(msg, process_setimg)

def process_setimg(message):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Cancelled — welcome image unchanged.")
        return
    if message.text and message.text.strip() == "/remove":
        settings_col.delete_one({"key": "welcome_image"})
        bot.send_message(ADMIN_ID, "🗑 Welcome image removed. /start will now show text only (unless WELCOME_IMAGE_URL is set).")
        return
    if not message.photo:
        bot.send_message(ADMIN_ID, "❌ That wasn't a photo. Try /admin → 🖼 Set Welcome Image again, or send /cancel.")
        return

    file_id = message.photo[-1].file_id
    settings_col.update_one({"key": "welcome_image"}, {"$set": {"value": file_id}}, upsert=True)
    bot.send_message(ADMIN_ID, "✅ Welcome image updated! It'll show the next time anyone taps /start.")

@bot.callback_query_handler(func=lambda call: call.data == "adm_setlistimg")
def adm_setlistimg(call):
    bot.answer_callback_query(call.id)
    msg = bot.send_message(call.message.chat.id,
        "🖼 Send the photo you want to show on the 'Select a Channel' screen.\n\nSend /cancel to abort, or /remove to clear it.")
    bot.register_next_step_handler(msg, process_setlistimg)

def process_setlistimg(message):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Cancelled — channel list image unchanged.")
        return
    if message.text and message.text.strip() == "/remove":
        settings_col.delete_one({"key": "channel_list_image"})
        bot.send_message(ADMIN_ID, "🗑 Channel list image removed.")
        return
    if not message.photo:
        bot.send_message(ADMIN_ID, "❌ That wasn't a photo. Try /admin → 🖼 Set Channel List Image again, or send /cancel.")
        return

    file_id = message.photo[-1].file_id
    settings_col.update_one({"key": "channel_list_image"}, {"$set": {"value": file_id}}, upsert=True)
    bot.send_message(ADMIN_ID, "✅ Channel list image updated!")

def create_access(user_id, ch_id, mins):
    """Creates the invite link and updates the user's record. mins may be the string 'lifetime' or a number-as-string.
    If the user already has time remaining on this channel, the new duration is added on top instead of resetting.
    Returns (invite_link_obj, is_lifetime)."""
    is_lifetime = str(mins).strip().lower() == "lifetime"
    existing = users_col.find_one({"user_id": user_id, "channel_id": ch_id})
    now_ts = datetime.now().timestamp()

    if is_lifetime:
        link = bot.create_chat_invite_link(ch_id, member_limit=1)
        users_col.update_one(
            {"user_id": user_id, "channel_id": ch_id},
            {"$set": {"lifetime": True}, "$unset": {"expiry": ""}},
            upsert=True
        )
        return link, True

    if existing and existing.get("lifetime"):
        # Already a lifetime member — don't downgrade them, just issue a fresh invite link.
        link = bot.create_chat_invite_link(ch_id, member_limit=1)
        return link, True

    mins_int = int(mins)
    base_ts = now_ts
    if existing and existing.get("expiry", 0) > now_ts:
        base_ts = existing["expiry"]  # extend from their current remaining time, not from now

    expiry_datetime = datetime.fromtimestamp(base_ts) + timedelta(minutes=mins_int)
    expiry_ts = int(expiry_datetime.timestamp())
    link = bot.create_chat_invite_link(ch_id, member_limit=1, expire_date=expiry_ts)
    users_col.update_one(
        {"user_id": user_id, "channel_id": ch_id},
        {"$set": {"expiry": expiry_datetime.timestamp()}, "$unset": {"lifetime": ""}},
        upsert=True
    )
    return link, False

# --- USER: PAYMENT FLOW ---

@bot.callback_query_handler(func=lambda call: call.data.startswith('select_'))
def user_pays(call):
    _, ch_id, mins = call.data.split('_')
    ch_data = channels_col.find_one({"channel_id": int(ch_id)})
    price, custom_label = plan_info(ch_data['plans'][mins])

    qr_url = f"https://api.qrserver.com/v1/create-qr-code/?size=300x300&data=upi://pay?pa={UPI_ID}%26am={price}%26cu=INR"
    if custom_label:
        plan_label = custom_label
    else:
        plan_label = "Lifetime Membership" if str(mins).strip().lower() == "lifetime" else f"{mins} Minutes"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ I Have Paid", callback_data=f"paid_{ch_id}_{mins}"))
    markup.add(InlineKeyboardButton("📞 Contact Admin", url=f"https://t.me/{CONTACT_USERNAME}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data=f"cancelpay_{ch_id}"))

    send_page(call.message.chat.id,
              f"Plan: {plan_label}\nPrice: ₹{price}\nUPI ID: `{UPI_ID}`\n\nPlease complete the payment and click 'I Have Paid'.",
              photo=qr_url, reply_markup=markup, parse_mode="Markdown")

    pending_payments[call.from_user.id] = {"ch_id": int(ch_id), "mins": mins, "price": int(price)}

@bot.callback_query_handler(func=lambda call: call.data.startswith('backtoplans_'))
def backtoplans(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    pending_payments.pop(call.from_user.id, None)
    show_plans(call.message.chat.id, ch_id, user_id=call.from_user.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('cancelpay_'))
def cancelpay(call):
    bot.answer_callback_query(call.id)
    pending_payments.pop(call.from_user.id, None)
    show_channel_list(call.message.chat.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith('paid_'))
def ask_for_utr(call):
    bot.answer_callback_query(call.id)
    # Sent as a fresh message (not via send_page) so the QR above stays visible instead of being replaced.
    sent = bot.send_message(call.message.chat.id,
        "🧾 Please send your 12-digit UTR / transaction reference number now to verify your payment.")
    last_page_msg[call.message.chat.id] = {"message_id": sent.message_id, "has_photo": False}

# --- BHARATPE UTR AUTO-VERIFICATION ---

@bot.message_handler(func=lambda msg: msg.from_user.id in pending_payments and msg.text and msg.text.strip().isdigit())
def verify_utr(msg):
    user_id = msg.from_user.id
    utr = msg.text.strip()
    plan = pending_payments.get(user_id)
    if not plan:
        return

    if used_utrs_col.find_one({"utr": utr}):
        bot.reply_to(msg, "⚠️ This UTR has already been used for a previous approval.")
        return

    try:
        resp = requests.get(
            "https://bharatpe-payment-checker.vercel.app/check",
            params={"token": BHARATPE_TOKEN, "utr": utr},
            timeout=10
        )
        data = resp.json()
    except Exception as e:
        bot.reply_to(msg, "⚠️ Verification service unavailable right now. Please try again in a minute, or contact admin.")
        bot.send_message(ADMIN_ID, f"UTR check failed for user {user_id}, utr {utr}: {e}")
        return

    if not data.get("success"):
        bot.reply_to(msg, f"❌ {data.get('message', 'UTR not found yet. Wait a minute and resend, or contact admin.')}")
        return

    paid_amount = data["data"]["amount"]
    if int(paid_amount) != int(plan["price"]):
        bot.reply_to(msg, f"⚠️ Amount mismatch — paid ₹{paid_amount}, expected ₹{plan['price']}. Contact admin.")
        bot.send_message(ADMIN_ID, f"⚠️ Amount mismatch: user {user_id}, paid ₹{paid_amount}, expected ₹{plan['price']}, utr {utr}")
        return

    ch_id, mins = plan["ch_id"], plan["mins"]
    try:
        link, is_lifetime = create_access(user_id, ch_id, mins)
        used_utrs_col.insert_one({"utr": utr, "user_id": user_id, "ch_id": ch_id, "amount": paid_amount, "used_at": datetime.now()})

        if is_lifetime:
            msg_text = f"🎉 <b>Payment Verified!</b>\n\nSubscription: Lifetime Membership ♾️\n\nJoin Link: {link.invite_link}\n\n✅ This is a lifetime membership — no expiry!"
        else:
            msg_text = f"🎉 <b>Payment Verified!</b>\n\nSubscription: {mins} Minutes\n\nJoin Link: {link.invite_link}\n\n⚠️ Note: This link and your access will expire in {mins} minutes."

        bot.send_message(user_id, msg_text, parse_mode="HTML")
        bot.send_message(ADMIN_ID, f"✅ Auto-approved user {user_id} for {'Lifetime' if is_lifetime else mins + ' mins'} via UTR {utr} (₹{paid_amount}).")
        del pending_payments[user_id]
    except Exception as e:
        bot.send_message(ADMIN_ID, f"❌ Error during auto-approval: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith('manage_'))
def manage_ch(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start={ch_id}"

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("🏷 Set Display Name", callback_data=f"setname_{ch_id}"))
    markup.add(InlineKeyboardButton("✏️ Edit Plans/Prices", callback_data=f"editplans_{ch_id}"))
    markup.add(InlineKeyboardButton("📝 Set Channel Details", callback_data=f"setdesc_{ch_id}"))
    markup.add(InlineKeyboardButton("🖼 Set Channel Image", callback_data=f"setchimg_{ch_id}"))
    markup.add(InlineKeyboardButton("🗑 Delete Channel", callback_data=f"delch_{ch_id}"))

    display_line = f"\nShown to users as: *{ch_data['display_name']}*" if ch_data.get('display_name') else "\nShown to users as: (real channel name — no custom display name set)"
    bot.edit_message_text(f"Settings for: *{ch_data['name']}*{display_line}\n\nYour Link: `{link}`",
                          call.message.chat.id, call.message.message_id, parse_mode="Markdown",
                          reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('setname_'))
def setname(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    msg = bot.send_message(call.message.chat.id,
        "🏷 Send the name you want shown to users instead of the real channel title.\n\nSend /cancel to abort, or /remove to go back to showing the real channel name.")
    bot.register_next_step_handler(msg, process_setname, ch_id)

def process_setname(message, ch_id):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Cancelled — display name unchanged.")
        return
    if message.text and message.text.strip() == "/remove":
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"display_name": ""}})
        bot.send_message(ADMIN_ID, "🗑 Display name removed. Users will now see the real channel name.")
        return
    if not message.text:
        bot.send_message(ADMIN_ID, "❌ That wasn't text. Try again via /channels, or send /cancel.")
        return

    channels_col.update_one({"channel_id": ch_id}, {"$set": {"display_name": message.text.strip()}})
    bot.send_message(ADMIN_ID, f"✅ Display name set to \"{message.text.strip()}\". Users will see this instead of the real channel title.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('editplans_'))
def editplans(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    current_parts = []
    for k, v in ch_data.get('plans', {}).items():
        price, label = plan_info(v)
        current_parts.append(f"{k}:{price}:{label}" if label else f"{k}:{price}")
    current = ", ".join(current_parts)
    msg = bot.send_message(call.message.chat.id,
        f"✏️ Current plans for *{ch_data['name']}*:\n`{current}`\n\n"
        f"Send the new full plan list in format `Min:Price` or `Min:Price:Label` (use `lifetime` instead of minutes for a lifetime plan). This replaces the plans above entirely. A custom label is optional.\n\n"
        f"Example:\n`1440:39, 10080:69:🥈 Silver Plan, lifetime:199:🥇 Gold Plan`",
        parse_mode="Markdown")
    bot.register_next_step_handler(msg, process_editplans, ch_id, ch_data['name'])

def process_editplans(message, ch_id, ch_name):
    try:
        raw_plans = message.text.split(',')
        plans_dict = {}
        for p in raw_plans:
            parts = p.strip().split(':', 2)
            t = parts[0].strip()
            t = t.lower() if t.lower() == "lifetime" else t
            pr = parts[1].strip()
            label = parts[2].strip() if len(parts) == 3 else None
            plans_dict[t] = {"price": pr, "label": label} if label else pr

        channels_col.update_one({"channel_id": ch_id}, {"$set": {"plans": plans_dict}})
        bot.send_message(ADMIN_ID, f"✅ Plans updated for *{ch_name}*.", parse_mode="Markdown")
    except Exception:
        bot.send_message(ADMIN_ID, "❌ Invalid format. Please use `Min:Price` or `Min:Price:Label`. Try again via /channels.", parse_mode="Markdown")

@bot.callback_query_handler(func=lambda call: call.data.startswith('setdesc_'))
def setdesc(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    msg = bot.send_message(call.message.chat.id,
        "📝 Send the channel details text you want shown to users (features, what's included, etc.) before they pick a plan.\n\nSend /cancel to abort, or /remove to clear it.")
    bot.register_next_step_handler(msg, process_setdesc, ch_id)

def process_setdesc(message, ch_id):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Cancelled — channel details unchanged.")
        return
    if message.text and message.text.strip() == "/remove":
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"description": ""}})
        bot.send_message(ADMIN_ID, "🗑 Channel details removed.")
        return
    if not message.text:
        bot.send_message(ADMIN_ID, "❌ That wasn't text. Try again via /channels, or send /cancel.")
        return

    channels_col.update_one({"channel_id": ch_id}, {"$set": {"description": message.text}})
    bot.send_message(ADMIN_ID, "✅ Channel details updated! It'll show the next time someone views this channel's plans.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('setchimg_'))
def setchimg(call):
    bot.answer_callback_query(call.id)
    ch_id = int(call.data.split('_')[1])
    msg = bot.send_message(call.message.chat.id,
        "🖼 Send the photo you want to show on this channel's plan screen.\n\nSend /cancel to abort, or /remove to clear it.")
    bot.register_next_step_handler(msg, process_setchimg, ch_id)

def process_setchimg(message, ch_id):
    if message.text and message.text.strip() == "/cancel":
        bot.send_message(ADMIN_ID, "Cancelled — channel image unchanged.")
        return
    if message.text and message.text.strip() == "/remove":
        channels_col.update_one({"channel_id": ch_id}, {"$unset": {"image": ""}})
        bot.send_message(ADMIN_ID, "🗑 Channel image removed.")
        return
    if not message.photo:
        bot.send_message(ADMIN_ID, "❌ That wasn't a photo. Try again via /channels, or send /cancel.")
        return

    file_id = message.photo[-1].file_id
    channels_col.update_one({"channel_id": ch_id}, {"$set": {"image": file_id}})
    bot.send_message(ADMIN_ID, "✅ Channel image updated! It'll show the next time someone views this channel's plans.")

@bot.callback_query_handler(func=lambda call: call.data.startswith('delch_'))
def confirm_delete_channel(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    if not ch_data:
        bot.answer_callback_query(call.id, "Channel not found.")
        return

    markup = InlineKeyboardMarkup()
    markup.add(InlineKeyboardButton("✅ Yes, Delete It", callback_data=f"confirmdel_{ch_id}"))
    markup.add(InlineKeyboardButton("❌ Cancel", callback_data=f"manage_{ch_id}"))

    bot.edit_message_text(f"⚠️ Are you sure you want to delete *{ch_data['name']}*?\n\nThis removes it from your channel list and from the /start menu. Existing subscribers already in the channel will NOT be auto-kicked by this action — only new signups stop.",
                          call.message.chat.id, call.message.message_id, parse_mode="Markdown",
                          reply_markup=markup)

@bot.callback_query_handler(func=lambda call: call.data.startswith('confirmdel_'))
def do_delete_channel(call):
    ch_id = int(call.data.split('_')[1])
    ch_data = channels_col.find_one({"channel_id": ch_id})
    name = ch_data['name'] if ch_data else str(ch_id)

    channels_col.delete_one({"channel_id": ch_id})
    bot.edit_message_text(f"🗑 Deleted *{name}*.\n\nUse /channels to see your remaining channels.",
                          call.message.chat.id, call.message.message_id, parse_mode="Markdown")

# Automate Kicking
def kick_expired_users():
    now = datetime.now().timestamp()
    expired_users = list(users_col.find({"expiry": {"$lte": now}}))
    bot_username = bot.get_me().username

    for user in expired_users:
        try:
            bot.ban_chat_member(user['channel_id'], user['user_id'])
            bot.unban_chat_member(user['channel_id'], user['user_id'])
        except Exception:
            pass  # e.g. already left the channel — safe to ignore, still clean up below

        # Always remove the expired record, even if the user has blocked the bot
        users_col.delete_one({"_id": user['_id']})

        try:
            rejoin_url = f"https://t.me/{bot_username}?start={user['channel_id']}"
            markup = InlineKeyboardMarkup().add(InlineKeyboardButton("🔄 Re-join / Renew", url=rejoin_url))
            bot.send_message(user['user_id'], "⚠️ Your subscription has expired.\n\nTo join again or renew, please click the button below:", reply_markup=markup)
        except Exception:
            pass  # user may have blocked the bot — cleanup above already happened regardless

# Daily summary of auto-approvals so the admin can eyeball activity
def daily_summary():
    since = datetime.now() - timedelta(hours=24)
    approvals = list(used_utrs_col.find({"used_at": {"$gte": since}}))

    if not approvals:
        bot.send_message(ADMIN_ID, "📊 *Daily Summary*\n\nNo approvals in the last 24 hours.", parse_mode="Markdown")
        return

    total_amount = sum(a.get("amount", 0) for a in approvals)
    lines = [f"📊 *Daily Summary — Last 24 Hours*\n\nTotal approvals: {len(approvals)}\nTotal collected: ₹{total_amount}\n"]
    for a in approvals[:20]:  # cap the list so it doesn't get huge
        lines.append(f"• User {a['user_id']} — ₹{a.get('amount', '?')} — UTR {a['utr']} — {a['used_at'].strftime('%H:%M')}")
    if len(approvals) > 20:
        lines.append(f"... and {len(approvals) - 20} more")

    bot.send_message(ADMIN_ID, "\n".join(lines), parse_mode="Markdown")

# --- STARTUP ---
if __name__ == '__main__':
    keep_alive()
    scheduler = BackgroundScheduler()
    scheduler.add_job(kick_expired_users, 'interval', minutes=1)
    scheduler.add_job(daily_summary, 'cron', hour=23, minute=59)
    scheduler.start()
    bot.remove_webhook()
    print("Bot is running...")
    bot.infinity_polling(timeout=20, long_polling_timeout=10)
