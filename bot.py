import asyncio
from datetime import datetime, timedelta
from typing import Dict, Set
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions
from aiohttp import web  # Add this for health check
import os

# --- CONFIGURATION ---
TOKEN = os.getenv("BOT_TOKEN", "8940641575:AAHhPd8vgjTsClwZZMgte1LoX1lP-xiXfcA")  # Use environment variable!
REACTION_EMOJI = "👍"
VERIFICATION_TIME_MINUTES = 10

# --- DATABASE (in-memory) ---
pending_users: Dict[int, Set[int]] = {}
verified_users: Dict[int, Set[int]] = {}

# --- SETUP ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- HEALTH CHECK SERVER (keeps Render happy) ---
app = web.Application()

async def health_check(request):
    return web.Response(text="Bot is running!", status=200)

app.router.add_get('/', health_check)
app.router.add_get('/health', health_check)

async def run_health_server():
    """Run a simple HTTP server for health checks"""
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.environ.get("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"✅ Health check server running on port {port}")

# --- HELPER FUNCTIONS ---
async def restrict_user(chat_id: int, user_id: int):
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )
    await bot.restrict_chat_member(chat_id, user_id, permissions)
    
async def unrestrict_user(chat_id: int, user_id: int):
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    await bot.restrict_chat_member(chat_id, user_id, permissions)

async def send_verification_message(chat_id: int, user_id: int):
    user = await bot.get_chat_member(chat_id, user_id)
    message = await bot.send_message(
        chat_id,
        f"⚠️ {user.user.first_name}, please react with {REACTION_EMOJI} to this message within {VERIFICATION_TIME_MINUTES} minutes to verify!\n\n"
        f"👉 Tap and hold the message, then select {REACTION_EMOJI}",
    )
    
    if chat_id not in pending_users:
        pending_users[chat_id] = set()
    pending_users[chat_id].add(user_id)
    
    asyncio.create_task(auto_mute_after_timeout(chat_id, user_id))

async def auto_mute_after_timeout(chat_id: int, user_id: int):
    await asyncio.sleep(VERIFICATION_TIME_MINUTES * 60)
    
    if chat_id in pending_users and user_id in pending_users[chat_id]:
        await restrict_user(chat_id, user_id)
        user = await bot.get_chat_member(chat_id, user_id)
        await bot.send_message(
            chat_id,
            f"🔇 {user.user.first_name} was muted for not verifying.\n"
            f"Send /verify (reply to their message) to unmute."
        )
        
        if chat_id in pending_users:
            pending_users[chat_id].discard(user_id)

# --- MESSAGE HANDLER ---
@dp.message()
async def check_verification(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    try:
        chat_member = await bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["creator", "administrator"]:
            return
    except:
        return
    
    is_verified = (
        chat_id in verified_users and 
        user_id in verified_users[chat_id]
    )
    
    if not is_verified:
        await message.delete()
        
        is_pending = (
            chat_id in pending_users and 
            user_id in pending_users[chat_id]
        )
        
        if not is_pending:
            await send_verification_message(chat_id, user_id)
        else:
            reminder = await message.answer(
                f"⚠️ {message.from_user.first_name}, please verify first!"
            )
            await asyncio.sleep(5)
            await reminder.delete()

# --- REACTION HANDLER ---
@dp.message_reaction()
async def handle_reaction(reaction_event: types.MessageReactionUpdated):
    if reaction_event.old_reaction == reaction_event.new_reaction:
        return
        
    chat_id = reaction_event.chat.id
    user_id = reaction_event.user.id
    
    try:
        chat_member = await bot.get_chat_member(chat_id, user_id)
        if chat_member.status in ["creator", "administrator"]:
            return
    except:
        return
    
    try:
        message = await bot.get_message(chat_id, reaction_event.message_id)
    except:
        return
    
    if message.from_user.id != bot.id:
        return
    
    has_reaction = False
    for reaction in reaction_event.new_reaction:
        if reaction.emoji == REACTION_EMOJI:
            has_reaction = True
            break
    
    if has_reaction:
        if chat_id not in verified_users:
            verified_users[chat_id] = set()
        verified_users[chat_id].add(user_id)
        
        if chat_id in pending_users:
            pending_users[chat_id].discard(user_id)
        
        await unrestrict_user(chat_id, user_id)
        
        await bot.send_message(
            chat_id,
            f"✅ {message.from_user.first_name} has been verified! Welcome!"
        )
        
        await asyncio.sleep(5)
        await message.delete()

# --- COMMANDS ---
@dp.message(Command("verify"))
async def manual_verify(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("This command only works in groups!")
        return
    
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if chat_member.status not in ["creator", "administrator"]:
            await message.answer("❌ Only admins can use this command!")
            return
    except:
        return
    
    if not message.reply_to_message:
        await message.answer("❌ Reply to a user's message to verify them!\nUsage: /verify (reply to message)")
        return
    
    user_id = message.reply_to_message.from_user.id
    chat_id = message.chat.id
    
    if chat_id not in verified_users:
        verified_users[chat_id] = set()
    verified_users[chat_id].add(user_id)
    
    await unrestrict_user(chat_id, user_id)
    await message.answer(f"✅ User verified and unmuted!")

@dp.message(Command("reset"))
async def reset_all(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    try:
        chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
        if chat_member.status not in ["creator", "administrator"]:
            await message.answer("❌ Only admins can use this command!")
            return
    except:
        return
    
    chat_id = message.chat.id
    if chat_id in verified_users:
        verified_users[chat_id].clear()
    if chat_id in pending_users:
        pending_users[chat_id].clear()
    
    await message.answer("🔄 All verifications reset!")

@dp.message(Command("stats"))
async def show_stats(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    chat_id = message.chat.id
    verified_count = len(verified_users.get(chat_id, set()))
    pending_count = len(pending_users.get(chat_id, set()))
    
    await message.answer(
        f"📊 **Group Statistics**\n"
        f"✅ Verified users: {verified_count}\n"
        f"⏳ Pending users: {pending_count}\n"
        f"🎯 Required reaction: {REACTION_EMOJI}\n"
        f"⏱️ Timeout: {VERIFICATION_TIME_MINUTES} minutes",
        parse_mode="Markdown"
    )

# --- START BOT ---
async def main():
    print("🤖 Reaction Verification Bot starting...")
    print(f"Required reaction: {REACTION_EMOJI}")
    print(f"Timeout: {VERIFICATION_TIME_MINUTES} minutes")
    
    # Start health check server
    await run_health_server()
    
    # Start bot polling
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
