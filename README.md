# Cali Discord AI Assistant

A powerful Discord bot featuring GPT-4o vision capabilities, persistent conversation memory, and document processing.

## Features

- **AI Conversations**: Uses OpenAI's GPT-4o-mini model to provide helpful responses
- **Vision Capabilities**: Processes and understands images shared in chat
- **Memory System**: Maintains conversation context between sessions using a MySQL database
- **Document Processing**: Extracts text from multiple file types:
  - PDF, DOCX, XLSX, TXT, RTF
- **Image Support**: Handles PNG, JPG, JPEG, GIF, WebP
- **Role-based Access Control**: Restricts usage to authorized Discord roles
- **Token Usage Tracking**: Monitors API consumption for usage metrics
- **Automatic Summarization**: Creates summaries of older messages to maintain context
- **Invisible Image Descriptions**: Generates and stores image descriptions for follow-up questions
- **Step-by-step Logging**: All process steps are logged via print statements (see Logging section)
- **Reminders System**: Set, list, and cancel reminders with natural language (see below)

## Reminders Functionality

Cali can set, list, and cancel reminders for you using natural language. Reminders are delivered via Discord DM at the specified time.

### Setting a Reminder
- **Examples:**
  - `Remind me to call mom in 5 minutes`
  - `Set a reminder for tomorrow at 8pm to take medicine`
  - `Don't let me forget to submit the report next Monday at 2pm`
- **Supported time formats:**
  - Relative: `in 10 minutes`, `in 2 hours`, `in 3 days`
  - Absolute: `at 8pm`, `at 3:30pm`, `tomorrow at 9am`, `next Friday at 2pm`, `on April 15th at 3pm`
  - Vague: `in the morning` (interpreted as 9:00 AM), `later` (in 2 hours), etc.
- **Timezones:**
  - By default, reminders use your last set or default timezone (UTC if not set).
  - You can update your timezone by messaging: `Change my timezone to Pacific`.

### Listing Your Reminders
- **Examples:**
  - `Show my reminders`
  - `What are my reminders?`
  - `Reminders?`
- Cali will reply with a list of your pending reminders, grouped by today, tomorrow, and future dates.

### Cancelling Reminders
- **Examples:**
  - `Cancel my reminder about calling mom`
  - `Cancel today's reminders`
  - `Cancel all my reminders`
  - `Cancel that` (cancels the most recent reminder)
- You can cancel by content, by time period (today/tomorrow/all), or all at once.

### Notes & Limitations
- **One reminder per message:** If you ask for multiple reminders in one message, Cali will ask you to set them one at a time.
- **Unsupported formats:** Location-based, event-based, conditional, and recurring reminders are not supported (e.g., `when I get home`, `every day at 8am`).
- **Time format:** Times are standardized to 12-hour format with AM/PM. Vague times are interpreted to the nearest reasonable time.
- **Maximum reminders:** There is no hard-coded limit, but performance is optimized for typical user loads.
- **Timezone support:** Cali can convert and store reminders in your preferred timezone. If not set, UTC is used.

### Example Usage
```
Remind me to drink water in 30 minutes
Remind me to call John at 8:00 PM
Show my reminders
Cancel my reminder about water
Change my timezone to Eastern Time
```

## Setup

### Prerequisites

- Python 3.8+
- MySQL database
- Discord bot token
- OpenAI API key

### Installation

1. Clone the repository
2. Install dependencies:
   ```bash
   pip install discord.py python-dotenv openai aiomysql pymupdf python-docx openpyxl llama-index striprtf aiohttp aiofiles
   ```
   > **Note:** `requests` is no longer required.
3. Create a `.env` file with the following:
   ```
   DISCORD_TOKEN=your_discord_token
   OPENAI_API_KEY=your_openai_api_key
   DB_HOST=localhost
   DB_USER=db_username
   DB_PASSWORD=db_password
   DB_NAME=db_name
   ```

### Database Setup

The bot automatically creates the required tables on first run:
- `user_threads`: Stores conversation memory
- `token_tracking`: Monitors token usage
- `user_lookup`: Maps Discord user IDs to usernames

## Configuration

All configuration is now in `config.py`:

```python
# config.py
MODEL = "gpt-4o-mini"  # Default model
MAX_TOKEN_LIMIT = 4000  # Maximum tokens to store in memory
MAX_MESSAGES = 15       # Maximum messages to keep per user
ENABLE_SUMMARIES = True # Enable conversation summarization
MAX_HISTORY_DAYS = 7    # Days to keep conversation history
ALLOWED_ROLES = {"Admin"}  # Roles allowed to use the bot
```

You can also edit the system prompt and other settings in `config.py`.

## Usage

1. Start the bot:
   ```bash
   python bot.py
   ```
   > **Note:** The main entry point is now `bot.py` (not `main.py`).

2. The bot will accept direct messages from users with authorized roles
3. Users can:
   - Send text messages
   - Share images for vision analysis
   - Upload documents for text extraction
   - Ask follow-up questions with context preserved

## Logging

- All process steps are logged using `print()` statements.
- If you run the bot with output redirection or as a service, logs will appear in `bot.log` (e.g., `python bot.py >> bot.log 2>&1` or via your process manager).
- This provides detailed, step-by-step visibility into the bot's operation.

## How It Works

- **Vision Processing**: Images are temporarily processed with base64 encoding for the current request only
- **Memory Management**: Conversation history enforces strict message limits and removes base64 data
- **Image Descriptions**: Generated invisibly after processing to provide context for follow-ups
- **Database Storage**: JSON-formatted conversation history with summaries

## Limitations

- Maximum 20 messages stored per user conversation
- Maximum memory size of 250KB per user
- Messages over token limits are summarized and older ones removed

#### 📚 Acknowledgements

This project uses the following open-source libraries:

- [discord.py](https://github.com/Rapptz/discord.py) (MIT License) — Discord API wrapper for bots.
- [python-dotenv](https://github.com/theskumar/python-dotenv) (BSD-3-Clause License) — Loads environment variables from `.env` files.
- [openai](https://github.com/openai/openai-python) (MIT License) — Python client for OpenAI APIs.
- [aiomysql](https://github.com/aio-libs/aiomysql) (MIT License) — Async support for MySQL with asyncio.
- [PyMuPDF](https://github.com/pymupdf/PyMuPDF) (AGPL v3 License) — Used for internal PDF parsing only; not redistributed or modified.
- [python-docx](https://github.com/python-openxml/python-docx) (MIT License) — Extracts content from Word `.docx` files.
- [openpyxl](https://github.com/pyexcel/openpyxl) (MIT License) — Reads and writes Excel `.xlsx` files.
- [llama-index](https://github.com/jerryjliu/llama_index) (MIT License) — Framework for connecting LLMs with external data.
- [striprtf](https://github.com/Alir3z4/striprtf) (MIT License) — Converts RTF content to plain text.
- [aiohttp](https://github.com/aio-libs/aiohttp) (Apache 2.0 License) — Async HTTP client/server framework.
- [aiofiles](https://github.com/Tinche/aiofiles) (Apache 2.0 License) — Async file handling with asyncio.
- [pytz](https://github.com/stub42/pytz) (MIT License) — Timezone support for Python.
- [python-dateutil](https://github.com/dateutil/dateutil) (BSD License) — Enhanced datetime parsing and timezone calculations.

> Note: PyMuPDF (AGPL v3) is used strictly for internal backend parsing of PDF content and is not redistributed or modified in this project.

