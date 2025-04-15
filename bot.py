import os
import discord
import requests
import time
import json
import asyncio
import fitz  # ✅ PDF Processing (PyMuPDF)
import aiomysql  # ✅ Async MySQL Database
import openpyxl  # ✅ Excel (.XLSX) Processing
from docx import Document  # ✅ DOCX Processing
from openai import OpenAI
from urllib.parse import urlparse
from dotenv import load_dotenv
from llama_index.core.memory import ChatMemoryBuffer  # ✅ LlamaIndex memory
import base64

# Define the roles that are allowed to use the bot
# Start with Admin role and we'll add the bot's roles dynamically
ALLOWED_ROLES = {"Admin"}  # Admin is always allowed
BOT_ROLES = set()  # Will be populated with the bot's roles and refreshed periodically

# ✅ Configure model
MODEL = "gpt-4o-mini"  # Default model

# ✅ Memory and context settings
MAX_TOKEN_LIMIT = 4000     # Maximum tokens to store in memory
MAX_MESSAGES = 15          # Strict maximum messages to keep per user - never exceeded
ENABLE_SUMMARIES = True    # Set to True to enable conversation summarization
SUMMARY_PROMPT = "Summarize the previous conversation in less than 150 words, focusing on key points the AI should remember:"
MAX_HISTORY_DAYS = 7       # Number of days to keep conversation history

# ✅ System Instructions for the AI
SYSTEM_INSTRUCTIONS = """
Cali's Response Guidelines

### Core Directives
You are Cali. This stands for Creative AI for Learning & Interaction, a highly efficient AI assistant designed to provide concise, accurate, and informative responses while minimizing token usage. Your primary goal is to deliver clear, precise, and to-the-point answers without sacrificing essential information.

###Purpose of Creative Campus (The Discord You Respond in)

-Do not use these words exactly, but this is a welcome message we send to all new discord users so they get an idea of what the community is for. So if someone asks you about Creative Campus or "this discord" in regards to what happens here, you give them a similar message as below:

You've just stepped onto the most dynamic and collaborative campus for real estate investing—Creative Campus! 🎓✨

This is your hub for mastering the SubTo, Top Tier TC, Gator Method, and Owners Club strategies. 💼🏡 Whether you're here to learn 📚, network 🤝, or close deals 💰, you're now part of a prestigious student body dedicated to creative finance and next-level investing.

Note: ⚠️ You will have access to the community you are part of. Please use the email associated with your account to access the server!

### Response Strategy

- Understand the request  
- Accurately interpret the user's question or instruction  
- Identify the key information needed to generate a relevant response  

- Generate a concise and accurate response  
- Keep responses short while maintaining clarity and informativeness  
- Avoid filler words, redundant phrasing, or unnecessary elaboration  
- Use simple, clear language that is easy to understand  

- Ensure readability and usability  
- Structure responses for quick comprehension  
- Prefer short sentences for complex topics  
- When applicable, provide direct answers first, followed by brief explanations if needed  

### Output Format

Standard replies should be one to two sentences unless additional details are necessary.  
Fact-based answers should provide direct, factual responses (e.g., "The capital of France is Paris.").  
Concept explanations should be brief and structured summaries (e.g., "Photosynthesis is how plants use sunlight to convert CO₂ and water into energy.").  

### Examples

**Example 1**  
**User:** What is the capital of Japan?  
**Cali:** Tokyo.  

**Example 2**  
**User:** Explain Newton's First Law of Motion.  
**Cali:** An object at rest stays at rest, and an object in motion stays in motion unless acted upon by an external force.  

**Example 3**  
**User:** How does a solar panel work?  
**Cali:** Solar panels convert sunlight into electricity using photovoltaic cells that generate an electric current when exposed to light.  

### Additional Notes

- Balance brevity with informativeness; keep responses short but meaningful.  
- Prioritize clarity; avoid overly technical jargon unless required.  
- Limit token usage; avoid excessive length while maintaining accuracy.
"""

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# ✅ LlamaIndex memory cache
memory_cache = {}

# ✅ Simple message history storage
message_history_cache = {}
conversation_summaries = {}

# ✅ Memory management functions
def create_new_memory():
    """Create a new LlamaIndex memory buffer"""
    memory = ChatMemoryBuffer.from_defaults(token_limit=MAX_TOKEN_LIMIT)
    return memory

