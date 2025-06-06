import os
import discord
import asyncio
import json
import fitz  # PDF Processing (PyMuPDF)
import aiomysql  # Async MySQL Database
import reminders.db_pool
from reminders.db_pool import create_db_pool
import openpyxl  # Excel (.XLSX) Processing
from docx import Document  # DOCX Processing
from openai import OpenAI
from urllib.parse import urlparse
from llama_index.core.memory import ChatMemoryBuffer  # LlamaIndex memory
import base64
import aiohttp
import aiofiles
from config import (
    DISCORD_TOKEN, OPENAI_API_KEY, DB_HOST, DB_USER, DB_PASSWORD, DB_NAME,
    ALLOWED_ROLES, MODEL, MAX_TOKEN_LIMIT, MAX_MESSAGES, ENABLE_SUMMARIES, SUMMARY_PROMPT, MAX_HISTORY_DAYS, SYSTEM_INSTRUCTIONS, BATCH_SIZE, IMAGE_ANALYSIS_SYSTEM_PROMPT
)
import signal
import reminders.reminder_handler as reminder_handler  # Add this import at the top
import reminders.scheduler as reminder_scheduler
import reminders.time_handler as reminder_time_handler
from reminders.reminder_handler import AWAITING_LOCATION
import time

# --- Globals and State ---
BOT_ROLES = set()
memory_cache = {}
message_history_cache = {}
conversation_summaries = {}

MAIN_EVENT_LOOP = None  # <-- Add this global

# --- Memory Management ---
def create_new_memory():
    return ChatMemoryBuffer.from_defaults(token_limit=MAX_TOKEN_LIMIT)

async def get_memory(user_id):
    global memory_cache, message_history_cache, conversation_summaries, db_pool
    if user_id not in message_history_cache:
        message_history_cache[user_id] = []
    if user_id not in conversation_summaries:
        conversation_summaries[user_id] = ""
    if user_id in memory_cache:
        return memory_cache[user_id]
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT memory_json FROM user_threads WHERE user_id = %s", (user_id,))
                result = await cursor.fetchone()
                if result and result[0]:
                    try:
                        data = json.loads(result[0])
                        if isinstance(data, dict) and "messages" in data and "summary" in data:
                            message_history_cache[user_id] = data["messages"]
                            conversation_summaries[user_id] = data["summary"]
                        else:
                            message_history_cache[user_id] = data
                            conversation_summaries[user_id] = ""
                        print(f"Loaded message history for user {user_id} from database")
                    except Exception as e:
                        print(f"Could not parse message history from database for user {user_id}: {e}")
                        message_history_cache[user_id] = []
                        conversation_summaries[user_id] = ""
                    memory = create_new_memory()
                else:
                    memory = create_new_memory()
                    message_history_cache[user_id] = []
                    conversation_summaries[user_id] = ""
                memory_cache[user_id] = memory
                return memory
    except Exception as e:
        print(f"❌ Error retrieving memory: {e}")
        memory = create_new_memory()
        memory_cache[user_id] = memory
        message_history_cache[user_id] = []
        conversation_summaries[user_id] = ""
        return memory

async def manage_conversation_history(user_id, new_message):
    global message_history_cache, conversation_summaries
    message_history_cache[user_id].append(new_message)
    # Batch summarization: summarize and remove BATCH_SIZE oldest messages at once
    while len(message_history_cache[user_id]) > MAX_MESSAGES:
        if ENABLE_SUMMARIES and len(message_history_cache[user_id]) > BATCH_SIZE:
            # Get the batch of oldest messages
            batch = message_history_cache[user_id][:BATCH_SIZE]
            # Prepare summary text
            if conversation_summaries[user_id]:
                summary_text = f"Previous summary: {conversation_summaries[user_id]}\n\nBatch of oldest messages:\n"
            else:
                summary_text = "Batch of oldest messages:\n"
            for msg in batch:
                summary_text += f"{msg['role']}: {msg['content']}\n"
            try:
                response = await asyncio.to_thread(
                    client.chat.completions.create,
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that summarizes conversations concisely."},
                        {"role": "user", "content": f"{SUMMARY_PROMPT}\n\n{summary_text}"}
                    ],
                    temperature=0.7,
                    max_tokens=200
                )
                new_summary = response.choices[0].message.content
                conversation_summaries[user_id] = new_summary
                print(f"[BATCH SUMMARY] Summarized and removed {BATCH_SIZE} messages for user {user_id}.")
                print(f"[BATCH SUMMARY] New summary: {new_summary[:80]}...")
                # Log token usage for summary
                if hasattr(response, 'usage'):
                    await log_token_usage(user_id, MODEL, response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
            except Exception as e:
                print(f"❌ Error updating batch summary: {e}")
        # Remove the batch from history
        del message_history_cache[user_id][:BATCH_SIZE]
        print(f"Removed {BATCH_SIZE} oldest messages to maintain cap of {MAX_MESSAGES} messages (batch mode)")

async def save_memory(user_id, memory):
    global memory_cache, message_history_cache, conversation_summaries, db_pool
    memory_cache[user_id] = memory
    while len(message_history_cache[user_id]) > MAX_MESSAGES:
        message_history_cache[user_id].pop(0)
        print(f"Enforcing strict message limit of {MAX_MESSAGES} before saving")
    for msg in message_history_cache[user_id]:
        if isinstance(msg.get("content"), list):
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    msg["content"] = item.get("text", "[Content removed due to size]")
                    break
                else:
                    msg["content"] = "[Content removed due to size]"
        if isinstance(msg.get("content"), str) and "base64" in msg.get("content", ""):
            msg["content"] = msg["content"].split("base64,")[0] + "base64,[IMAGE DATA REMOVED]"
    memory_data = {
        "messages": message_history_cache[user_id],
        "summary": conversation_summaries[user_id]
    }
    memory_json = json.dumps(memory_data)
    data_size_kb = len(memory_json) / 1024
    print(f"Memory size for user {user_id}: {data_size_kb:.2f} KB")
    MAX_MEMORY_SIZE_KB = 250
    if data_size_kb > MAX_MEMORY_SIZE_KB:
        print(f"⚠️ Memory size exceeds limit ({data_size_kb:.2f}KB > {MAX_MEMORY_SIZE_KB}KB). Trimming conversation.")
        message_history_cache[user_id] = [{"role": "user", "content": "Let's continue our conversation."}]
        conversation_summaries[user_id] = "Previous conversation was too large and had to be reset."
        memory_data = {
            "messages": message_history_cache[user_id],
            "summary": conversation_summaries[user_id]
        }
        memory_json = json.dumps(memory_data)
        data_size_kb = len(memory_json) / 1024
        print(f"Reduced memory size for user {user_id}: {data_size_kb:.2f} KB")
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 1 FROM user_threads WHERE user_id = %s", (user_id,))
                exists = await cursor.fetchone()
                if exists:
                    query = """
                    UPDATE user_threads 
                    SET memory_json = %s, last_used = NOW()
                    WHERE user_id = %s
                    """
                    await cursor.execute(query, (memory_json, user_id))
                else:
                    query = """
                    INSERT INTO user_threads (user_id, memory_json) VALUES (%s, %s)
                    """
                    await cursor.execute(query, (user_id, memory_json))
                await conn.commit()
                print(f"✅ Saved memory for user {user_id}")
    except Exception as e:
        print(f"❌ Failed to save memory: {e}")

# --- Database and Bot Setup ---
async def create_db_connection():
    try:
        pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            minsize=1,
            maxsize=5
        )
        print("✅ Connected to MySQL database (async)")
        return pool
    except Exception as e:
        print(f"❌ Async database connection failed: {e}")
        return None

