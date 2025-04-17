import time
import threading
import logging
import schedule
from datetime import datetime, timedelta
import pytz
import json
import sys
import os
import openai
import heapq
from queue import PriorityQueue
from threading import Event
import asyncio
from bot import MAIN_EVENT_LOOP
import inspect

# Add the parent directory to the path to help with imports
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from config import MODEL
from config import get_reminder_notification_prompt
from .db import get_due_reminders, mark_reminder_sent

# Global event for stopping the scheduler
stop_event = Event()

# Add a global messaging function for Discord patching
reminders_send_message = lambda recipient, content, **kwargs: (_ for _ in ()).throw(NotImplementedError('reminders_send_message must be patched by the Discord bot.'))

# Add a global token usage logger for Discord patching
reminders_log_token_usage = lambda user_id, model, prompt_tokens, completion_tokens, purpose=None, chat_guid=None: None

async def run_scheduler_async():
    logging.info("🔔 Reminder scheduler running with 1 second interval (async)")
    while not stop_event.is_set():
        try:
            due_reminders = get_due_reminders()
            if due_reminders:
                for reminder in due_reminders:
                    try:
                        reminder_id = reminder['id']
                        user_id = reminder['user_id']
                        content = reminder['content']
                        # Generate the reminder notification message async
                        notification = await asyncio.to_thread(generate_notification_message, content, user_id)
                        # Send the notification with is_reminder_notification=True
                        send_success = await asyncio.to_thread(reminders_send_message, user_id, notification, is_reminder_notification=True)
                        if send_success:
                            if mark_reminder_sent(reminder_id):
                                logging.info(f"✅ Sent reminder {reminder_id} to {user_id}")
                            else:
                                logging.error(f"❌ Sent reminder {reminder_id} but failed to mark as sent")
                        else:
                            logging.error(f"❌ Failed to send reminder {reminder_id} to {user_id}. Marking as sent to avoid retry loop.")
                            mark_reminder_sent(reminder_id)
                            continue
                        await asyncio.sleep(1)
                    except Exception as e:
                        logging.error(f"❌ Error processing reminder {reminder.get('id', 'unknown')}: {e}")
                        logging.exception("Full traceback:")
            await asyncio.sleep(1)
        except Exception as e:
            logging.error(f"❌ Error in scheduler loop: {e}")
            logging.exception("Full traceback:")
            await asyncio.sleep(1)

def start_reminder_scheduler():
    logging.info("🔔 Starting async reminder scheduler")
    global stop_event
    stop_event.clear()
    loop = asyncio.get_running_loop()
    loop.create_task(run_scheduler_async())
    return True

def generate_notification_message(content, user_id=None):
    """Generate a friendly notification message"""
    try:
        # Prepare the reminder data
        reminder_data = {"content": content}
        # API call to generate notification
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": get_reminder_notification_prompt()},
                {"role": "user", "content": json.dumps(reminder_data)}
            ],
            temperature=0.7,
            max_tokens=100
        )
        # Track token usage (always call as a normal function, never as a coroutine)
        if user_id and hasattr(response, 'usage'):
            try:
                if inspect.iscoroutinefunction(reminders_log_token_usage):
                    import asyncio
                    asyncio.run(reminders_log_token_usage(
                        user_id,
                        MODEL,
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        response.usage.prompt_tokens + response.usage.completion_tokens
                    ))
                else:
                    reminders_log_token_usage(
                        user_id,
                        MODEL,
                        response.usage.prompt_tokens,
                        response.usage.completion_tokens,
                        response.usage.prompt_tokens + response.usage.completion_tokens
                    )
            except Exception as e:
                logging.error(f"❌ Error logging token usage: {e}")
        notification = response.choices[0].message.content.strip()
        return notification
    except Exception as e:
        logging.error(f"❌ Error generating notification: {e}")
        # Fallback notification
        return f"⏰ Reminder: {content}\n(There was an error generating a full AI notification. Please check your OpenAI API setup.)"

def stop_scheduler():
    """Stop the reminder scheduler"""
    global stop_event
    logging.info("🔔 Stopping reminder scheduler")
    stop_event.set() 