async def get_memory(user_id):
    """Get memory for a user, either from cache or database"""
    global memory_cache, message_history_cache, conversation_summaries, db_pool
    
    # Initialize message history if not exists
    if user_id not in message_history_cache:
        message_history_cache[user_id] = []
    
    # Initialize summary if not exists
    if user_id not in conversation_summaries:
        conversation_summaries[user_id] = ""
    
    # Check if memory is in cache
    if user_id in memory_cache:
        return memory_cache[user_id]
    
    # If not in cache, try to get from database
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT memory_json FROM user_threads WHERE user_id = %s", (user_id,))
                result = await cursor.fetchone()
                
                if result and result[0]:
                    # Try to deserialize message history from JSON
                    try:
                        data = json.loads(result[0])
                        
                        # Check if data has the new format (with summary)
                        if isinstance(data, dict) and "messages" in data and "summary" in data:
                            message_history_cache[user_id] = data["messages"]
                            conversation_summaries[user_id] = data["summary"]
                        else:
                            # Old format - just messages array
                            message_history_cache[user_id] = data
                            conversation_summaries[user_id] = ""
                        
                        print(f"Loaded message history for user {user_id} from database")
                    except Exception as e:
                        print(f"Could not parse message history from database for user {user_id}: {e}")
                        message_history_cache[user_id] = []
                        conversation_summaries[user_id] = ""
                    
                    # Create new memory object
                    memory = create_new_memory()
                else:
                    # Create new memory if not found
                    memory = create_new_memory()
                    message_history_cache[user_id] = []
                    conversation_summaries[user_id] = ""
                
                # Cache the memory
                memory_cache[user_id] = memory
                return memory
    except Exception as e:
        print(f"❌ Error retrieving memory: {e}")
        # Return new memory as fallback
        memory = create_new_memory()
        memory_cache[user_id] = memory
        message_history_cache[user_id] = []
        conversation_summaries[user_id] = ""
        return memory

async def generate_summary(messages):
    """Generate a summary of older messages"""
    if not messages:
        return ""
        
    try:
        # Convert messages to a readable conversation format
        conversation = ""
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            conversation += f"{role.capitalize()}: {content}\n\n"
        
        # Use the OpenAI API to generate a summary
        response = await asyncio.to_thread(
            client.chat.completions.create,
            model=MODEL,
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes conversations concisely."},
                {"role": "user", "content": f"{SUMMARY_PROMPT}\n\n{conversation}"}
            ],
            temperature=0.7,
            max_tokens=200
        )
        
        summary = response.choices[0].message.content
        print(f"Generated conversation summary: {summary[:50]}...")
        return summary
    except Exception as e:
        print(f"❌ Error generating summary: {e}")
        return "Previous conversation could not be summarized."

async def manage_conversation_history(user_id, new_message):
    """Add a message to history and manage the conversation size by enforcing the message cap"""
    global message_history_cache, conversation_summaries
    
    # Add the new message to history
    message_history_cache[user_id].append(new_message)
    
    # Check if we exceed the maximum allowed messages
    if len(message_history_cache[user_id]) > MAX_MESSAGES:
        # Generate summary of the oldest message before removing it
        if ENABLE_SUMMARIES and message_history_cache[user_id]:
            # Get the oldest message that will be removed
            oldest_message = message_history_cache[user_id][0]
            
            # Create a temporary summary message if we need to summarize
            if conversation_summaries[user_id]:
                summary_text = f"Previous summary: {conversation_summaries[user_id]}\n\nOldest message:\n{oldest_message['role']}: {oldest_message['content']}"
            else:
                summary_text = f"Oldest message:\n{oldest_message['role']}: {oldest_message['content']}"
                
            # Generate or update summary for this message
            try:
                if oldest_message['role'] != 'system':  # Don't summarize system messages
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
                    print(f"Updated conversation summary when removing oldest message")
            except Exception as e:
                print(f"❌ Error updating summary: {e}")
        
        # Remove the oldest message to maintain the cap
        message_history_cache[user_id].pop(0)
        print(f"Removed oldest message to maintain cap of {MAX_MESSAGES} messages")

