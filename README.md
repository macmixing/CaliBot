# Discord AI Chatbot

## Overview
This is a Discord chatbot that integrates OpenAI's assistant API with MySQL for tracking user interactions. The bot allows specific roles to interact with it via Direct Messages (DMs) and supports text, image, and file processing. Files are stored temporarily lcoally and deleted after a response is sent. It's lightweight and easy to set up!

## Features
- ✅ AI-powered responses using OpenAI's Assistant API
- ✅ Supports text, image, and document uploads
- ✅ Role-based access control
- ✅ MySQL integration for tracking user threads
- ✅ Runs automatically on startup
- ✅ Logs all activity for debugging

## Requirements

Before setting up the bot, ensure you have the following installed:

- Python 3.9+
- `pip` (Python package manager)
- `git` (for version control)
- `MySQL Server 8.0+`
- A Discord Developer Account
- Discord bot token
- An OpenAI API key

## Setup Instructions

### 1️⃣ Clone the Repository
```bash
git clone git@github.com:macmixing/discordgpt.git
cd discordgpt
```

### 2️⃣ Create a Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate  # Mac/Linux
venv\Scripts\activate    # Windows
```

### 3️⃣ Install Dependencies
```bash
pip install -r requirements.txt
```

### 4️⃣ Set Up the `.env` File
Create a `.env` file in the root directory and add the following:
```ini
DISCORD_TOKEN=your_discord_bot_token
OPENAI_API_KEY=your_openai_api_key
ASSISTANT_ID=your_assistant_id
DB_HOST=localhost
DB_USER=your_mysql_user
DB_PASSWORD=your_mysql_password
DB_NAME=your_database_name
```

> **Note:** Never commit `.env` files to GitHub to protect sensitive credentials.

### 5️⃣ Set Up MySQL Database
Log into MySQL and create the database and necessary table:
```sql
CREATE DATABASE chatbot_db;
USE chatbot_db;
CREATE TABLE user_threads (
    user_id VARCHAR(50) PRIMARY KEY,
    thread_id VARCHAR(255) NOT NULL
);
```

### 6️⃣ Run the Bot
```bash
nohup bash -c 'source venv/bin/activate && python -u bot.py' > bot.log 2>&1 &
```
This runs the bot in the background and logs output in `bot.log`.

### 7️⃣ Check Logs in Real Time
```bash
tail -f bot.log
```

### 8️⃣ Stop the Bot
Find the process ID (PID):
```bash
ps aux | grep bot.py
```
Then kill the process:
```bash
kill -9 PID
```

### 9️⃣ Ensure MySQL and the Bot Start on Reboot
Enable MySQL on startup:
```bash
sudo systemctl enable mysqld
```
Enable the bot on startup:
```bash
sudo systemctl enable discord-bot
```

### 🔄 Updating the Bot from GitHub
If changes are made to GitHub, update the server with:
```bash
git pull origin main
```
Then restart the bot:
```bash
sudo systemctl restart discord-bot
```

## Notes
- If the bot crashes, it will automatically restart.
- Ensure that `.gitignore` includes `.env` and `venv/`.
- To modify role permissions, edit `ALLOWED_ROLES` in `bot.py`.

## Contributing
Feel free to submit pull requests to improve the bot!

