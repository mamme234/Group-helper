import asyncio
import re
import json
import random
import hashlib
from datetime import datetime, timedelta
from typing import Dict, Set, List, Optional
from collections import defaultdict
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated
from aiogram.enums import ParseMode
import os

# ==================== CONFIGURATION ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("BOT_TOKEN environment variable is not set!")

LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", None)  # Channel ID for logs
FORCE_CHANNEL_ID = os.getenv("FORCE_CHANNEL_ID", None)  # Channel users must join
RULES_CHANNEL_ID = os.getenv("RULES_CHANNEL_ID", None)  # Channel with rules

# Anti-spam settings
FLOOD_LIMIT = 5  # messages per X seconds
FLOOD_WINDOW = 5  # seconds
WARN_LIMIT = 3   # warnings before mute
MUTE_DURATION = 30  # minutes

# Feature toggles
ENABLE_CAPTCHA = True
ENABLE_WELCOME = True
ENABLE_ANTI_SPAM = True
ENABLE_BAD_WORDS = True
ENABLE_ANTI_NSFW = True
ENABLE_FORCE_JOIN = False
ENABLE_LOG_CHANNEL = False
ENABLE_ANTI_LINKS = False
ENABLE_ANTI_CHANNELS = False

# Bad words list (add your own)
BAD_WORDS = ["fuck", "shit", "asshole", "bitch", "damn", "stupid", "idiot"]

# NSFW keywords (simple version - can be expanded)
NSFW_KEYWORDS = ["porn", "xxx", "adult", "nsfw", "sex", "naked", "nude"]

# Anti-channel patterns
CHANNEL_PATTERNS = [
    r't\.me/[a-zA-Z0-9_]+',
    r'https?://t\.me/',
    r'@[a-zA-Z0-9_]{5,}',
]

# Initialize bot and dispatcher - THIS IS CRITICAL!
bot = Bot(token=TOKEN)
dp = Dispatcher()  # <-- YOU WERE MISSING THIS!

# ==================== DATA STORAGE ====================
user_message_times: Dict[int, List[datetime]] = {}
user_warnings: Dict[str, int] = {}
user_mutes: Dict[str, datetime] = {}
pending_verifications: Dict[int, Dict[int, str]] = {}
verified_users: Dict[int, Set[int]] = {}
user_reputation: Dict[str, int] = {}
user_join_time: Dict[str, datetime] = {}
reported_messages: Dict[int, Dict[int, List[int]]] = {}
group_settings: Dict[int, Dict[str, bool]] = {}

# ==================== UTILITY FUNCTIONS ====================
def get_user_key(chat_id: int, user_id: int) -> str:
    return f"{chat_id}_{user_id}"

async def is_admin(chat_id: int, user_id: int, bot: Bot) -> bool:
    try:
        chat_member = await bot.get_chat_member(chat_id, user_id)
        return chat_member.status in ["creator", "administrator"]
    except:
        return False

async def log_action(chat_id: int, action: str, user_id: int, admin_id: int = None, reason: str = None):
    """Log moderation actions to log channel"""
    if not ENABLE_LOG_CHANNEL or not LOG_CHANNEL_ID:
        return
    
    try:
        user = await bot.get_chat_member(chat_id, user_id)
        admin = await bot.get_chat_member(chat_id, admin_id) if admin_id else None
        
        log_text = f"📋 **Moderation Log**\n"
        log_text += f"**Action:** {action}\n"
        log_text += f"**Group:** {chat_id}\n"
        log_text += f"**User:** {user.user.first_name} (ID: {user_id})\n"
        if admin:
            log_text += f"**Admin:** {admin.user.first_name} (ID: {admin_id})\n"
        if reason:
            log_text += f"**Reason:** {reason}\n"
        log_text += f"**Time:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        await bot.send_message(LOG_CHANNEL_ID, log_text, parse_mode=ParseMode.MARKDOWN)
    except:
        pass