async def save_memory(user_id, memory):
    """Save memory to database and update cache"""
    global memory_cache, message_history_cache, conversation_summaries, db_pool
    
    # Update cache
    memory_cache[user_id] = memory
    
    # Enforce strict message limit before saving
    while len(message_history_cache[user_id]) > MAX_MESSAGES:
        message_history_cache[user_id].pop(0)
        print(f"Enforcing strict message limit of {MAX_MESSAGES} before saving")
    
    # CRITICAL SAFETY CHECK: Remove any potential base64 encoded images from message content
    for msg in message_history_cache[user_id]:
        if isinstance(msg.get("content"), list):
            # Convert multimodal content to simple text
            for item in msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    msg["content"] = item.get("text", "[Content removed due to size]")
                    break
                else:
                    msg["content"] = "[Content removed due to size]"
        
        # Check if content contains base64 data and remove it
        if isinstance(msg.get("content"), str) and "base64" in msg.get("content", ""):
            # Replace the base64 content with a placeholder
            msg["content"] = msg["content"].split("base64,")[0] + "base64,[IMAGE DATA REMOVED]"
    
    # Prepare message history and summary as a dictionary
    memory_data = {
        "messages": message_history_cache[user_id],
        "summary": conversation_summaries[user_id]
    }
    
    # Serialize to JSON
    memory_json = json.dumps(memory_data)
    
    # Calculate approximate size of memory data
    data_size_kb = len(memory_json) / 1024
    print(f"Memory size for user {user_id}: {data_size_kb:.2f} KB")
    
    # Check if memory size is too large (over 250KB)
    MAX_MEMORY_SIZE_KB = 250
    if data_size_kb > MAX_MEMORY_SIZE_KB:
        print(f"⚠️ Memory size exceeds limit ({data_size_kb:.2f}KB > {MAX_MEMORY_SIZE_KB}KB). Trimming conversation.")
        # Reset memory to a clean minimal state
        # If we reached this point, we have a serious memory issue, so take drastic measures
        message_history_cache[user_id] = [{"role": "user", "content": "Let's continue our conversation."}]
        conversation_summaries[user_id] = "Previous conversation was too large and had to be reset."
        
        # Regenerate memory_json with reduced data
        memory_data = {
            "messages": message_history_cache[user_id],
            "summary": conversation_summaries[user_id]
        }
        memory_json = json.dumps(memory_data)
        data_size_kb = len(memory_json) / 1024
        print(f"Reduced memory size for user {user_id}: {data_size_kb:.2f} KB")
    
    # Save to database
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # First check if the user exists in the table
                await cursor.execute("SELECT 1 FROM user_threads WHERE user_id = %s", (user_id,))
                exists = await cursor.fetchone()
                
                if exists:
                    # User exists, do an UPDATE 
                    query = """
                    UPDATE user_threads 
                    SET memory_json = %s, last_used = NOW()
                    WHERE user_id = %s
                    """
                    await cursor.execute(query, (memory_json, user_id))
                else:
                    # User doesn't exist, do an INSERT
                    # We still need to supply all required columns in the current table
                    # but we'll remove this requirement once thread_id is dropped
                    query = """
                    INSERT INTO user_threads 
                    VALUES (%s, %s, %s, NOW())
                    """
                    await cursor.execute(query, (user_id, user_id, memory_json))
                
                await conn.commit()
                print(f"✅ Saved memory for user {user_id}")
    except Exception as e:
        print(f"❌ Failed to save memory: {e}")

# ✅ Function to clear stale memory cache entries
async def reset_memory_cache():
    global memory_cache, db_pool
    while True:
        await asyncio.sleep(3600)  # Check every hour
        
        try:
            async with db_pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    # Get user threads that haven't been used in the last 24 hours
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

# ✅ Establish Async MySQL Connection Pool
async def create_db_connection():
    try:
        pool = await aiomysql.create_pool(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            minsize=1,  # Minimum number of connections in the pool
            maxsize=5   # Maximum number of connections in the pool
        )
        print("✅ Connected to MySQL database (async)")
        return pool
    except Exception as e:
        print(f"❌ Async database connection failed: {e}")
        return None

