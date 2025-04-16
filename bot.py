import os
import discord
import asyncio
import json
import fitz  # PDF Processing (PyMuPDF)
import aiomysql  # Async MySQL Database
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
    ALLOWED_ROLES, MODEL, MAX_TOKEN_LIMIT, MAX_MESSAGES, ENABLE_SUMMARIES, SUMMARY_PROMPT, MAX_HISTORY_DAYS, SYSTEM_INSTRUCTIONS, BATCH_SIZE
)

# --- Globals and State ---
BOT_ROLES = set()
memory_cache = {}
message_history_cache = {}
conversation_summaries = {}
db_pool = None

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
        async with db_pool.acquire() as conn:
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
        async with db_pool.acquire() as conn:
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
                    INSERT INTO user_threads 
                    VALUES (%s, %s, %s, NOW())
                    """
                    await cursor.execute(query, (user_id, user_id, memory_json))
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
    for i in range(0, len(text), max_length):
        chunk = text[i:i + max_length]
        await channel.send(chunk)

# --- Event Handlers ---
@bot.event
async def on_ready():
    global db_pool, BOT_ROLES
    db_pool = await create_db_connection()
    print(f'✅ Logged in as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return
    # For brevity, only allow all users in this refactor (add role checks as needed)
    asyncio.create_task(handle_user_message(message))

async def handle_user_message(message):
    if message.author == bot.user:
        return
    async with message.channel.typing():
        if isinstance(message.channel, discord.DMChannel):
            user_id = str(message.author.id)
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
                        ALLOWED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".rtf", ".png", ".jpg", ".jpeg", ".gif", ".webp"}
                        file_extension = os.path.splitext(filename)[1]
                        if file_extension not in ALLOWED_EXTENSIONS:
                            print(f"⚠️ Unsupported file type: {filename}")
                            await message.channel.send(
                                "⚠️ Unsupported file type detected. Please upload one of the supported formats:\n"
                                "📄 Documents: .pdf, .docx, .xlsx, .txt, .rtf\n"
                                "🖼️ Images: .png, .jpg, .jpeg, .gif, .webp"
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
                                                file_response = await asyncio.to_thread(client.files.create, file=image_file, purpose="vision")
                                                file_id = file_response.id
                                                image_base64 = base64.b64encode(open(image_path, 'rb').read()).decode('utf-8')
                                                image_files.append({
                                                    "type": "image_url", 
                                                    "image_url": {"url": f"data:image/jpeg;base64,{image_base64}"}
                                                })
                                                all_content += f"[Image: {filename}]\n"
                                                print(f"✅ Image successfully saved at: {image_path}")
                                                print(f"✅ Image uploaded to OpenAI. File ID: {file_id}")
                                            except Exception as upload_error:
                                                print(f"❌ OpenAI upload failed: {upload_error}")
                                                await message.channel.send("⚠️ Image upload failed.")
                                                return
                                        os.remove(image_path)
                                    else:
                                        print(f"❌ Image request failed, status code: {resp.status}")
                                        await message.channel.send("⚠️ Image download failed. Please try again.")
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
                                        await message.channel.send("⚠️ File download failed. Please try again.")
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
                                await message.channel.send("⚠️ No readable content found in the file.")
                            os.remove(file_path)
                if not all_content and not image_files:
                    print("❌ No valid input for AI. Skipping processing.")
                    await message.channel.send("⚠️ Please send a message, an image, or a supported file.")
                    return
                user_message = {"role": "user", "content": all_content}
                await manage_conversation_history(user_id, user_message)
                try:
                    memory.put(user_message)
                except Exception as e:
                    print(f"Warning: Could not add message to LlamaIndex memory: {e}")
                messages = []
                messages.append({"role": "system", "content": SYSTEM_INSTRUCTIONS})
                if ENABLE_SUMMARIES and conversation_summaries[user_id]:
                    messages.append({"role": "system", "content": f"Previous conversation summary: {conversation_summaries[user_id]}"})
                messages.extend(message_history_cache[user_id].copy())
                if image_files and messages:
                    for i in range(len(messages) - 1, -1, -1):
                        if messages[i]["role"] == "user":
                            if isinstance(messages[i].get("content"), str):
                                content = [{"type": "text", "text": messages[i]["content"]}]
                                content.extend(image_files)
                                messages[i]["content"] = content
                            break
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
                        await message.channel.send("⚠️ There was an error getting a response. Please try again later.")
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
                else:
                    assistant_reply = "⚠️ No response from the assistant."
                await send_long_message(message.channel, assistant_reply)
            except Exception as e:
                await message.channel.send("⚠️ An error occurred while processing your message.")

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN) 