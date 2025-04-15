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
   pip install discord.py requests python-dotenv openai aiomysql pymupdf python-docx openpyxl llama-index striprtf
   ```
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

Edit these variables at the top of `bot.py`:

```python
# Model selection
MODEL = "gpt-4o-mini"  # Default model

# Memory settings
MAX_TOKEN_LIMIT = 4000  # Maximum tokens to store in memory
MAX_MESSAGES = 15       # Maximum messages to keep per user
ENABLE_SUMMARIES = True # Enable conversation summarization
MAX_HISTORY_DAYS = 7    # Days to keep conversation history

# Access control
ALLOWED_ROLES = {"Admin"}  # Roles allowed to use the bot
```

## Usage

1. Start the bot:
   ```bash
   python main.py
   ```

2. The bot will accept direct messages from users with authorized roles
3. Users can:
   - Send text messages
   - Share images for vision analysis
   - Upload documents for text extraction
   - Ask follow-up questions with context preserved

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