# ✅ Ensure token_tracking table exists
async def ensure_token_tracking_table():
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # First check if the table already exists
                await cursor.execute("SHOW TABLES LIKE 'token_tracking'")
                table_exists = await cursor.fetchone()
                
                if not table_exists:
                    # Only create the table if it doesn't exist
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
                
                # Check if user_lookup table exists
                await cursor.execute("SHOW TABLES LIKE 'user_lookup'")
                user_lookup_exists = await cursor.fetchone()
                
                if not user_lookup_exists:
                    # Create user_lookup table for mapping user_id to usernames
                    create_lookup_query = """
                    CREATE TABLE user_lookup (
                        user_id VARCHAR(255) PRIMARY KEY,
                        username VARCHAR(255) NOT NULL,
                        display_name VARCHAR(255),
                        last_updated DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                    await cursor.execute(create_lookup_query)
                    await conn.commit()
                    print("✅ User lookup table created")
                else:
                    print("✅ User lookup table already exists")
    except Exception as e:
        print(f"❌ Failed to check/create tables: {e}")

# ✅ Function to log token usage
async def log_token_usage(user_id, model, prompt_tokens, completion_tokens, total_tokens):
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO token_tracking 
                (user_id, model, prompt_tokens, completion_tokens, total_tokens) 
                VALUES (%s, %s, %s, %s, %s)
                """
                # No more thread_id reference
                await cursor.execute(query, (user_id, model, prompt_tokens, completion_tokens, total_tokens))
                await conn.commit()
                print(f"✅ Token usage recorded - Model: {model}, Prompt: {prompt_tokens}, Completion: {completion_tokens}, Total: {total_tokens}")
    except Exception as e:
        print(f"❌ Failed to log token usage: {e}")

# ✅ Global variable for MySQL connection pool
db_pool = None  

# Initialize OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Initialize Discord bot
intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True  # Enable DMs
intents.guilds = True  # Allows bot to see servers it's in
intents.members = True  # Enables fetching member roles
bot = discord.Client(intents=intents)

# Create directories for image & file storage
IMAGE_DIR = "discord-images"
FILE_DIR = "discord-files"
os.makedirs(IMAGE_DIR, exist_ok=True)
os.makedirs(FILE_DIR, exist_ok=True)

# ✅ Function to update username in lookup table
async def update_username_lookup(user_id, username, display_name=None):
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                query = """
                INSERT INTO user_lookup (user_id, username, display_name, last_updated) 
                VALUES (%s, %s, %s, NOW()) AS new_user
                ON DUPLICATE KEY UPDATE 
                    username = new_user.username, 
                    display_name = new_user.display_name,
                    last_updated = NOW()
                """
                await cursor.execute(query, (user_id, username, display_name))
                await conn.commit()
                print(f"✅ Updated username mapping for {username} ({user_id})")
    except Exception as e:
        print(f"❌ Failed to update username lookup: {e}")

# ✅ Function to update bot roles from all servers
async def update_bot_roles():
    global BOT_ROLES
    # Clear existing roles to get a fresh list
    BOT_ROLES.clear()
    
    # Collect all roles the bot has across all servers
    for guild in bot.guilds:
        bot_member = guild.get_member(bot.user.id)
        if bot_member:
            # Add all the bot's roles (except @everyone which everyone has)
            for role in bot_member.roles:
                if role.name != "@everyone":
                    BOT_ROLES.add(role.name)
    
    print(f"✅ Bot roles refreshed: {', '.join(BOT_ROLES) if BOT_ROLES else 'No special roles'}")
    return BOT_ROLES

async def cleanup_oversized_memory():
    """Clean up any oversized memory data in the database"""
    global db_pool
    try:
        async with db_pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # First count how many oversized records we have
                query = """
                SELECT user_id, LENGTH(memory_json)/1024 as size_kb 
                FROM user_threads 
                WHERE LENGTH(memory_json) > 262144
                """  # 256KB = 262144 bytes
                await cursor.execute(query)
                results = await cursor.fetchall()
                
                if results:
                    print(f"⚠️ Found {len(results)} users with oversized memory (>256KB)")
                    for user_id, size_kb in results:
                        print(f"User {user_id}: {size_kb:.2f} KB")
                        
                        # Reset their memory to minimal state
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