# --- Discord Bot Setup ---
intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True
intents.guilds = True
intents.members = True
bot = discord.Client(intents=intents)

IMAGE_DIR = "discord-images"
FILE_DIR = "discord-files"
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)

# --- OpenAI Client ---
client = OpenAI(api_key=OPENAI_API_KEY)

# --- Utility Functions ---
async def send_long_message(channel, text):
    max_length = 2000
    start = 0
    while start < len(text):
        end = min(start + max_length, len(text))
        if end < len(text):
            split_at = max(text.rfind(p, start, end) for p in ('.', '!', '?', '\n'))
            if split_at > start:
                end = split_at + 1
        await channel.send(text[start:end].strip())
        start = end

# --- Token Usage Tracking ---
async def log_token_usage(user_id, model, prompt_tokens, completion_tokens, total_tokens):
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO token_tracking 
                (user_id, model, prompt_tokens, completion_tokens, total_tokens) 
                VALUES (%s, %s, %s, %s, %s)
                """
                await cursor.execute(query, (user_id, model, prompt_tokens, completion_tokens, total_tokens))
                await conn.commit()
                print(f"✅ Token usage recorded - Model: {model}, Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}")
    except Exception as e:
        print(f"❌ Failed to log token usage: {e}")

# --- User Lookup Table ---
async def update_username_lookup(user_id, username, display_name=None):
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # Step 1: Get current privacy_notified value if exists
                await cursor.execute("SELECT privacy_notified FROM user_lookup WHERE user_id=%s", (user_id,))
                row = await cursor.fetchone()
                privacy_notified = bool(row[0]) if row is not None else False
                # Step 2: Upsert with correct privacy_notified value
                query = """
                INSERT INTO user_lookup (user_id, username, display_name, last_updated, privacy_notified) 
                VALUES (%s, %s, %s, NOW(), %s) AS new_user
                ON DUPLICATE KEY UPDATE 
                    username = new_user.username, 
                    display_name = new_user.display_name,
                    last_updated = NOW(),
                    privacy_notified = new_user.privacy_notified
                """
                await cursor.execute(query, (user_id, username, display_name, privacy_notified))
                await conn.commit()
                print(f"✅ Updated username mapping for {username} ({user_id})")
    except Exception as e:
        print(f"❌ Failed to update username lookup: {e}")

# --- Table Creation/Verification ---
async def ensure_token_tracking_table():
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SHOW TABLES LIKE 'token_tracking'")
                table_exists = await cursor.fetchone()
                if not table_exists:
                    create_table_query = """
                    CREATE TABLE token_tracking (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id VARCHAR(255) NOT NULL,
                        model VARCHAR(255) DEFAULT NULL,
                        prompt_tokens INT NOT NULL,
                        completion_tokens INT NOT NULL,
                        total_tokens INT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                    await cursor.execute(create_table_query)
                    await conn.commit()
                    print("✅ Token tracking table created")
                else:
                    print("✅ Token tracking table already exists")
                await cursor.execute("SHOW TABLES LIKE 'user_lookup'")
                user_lookup_exists = await cursor.fetchone()
                if not user_lookup_exists:
                    create_lookup_query = """
                    CREATE TABLE user_lookup (
                        user_id VARCHAR(255) PRIMARY KEY,
                        username VARCHAR(255) NOT NULL,
                        display_name VARCHAR(255),
                        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
                        privacy_notified BOOLEAN DEFAULT FALSE
                    )
                    """
                    await cursor.execute(create_lookup_query)
                    await conn.commit()
                    print("✅ User lookup table created")
                else:
                    # Ensure privacy_notified column exists
                    try:
                        await cursor.execute("SHOW COLUMNS FROM user_lookup LIKE 'privacy_notified'")
                        col_exists = await cursor.fetchone()
                        if not col_exists:
                            await cursor.execute("ALTER TABLE user_lookup ADD COLUMN privacy_notified BOOLEAN DEFAULT FALSE")
                            await conn.commit()
                            print("✅ Added privacy_notified column to user_lookup table")
                        else:
                            print("✅ privacy_notified column already exists in user_lookup table")
                    except Exception as e:
                        print(f"❌ Failed to ensure privacy_notified column: {e}")
                    print("✅ User lookup table already exists")
    except Exception as e:
        print(f"❌ Failed to check/create tables: {e}")

# --- Role Management ---
async def update_bot_roles():
    global BOT_ROLES
    BOT_ROLES.clear()
    for guild in bot.guilds:
        bot_member = guild.get_member(bot.user.id)
        if bot_member:
            for role in bot_member.roles:
                if role.name != "@everyone":
                    BOT_ROLES.add(role.name)
    print(f"✅ Bot roles refreshed: {', '.join(BOT_ROLES) if BOT_ROLES else 'No special roles'}")
    return BOT_ROLES

# --- Background Tasks ---
async def reset_memory_cache():
    global memory_cache, db_pool
    while True:
        await asyncio.sleep(3600)
        try:
            async with reminders.db_pool.db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    query = """
                    SELECT user_id FROM user_threads 
                    WHERE last_used < NOW() - INTERVAL 24 HOUR
                    """
                    await cursor.execute(query)
                    results = await cursor.fetchall()
                    if results:
                        for user_id in results:
                            user_id = user_id[0]
                            if user_id in memory_cache:
                                del memory_cache[user_id]
                                print(f"🧹 ✅ Removed cached memory for inactive user {user_id}.")
                    else:
                        print("🔍 No stale memory cache entries found.")
        except Exception as e:
            print(f"⚠️ Error during memory cache cleanup: {e}")

async def cleanup_oversized_memory():
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                SELECT user_id, LENGTH(memory_json)/1024 as size_kb 
                FROM user_threads 
                WHERE LENGTH(memory_json) > 262144
                """
                await cursor.execute(query)
                results = await cursor.fetchall()
                if results:
                    print(f"⚠️ Found {len(results)} users with oversized memory (>256KB)")
                    for user_id, size_kb in results:
                        print(f"User {user_id}: {size_kb:.2f} KB")
                        reset_query = """
                        UPDATE user_threads 
                        SET memory_json = %s
                        WHERE user_id = %s
                        """
                        reset_data = json.dumps({
                            "messages": [{"role": "system", "content": "Previous conversation was too large and has been reset."}],
                            "summary": "Memory was reset due to excessive size."
                        })
                        await cursor.execute(reset_query, (reset_data, user_id))
                    await conn.commit()
                    print(f"✅ Reset memory for {len(results)} users with oversized data")
                else:
                    print("✅ No oversized memory data found in database")
    except Exception as e:
        print(f"❌ Failed to clean up memory: {e}")

# --- Shutdown Handling ---
async def close_db_connection():
    
    if reminders.db_pool.db_pool:
        reminders.db_pool.db_pool.close()
        await reminders.db_pool.db_pool.wait_closed()
        print("✅ Async database connection closed.")

async def shutdown():
    print("⏳ Initiating shutdown...")
    if reminders.db_pool.db_pool:
        await close_db_connection()  # This function should also be updated to use reminders.db_pool.db_pool if needed
    print("✅ Shutdown complete. Exiting process now...")
    os._exit(0)

def handle_shutdown():
    asyncio.create_task(shutdown())

signal.signal(signal.SIGTERM, lambda signum, frame: handle_shutdown())
signal.signal(signal.SIGINT, lambda signum, frame: handle_shutdown())

# --- Event Handlers ---
@bot.event
async def on_ready():
    global BOT_ROLES, MAIN_EVENT_LOOP
    MAIN_EVENT_LOOP = asyncio.get_running_loop()
    import traceback
    import sys
    import traceback
    try:
        await create_db_pool()
        print(f"[bot.py] db_pool after create_db_pool: {reminders.db_pool.db_pool} id={id(reminders.db_pool.db_pool)} type={type(reminders.db_pool.db_pool)}", flush=True)
    except Exception as e:
        print(f"❌ Failed to connect to async MySQL: {e}", flush=True)
        import sys
        traceback.print_exc(file=sys.stdout)
        print('--- END TRACEBACK ---', flush=True)
        import os
        import time
        time.sleep(1)
        os._exit(1)
    await update_bot_roles()
    if reminders.db_pool.db_pool:
        print("✅ Async MySQL connection established.")
        await ensure_token_tracking_table()
        await cleanup_oversized_memory()
        asyncio.create_task(reset_memory_cache())
    else:
        print("❌ Failed to connect to async MySQL.")
        import sys
        sys.exit(1)
    print(f'✅ Logged in as {bot.user}')

    # --- REMINDER PATCHING AND SCHEDULER START ---
    def send_discord_dm(recipient, content, **kwargs):
        global MAIN_EVENT_LOOP
        if not (MAIN_EVENT_LOOP and MAIN_EVENT_LOOP.is_running()):
            print(f"❌ MAIN_EVENT_LOOP is not set or not running for recipient {recipient}")
            return False
        user = None
        try:
            if recipient.isdigit():
                user = bot.get_user(int(recipient))
                if not user:
                    future = asyncio.run_coroutine_threadsafe(bot.fetch_user(int(recipient)), MAIN_EVENT_LOOP)
                    user = future.result(timeout=10)
        except Exception as e:
            print(f"❌ Exception fetching user {recipient}: {e}")
            user = None
        if user:
            try:
                # Get the view if it exists in kwargs
                view = kwargs.get('view', None)
                # Pass the view to the send method
                future = asyncio.run_coroutine_threadsafe(user.send(content, view=view), MAIN_EVENT_LOOP)
                future.result(timeout=10)
                return True
            except Exception as e:
                print(f"❌ Exception sending DM to {recipient}: {e}")
                return False
        else:
            print(f"❌ Could not find user for recipient {recipient}")
            return False

    def reminders_log_token_usage(user_id_unused, model, prompt_tokens, completion_tokens, total_tokens):
        if MAIN_EVENT_LOOP and MAIN_EVENT_LOOP.is_running():
            asyncio.run_coroutine_threadsafe(log_token_usage(user_id_unused, model, prompt_tokens, completion_tokens, total_tokens), MAIN_EVENT_LOOP)

    import reminders.reminder_handler as reminder_handler
    import reminders.scheduler as reminder_scheduler
    import reminders.time_handler as reminder_time_handler
    reminder_handler.reminders_send_message = send_discord_dm
    reminder_scheduler.reminders_send_message = send_discord_dm
    reminder_time_handler.reminders_send_message = send_discord_dm
    reminder_handler.reminders_log_token_usage = reminders_log_token_usage
    reminder_scheduler.reminders_log_token_usage = reminders_log_token_usage
    reminder_time_handler.reminders_log_token_usage = reminders_log_token_usage

    # Start the reminder scheduler and log to the console
    reminder_scheduler.start_reminder_scheduler()
    print("✅ Reminder Scheduler running in the background.")
    
    # --- PERSISTENT CANCEL BUTTON HANDLER ---
@bot.event
async def on_interaction(interaction):
        # We only want to handle persistent buttons from previous sessions
        # Current session buttons are handled by their regular callbacks
        if interaction.type == discord.InteractionType.component:
            if interaction.data.get('custom_id', '').startswith('cancel_reminder_'):
                # Store the current time when this handler sees the interaction
                interaction_time = time.time()
                
                # Wait a short time to see if the original handler processes it
                # If the button was created in this session, the original handler will process it
                # If the button is from before restart, the original handler won't exist
                await asyncio.sleep(0.5)
                
                # Check if the interaction has been responded to already by the original handler
                if interaction.response.is_done():
                    # Original handler already processed it, nothing more to do
                    return
                
                # If we get here, it's a persistent button from before restart
                # that doesn't have its callback connected anymore
                try:
                    custom_id = interaction.data['custom_id']
                    reminder_id = int(custom_id.split('_')[2])
                    user_id = str(interaction.user.id)
                    
                    print(f"☑️ Processing persistent button from previous session: reminder {reminder_id} for user {user_id}")
                    
                    # Cancel the reminder
                    from reminders.db import cancel_reminder
                    success = cancel_reminder(reminder_id, user_id)
                    
                    if success:
                        print(f"✅ Successfully cancelled reminder {reminder_id} via persistent button")
                        await interaction.response.send_message("✅ Reminder cancelled successfully!", ephemeral=True)
                    else:
                        print(f"❌ Failed to cancel reminder {reminder_id} via persistent button")
                        await interaction.response.send_message("❌ Failed to cancel reminder. It may have already been cancelled.", ephemeral=True)
                except Exception as e:
                    print(f"❌ Error in persistent cancel button: {e}")
                    await interaction.response.send_message("❌ An error occurred while cancelling the reminder.", ephemeral=True)

@bot.event
async def on_guild_join(guild):
    print(f"✅ Joined new server: {guild.name}")
    await update_bot_roles()

@bot.event
async def on_guild_remove(guild):
    print(f"⚠️ Left server: {guild.name}")
    await update_bot_roles()

@bot.event
async def on_member_update(before, after):
    if before.id == bot.user.id:
        if set(before.roles) != set(after.roles):
            print("✅ Bot roles changed, refreshing role list")
            await update_bot_roles()

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    # Check for updated bot roles with each message
    await update_bot_roles()

    # Only process DMs
    if not isinstance(message.channel, discord.DMChannel):
        return  # Ignore messages outside of DMs
    
    # Fetch the user's roles from mutual servers
    user_roles = set()
    for guild in bot.guilds:  # Check each server the bot is in
        member = guild.get_member(message.author.id)  # Try to get the user
        if member:
            # Add their roles (except @everyone which everyone has)
            for role in member.roles:
                if role.name != "@everyone":
                    user_roles.add(role.name)
    
    # Check if user has Admin role or any of the bot's roles
    if not (user_roles.intersection(ALLOWED_ROLES) or user_roles.intersection(BOT_ROLES)):
        await send_with_privacy("🚫✨ **Access Denied**… for now! \n\n I'm still in *beta mode* 🧪 and only certain roles can chat with me right now. \n\n**Hang tight** — more access is coming soon!")
        return  # Stop further processing
    
    asyncio.create_task(handle_user_message(message))

async def set_privacy_notified(user_id):
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("UPDATE user_lookup SET privacy_notified=TRUE WHERE user_id=%s", (user_id,))
                await conn.commit()
    except Exception as e:
        print(f"❌ Failed to update privacy_notified: {e}")

async def privacy_notice_needed(user_id):
    
    try:
        async with reminders.db_pool.db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT privacy_notified FROM user_lookup WHERE user_id=%s", (user_id,))
                row = await cursor.fetchone()
                if row is None:
                    return True
                return not bool(row[0])
    except Exception as e:
        print(f"❌ Failed to check privacy_notified: {e}")
        return False

async def send_privacy_notice(channel):
    notice = "⚠️ For quality assurance, your recent messages may be stored temporarily to help me remember things about you. 😊 Please don’t share sensitive information."
    async with channel.typing():
        await asyncio.sleep(1)
        await channel.send(notice)

async def handle_user_message(message):
    if message.author == bot.user:
        return
    async with message.channel.typing():
        if isinstance(message.channel, discord.DMChannel):
            user_id = str(message.author.id)
            username = message.author.name
            display_name = getattr(message.author, 'display_name', username)
            await update_username_lookup(user_id, username, display_name)
            privacy_needed = await privacy_notice_needed(user_id)
            
            # ... (rest of your code here)
            
            # At the very end of your DM handling logic, after all responses:
            async def maybe_send_privacy():
                if privacy_needed:
                    await asyncio.sleep(2)
                    await send_privacy_notice(message.channel)
                    await set_privacy_notified(user_id)

            def privacy_wrap_send(func):
                async def wrapped(*args, **kwargs):
                    result = await func(*args, **kwargs)
                    await maybe_send_privacy()
                    return result
                return wrapped

            send_with_privacy = privacy_wrap_send(message.channel.send)
            send_long_with_privacy = privacy_wrap_send(send_long_message)

            
            # Get user's Discord roles
            user_roles = set()
            for guild in bot.guilds:
                member = guild.get_member(message.author.id)
                if member:
                    for role in member.roles:
                        if role.name != "@everyone":
                            user_roles.add(role.name)
            
            # Format roles string
            user_roles_str = ""
            if user_roles:
                user_roles_str = f"\nUser Discord Role(s): {', '.join(sorted(user_roles))}"
            
            # --- LOCATION RESPONSE HANDLING ---
            if user_id in AWAITING_LOCATION and AWAITING_LOCATION[user_id] is not None:
                # IMPORTANT: Only process if there's actual text content
                # Otherwise, let the attachment handler process it for voice messages
                if message.content and message.content.strip():
                    print(f"💬 Processing text location response: '{message.content}'")
                    reminder_handler.process_location_response(message.content, user_id)
                    return
                # If no content (voice message), continue to attachment handling
            # --- REMINDER INTEGRATION START ---
            # Patch reminders_send_message in all reminders modules
            def send_discord_dm(recipient, content, **kwargs):
                # Get the view if it exists in kwargs
                view = kwargs.get('view', None)
                # Pass the view to the send method
                coro = message.channel.send(content, view=view)
                asyncio.run_coroutine_threadsafe(coro, MAIN_EVENT_LOOP)
                return True
            reminder_handler.reminders_send_message = send_discord_dm
            reminder_scheduler.reminders_send_message = send_discord_dm
            reminder_time_handler.reminders_send_message = send_discord_dm
            # Patch reminders_log_token_usage to use our system
            def reminders_log_token_usage(user_id_unused, model, prompt_tokens, completion_tokens, total_tokens):
                if MAIN_EVENT_LOOP and MAIN_EVENT_LOOP.is_running():
                    asyncio.run_coroutine_threadsafe(log_token_usage(user_id_unused, model, prompt_tokens, completion_tokens, total_tokens), MAIN_EVENT_LOOP)
            reminder_handler.reminders_log_token_usage = reminders_log_token_usage
            reminder_scheduler.reminders_log_token_usage = reminders_log_token_usage
            reminder_time_handler.reminders_log_token_usage = reminders_log_token_usage
            # Use the message content for detection
            text = message.content.strip() if message.content else ""
            if text:
                # Respond to timezone help queries (very flexible)
                import re
                # Only show help if NOT a direct change command
                if re.search(r"\bchange\b.*\btime[\s-]?zone\b", text, re.IGNORECASE) and not re.search(r"\bchange\b.*\btime[\s-]?zone\b.*\bto\b.*\w+", text, re.IGNORECASE):
                    await send_with_privacy('🌎 To change your timezone, type "change my timezone to [location]".')
                    return
                # Respond to 'what is my timezone' queries (very flexible)
                from reminders.db import get_user_timezone
                if re.search(r"what('?s| is|\s+is)?\s+(my|the)?\s*time[\s-]?zone( am i in| do i have| is it)?\b", text, re.IGNORECASE):
                    tz = get_user_timezone(user_id) or 'Not set'
                    await send_with_privacy(f'🌎 **Timezone:** {tz}')
                    return
                op_type = reminder_handler.detect_reminder_operation(text, user_id)
                if op_type == 'create':
                    reminder_handler.process_reminder_request(text, user_id)
                    return
                elif op_type == 'list':
                    reminders_list = reminder_handler.process_list_request(user_id)
                    await send_with_privacy(reminders_list)
                    return
                elif op_type == 'cancel':
                    cancel_result = reminder_handler.process_cancel_request(text, user_id)
                    await send_with_privacy(cancel_result)
                    return
                elif op_type == 'location':
                    reminder_handler.process_location_update(text, user_id)
                    return
                elif op_type == 'time':
                    # Get response for time query
                    response, _ = reminder_time_handler.process_time_query(text, user_id)
                    
                    # Only send a response if there is one (empty responses are used for location requests)
                    if response:
                        await send_with_privacy(response)
                    return
            # --- REMINDER INTEGRATION END ---
            memory = await get_memory(user_id)
            try:
                all_content = ""
                image_files = []
                if message.content:
                    print(f"📝 Received text: {message.content}")
                    all_content += message.content + "\n"
                if message.attachments:
                    for attachment in message.attachments:
                        file_url = attachment.url
                        filename = attachment.filename.lower()
                        parsed_url = urlparse(file_url)
                        ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".rtf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ogg"}
                        file_extension = os.path.splitext(filename)[1]
                        if file_extension not in ALLOWED_EXTENSIONS:
                            print(f"⚠️ Unsupported file type: {filename}")
                            await send_with_privacy(
                                "⚠️ Unsupported file type detected. Please upload one of the supported formats:\n"
                                "📄 Documents: .pdf, .docx, .xlsx, .txt, .rtf\n"
                                "🖼️ Images: .png, .jpg, .jpeg, .gif, .webp\n"
                                "🔊 Audio: .ogg"
                            )
                            return
                        if filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            print(f"🔹 Detected image: {file_url}")
                            image_path = os.path.join(IMAGE_DIR, filename)
                            async with aiohttp.ClientSession() as session:
                                async with session.get(file_url) as resp:
                                    if resp.status == 200:
                                        async with aiofiles.open(image_path, "wb") as file:
                                            async for chunk in resp.content.iter_chunked(1024):
                                                await file.write(chunk)
                                        with open(image_path, "rb") as image_file:
                                            try:
                                                image_base64 = base64.b64encode(image_file.read()).decode('utf-8')
                                                image_files.append({
                                                    "type": "image_url",
                                                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                                                })
                                                all_content += f"[Image: {filename}]\n"
                                                print(f"✅ Image successfully saved at: {image_path}")
                                            except Exception as upload_error:
                                                print(f"❌ Image base64 conversion failed: {upload_error}")
                                                await send_with_privacy("⚠️ Image processing failed.")
                                                return
                                        os.remove(image_path)
                                    else:
                                        print(f"❌ Image request failed, status code: {resp.status}")
                                        await send_with_privacy("⚠️ Image download failed. Please try again.")
                                        return
                        elif filename.endswith(".ogg"):
                            print(f"🔊 Detected audio file: {file_url}")
                            # Create a unique filename with timestamp and user ID to prevent collisions
                            unique_filename = f"voice_{user_id}_{int(time.time())}.ogg"
                            file_path = os.path.join(FILE_DIR, unique_filename)
                            async with aiohttp.ClientSession() as session:
                                async with session.get(file_url) as resp:
                                    if resp.status == 200:
                                        async with aiofiles.open(file_path, "wb") as file:
                                            async for chunk in resp.content.iter_chunked(1024):
                                                await file.write(chunk)
                                        print(f"✅ Audio file successfully saved at: {file_path}")
                                    else:
                                        print(f"❌ Audio file request failed, status code: {resp.status}")
                                        await send_with_privacy("⚠️ Audio file download failed. Please try again.")
                                        return
                            
                            # Transcribe the audio file using Whisper API
                            try:
                                with open(file_path, "rb") as audio_file:
                                    transcription = await asyncio.to_thread(
                                        client.audio.transcriptions.create,
                                        file=audio_file,
                                        model="whisper-1",
                                        timeout=30.0
                                    )
                                    transcribed_text = transcription.text
                                    print(f"✅ Transcribed {len(transcribed_text)} characters from audio file")
                                    
                                    # Log token usage for the transcription
                                    await log_token_usage(user_id, "whisper-1", 0, len(transcribed_text.split()), len(transcribed_text.split()))
                                    
                                    # Clean up the file
                                    os.remove(file_path)
                                    print(f"✅ Deleted audio file: {file_path}")

                                    # COMPLETELY DIFFERENT APPROACH: Instead of creating a SimpleMessage and recursively calling,
                                    # simply modify the original message's content and continue with the SAME handle_user_message function
                                    print(f"✅ Processing transcribed voice message: '{transcribed_text}'")
                                    
                                    # Replace the message content with transcribed text
                                    message.content = transcribed_text
                                    
                                    # Reset the function flow to the beginning of text processing
                                    # This will process the message through the exact same pipeline as typed text
                                    if isinstance(message.channel, discord.DMChannel):
                                        # Skip username/lookup as we already did it
                                        # Go directly to the location check
                                        if user_id in AWAITING_LOCATION and AWAITING_LOCATION[user_id] is not None:
                                            print(f"🌎 Processing transcribed location: {transcribed_text}")
                                            reminder_handler.process_location_response(transcribed_text, user_id)
                                            return
                                            
                                        # Rest of the reminder processing
                                        if text := message.content.strip():
                                            import re
                                            # Only show help if NOT a direct change command
                                            if re.search(r"\bchange\b.*\btime[\s-]?zone\b", text, re.IGNORECASE) and not re.search(r"\bchange\b.*\btime[\s-]?zone\b.*\bto\b.*\w+", text, re.IGNORECASE):
                                                await send_with_privacy('🌎 To change your timezone, type "change my timezone to [location]".')
                                                return
                                            from reminders.db import get_user_timezone
                                            if re.search(r"what('?s| is|\s+is)?\s+(my|the)?\s*time[\s-]?zone( am i in| do i have| is it)?\b", text, re.IGNORECASE):
                                                tz = get_user_timezone(user_id) or 'Not set'
                                                await send_with_privacy(f'🌎 **Timezone:** {tz}')
                                                return
                                            op_type = reminder_handler.detect_reminder_operation(text, user_id)
                                            if op_type == 'create':
                                                reminder_handler.process_reminder_request(text, user_id)
                                                return
                                            elif op_type == 'list':
                                                reminders_list = reminder_handler.process_list_request(user_id)
                                                await send_with_privacy(reminders_list)
                                                return
                                            elif op_type == 'cancel':
                                                cancel_result = reminder_handler.process_cancel_request(text, user_id)
                                                await send_with_privacy(cancel_result)
                                                return
                                            elif op_type == 'location':
                                                reminder_handler.process_location_update(text, user_id)
                                                return
                                            elif op_type == 'time':
                                                response, _ = reminder_time_handler.process_time_query(text, user_id)
                                                if response:
                                                    await send_with_privacy(response)
                                                return
                                                
                                        # If we get here, it's not a reminder operation
                                        # Proceed with normal conversation processing
                                        memory = await get_memory(user_id)
                                        all_content = ""
                                        if message.content:
                                            print(f"📝 Processing transcribed text: {message.content}")
                                            all_content += message.content + "\n"
                                            
                                        # Continue with normal conversation processing
                                        # (Note: We're bypassing all the file attachment handling since we already did that)
                                        user_message = {"role": "user", "content": all_content}
                                        await manage_conversation_history(user_id, user_message)
                                        try:
                                            memory.put(user_message)
                                        except Exception as e:
                                            print(f"Warning: Could not add message to LlamaIndex memory: {e}")
                                        messages = []
                                        messages.append({"role": "system", "content": SYSTEM_INSTRUCTIONS + user_roles_str})
                                        if ENABLE_SUMMARIES and conversation_summaries[user_id]:
                                            messages.append({"role": "system", "content": f"Previous conversation summary: {conversation_summaries[user_id]}"})
                                        messages.extend(message_history_cache[user_id].copy())
                                        
                                        # Rest of normal message processing
                                        response = None
                                        async with message.channel.typing():
                                            try:
                                                response = await asyncio.to_thread(
                                                    client.chat.completions.create,
                                                    model=MODEL,
                                                    messages=messages,
                                                    temperature=0.7
                                                )
                                            except Exception as e:
                                                await send_with_privacy("⚠️ There was an error getting a response. Please try again later.")
                                                return
                                        if response and response.choices and len(response.choices) > 0:
                                            assistant_reply = response.choices[0].message.content
                                            assistant_message = {"role": "assistant", "content": assistant_reply}
                                            await manage_conversation_history(user_id, assistant_message)
                                            try:
                                                memory.put(assistant_message)
                                            except Exception as e:
                                                print(f"Warning: Could not add assistant message to LlamaIndex memory: {e}")
                                            await save_memory(user_id, memory)
                                            await log_token_usage(user_id, MODEL, response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
                                        else:
                                            assistant_reply = "⚠️ No response from the assistant."
                                        await send_long_with_privacy(message.channel, assistant_reply)
                                    return
                            except Exception as e:
                                print(f"❌ Audio transcription failed: {e}")
                                await send_with_privacy("⚠️ Audio transcription failed. Please try again.")
                                if os.path.exists(file_path):
                                    os.remove(file_path)
                                return
                        elif filename.endswith((".pdf", ".docx", ".xlsx", ".txt", ".rtf")):
                            print(f"📄 Detected file: {file_url}")
                            file_path = os.path.join(FILE_DIR, filename)
                            async with aiohttp.ClientSession() as session:
                                async with session.get(file_url) as resp:
                                    if resp.status == 200:
                                        async with aiofiles.open(file_path, "wb") as file:
                                            async for chunk in resp.content.iter_chunked(1024):
                                                await file.write(chunk)
                                    else:
                                        print(f"❌ File request failed, status code: {resp.status}")
                                        await send_with_privacy("⚠️ File download failed. Please try again.")
                                        return
                            extracted_text = None
                            if filename.endswith(".pdf"):
                                with fitz.open(file_path) as pdf:
                                    extracted_text = "\n".join([page.get_text() for page in pdf])
                            elif filename.endswith(".docx"):
                                doc = Document(file_path)
                                extracted_text = "\n".join([para.text for para in doc.paragraphs])
                            elif filename.endswith(".xlsx"):
                                workbook = openpyxl.load_workbook(file_path)
                                extracted_text = "\n".join(
                                    [str(cell.value) for sheet in workbook.worksheets for row in sheet.iter_rows() for cell in row if cell.value]
                                )
                            elif filename.endswith(".txt"):
                                with open(file_path, "r", encoding="utf-8") as txt_file:
                                    extracted_text = txt_file.read()
                            elif filename.endswith(".rtf"):
                                from striprtf.striprtf import rtf_to_text
                                with open(file_path, "r", encoding="utf-8", errors="ignore") as rtf_file:
                                    extracted_text = rtf_to_text(rtf_file.read())
                                    extracted_text = extracted_text.encode("utf-8", "ignore").decode("utf-8")
                            if extracted_text:
                                print(f"✅ Extracted {len(extracted_text)} characters from {filename}")
                                all_content += f"\n[Content from {filename}]:\n{extracted_text}\n"
                            else:
                                await send_with_privacy("⚠️ No readable content found in the file.")
                            os.remove(file_path)
                if not all_content and not image_files:
                    print("❌ No valid input for AI. Skipping processing.")
                    await send_with_privacy("⚠️ Please send a message, an image, or a supported file.")
                    return
                # IMAGE FLOW: If we have at least one image, use the new image analysis pipeline
                if image_files:
                    # Build multimodal content (text + all images)
                    multimodal_content = []
                    if all_content.strip():
                        multimodal_content.append({"type": "text", "text": all_content.strip()})
                    multimodal_content.extend(image_files)
                    user_message = {"role": "user", "content": multimodal_content}
                    # Build the system prompt and message list
                    messages = []
                    messages.append({"role": "system", "content": IMAGE_ANALYSIS_SYSTEM_PROMPT})
                    # Add previous chat history (excluding system prompts and fake image URLs)
                    for msg in message_history_cache[user_id]:
                        if msg.get("role") == "system":
                            continue
                        content = msg.get("content")
                        # Skip fake image URLs or system image descriptions
                        if (
                            isinstance(content, list)
                            and any(
                                item.get("type") == "image_url" and (
                                    "[Image:" in item.get("image_url", {}).get("url", "") or
                                    item.get("image_url", {}).get("url", "").startswith("[Image")
                                )
                                for item in content if isinstance(item, dict)
                            )
                        ):
                            # Convert to plain text summary if possible
                            text = None
                            for item in content:
                                if isinstance(item, dict) and item.get("type") == "text":
                                    text = item.get("text")
                                    break
                            if not text:
                                for item in content:
                                    if isinstance(item, dict) and item.get("type") == "image_url":
                                        text = item.get("image_url", {}).get("url")
                                        break
                            if text:
                                messages.append({"role": msg.get("role", "user"), "content": text})
                        else:
                            messages.append(msg)
                    # Add the new user message (with image and/or prompt)
                    messages.append(user_message)
                    # Call the LLM
                    import re
                    response = await asyncio.to_thread(
                        client.chat.completions.create,
                        model=MODEL,
                        messages=messages,
                        temperature=0.7
                    )
                    response_text = response.choices[0].message.content
                    # Log token usage for image analysis
                    if hasattr(response, 'usage'):
                        await log_token_usage(
                            user_id,
                            MODEL,
                            getattr(response.usage, 'prompt_tokens', 0),
                            getattr(response.usage, 'completion_tokens', 0),
                            getattr(response.usage, 'total_tokens', 0)
                        )
                    # Parse JSON for detailed_description and prompt_response
                    try:
                        match = re.search(r'\{.*\}', response_text, re.DOTALL)
                        if match:
                            json_str = match.group(0)
                            json_str = re.sub(r',\s*}', '}', json_str)
                            json_str = re.sub(r',\s*]', ']', json_str)
                            parsed = json.loads(json_str)
                            detailed_description = parsed.get("detailed_description")
                            prompt_response = parsed.get("prompt_response")
                        else:
                            raise ValueError("No JSON object found in LLM response")
                    except Exception as e:
                        print(f"[ImageAnalysis] Failed to parse LLM JSON response: {e}")
                        print(f"[ImageAnalysis] Raw LLM response: {response_text}")
                        detailed_description = None
                        prompt_response = None
                    # Add the multimodal message to memory
                    await manage_conversation_history(user_id, user_message)
                    # Add the detailed description as a plain user message for context
                    if detailed_description:
                        await manage_conversation_history(user_id, {"role": "assistant", "content": f"[Image description: {detailed_description}]"})
                    # Add the prompt_response as an assistant message for context
                    if prompt_response:
                        await manage_conversation_history(user_id, {"role": "assistant", "content": prompt_response})
                    await save_memory(user_id, memory)
                    # Send the appropriate response to the user
                    if all_content.strip() and prompt_response:
                        await send_long_with_privacy(message.channel, prompt_response)
                    elif detailed_description:
                        await send_long_with_privacy(message.channel, detailed_description)
                    else:
                        await send_with_privacy("I couldn't analyze that image. Please try again with a clearer image.")
                    return
                # TEXT/DOC FLOW: If we get here, it's a text/file message (no images)
                user_message = {"role": "user", "content": all_content}
                await manage_conversation_history(user_id, user_message)
                try:
                    memory.put(user_message)
                except Exception as e:
                    print(f"Warning: Could not add message to LlamaIndex memory: {e}")
                messages = []
                messages.append({"role": "system", "content": SYSTEM_INSTRUCTIONS + user_roles_str})
                if ENABLE_SUMMARIES and conversation_summaries[user_id]:
                    messages.append({"role": "system", "content": f"Previous conversation summary: {conversation_summaries[user_id]}"})
                messages.extend(message_history_cache[user_id].copy())
                response = None
                async with message.channel.typing():
                    try:
                        response = await asyncio.to_thread(
                            client.chat.completions.create,
                            model=MODEL,
                            messages=messages,
                            temperature=0.7
                        )
                    except Exception as e:
                        await send_with_privacy("⚠️ There was an error getting a response. Please try again later.")
                        return
                if response and response.choices and len(response.choices) > 0:
                    assistant_reply = response.choices[0].message.content
                    assistant_message = {"role": "assistant", "content": assistant_reply}
                    await manage_conversation_history(user_id, assistant_message)
                    try:
                        memory.put(assistant_message)
                    except Exception as e:
                        print(f"Warning: Could not add assistant message to LlamaIndex memory: {e}")
                    await save_memory(user_id, memory)
                    await log_token_usage(user_id, MODEL, response.usage.prompt_tokens, response.usage.completion_tokens, response.usage.total_tokens)
                else:
                    assistant_reply = "⚠️ No response from the assistant."
                await send_long_with_privacy(message.channel, assistant_reply)
            except Exception as e:
                await send_with_privacy("⚠️ An error occurred while processing your message.")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN) 