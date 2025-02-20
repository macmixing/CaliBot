import os
import discord
import requests
import time
import json
import asyncio
import fitz  # ✅ PDF Processing (PyMuPDF)
import mysql.connector  # ✅ MySQL Database
from mysql.connector import Error
import openpyxl  # ✅ Excel (.XLSX) Processing
from docx import Document  # ✅ DOCX Processing
from openai import OpenAI
from urllib.parse import urlparse
from dotenv import load_dotenv

# Define the roles that are allowed to use the bot
ALLOWED_ROLES = {"Admin", "Assistant", "Moderator", "Owners Club", "SubTo", "Top Tier TC", "Gator"}

# Load environment variables
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
ASSISTANT_ID = os.getenv("ASSISTANT_ID")
DB_HOST = os.getenv("DB_HOST")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_NAME = os.getenv("DB_NAME")

# ✅ Establish MySQL Connection
def create_db_connection():
    try:
        connection = mysql.connector.connect(
            host=DB_HOST,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        if connection.is_connected():
            print("✅ Connected to MySQL database")
        return connection
    except Error as e:
        print(f"❌ Database connection failed: {e}")
        return None

# ✅ Initialize connection
db_connection = create_db_connection()
db_cursor = db_connection.cursor()

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

# ✅ Function to store user thread ID in MySQL
def save_thread(user_id, thread_id):
    try:
        query = """
        INSERT INTO user_threads (user_id, thread_id) 
        VALUES (%s, %s) 
        ON DUPLICATE KEY UPDATE thread_id = VALUES(thread_id)
        """
        db_cursor.execute(query, (user_id, thread_id))
        db_connection.commit()
        print(f"✅ Stored thread ID for user {user_id}.")
    except Error as e:
        print(f"❌ Failed to store thread ID: {e}")

# ✅ Function to get a user's thread ID from MySQL
def get_thread_id(user_id):
    db_cursor.execute("SELECT thread_id FROM user_threads WHERE user_id = %s", (user_id,))
    result = db_cursor.fetchone()
    return result[0] if result else None

@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

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

    # ✅ If no roles were found, deny access
    if not user_roles.intersection(ALLOWED_ROLES):
        await message.channel.send("Thanks for reaching out! Please contact an admin for further assistance!")
        return  # Stop further processing

    # ✅ User has permission, proceed with message processing
    asyncio.create_task(handle_user_message(message))  # ✅ Run in background
async def handle_user_message(message):
    if message.author == bot.user:
        return  # Ignore bot's own messages

    async with message.channel.typing():  # ✅ Bot shows "typing" indicator

        if isinstance(message.channel, discord.DMChannel):  # Only respond to DMs
            user_id = str(message.author.id)

            user_id = str(message.author.id)

            # ✅ Check if user has an existing thread, or create one
            thread_id = get_thread_id(user_id)

            if not thread_id:
                thread = await asyncio.to_thread(lambda: client.beta.threads.create())
                thread_id = thread.id
                save_thread(user_id, thread_id)  # ✅ Store in MySQL
                print(f"✅ Created new thread for user {user_id}: {thread_id}")
            else:
                print(f"✅ Retrieved existing thread for user {user_id}: {thread_id}")

            try:
                content_data = []
                image_saved = False
                file_saved = False

                # ✅ Handle text messages
                if message.content:
                    print(f"📝 Received text: {message.content}")
                    content_data.append({"type": "text", "text": message.content})

                # ✅ Handle images and files
                if message.attachments:
                    for attachment in message.attachments:
                        file_url = attachment.url
                        filename = attachment.filename.lower()
                        parsed_url = urlparse(file_url)

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
                                image_saved = True
                            else:
                                print(f"❌ Image request failed, status code: {response.status_code}")
                                await message.channel.send("⚠️ Image download failed. Please try again.")
                                return

                            # ✅ Upload image to OpenAI
                            with open(image_path, "rb") as image_file:
                                try:
                                    file_response = await asyncio.to_thread(client.files.create, file=image_file, purpose="vision")
                                    file_id = file_response.id
                                    print(f"✅ Image uploaded to OpenAI. File ID: {file_id}")
                                    content_data.append({"type": "image_file", "image_file": {"file_id": file_id}})

                                    # ✅ Add image to content_data
                                    content_data.append({"type": "image_file", "image_file": {"file_id": file_id}})
                                except Exception as upload_error:
                                    print(f"❌ OpenAI upload failed: {upload_error}")
                                    await message.channel.send("⚠️ Image upload failed.")
                                    return
                            os.remove(image_path)

                        # ✅ File Handling (.DOCX, .PDF, .XLSX)
                        elif filename.endswith((".pdf", ".docx", ".xlsx")):
                            print(f"📄 Detected file: {file_url}")
                            file_path = os.path.join(FILE_DIR, filename)

                            # ✅ Download the file
                            response = requests.get(file_url, stream=True)
                            if response.status_code == 200:
                                with open(file_path, "wb") as file:
                                    for chunk in response.iter_content(1024):
                                        file.write(chunk)
                                print(f"✅ File successfully saved at: {file_path}")
                                file_saved = True
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

                            # ✅ Add extracted text to OpenAI request
                            if extracted_text:
                                print(f"✅ Extracted {len(extracted_text)} characters from {filename}")
                                content_data.append({"type": "text", "text": extracted_text})
                            else:
                                print(f"⚠️ No readable content in {filename}")
                                await message.channel.send("⚠️ No readable content found in the file.")
                            os.remove(file_path)
    
                # ✅ Ensure OpenAI receives valid input
                if not content_data:
                    print("❌ No valid input for AI. Skipping processing.")
                    await message.channel.send("⚠️ Please send a message, an image, or a supported file.")
                    return

                # ✅ Send message + image/file (if available) to OpenAI
                await asyncio.to_thread(client.beta.threads.messages.create, thread_id=thread_id, role="user", content=content_data)

                # ✅ Show typing indicator while processing
                async with message.channel.typing():
                    print("⏳ Processing OpenAI request...")

                        # ✅ Run OpenAI processing FIRST
                await asyncio.to_thread(client.beta.threads.runs.create_and_poll, thread_id=thread_id, assistant_id=ASSISTANT_ID)

                # ✅ Fetch the latest message AFTER processing completes
                messages = await asyncio.to_thread(
                    client.beta.threads.messages.list,
                    thread_id=thread_id,
                    order="desc",
                    limit=1
                        )


                # ✅ Extract AI response
                if messages.data and messages.data[0].role == "assistant":
                    assistant_reply = messages.data[0].content[0].text.value
                else:
                    assistant_reply = "⚠️ No response from the assistant."

                # ✅ Send the AI response in a single message
                await message.channel.send(assistant_reply[:2000])


            except Exception as e:
                print(f"❌ Error: {e}")



# ✅ Close database connection when bot shuts down
import atexit

def close_db_connection():
    if db_connection.is_connected():
        db_cursor.close()
        db_connection.close()
        print("✅ Database connection closed.")

atexit.register(close_db_connection)


# Run the bot
bot.run(DISCORD_TOKEN)