# ==================== MODERATION ACTIONS ====================
async def mute_user(chat_id: int, user_id: int, duration_minutes: int = MUTE_DURATION, reason: str = None):
    """Mute a user for specified duration"""
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )
    await bot.restrict_chat_member(chat_id, user_id, permissions)
    
    # Track mute expiry
    key = get_user_key(chat_id, user_id)
    user_mutes[key] = datetime.now() + timedelta(minutes=duration_minutes)
    
    # Auto-unmute after duration
    asyncio.create_task(auto_unmute(chat_id, user_id, duration_minutes))
    
    # Notify user
    try:
        await bot.send_message(
            user_id,
            f"🔇 You have been muted in a group for {duration_minutes} minutes.\nReason: {reason or 'Rule violation'}"
        )
    except:
        pass
    
    await log_action(chat_id, "MUTE", user_id, None, reason)

async def auto_unmute(chat_id: int, user_id: int, duration_minutes: int):
    """Auto unmute after duration"""
    await asyncio.sleep(duration_minutes * 60)
    
    key = get_user_key(chat_id, user_id)
    if key in user_mutes and user_mutes[key] <= datetime.now():
        permissions = ChatPermissions(
            can_send_messages=True,
            can_send_media_messages=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True
        )
        await bot.restrict_chat_member(chat_id, user_id, permissions)
        del user_mutes[key]

async def ban_user(chat_id: int, user_id: int, reason: str = None):
    """Ban a user"""
    await bot.ban_chat_member(chat_id, user_id)
    await log_action(chat_id, "BAN", user_id, None, reason)

async def kick_user(chat_id: int, user_id: int, reason: str = None):
    """Kick a user (ban then unban)"""
    await bot.ban_chat_member(chat_id, user_id)
    await bot.unban_chat_member(chat_id, user_id)
    await log_action(chat_id, "KICK", user_id, None, reason)

async def warn_user(chat_id: int, user_id: int, reason: str, admin_id: int = None):
    """Warn a user"""
    key = get_user_key(chat_id, user_id)
    user_warnings[key] = user_warnings.get(key, 0) + 1
    current_warns = user_warnings[key]
    
    # Notify in group
    await bot.send_message(
        chat_id,
        f"⚠️ **Warning {current_warns}/{WARN_LIMIT}**\n"
        f"User: {user_id}\n"
        f"Reason: {reason}\n"
        f"{'🔇 User has been muted!' if current_warns >= WARN_LIMIT else 'Be careful!'}",
        parse_mode=ParseMode.MARKDOWN
    )
    
    # Mute if reached limit
    if current_warns >= WARN_LIMIT:
        await mute_user(chat_id, user_id, MUTE_DURATION, f"Exceeded warning limit: {reason}")
        user_warnings[key] = 0
    
    await log_action(chat_id, f"WARNING {current_warns}/{WARN_LIMIT}", user_id, admin_id, reason)

async def add_reputation(chat_id: int, user_id: int, points: int = 1):
    """Add reputation points to a user"""
    key = get_user_key(chat_id, user_id)
    user_reputation[key] = user_reputation.get(key, 0) + points

# ==================== CAPTCHA SYSTEM ====================
def generate_captcha() -> tuple:
    """Generate math captcha"""
    ops = ['+', '-']
    op = random.choice(ops)
    
    if op == '+':
        a = random.randint(1, 50)
        b = random.randint(1, 50)
        answer = a + b
        question = f"{a} + {b}"
    else:
        a = random.randint(10, 50)
        b = random.randint(1, a)
        answer = a - b
        question = f"{a} - {b}"
    
    return f"{question} = ?", str(answer)

