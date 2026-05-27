import asyncio
from datetime import datetime, timedelta
from typing import Dict, Set
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import ChatPermissions

# --- CONFIGURATION ---
TOKEN = "8940641575:AAHhPd8vgjTsClwZZMgte1LoX1lP-xiXfcA"
REACTION_EMOJI = "👍"  # Change to any emoji: ❤️, 🔥, 🎉, etc.
VERIFICATION_TIME_MINUTES = 10  # Time to react before being muted

# --- DATABASE (in-memory, use Redis/DB for production) ---
pending_users: Dict[int, Set[int]] = {}  # {chat_id: set(user_ids)}
verified_users: Dict[int, Set[int]] = {}  # {chat_id: set(user_ids)}

# --- SETUP ---
bot = Bot(token=TOKEN)
dp = Dispatcher()

# --- HELPER FUNCTIONS ---
async def restrict_user(chat_id: int, user_id: int):
    """Mute a user who didn't react"""
    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_media_messages=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False
    )
    await bot.restrict_chat_member(chat_id, user_id, permissions)
    
async def unrestrict_user(chat_id: int, user_id: int):
    """Remove all restrictions after reaction"""
    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_media_messages=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True
    )
    await bot.restrict_chat_member(chat_id, user_id, permissions)

async def send_verification_message(chat_id: int, user_id: int):
    """Send a message asking for reaction"""
    user = await bot.get_chat_member(chat_id, user_id)
    message = await bot.send_message(
        chat_id,
        f"⚠️ {user.user.first_name}, please react with {REACTION_EMOJI} to this message within {VERIFICATION_TIME_MINUTES} minutes to verify you're human!\n\n"
        f"👉 Just tap and hold the message, then select {REACTION_EMOJI}",
        reply_to_message_id=None
    )
    
    # Store pending user
    if chat_id not in pending_users:
        pending_users[chat_id] = set()
    pending_users[chat_id].add(user_id)
    
    # Auto-mute after timeout
    asyncio.create_task(auto_mute_after_timeout(chat_id, user_id, message.message_id))

async def auto_mute_after_timeout(chat_id: int, user_id: int, message_id: int):
    """Wait for timeout, then mute if no reaction"""
    await asyncio.sleep(VERIFICATION_TIME_MINUTES * 60)
    
    # Check if user is still pending (not verified)
    if chat_id in pending_users and user_id in pending_users[chat_id]:
        await restrict_user(chat_id, user_id)
        
        # Notify group
        user = await bot.get_chat_member(chat_id, user_id)
        await bot.send_message(
            chat_id,
            f"🔇 {user.user.first_name} was muted for not verifying within {VERIFICATION_TIME_MINUTES} minutes.\n"
            f"Send me a DM to verify and get unmuted!"
        )
        
        # Clean up
        if chat_id in pending_users and user_id in pending_users[chat_id]:
            pending_users[chat_id].discard(user_id)

# --- MESSAGE HANDLER: Catch messages from non-verified users ---
@dp.message()
async def check_verification(message: types.Message):
    # Only check group/supergroup messages
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    chat_id = message.chat.id
    user_id = message.from_user.id
    
    # Skip admins (they don't need verification)
    chat_member = await bot.get_chat_member(chat_id, user_id)
    if chat_member.status in ["creator", "administrator"]:
        return
    
    # Check if user is verified
    is_verified = (
        chat_id in verified_users and 
        user_id in verified_users[chat_id]
    )
    
    # If not verified, delete message and send reminder
    if not is_verified:
        await message.delete()
        
        # Check if user is already pending
        is_pending = (
            chat_id in pending_users and 
            user_id in pending_users[chat_id]
        )
        
        if not is_pending:
            await send_verification_message(chat_id, user_id)
        else:
            # Send ephemeral reminder (will auto-delete)
            reminder = await message.answer(
                f"⚠️ {message.from_user.first_name}, please verify first by reacting with {REACTION_EMOJI} to the verification message!",
                reply_to_message_id=message.message_id
            )
            await asyncio.sleep(5)
            await reminder.delete()

# --- REACTION HANDLER: Catch when user reacts ---
@dp.message_reaction()
async def handle_reaction(reaction_event: types.MessageReactionUpdated):
    # Check if the reacted message was sent by the bot
    if reaction_event.old_reaction == reaction_event.new_reaction:
        return  # No change
        
    chat_id = reaction_event.chat.id
    user_id = reaction_event.user.id
    message_id = reaction_event.message_id
    
    # Skip admins
    chat_member = await bot.get_chat_member(chat_id, user_id)
    if chat_member.status in ["creator", "administrator"]:
        return
    
    # Get the message
    try:
        message = await bot.get_message(chat_id, message_id)
    except:
        return
    
    # Check if message was sent by the bot (our verification message)
    if message.from_user.id != bot.id:
        return
    
    # Check if user has the required reaction
    has_reaction = False
    for reaction in reaction_event.new_reaction:
        if reaction.emoji == REACTION_EMOJI:
            has_reaction = True
            break
    
    if has_reaction:
        # Verify the user
        if chat_id not in verified_users:
            verified_users[chat_id] = set()
        verified_users[chat_id].add(user_id)
        
        # Remove from pending
        if chat_id in pending_users:
            pending_users[chat_id].discard(user_id)
        
        # Unmute user if they were muted
        await unrestrict_user(chat_id, user_id)
        
        # Confirmation message
        await bot.send_message(
            chat_id,
            f"✅ {message.from_user.first_name} has been verified! Welcome to the group! 🎉",
            reply_to_message_id=message_id
        )
        
        # Delete verification message after 5 seconds
        await asyncio.sleep(5)
        await message.delete()

# --- COMMAND: Manually verify someone (admin only) ---
@dp.message(Command("verify"))
async def manual_verify(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    # Check admin status
    chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if chat_member.status not in ["creator", "administrator"]:
        await message.answer("❌ Only admins can use this command!")
        return
    
    # Get user to verify (reply to a message or mention)
    user_id = None
    if message.reply_to_message:
        user_id = message.reply_to_message.from_user.id
    elif len(message.text.split()) > 1:
        # Try to parse mention or ID
        target = message.text.split()[1]
        if target.startswith("@"):
            # Get user by username (simplified)
            pass  # Would need more complex logic
        elif target.isdigit():
            user_id = int(target)
    
    if not user_id:
        await message.answer("Usage: /verify (reply to a user's message) or /verify [user_id]")
        return
    
    # Verify the user
    chat_id = message.chat.id
    if chat_id not in verified_users:
        verified_users[chat_id] = set()
    verified_users[chat_id].add(user_id)
    
    await unrestrict_user(chat_id, user_id)
    await message.answer(f"✅ User has been manually verified and unmuted!")

# --- COMMAND: Reset all verifications ---
@dp.message(Command("reset_verifications"))
async def reset_all(message: types.Message):
    if message.chat.type not in ["group", "supergroup"]:
        return
    
    chat_member = await bot.get_chat_member(message.chat.id, message.from_user.id)
    if chat_member.status not in ["creator", "administrator"]:
        await message.answer("❌ Only admins can use this command!")
        return
    
    chat_id = message.chat.id
    if chat_id in verified_users:
        verified_users[chat_id].clear()
    if chat_id in pending_users:
        pending_users[chat_id].clear()
    
    await message.answer("🔄 All verifications have been reset! New members will need to verify again.")

# --- START BOT ---
async def main():
    print("🤖 Reaction Verification Bot started!")
    print(f"Required reaction: {REACTION_EMOJI}")
    print(f"Timeout: {VERIFICATION_TIME_MINUTES} minutes")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
