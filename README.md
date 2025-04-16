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

- Maximum 15 messages stored per user conversation
- Maximum memory size of 250KB per user
- Messages over token limits are summarized and older ones removed

## License

[Your license information] 