@bot.event
async def on_ready():
    global db_pool, BOT_ROLES
    db_pool = await create_db_connection()  # ✅ Create async DB connection
    
    # Initial role collection
    await update_bot_roles()
    
    if db_pool:
        print("✅ Async MySQL connection established.")
        # ✅ Ensure token tracking table exists
        await ensure_token_tracking_table()
        # ✅ Clean up any oversized memory data
        await cleanup_oversized_memory()
        # ✅ Start memory cache cleanup for inactive users
        asyncio.create_task(reset_memory_cache())
    else:
        print("❌ Failed to connect to async MySQL.")

    print(f'✅ Logged in as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    # Check for updated bot roles with each message
    await update_bot_roles()

    # Only process DMs
    if not isinstance(message.channel, discord.DMChannel):
        return  # Ignore messages outside of DMs

    # ✅ Fetch the user's roles from a mutual server
    user_roles = set()
    
    for guild in bot.guilds:  # Check each server the bot is in
        member = guild.get_member(message.author.id)  # Try to get the user
        if member:
            user_roles.update({role.name for role in member.roles})  # Add their roles
            break  # Stop after finding the first mutual server

    # ✅ Check if user has Admin role or any of the bot's roles
    if not (user_roles.intersection(ALLOWED_ROLES) or user_roles.intersection(BOT_ROLES)):
        await message.channel.send("🚫✨ **Access Denied**… for now! I'm still in *beta mode* 🧪 and only certain roles can chat with me right now. \n**Hang tight** — more access is coming soon! 💫🤖")
        return  # Stop further processing

    # ✅ User has permission, proceed with message processing
    asyncio.create_task(handle_user_message(message))  # ✅ Run in background

async def handle_user_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    async with message.channel.typing():  # ✅ Bot shows "typing" indicator
        if isinstance(message.channel, discord.DMChannel):  # Only respond to DMs
            user_id = str(message.author.id)

            # ✅ Update username lookup table
            username = message.author.name
            display_name = getattr(message.author, 'display_name', username)
            await update_username_lookup(user_id, username, display_name)
            
            # ✅ Get user's memory
            memory = await get_memory(user_id)

            try:
                all_content = ""
                image_files = []
                
                # ✅ Handle text messages
                if message.content:
                    print(f"📝 Received text: {message.content}")
                    all_content += message.content + "\n"

                # ✅ Handle images and files
                if message.attachments:
                    for attachment in message.attachments:
                        file_url = attachment.url
                        filename = attachment.filename.lower()
                        parsed_url = urlparse(file_url)

                        # ✅ Check if the file type is supported
                        ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".rtf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
                        file_extension = os.path.splitext(filename)[1]  # Extract the file extension
                        if file_extension not in ALLOWED_EXTENSIONS:
                            print(f"⚠️ Unsupported file type: {filename}")
                            await message.channel.send(
                                "⚠️ Unsupported file type detected. Please upload one of the supported formats:\n"
                                "📄 Documents: .pdf, .docx, .xlsx, .txt, .rtf\n"
                                "🖼️ Images: .png, .jpg, .jpeg, .gif, .webp"
                            )
                            return  # Stop processing this file

                        # ✅ Image Handling
                        if filename.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                            print(f"🔹 Detected image: {file_url}")
                            image_path = os.path.join(IMAGE_DIR, filename)

                            # ✅ Download the image
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                with open(image_path, "wb") as file:
                                    for chunk in response.iter_content(1024):
                                        file.write(chunk)
                                print(f"✅ Image successfully saved at: {image_path}")
                                
                                # ✅ Upload image to OpenAI
                                with open(image_path, "rb") as image_file:
                                    try:
                                        file_response = await asyncio.to_thread(client.files.create, file=image_file, purpose="vision")
                                        file_id = file_response.id
                                        print(f"✅ Image uploaded to OpenAI. File ID: {file_id}")
                                        
                                        # Create base64 image data for CURRENT request only - DO NOT STORE THIS
                                        image_base64 = base64.b64encode(open(image_path, 'rb').read()).decode('utf-8')
                                        
                                        # Add image file reference to temporary list for current API call only
                                        image_files.append({
                                            "type": "image_url", 
                                            "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                                        })
                                        
                                        # IMPORTANT: Don't add the base64 data to the actual memory, just a reference
                                        all_content += f"[Image: {filename}]\n"
                                        
                                    except Exception as upload_error:
                                        print(f"❌ OpenAI upload failed: {upload_error}")
                                        await message.channel.send("⚠️ Image upload failed.")
                                        return
                                os.remove(image_path)  # Clean up the file after processing
                            else:
                                print(f"❌ Image request failed, status code: {response.status_code}")
                                await message.channel.send("⚠️ Image download failed. Please try again.")
                                return

                        # ✅ File Handling (.DOCX, .PDF, .XLSX, .TXT, .RTF)
                        elif filename.endswith((".pdf", ".docx", ".xlsx", ".txt", ".rtf")):  # ✅ Added .RTF support
                            print(f"📄 Detected file: {file_url}")
                            file_path = os.path.join(FILE_DIR, filename)

                            # ✅ Download the file
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                with open(file_path, "wb") as file:
                                    for chunk in response.iter_content(1024):
                                        file.write(chunk)
                                print(f"✅ File successfully saved at: {file_path}")
                            else:
                                print(f"❌ File request failed, status code: {response.status_code}")
                                await message.channel.send("⚠️ File download failed. Please try again.")
                                return

                            # ✅ Extract text from supported files
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
                            elif filename.endswith(".rtf"):  # ✅ NEW RTF SUPPORT
                                from striprtf.striprtf import rtf_to_text
                                with open(file_path, "r", encoding="utf-8", errors="ignore") as rtf_file:
                                    extracted_text = rtf_to_text(rtf_file.read())  # ✅ Convert RTF to plain text
                                    extracted_text = extracted_text.encode("utf-8", "ignore").decode("utf-8")  # ✅ Fix encoding issues

                            # ✅ Add extracted text to content
                            if extracted_text:
                                print(f"✅ Extracted {len(extracted_text)} characters from {filename}")
                                all_content += f"\n[Content from {filename}]:\n{extracted_text}\n"
                            else:
                                print(f"⚠️ No readable content in {filename}")
                                await message.channel.send("⚠️ No readable content found in the file.")
                            os.remove(file_path)
    
                # ✅ Ensure we have valid input
                if not all_content and not image_files:
                    print("❌ No valid input for AI. Skipping processing.")
                    await message.channel.send("⚠️ Please send a message, an image, or a supported file.")
                    return

                # ✅ Add user message to history with message cap enforcement
                user_message = {"role": "user", "content": all_content}
                await manage_conversation_history(user_id, user_message)
                
                # Add to LlamaIndex memory for completeness but don't rely on it
                try:
                    memory.put(user_message)
                except Exception as e:
                    print(f"Warning: Could not add message to LlamaIndex memory: {e}")
                
                # ✅ Prepare messages for OpenAI with potential summary context
                messages = []
                
                # 1. Add system instruction
                messages.append({"role": "system", "content": SYSTEM_INSTRUCTIONS})
                
                # 2. Add summary if available and enabled
                if ENABLE_SUMMARIES and conversation_summaries[user_id]:
                    messages.append({"role": "system", "content": f"Previous conversation summary: {conversation_summaries[user_id]}"})
                
                # 3. Add recent message history
                messages.extend(message_history_cache[user_id].copy())

                # 4. Add images to the user message if needed
                if image_files and messages:
                    # Find the last user message
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i]["role"] == "user":
                            if isinstance(messages[i].get("content"), str):
                                # CRITICAL: Preserve vision capabilities by including base64 images
                                # Convert content to multimodal format
                                content = [{"type": "text", "text": messages[i]["content"]}]
                                # Add each image - THIS LINE IS ESSENTIAL FOR VISION CAPABILITIES
                                content.extend(image_files)
                                messages[i]["content"] = content
                            break
                
                # ✅ Call OpenAI with the full conversation history
                response = None
                async with message.channel.typing():
                    print("⏳ Processing OpenAI request...")
                    try:
                        response = await asyncio.to_thread(
                            client.chat.completions.create,
                            model=MODEL,
                            messages=messages,
                            temperature=0.7
                        )
                        
                        # Store image descriptions if there were images in this request
                        if image_files and all_content:
                            # Get the description only after getting main response
                            # This doesn't affect vision capabilities for the main response
                            try:
                                # Create a separate request just for image descriptions
                                for img_idx, img in enumerate(image_files):
                                    # IMPORTANT: This is a secondary request that won't affect the main vision capabilities
                                    img_description_request = [
                                        {"role": "system", "content": "Describe this image in detail."},
                                        {"role": "user", "content": [
                                            {"type": "text", "text": "Please describe this image in detail:"},
                                            img
                                        ]}
                                    ]
                                    description_response = await asyncio.to_thread(
                                        client.chat.completions.create,
                                        model=MODEL,
                                        messages=img_description_request,
                                        temperature=0.7,
                                        max_tokens=150
                                    )
                                    if description_response and description_response.choices:
                                        description = description_response.choices[0].message.content
                                        print(f"✅ Image {img_idx+1} description: {description[:50]}...")
                                        
                                        # Store image description in memory for followup questions only
                                        # This is invisible to the user but helps AI with context
                                        image_note = {
                                            "role": "system",  # Using system role so it's invisible to user
                                            "content": f"[IMAGE DESCRIPTION (not visible to user): {description}]",
                                            "name": "image_description"  # Adding name for filtering if needed
                                        }
                                        
                                        # Add to memory immediately after the user's message that contained the image
                                        # Find the most recent user message in the history
                                        for i in range(len(message_history_cache[user_id])-1, -1, -1):
                                            if message_history_cache[user_id][i].get("role") == "user":
                                                # Insert after this user message
                                                message_history_cache[user_id].insert(i+1, image_note)
                                                break
                                        
                                        # Save updated memory to database
                                        await save_memory(user_id, message_history_cache[user_id])
                            except Exception as desc_error:
                                # CRITICAL: Don't let description errors affect the main functionality
                                print(f"⚠️ Error getting image description (but main vision still worked): {desc_error}")
                    except Exception as e:
                        print(f"❌ Error calling OpenAI: {e}")
                        await message.channel.send("⚠️ There was an error getting a response. Please try again later.")
                        return

                # ✅ Extract response text
                if response and response.choices and len(response.choices) > 0:
                    assistant_reply = response.choices[0].message.content
                    
                    # ✅ Add assistant response to history with message cap enforcement
                    assistant_message = {"role": "assistant", "content": assistant_reply}
                    await manage_conversation_history(user_id, assistant_message)
                    
                    # Add to LlamaIndex memory but don't rely on it
                    try:
                        memory.put(assistant_message)
                    except Exception as e:
                        print(f"Warning: Could not add assistant message to LlamaIndex memory: {e}")
                    
                    # ✅ Save memory to database
                    await save_memory(user_id, memory)
                    
                    # ✅ Log token usage
                    if hasattr(response, 'usage'):
                        model_used = MODEL
                        prompt_tokens = response.usage.prompt_tokens
                        completion_tokens = response.usage.completion_tokens
                        total_tokens = response.usage.total_tokens
                        
                        await log_token_usage(user_id, model_used, prompt_tokens, completion_tokens, total_tokens)
                else:
                    assistant_reply = "⚠️ No response from the assistant."

                # ✅ Send the AI response in chunks if needed
                await send_long_message(message.channel, assistant_reply)

            except Exception as e:
                print(f"❌ Error: {e}")
                await message.channel.send("⚠️ An error occurred while processing your message.")

# ✅ Function to send long messages in chunks
async def send_long_message(channel, text):
    """ Splits long messages into chunks of 2,000 characters and sends them sequentially. """
    max_length = 2000  # Discord's message limit

    for i in range(0, len(text), max_length):
        chunk = text[i:i + max_length]
        await channel.send(chunk)

# ✅ Async function to close the database connection pool on shutdown
async def close_db_connection():
    global db_pool
    if db_pool:
        db_pool.close()
        await db_pool.wait_closed()
        print("✅ Async database connection closed.")

import signal

async def shutdown():
    print("⏳ Initiating shutdown...")
    if db_pool:
        await close_db_connection()
    print("✅ Shutdown complete. Exiting process now...")
    os._exit(0)  # Force exit

def handle_shutdown():
    asyncio.create_task(shutdown())

signal.signal(signal.SIGTERM, lambda signum, frame: handle_shutdown())
signal.signal(signal.SIGINT, lambda signum, frame: handle_shutdown())

# Add these event handlers to update roles when joining/leaving servers
@bot.event
async def on_guild_join(guild):
    print(f"✅ Joined new server: {guild.name}")
    await update_bot_roles()  # Update roles when joining a new server

@bot.event
async def on_guild_remove(guild):
    print(f"⚠️ Left server: {guild.name}")
    await update_bot_roles()  # Update roles when leaving a server

@bot.event
async def on_member_update(before, after):
    # Check if it's the bot that was updated
    if before.id == bot.user.id:
        # Check if roles were changed
        if set(before.roles) != set(after.roles):
            print("✅ Bot roles changed, refreshing role list")
            await update_bot_roles()

# Run the bot
bot.run(DISCORD_TOKEN)