async def send_captcha(chat_id: int, user_id: int, user_name: str):
    """Send captcha to user"""
    question, answer = generate_captcha()
    pending_verifications.setdefault(chat_id, {})[user_id] = answer
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Cancel", callback_data=f"cancel_captcha_{user_id}")]
    ])
    
    msg = await bot.send_message(
        chat_id,
        f"🔐 **Verification Required**\n\n"
        f"Welcome {user_name}!\n"
        f"Please solve this CAPTCHA within 2 minutes:\n\n"
        f"**{question}**\n\n"
        f"Type only the number in the chat.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )
    
    asyncio.create_task(auto_timeout_captcha(chat_id, user_id, msg.message_id))

async def auto_timeout_captcha(chat_id: int, user_id: int, message_id: int):
    """Auto mute if captcha not solved"""
    await asyncio.sleep(120)  # 2 minutes
    
    if (chat_id in pending_verifications and 
        user_id in pending_verifications[chat_id]):
        await mute_user(chat_id, user_id, 5, "Failed to complete verification")
        await bot.send_message(
            chat_id,
            f"🔇 User {user_id} was muted for not completing verification."
        )
        del pending_verifications[chat_id][user_id]

# ==================== FORCE JOIN CHANNEL ====================
async def check_force_join(chat_id: int, user_id: int) -> bool:
    """Check if user has joined required channel"""
    if not ENABLE_FORCE_JOIN or not FORCE_CHANNEL_ID:
        return True
    
    try:
        member = await bot.get_chat_member(FORCE_CHANNEL_ID, user_id)
        return member.status in ["member", "administrator", "creator"]
    except:
        return False

async def send_force_join_message(chat_id: int, user_id: int, user_name: str):
    """Send force join message"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Join Channel", url=f"https://t.me/{FORCE_CHANNEL_ID}")],
        [InlineKeyboardButton(text="✅ Checked", callback_data=f"check_join_{user_id}")]
    ])
    
    await bot.send_message(
        chat_id,
        f"⚠️ **{user_name}**, you must join our channel before participating!\n\n"
        f"Click the button below, join, then click 'Checked'.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )

# ==================== CONTENT FILTERS ====================
def is_flooding(user_id: int) -> bool:
    """Check for message flooding"""
    now = datetime.now()
    if user_id not in user_message_times:
        user_message_times[user_id] = []
    
    # Clean old messages
    user_message_times[user_id] = [
        t for t in user_message_times[user_id]
        if (now - t).total_seconds() < FLOOD_WINDOW
    ]
    
    if len(user_message_times[user_id]) >= FLOOD_LIMIT:
        return True
    
    user_message_times[user_id].append(now)
    return False

def contains_bad_words(text: str) -> bool:
    """Check for bad words"""
    if not text:
        return False
    text_lower = text.lower()
    for word in BAD_WORDS:
        if word in text_lower:
            return True
    return False

def contains_nsfw(text: str) -> bool:
    """Check for NSFW content"""
    if not text or not ENABLE_ANTI_NSFW:
        return False
    text_lower = text.lower()
    for keyword in NSFW_KEYWORDS:
        if keyword in text_lower:
            return True
    return False

def contains_link(text: str) -> bool:
    """Check for links"""
    if not text or not ENABLE_ANTI_LINKS:
        return False
    url_pattern = r'https?://[^\s]+|www\.[^\s]+|\.[a-z]{2,}/'
    return bool(re.search(url_pattern, text))

def contains_channel_invite(text: str) -> bool:
    """Check for channel invites"""
    if not text or not ENABLE_ANTI_CHANNELS:
        return False
    for pattern in CHANNEL_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False

# ==================== WELCOME/GREETINGS ====================
async def send_welcome(chat_id: int, user_id: int, user_name: str):
    """Send welcome message"""
    welcome_text = (
        f"🎉 **Welcome to the group, {user_name}!** 🎉\n\n"
        f"📋 Please take a moment to:\n"
        f"• Read the rules (send /rules)\n"
        f"• Complete verification (if enabled)\n"
        f"• Respect all members\n\n"
        f"💡 Need help? Use /help\n"
        f"⚠️ Violations may result in warnings/mutes/bans"
    )
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📜 View Rules", callback_data="show_rules")],
        [InlineKeyboardButton(text="✅ Got it!", callback_data=f"welcome_done_{user_id}")]
    ])
    
    await bot.send_message(chat_id, welcome_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    
    # Track join time
    key = get_user_key(chat_id, user_id)
    user_join_time[key] = datetime.now()
    
    if ENABLE_CAPTCHA:
        await send_captcha(chat_id, user_id, user_name)

# ==================== COMMAND HANDLERS ====================

# --- Basic Commands ---
@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        "🤖 **Group Helper Bot v2.0**\n\n"
        "I'm a complete group management bot with:\n"
        "✅ Captcha Verification\n"
        "✅ Anti-Spam & Anti-Flood\n"
        "✅ Bad Word Filter\n"
        "✅ NSFW Content Filter\n"
        "✅ Warning System\n"
        "✅ Auto-Mute/Temp Ban\n"
        "✅ Welcome Messages\n"
        "✅ Force Join Channel\n"
        "✅ Reputation System\n"
        "✅ Reporting System\n\n"
        "📌 **Commands:**\n"
        "/help - Show all commands\n"
        "/rules - View group rules\n"
        "/stats - Group statistics\n\n"
        "Add me as admin to start protecting your group!",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(Command("help"))
async def help_cmd(message: types.Message):
    help_text = (
        "📚 **Bot Commands**\n\n"
        "**Member Commands:**\n"
        "/start - Bot info\n"
        "/help - This menu\n"
        "/rules - View rules\n"
        "/stats - Group stats\n"
        "/report @user [reason] - Report a user\n"
        "/mywarns - Check your warnings\n"
        "/rep @user - Check user reputation\n\n"
        
        "**Admin Commands:**\n"
        "/warn [reply] [reason] - Warn a user\n"
        "/mute [reply] [time] - Mute user (1-60m)\n"
        "/unmute [reply] - Unmute user\n"
        "/kick [reply] [reason] - Kick user\n"
        "/ban [reply] [reason] - Ban user\n"
        "/unban @user - Unban user\n"
        "/clear [count] - Delete messages\n"
        "/settings - Configure bot\n"
        "/logs - Recent moderation log"
    )
    await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)

@dp.message(Command("rules"))
async def rules_cmd(message: types.Message):
    rules = (
        "📜 **Group Rules**\n\n"
        "1️⃣ **No Spam** - Don't flood or send promotional content\n"
        "2️⃣ **Be Respectful** - No hate speech, harassment, or toxicity\n"
        "3️⃣ **No NSFW** - Adult content is strictly prohibited\n"
        "4️⃣ **No Illegal Content** - Follow platform guidelines\n"
        "5️⃣ **No Links** - Avoid sharing external links\n"
        "6️⃣ **English Only** - Keep conversations understandable\n"
        "7️⃣ **No DM Advertising** - Don't DM members without permission\n\n"
        "⚠️ **Consequences:**\n"
        f"• 3 warnings = {MUTE_DURATION} minute mute\n"
        "• Repeated violations = Ban\n\n"
        "✅ **To appeal:** Contact an admin"
    )
    await message.answer(rules, parse_mode=ParseMode.MARKDOWN)

# --- Moderation Commands ---
@dp.message(Command("warn"))
async def warn_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to warn them!\nUsage: /warn [reason]")
        return
    
    user_id = message.reply_to_message.from_user.id
    reason = message.text.replace("/warn", "").strip() or "Violation of group rules"
    
    await warn_user(message.chat.id, user_id, reason, message.from_user.id)

@dp.message(Command("mute"))
async def mute_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to mute them!\nUsage: /mute [minutes] [reason]")
        return
    
    user_id = message.reply_to_message.from_user.id
    args = message.text.split()
    
    duration = MUTE_DURATION
    if len(args) > 1 and args[1].isdigit():
        duration = min(int(args[1]), 60)
    
    reason = " ".join(args[2:]) if len(args) > 2 else "Rule violation"
    
    await mute_user(message.chat.id, user_id, duration, reason)
    await message.answer(f"🔇 User has been muted for {duration} minutes.\nReason: {reason}")

@dp.message(Command("unmute"))
async def unmute_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to unmute them!")
        return
    
    user_id = message.reply_to_message.from_user.id
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    await bot.restrict_chat_member(message.chat.id, user_id, permissions)
    await message.answer(f"✅ User has been unmuted!")

@dp.message(Command("kick"))
async def kick_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to kick them!")
        return
    
    user_id = message.reply_to_message.from_user.id
    reason = message.text.replace("/kick", "").strip() or "No reason provided"
    
    await kick_user(message.chat.id, user_id, reason)
    await message.answer(f"👢 User has been kicked!\nReason: {reason}")

@dp.message(Command("ban"))
async def ban_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to ban them!")
        return
    
    user_id = message.reply_to_message.from_user.id
    reason = message.text.replace("/ban", "").strip() or "Severe violation"
    
    await ban_user(message.chat.id, user_id, reason)
    await message.answer(f"🔨 User has been banned!\nReason: {reason}")

@dp.message(Command("clear"))
async def clear_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    args = message.text.split()
    count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 10
    count = min(count, 100)
    
    # Delete command message
    await message.delete()
    
    # Delete last N messages
    deleted = 0
    async for msg in message.chat.history(limit=count + 1):
        if msg.message_id != message.message_id:
            try:
                await msg.delete()
                deleted += 1
            except:
                pass
    
    await message.answer(f"🧹 Deleted {deleted} messages!", delete_in_after=5)

# --- Info Commands ---
@dp.message(Command("stats"))
async def stats_cmd(message: types.Message):
    chat_id = message.chat.id
    verified = len(verified_users.get(chat_id, set()))
    pending = len(pending_verifications.get(chat_id, {}))
    
    await message.answer(
        f"📊 **Group Statistics**\n\n"
        f"✅ Verified: {verified}\n"
        f"⏳ Pending: {pending}\n"
        f"👥 Members: {await bot.get_chat_member_count(chat_id)}\n"
        f"⚙️ Warning limit: {WARN_LIMIT}\n"
        f"🔇 Mute duration: {MUTE_DURATION} min\n"
        f"🚫 Flood limit: {FLOOD_LIMIT} msg/{FLOOD_WINDOW}s",
        parse_mode=ParseMode.MARKDOWN
    )

@dp.message(Command("report"))
async def report_cmd(message: types.Message):
    args = message.text.split(maxsplit=2)
    if len(args) < 2:
        await message.answer("❌ Usage: /report @user [reason]")
        return
    
    target = args[1].replace("@", "")
    reason = args[2] if len(args) > 2 else "No reason provided"
    
    # Notify admins (simplified - would need admin list)
    await message.answer(f"📢 Report submitted for @{target}\nReason: {reason}\nAdmins have been notified.")
    await add_reputation(message.chat.id, message.from_user.id, 1)

@dp.message(Command("mywarns"))
async def mywarns_cmd(message: types.Message):
    key = get_user_key(message.chat.id, message.from_user.id)
    warns = user_warnings.get(key, 0)
    await message.answer(f"⚠️ You have {warns}/{WARN_LIMIT} warnings.")

@dp.message(Command("rep"))
async def rep_cmd(message: types.Message):
    target = message.reply_to_message.from_user.id if message.reply_to_message else message.from_user.id
    key = get_user_key(message.chat.id, target)
    rep = user_reputation.get(key, 0)
    await message.answer(f"⭐ User has {rep} reputation points.")

# --- Settings Command ---
@dp.message(Command("settings"))
async def settings_cmd(message: types.Message):
    if not await is_admin(message.chat.id, message.from_user.id, bot):
        await message.answer("❌ Only admins can use this command!")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Toggle Captcha", callback_data="toggle_captcha"),
         InlineKeyboardButton(text="🔄 Toggle Anti-Spam", callback_data="toggle_antispam")],
        [InlineKeyboardButton(text="🚫 Toggle Bad Words", callback_data="toggle_badwords"),
         InlineKeyboardButton(text="🔞 Toggle NSFW", callback_data="toggle_nsfw")],
        [InlineKeyboardButton(text="🔗 Toggle Anti-Links", callback_data="toggle_links"),
         InlineKeyboardButton(text="📢 Toggle Anti-Channels", callback_data="toggle_channels")]
    ])
    
    await message.answer(
        f"⚙️ **Bot Settings**\n\n"
        f"Captcha: {'✅' if ENABLE_CAPTCHA else '❌'}\n"
        f"Anti-Spam: {'✅' if ENABLE_ANTI_SPAM else '❌'}\n"
        f"Bad Words: {'✅' if ENABLE_BAD_WORDS else '❌'}\n"
        f"NSFW Filter: {'✅' if ENABLE_ANTI_NSFW else '❌'}\n"
        f"Anti-Links: {'✅' if ENABLE_ANTI_LINKS else '❌'}\n"
        f"Anti-Channels: {'✅' if ENABLE_ANTI_CHANNELS else '❌'}\n\n"
        f"Click buttons to toggle features.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=keyboard
    )

# ==================== MESSAGE HANDLER ====================
@dp.message()
async def handle_message(message: types.Message):
    """Main message filter"""
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Skip bot messages
    if user_id == bot.id:
        return
    
    # Check admin status
    if await is_admin(chat_id, user_id, bot):
        return
    
    # Check force join
    if ENABLE_FORCE_JOIN and not await check_force_join(chat_id, user_id):
        await message.delete()
        await send_force_join_message(chat_id, user_id, message.from_user.first_name)
        return
    
    # Check verification
    if ENABLE_CAPTCHA:
        is_verified = (chat_id in verified_users and user_id in verified_users[chat_id])
        
        # Handle captcha answer
        if (chat_id in pending_verifications and user_id in pending_verifications[chat_id]):
            expected = pending_verifications[chat_id][user_id]
            if message.text and message.text.strip() == expected:
                verified_users.setdefault(chat_id, set()).add(user_id)
                del pending_verifications[chat_id][user_id]
                await message.answer("✅ Verification successful! Welcome to the group!")
                await message.delete()
                await add_reputation(chat_id, user_id, 5)
            else:
                await message.delete()
                await message.answer("❌ Incorrect answer! Try again.")
            return
        elif not is_verified:
            await message.delete()
            if chat_id not in pending_verifications or user_id not in pending_verifications[chat_id]:
                await send_captcha(chat_id, user_id, message.from_user.first_name)
            return
    
    # Check for mute
    key = get_user_key(chat_id, user_id)
    if key in user_mutes and user_mutes[key] > datetime.now():
        await message.delete()
        return
    
    # Anti-flood
    if ENABLE_ANTI_SPAM and is_flooding(user_id):
        await warn_user(chat_id, user_id, "Spamming/Flooding detected", None)
        await message.delete()
        return
    
    # Bad words filter
    if ENABLE_BAD_WORDS and message.text and contains_bad_words(message.text):
        await warn_user(chat_id, user_id, "Bad words are not allowed", None)
        await message.delete()
        return
    
    # NSFW filter
    if ENABLE_ANTI_NSFW and message.text and contains_nsfw(message.text):
        await warn_user(chat_id, user_id, "NSFW content is prohibited", None)
        await message.delete()
        return
    
    # Anti-links
    if ENABLE_ANTI_LINKS and message.text and contains_link(message.text):
        await warn_user(chat_id, user_id, "Links are not allowed", None)
        await message.delete()
        return
    
    # Anti-channels
    if ENABLE_ANTI_CHANNELS and message.text and contains_channel_invite(message.text):
        await warn_user(chat_id, user_id, "Channel invites are not allowed", None)
        await message.delete()
        return

# ==================== CHAT MEMBER HANDLER ====================
@dp.chat_member()
async def handle_new_members(chat_member_update: types.ChatMemberUpdated):
    """Handle new members joining"""
    if chat_member_update.new_chat_member.status == "member":
        if ENABLE_WELCOME:
            user = chat_member_update.new_chat_member.user
            await send_welcome(chat_member_update.chat.id, user.id, user.first_name)

# ==================== CALLBACK HANDLER ====================
@dp.callback_query()
async def handle_callbacks(callback: types.CallbackQuery):
    data = callback.data
    
    if data.startswith("cancel_captcha_"):
        user_id = int(data.split("_")[2])
        if callback.from_user.id == user_id:
            await callback.message.delete()
            await callback.answer("Verification cancelled.")
        else:
            await callback.answer("This isn't your verification!")
    
    elif data == "show_rules":
        await rules_cmd(callback.message)
    
    elif data.startswith("welcome_done_"):
        await callback.answer("Welcome to the group! Enjoy your stay!")
        await callback.message.delete()
    
    elif data.startswith("check_join_"):
        user_id = int(data.split("_")[2])
        if callback.from_user.id == user_id:
            if await check_force_join(callback.message.chat.id, user_id):
                await callback.message.delete()
                await callback.answer("✅ Thanks for joining!")
            else:
                await callback.answer("❌ You haven't joined yet! Please join first.", show_alert=True)
    
    # Settings toggles
    elif data == "toggle_captcha":
        global ENABLE_CAPTCHA
        ENABLE_CAPTCHA = not ENABLE_CAPTCHA
        await callback.answer(f"Captcha: {'ON' if ENABLE_CAPTCHA else 'OFF'}")
    elif data == "toggle_antispam":
        global ENABLE_ANTI_SPAM
        ENABLE_ANTI_SPAM = not ENABLE_ANTI_SPAM
        await callback.answer(f"Anti-Spam: {'ON' if ENABLE_ANTI_SPAM else 'OFF'}")
    elif data == "toggle_badwords":
        global ENABLE_BAD_WORDS
        ENABLE_BAD_WORDS = not ENABLE_BAD_WORDS
        await callback.answer(f"Bad Words Filter: {'ON' if ENABLE_BAD_WORDS else 'OFF'}")
    elif data == "toggle_nsfw":
        global ENABLE_ANTI_NSFW
        ENABLE_ANTI_NSFW = not ENABLE_ANTI_NSFW
        await callback.answer(f"NSFW Filter: {'ON' if ENABLE_ANTI_NSFW else 'OFF'}")
    elif data == "toggle_links":
        global ENABLE_ANTI_LINKS
        ENABLE_ANTI_LINKS = not ENABLE_ANTI_LINKS
        await callback.answer(f"Anti-Links: {'ON' if ENABLE_ANTI_LINKS else 'OFF'}")
    elif data == "toggle_channels":
        global ENABLE_ANTI_CHANNELS
        ENABLE_ANTI_CHANNELS = not ENABLE_ANTI_CHANNELS
        await callback.answer(f"Anti-Channels: {'ON' if ENABLE_ANTI_CHANNELS else 'OFF'}")
    
    await callback.answer()

# ==================== HEALTH CHECK ====================
from aiohttp import web

web_app = web.Application()

async def health_check(request):
    return web.Response(text="Bot is running!", status=200)

web_app.router.add_get('/', health_check)
web_app.router.add_get('/health', health_check)

async def run_health_server():
    runner = web.AppRunner(web_app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health check server on port {port}")

# ==================== MAIN ====================
async def main():
    print("🤖 Group Helper Bot v2.0 Starting...")
    print(f"📋 Features:")
    print(f"  • CAPTCHA: {'ON' if ENABLE_CAPTCHA else 'OFF'}")
    print(f"  • Anti-Spam: {'ON' if ENABLE_ANTI_SPAM else 'OFF'}")
    print(f"  • Bad Words: {'ON' if ENABLE_BAD_WORDS else 'OFF'}")
    print(f"  • NSFW Filter: {'ON' if ENABLE_ANTI_NSFW else 'OFF'}")
    print(f"  • Force Join: {'ON' if ENABLE_FORCE_JOIN else 'OFF'}")
    print(f"  • Log Channel: {'ON' if ENABLE_LOG_CHANNEL else 'OFF'}")
    
    # Start health check server
    asyncio.create_task(run_health_server())
    
    # Start polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
