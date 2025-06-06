import logging
import json
import openai
from datetime import datetime, timedelta
import pytz
from dateutil import parser
from dateutil.relativedelta import relativedelta
import sys
import os
import re
import discord
from discord.ui import Button, View

# Add the parent directory to the path to help with imports
current_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if current_dir not in sys.path:
    sys.path.append(current_dir)

from config import MODEL
from config import (
    get_reminder_detection_prompt,
    get_reminder_extraction_prompt,
    get_timezone_extraction_prompt,
    get_reminder_operation_detection_prompt,
    get_reminder_cancellation_extraction_prompt,
    get_current_date_formatted
)
from .db import (
    save_reminder,
    get_user_timezone,
    update_user_timezone,
    get_user_reminders,
    get_reminder_by_content,
    get_last_created_reminder,
    cancel_reminder,
    get_any_reminder_timezone
)

# Conversation state tracking
# Conversation state tracking
from collections import defaultdict
from datetime import datetime, timedelta

class TimeoutDict(defaultdict):
    def __init__(self):
        super().__init__(dict)
        self.timeouts = {}
    
    def __getitem__(self, key):
        if key in self.timeouts:
            if datetime.now() - self.timeouts[key] > timedelta(seconds=20):
                del self[key]
                del self.timeouts[key]
                return None
        return super().__getitem__(key)
    
    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.timeouts[key] = datetime.now()

AWAITING_LOCATION = TimeoutDict()  # user_id -> reminder_data with 10s timeout

# Add a global messaging function for Discord patching
reminders_send_message = lambda recipient, content, **kwargs: (_ for _ in ()).throw(NotImplementedError('reminders_send_message must be patched by the Discord bot.'))

# Add a global token usage logger for Discord patching
reminders_log_token_usage = lambda user_id, model, prompt_tokens, completion_tokens, total_tokens: None

# Custom button for cancelling reminders
class CancelReminderButton(Button):
    def __init__(self, reminder_id):
        # Ensure reminder_id is a proper integer
        try:
            reminder_id_int = int(reminder_id)
            custom_id = f"cancel_reminder_{reminder_id_int}"
            logging.info(f"⏰ Creating cancel button with ID: {reminder_id_int}")
        except (ValueError, TypeError):
            logging.error(f"❌ Invalid reminder ID for button: {reminder_id}")
            custom_id = "cancel_reminder_invalid"
            
        super().__init__(
            style=discord.ButtonStyle.secondary,  # Grey button (more subtle than red)
            label="Cancel Reminder",
            custom_id=custom_id
        )
        
    async def callback(self, interaction):
        try:
            user_id = str(interaction.user.id)
            
            # Extract reminder ID from custom_id
            custom_id_parts = self.custom_id.split('_')
            if len(custom_id_parts) != 3 or custom_id_parts[0] != "cancel" or custom_id_parts[1] != "reminder":
                logging.error(f"❌ Invalid custom_id format: {self.custom_id}")
                await interaction.response.send_message("❌ Invalid reminder format. Please try cancelling manually.", ephemeral=True)
                return
                
            reminder_id_str = custom_id_parts[2]
            if reminder_id_str == "invalid":
                logging.error(f"❌ Button has invalid reminder ID")
                await interaction.response.send_message("❌ This button has an invalid reminder ID. Please try cancelling manually.", ephemeral=True)
                return
                
            try:
                reminder_id = int(reminder_id_str)
            except ValueError:
                logging.error(f"❌ Cannot convert {reminder_id_str} to integer")
                await interaction.response.send_message("❌ Invalid reminder ID format. Please try cancelling manually.", ephemeral=True)
                return
            
            logging.info(f"⚠️ Button clicked: Attempting to cancel reminder {reminder_id} for user {user_id}")
            
            # Cancel the reminder when clicked
            success = cancel_reminder(reminder_id, user_id)
            
            if success:
                logging.info(f"✅ Successfully cancelled reminder {reminder_id} via button")
                await interaction.response.send_message("✅ Reminder cancelled successfully!", ephemeral=True)
            else:
                logging.error(f"❌ Failed to cancel reminder {reminder_id} via button. May be already cancelled.")
                await interaction.response.send_message("❌ Failed to cancel reminder. It may have already been cancelled.", ephemeral=True)
        except Exception as e:
            logging.error(f"❌ Error in cancel button callback: {e}")
            logging.error(f"⚠️ Button custom_id: {self.custom_id}")
            await interaction.response.send_message("❌ An error occurred while cancelling the reminder.", ephemeral=True)

def detect_reminder_request(text, user_id=None):
    """Determine if a message is requesting to set a reminder"""
    try:
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": get_reminder_detection_prompt(datetime.now().strftime('%Y-%m-%d'))},
                {"role": "user", "content": f"Message: {text}"}
            ],
            temperature=0.1,
            max_tokens=10
        )
        if user_id and hasattr(response, 'usage'):
            reminders_log_token_usage(
                user_id,
                MODEL,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.prompt_tokens + response.usage.completion_tokens
            )
        result = response.choices[0].message.content.strip().lower()
        logging.info(f"⏰ Reminder detection response: '{result}' for message: {text[:50]}...")
        return result
    except Exception as e:
        logging.error(f"❌ Error in reminder detection: {e}")
        return "no"

def extract_reminder_details(text, user_id=None):
    """Extract reminder content, time and timezone from text"""
    try:
        # API call to extract reminder details
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": get_reminder_extraction_prompt(datetime.now().strftime('%Y-%m-%d'))},
                {"role": "user", "content": f"Extract reminder details from: {text}"}
            ],
            temperature=0.1,
            max_tokens=300
        )
        
        # Track token usage
        if user_id and hasattr(response, 'usage'):
            reminders_log_token_usage(
                user_id,
                MODEL,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.prompt_tokens + response.usage.completion_tokens
            )
        
        # Parse the response
        result = response.choices[0].message.content.strip()
        json_start = result.find('{')
        json_end = result.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = result[json_start:json_end]
            reminder_data = json.loads(json_str)
            return reminder_data
        else:
            logging.error(f"❌ Could not extract JSON from response: {result}")
            return None
    except Exception as e:
        logging.error(f"❌ Error extracting reminder details: {e}")
        return None

def extract_timezone_from_location(location_text, user_id=None):
    """Convert a location description to a timezone string"""
    try:
        # API call to extract timezone
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": get_timezone_extraction_prompt()},
                {"role": "user", "content": f"Location: {location_text}"}
            ],
            temperature=0.1,
            max_tokens=30
        )
        
        # Track token usage
        if user_id and hasattr(response, 'usage'):
            reminders_log_token_usage(
                user_id,
                MODEL,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.prompt_tokens + response.usage.completion_tokens
            )
        
        timezone = response.choices[0].message.content.strip()
        
        # Validate timezone format
        if not timezone or timezone.lower() == "unknown":
            return None
            
        return timezone
    except Exception as e:
        logging.error(f"❌ Error extracting timezone: {e}")
        return None

def process_reminder_time(time_str, current_time=None, timezone=None):
    """Convert reminder time to absolute datetime"""
    try:
        # If no current_time provided, use now in UTC
        if not current_time:
            current_time = datetime.now(pytz.UTC)
        
        # If no timezone provided, default to UTC
        if not timezone:
            timezone = 'UTC'
            
        # Get the user's timezone object
        user_tz = pytz.timezone(timezone)
        
        # Get current time in user's timezone for comparisons
        current_local = current_time.astimezone(user_tz)
        
        # Check if it's a relative time (starts with "in" and contains a number and time unit)
        relative_pattern = r'^in\s+(\d+)\s+(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)$'
        match = re.match(relative_pattern, time_str.lower())

        # If not a simple relative time, check if it's a combined format (relative + specific time)
        combined_pattern = r'^in\s*(\d+)\s*(second|seconds|minute|minutes|hour|hours|day|days|week|weeks|month|months|year|years)(?:\s+and)?\s+(?:at|for|on)\s+(.+)$'
        combined_match = re.match(combined_pattern, time_str.lower())
            
        if combined_match:
            logging.info("⏰ Detected combined time format")
            number = int(combined_match.group(1))
            unit = combined_match.group(2).rstrip('s')  # Remove trailing 's' for singular form
            time_part = combined_match.group(3)  # The "at X:XX" part
            
            # First calculate the future date using the relative part
            unit_map = {
                'second': 'seconds',
                'minute': 'minutes',
                'hour': 'hours',
                'day': 'days',
                'week': 'weeks',
                'month': 'days',  # Approximate
                'year': 'days'    # Approximate
            }
            
            delta_params = {unit_map[unit]: number}
            if unit == 'month':
                delta_params['days'] = number * 30
            elif unit == 'year':
                delta_params['days'] = number * 365
                
            # Get the future date from the relative part
            future_date = current_time + timedelta(**delta_params)
            
            # Now parse the time part and apply it to the future date
            try:
                # Parse just the time part (e.g., "6pm")
                parsed_time = parser.parse(time_part, fuzzy=True)

                # Localize the parsed time to user timezone if it doesn't have a timezone
                if parsed_time.tzinfo is None:
                    parsed_time = user_tz.localize(parsed_time)

                # Convert future_date to user timezone, apply the time, then convert back to UTC
                future_date_local = future_date.astimezone(user_tz)
                future_time_local = future_date_local.replace(
                    hour=parsed_time.hour,
                    minute=parsed_time.minute,
                    second=0,
                    microsecond=0
                )
                future_time = future_time_local.astimezone(pytz.UTC)

                logging.info(f"⏰ Calculated combined time format: {future_time}")
                return future_time
            except Exception as e:
                logging.error(f"❌ Error processing combined time format: {e}")
                return None
        
        if match:
            logging.info("⏰ Detected relative time format")
            number = int(match.group(1))
            unit = match.group(2).rstrip('s')  # Remove trailing 's' for singular form
            
            # Map units to timedelta parameters
            unit_map = {
                'second': 'seconds',
                'minute': 'minutes',
                'hour': 'hours',
                'day': 'days',
                'week': 'weeks',
                'month': 'days',  # Approximate
                'year': 'days'    # Approximate
            }
            
            # Create timedelta with the appropriate unit
            delta_params = {unit_map[unit]: number}
            if unit == 'month':
                delta_params['days'] = number * 30
            elif unit == 'year':
                delta_params['days'] = number * 365
                
            future_time = current_time + timedelta(**delta_params)
            logging.info(f"⏰ Calculated future time for relative time: {future_time}")
            return future_time
        
        # Special handling for "next [day]" cases
        time_str_lower = time_str.lower()
        if time_str_lower.startswith('next '):
            try:
                # Parse the time without the "next" keyword first
                base_time_str = time_str_lower.replace('next ', '', 1)
                parsed_time = parser.parse(base_time_str, fuzzy=True)
                
                # If the parsed time has no timezone, assign the user's timezone
                if parsed_time.tzinfo is None:
                    parsed_time = user_tz.localize(parsed_time)
                
                # Get weekday numbers (0=Monday through 6=Sunday)
                current_weekday = current_local.weekday()
                target_weekday = parsed_time.weekday()
                
                logging.info(f"⏰ Current weekday: {current_weekday}, Target weekday: {target_weekday}")
                
                # First get to the next occurrence of the day
                while parsed_time < current_local:
                    parsed_time = parsed_time + timedelta(days=1)
                

                
                # Convert to UTC for storage
                utc_time = parsed_time.astimezone(pytz.UTC)
                logging.info(f"⏰ Final UTC time for 'next [day]': {utc_time}")
                return utc_time
                
            except Exception as e:
                logging.error(f"❌ Error processing 'next [day]' time: {e}")
                # Fall through to regular processing
        
        # Special handling for natural language date expressions
        if time_str_lower in ["the day after tomorrow", "day after tomorrow"]:
            try:
                # Calculate the date (tomorrow + 1 day) in user's timezone
                target_time = current_local + timedelta(days=2)
                # Set to midnight in the user's timezone
                target_time = target_time.replace(hour=0, minute=0, second=0, microsecond=0)
                # Convert to UTC for storage
                utc_time = target_time.astimezone(pytz.UTC)
                logging.info(f"⏰ Final UTC time for 'day after tomorrow': {utc_time}")
                return utc_time
            except Exception as e:
                logging.error(f"❌ Error processing 'day after tomorrow': {e}")
                return None
        
        # Try to parse as natural language for absolute times
        try:
            # Check for "tomorrow" in the time string first
            is_tomorrow = "tomorrow" in time_str.lower()
            
            # Handle special time words
            time_str_lower = time_str.lower()

            # Handle ambiguous times like "at 8" without AM/PM
            at_time_pattern = r'at\s+(\d{1,2})(?=\s|$)(?!:\d+|\s*[ap]m|\s*noon|\s*midnight)'
            match = re.search(at_time_pattern, time_str_lower)
            if match:
                hour = int(match.group(1))
                if hour <= 12:  # Only handle 12-hour format times
                    current_hour = current_local.hour
                    # If current time is before noon (12 PM)
                    if current_hour < 12:
                        # If specified hour is less than current hour, assume PM
                        # If specified hour is greater than current hour, assume AM
                        meridian = "PM" if hour <= current_hour else "AM"
                    else:
                        # If current time is after noon, always assume PM for ambiguous times
                        meridian = "PM"
                    # Replace the matched "at X" with "at X AM/PM"
                    time_str = re.sub(at_time_pattern, f'at {hour} {meridian}', time_str)
                    logging.info(f"⏰ Ambiguous time '{match.group(0)}' interpreted as '{hour} {meridian}'")

            if "noon" in time_str_lower:
                time_str = time_str.replace("noon", "12:00 PM")
            elif "midnight" in time_str_lower:
                time_str = time_str.replace("midnight", "12:00 AM")
            
            # Parse the time in the user's timezone
            parsed_time = parser.parse(time_str, fuzzy=True)
            
            # If the parsed time has no timezone, assign the user's timezone
            if parsed_time.tzinfo is None:
                parsed_time = user_tz.localize(parsed_time)
            
            # Check if we need to adjust to the next day or not
            # If the time is in the past and not explicitly marked as tomorrow
            if parsed_time < current_local and not is_tomorrow:
                # If the time is in the past, assume it's for tomorrow
                parsed_time = parsed_time + timedelta(days=1)
                logging.info(f"⏰ Time was in the past, adjusted to tomorrow: {parsed_time}")
            elif is_tomorrow:
                # If explicitly marked as tomorrow, add one day
                parsed_time = parsed_time + timedelta(days=1)
                logging.info(f"⏰ Time marked as tomorrow, adjusted: {parsed_time}")
            
            # Convert to UTC for storage
            utc_time = parsed_time.astimezone(pytz.UTC)
            logging.info(f"⏰ Final UTC time: {utc_time}")
            return utc_time
            
        except Exception as e:
            logging.error(f"❌ Error with fuzzy parsing: {e}")
            return None
            
    except Exception as e:
        logging.error(f"❌ Error processing time: {e}")
        return None

def generate_confirmation_message(reminder_data, user_id=None):
    """Generate a friendly confirmation message using a rule-based approach"""
    try:
        # Extract the task from the reminder data
        task = reminder_data.get('content', '')
        
        # Handle relative times (e.g., "in 5 minutes")
        if reminder_data['time'].startswith('in '):
            # For relative times, use the time string directly
            time_str = reminder_data['time']
            message = f"Got it! I'll remind you {time_str} ✅"
            return message
        
        # For non-relative times, convert UTC time back to original timezone
        from datetime import datetime, timedelta
        from pytz import timezone
        
        # Get the original timezone from the reminder data
        original_timezone = reminder_data.get('original_timezone', reminder_data.get('timezone', 'UTC'))
        
        # Parse the UTC time
        utc_time = datetime.strptime(reminder_data['scheduled_time'], '%Y-%m-%d %H:%M:%S')
        utc_time = timezone('UTC').localize(utc_time)
        
        # Convert to original timezone
        local_time = utc_time.astimezone(timezone(original_timezone))
        
        # Format the time in 12-hour format
        formatted_time = local_time.strftime('%I:%M %p').lstrip('0')
        
        # Get current time in user's timezone for date comparison
        current_time = datetime.now(timezone(original_timezone))
        
        # Determine if the reminder is for today, tomorrow, or a future date
        if local_time.date() == current_time.date():
            # Today
            date_str = "today"
        elif local_time.date() == current_time.date() + timedelta(days=1):
            # Tomorrow
            date_str = "tomorrow"
        else:
            # Future date
            date_str = local_time.strftime("%B %d, %Y")
        
        # Generate the confirmation message based on the date
        if date_str == "today":
            message = f"Got it! I'll remind you today at {formatted_time} ✅"
        elif date_str == "tomorrow":
            message = f"OK! I'll remind you tomorrow at {formatted_time} ✅"
        else:
            message = f"Got it! I'll remind you on {date_str} at {formatted_time} ✅"
        
        return message
    
    except Exception as e:
        logging.error(f"❌ Error generating confirmation message: {e}")
        # Fallback message in case of error
        return f"Got it! I'll remind you to {reminder_data.get('content', '')} at {reminder_data.get('time', '')} ✅"

def process_reminder_request(text, user_id):
    """Process a reminder request and respond to the user"""
    # Extract reminder details
    reminder_data = extract_reminder_details(text, user_id)
    
    # Extract recipient and service from user_id
    recipient = user_id.split(';-;')[-1] if ';-;' in user_id else user_id
    service = user_id.split(';-;')[0] if ';-;' in user_id else None
    service_type = "SMS" if service and service.lower() == "sms" else "iMessage"
    
    if not reminder_data:
        reminders_send_message(recipient, "I couldn't understand that reminder request. Could you try again with a specific time?", user_id=user_id, service=service_type)
        return True
    
    # Check if this is an error response
    if reminder_data.get('error'):
        reminders_send_message(recipient, reminder_data['message'], user_id=user_id, service=service_type)
        return True
    
    # Check if timezone/location info is needed
    needs_timezone = reminder_data.get('needs_timezone', True)
    
    # For relative times (like "in 5 minutes"), we don't need timezone
    if not needs_timezone:
        reminder_data['timezone'] = 'UTC'
        logging.info("⏰ Using temporary UTC timezone for relative time reminder")
    else:
        # For absolute times, we need a proper timezone
        if not reminder_data.get('timezone'):
            # Check if we have a timezone from previous reminders
            timezone = get_user_timezone(user_id)
            
            if timezone and timezone != 'UTC':
                # Use existing timezone
                reminder_data['timezone'] = timezone
                logging.info(f"⏰ Using existing timezone {timezone} from previous reminders")
            else:
                # Check for timezone in any existing reminders (including cancelled ones)
                timezone = get_any_reminder_timezone(user_id)
                
                if timezone and timezone != 'UTC':
                    # Use timezone from existing reminder
                    reminder_data['timezone'] = timezone
                    logging.info(f"⏰ Using timezone {timezone} from existing reminder")
                else:
                    # Ask for location
                    reminders_send_message(recipient, "To set your reminder perfectly, I just need to know where in the world you are! 🌎📍 Mind sharing your location? 😄", user_id=user_id, service=service_type)
                    # Store reminder data while waiting for location
                    AWAITING_LOCATION[user_id] = reminder_data
                    return True
    
    # Process the time
    scheduled_time = process_reminder_time(
        reminder_data['time'],
        timezone=reminder_data['timezone']  # Now always has a value
    )
    
    if not scheduled_time:
        reminders_send_message(recipient, "Hmm, that time has me scratching my head! 🤔⏰ Could you give me the reminder time again in a clearer format? Thanks! 😊", user_id=user_id, service=service_type)
        return True
    
    # Save the reminder with the timezone
    reminder_id = save_reminder(
        user_id=user_id,
        content=reminder_data['content'],
        scheduled_time=scheduled_time,
        timezone=reminder_data['timezone']  # Always save the timezone
    )
    
    if not reminder_id:
        reminders_send_message(recipient, "Uh-oh! 🙈 I had a little trouble saving your reminder. Can you give it another go? Thanks for your patience! 😊🔄", user_id=user_id, service=service_type)
        return True
    
    print(f"[REMINDER CREATED] User {user_id}: '{text}' -> Reminder ID: {reminder_id}")
    
    # Generate confirmation message
    reminder_data['scheduled_time'] = scheduled_time.strftime('%Y-%m-%d %H:%M:%S')
    confirmation = generate_confirmation_message(reminder_data, user_id)
    
    # Create a view with a cancel button
    view = View(timeout=None)  # No timeout so button works indefinitely
    cancel_button = CancelReminderButton(reminder_id)
    view.add_item(cancel_button)
    
    # Send confirmation with the button
    reminders_send_message(recipient, confirmation, user_id=user_id, service=service_type, view=view)
    
    return True

def process_location_response(text, user_id):
    """Process a location response and complete reminder creation"""
    try:
        # Extract recipient and service from user_id
        recipient = user_id.split(';-;')[-1] if ';-;' in user_id else user_id
        service = user_id.split(';-;')[0] if ';-;' in user_id else None
        service_type = "SMS" if service and service.lower() == "sms" else "iMessage"
        
        # Get the pending reminder data
        reminder_data = AWAITING_LOCATION[user_id]
        if reminder_data is None:
            # Request has timed out
            logging.info(f"⏰ Location request for {user_id} has timed out")
            reminders_send_message(recipient, "Looks like we moved on from that location request—no worries! 😄👍🌎", user_id=user_id, service=service_type)
            return True
        del AWAITING_LOCATION[user_id]
        
        # Get timezone from location
        timezone = extract_timezone_from_location(text, user_id)
        
        if not timezone:
            reminders_send_message(recipient, "Oops! 🌎🤷‍♂️ I couldn't pinpoint that location. Can you share a major city or your timezone instead? That'll help me set your reminder just right! 📍😊", user_id=user_id, service=service_type)
            # Put the reminder data back in the waiting list
            AWAITING_LOCATION[user_id] = reminder_data
            return True
        
        # Check if this was from a time query
        if reminder_data.get('is_time_query'):
            # Create a cancelled reminder to store the timezone
            scheduled_time = datetime.now(pytz.UTC)  # Use current time as placeholder
            reminder_id = save_reminder(
                user_id=user_id,
                content='timezone setup',
                scheduled_time=scheduled_time,
                timezone=timezone,
                status='cancelled'  # Set status as cancelled
            )
            
            if not reminder_id:
                reminders_send_message(recipient, "I had trouble saving your timezone. Please try asking for the time again.", user_id=user_id, service=service_type)
                return True
            
            # Import time handler here to avoid circular imports
            from .time_handler import _handle_current_time
            
            # Get and send the current time
            time_response = _handle_current_time(timezone)
            reminders_send_message(recipient, time_response, user_id=user_id, service=service_type)
            return True
        
        # Add timezone to reminder data
        reminder_data['timezone'] = timezone
        
        # Process the time
        scheduled_time = process_reminder_time(
            reminder_data['time'],
            timezone=timezone
        )
        
        if not scheduled_time:
            reminders_send_message(recipient, "I had trouble understanding the time for your reminder. Could you try again?", user_id=user_id, service=service_type)
            return True
        
        # Save to database
        reminder_id = save_reminder(
            user_id=user_id,
            content=reminder_data['content'],
            scheduled_time=scheduled_time,
            timezone=timezone
        )
        
        if not reminder_id:
            reminders_send_message(recipient, "Sorry, I had trouble saving your reminder. Please try again.", user_id=user_id, service=service_type)
            return True
        
        # Generate confirmation
        reminder_data['scheduled_time'] = scheduled_time.strftime('%Y-%m-%d %H:%M:%S')
        confirmation = generate_confirmation_message(reminder_data, user_id)
        
        # Create a view with a cancel button
        view = View(timeout=None)  # No timeout so button works indefinitely
        cancel_button = CancelReminderButton(reminder_id)
        view.add_item(cancel_button)
        
        # Send confirmation with the button
        reminders_send_message(recipient, confirmation, user_id=user_id, service=service_type, view=view)
        
        return True
    except Exception as e:
        logging.error(f"❌ Error processing location: {e}")
        reminders_send_message(recipient, "Sorry, I had trouble setting your reminder with that location.", user_id=user_id, service=service_type)
        # Clear the pending request
        if user_id in AWAITING_LOCATION:
            del AWAITING_LOCATION[user_id]
        return True

def process_location_update(text, user_id):
    """Process a request to update user's location/timezone"""
    try:
        # Extract recipient and service from user_id
        recipient = user_id.split(';-;')[-1] if ';-;' in user_id else user_id
        service = user_id.split(';-;')[0] if ';-;' in user_id else None
        service_type = "SMS" if service and service.lower() == "sms" else "iMessage"
        
        # Extract timezone from location
        timezone = extract_timezone_from_location(text, user_id)
        
        if not timezone:
            reminders_send_message(recipient, "Sorry, I couldn't recognize that location. Could you provide a major city or timezone?", user_id=user_id, service=service_type)
            return True
        
        # Update timezone for pending reminders
        updated = update_user_timezone(user_id, timezone)
        
        if updated:
            reminders_send_message(recipient, f"✅ I've updated your location. I'll now use {timezone} timezone.", user_id=user_id, service=service_type)
        else:
            reminders_send_message(recipient, f"✅ I've noted your location ({timezone}). I'll use this for your future reminders.", user_id=user_id, service=service_type)
        
        return True
    except Exception as e:
        logging.error(f"❌ Error updating location: {e}")
        reminders_send_message(recipient, "Sorry, I had trouble updating your location. Please try again.", user_id=user_id, service=service_type)
        return True

def detect_reminder_operation(text: str, user_id=None) -> str:
    """Determine what type of reminder operation is being requested"""
    try:
        # First check for time queries using simpler pattern matching
        from .time_handler import _parse_time_query
        time_type, _ = _parse_time_query(text)
        if time_type != 'unknown':
            return 'time'
        
        # If not a time query, proceed with normal reminder operation detection
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": get_reminder_operation_detection_prompt(datetime.now().strftime('%Y-%m-%d'))},
                {"role": "user", "content": f"Message: {text}"}
            ],
            temperature=0.1,
            max_tokens=10
        )
        
        # Track token usage
        if user_id and hasattr(response, 'usage'):
            reminders_log_token_usage(
                user_id,
                MODEL,
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.prompt_tokens + response.usage.completion_tokens
            )
        
        result = response.choices[0].message.content.strip().lower()
        logging.info(f"⏰ Reminder operation detection: '{result}' for message: {text[:50]}...")
        return result
    except Exception as e:
        logging.error(f"❌ Error in reminder operation detection: {e}")
        return "none"

def format_reminder_list(reminders: list, user_timezone: str) -> str:
    """Format a list of reminders in a user-friendly way"""
    if not reminders:
        return f"📝 You don't have any reminders set up yet.\n🌎 **Timezone:** {user_timezone}"
    
    # Get current time in user's timezone for date comparisons
    user_tz = pytz.timezone(user_timezone)
    now = datetime.now(user_tz)
    today = now.date()
    tomorrow = today + timedelta(days=1)
    
    # Group reminders by day
    today_reminders_rel = []  # For relative time reminders (strings)
    today_reminders_abs = []  # For absolute time reminders (datetimes)
    tomorrow_reminders = []
    future_reminders = []
    
    for reminder in reminders:
        try:
            # Get the stored UTC time
            utc_time = reminder['scheduled_time'].replace(tzinfo=pytz.UTC)
            
            # If this is a relative time reminder (timezone is UTC)
            if reminder['timezone'] == 'UTC':
                # Calculate the time difference
                time_diff = utc_time - now.astimezone(pytz.UTC)
                total_minutes = int(time_diff.total_seconds() / 60)
                
                # Format the relative time
                if total_minutes < 60:
                    time_str = f"in {total_minutes} minute{'s' if total_minutes != 1 else ''}"
                    # Show short relative reminders under today
                    today_reminders_rel.append((time_str, reminder))
                elif total_minutes < 1440:  # Less than 24 hours
                    hours = total_minutes // 60
                    time_str = f"in {hours} hour{'s' if hours != 1 else ''}"
                    today_reminders_rel.append((time_str, reminder))
                else:
                    days = total_minutes // 1440
                    time_str = f"in {days} day{'s' if days != 1 else ''}"
                    # For reminders >= 1 day, show under future with date
                    local_time = utc_time.astimezone(user_tz)
                    future_reminders.append((local_time, reminder, time_str))
                continue
            
            # For absolute time reminders, convert UTC to current user timezone for date grouping
            local_time = utc_time.astimezone(user_tz)
            reminder_date = local_time.date()
            
            # For display time, we want to show the same local time regardless of timezone
            # So we convert the UTC time to the original timezone first
            original_tz = pytz.timezone(reminder['original_timezone']) if reminder['original_timezone'] else pytz.UTC
            original_local_time = utc_time.astimezone(original_tz)
            
            # Then create a display time with the original hour/minute but current date
            display_time = local_time.replace(
                hour=original_local_time.hour,
                minute=original_local_time.minute
            )
            
            # Group by date
            if reminder_date == today:
                today_reminders_abs.append((display_time, reminder))
            elif reminder_date == tomorrow:
                tomorrow_reminders.append((display_time, reminder))
            else:
                future_reminders.append((display_time, reminder))
        except Exception as e:
            logging.error(f"❌ Error processing reminder for display: {e}")
            continue
    
    # Format the message
    message = "📋 **Here are your reminders:**\n\n"
    
    # Today's reminders
    if today_reminders_abs or today_reminders_rel:
        message += "**Today**:\n"
        
        # Process relative time reminders FIRST (sorted by the timestring)
        sorted_rel = sorted(today_reminders_rel, key=lambda x: (x[1]['scheduled_time'], x[1]['content']))
        for time_str, reminder in sorted_rel:
            content = ' '.join(word.capitalize() for word in reminder['content'].split())
            message += f"• {content} {time_str}\n"
        
        # Process absolute time reminders SECOND (sorted by time)
        sorted_abs = sorted(today_reminders_abs, key=lambda x: (x[0], x[1]['content']))
        for time, reminder in sorted_abs:
            content = ' '.join(word.capitalize() for word in reminder['content'].split())
            time_str = time.strftime("%I:%M %p").lstrip("0")
            message += f"• {content} at {time_str}\n"
        
        message += "\n"
    
    # Tomorrow's reminders
    if tomorrow_reminders:
        message += "**Tomorrow**:\n"
        for time, reminder in sorted(tomorrow_reminders, key=lambda x: (x[0], x[1]['content'])):
            time_str = time.strftime("%I:%M %p").lstrip("0")
            content = ' '.join(word.capitalize() for word in reminder['content'].split())
            message += f"• {content} at {time_str}\n"
        message += "\n"
    
    # Future reminders
    if future_reminders:
        message += "**Future**:\n"
        for future_item in sorted(future_reminders, key=lambda x: (x[0], x[1]['content'])):
            # Support both (local_time, reminder, time_str) and (local_time, reminder) tuples
            if len(future_item) == 3:
                time, reminder, _ = future_item  # ignore rel_time_str
            else:
                time, reminder = future_item
            # Show year if it's different from current year
            if time.year != now.year:
                date_str = time.strftime("%B %d, %Y")
            else:
                date_str = time.strftime("%B %d")
            content = ' '.join(word.capitalize() for word in reminder['content'].split())
            time_str = time.strftime("%I:%M %p").lstrip("0")
            message += f"• {content} on {date_str} at {time_str}\n"
    
    # Add help tip for cancellation
    if today_reminders_abs or today_reminders_rel or tomorrow_reminders or future_reminders:
        message += "\n💡 **Tip:**\n"
        message += "• To cancel a reminder, type \"cancel reminder about [content]\"\n"
        message += "• To change your timezone, type \"change my timezone to [location]\"\n"
        message += f"\n🌎 **Timezone:** {user_timezone}\n"
    
    return message.strip()

def process_list_request(user_id: str) -> str:
    """Process a request to list reminders"""
    # Get the user's timezone
    timezone = get_user_timezone(user_id) or "UTC"
    
    # Get pending reminders
    reminders = get_user_reminders(user_id, status="pending")
    
    # Format the list
    return format_reminder_list(reminders, timezone)

def modify_reminder_list_for_display(reminders, user_timezone):
    """Convert reminder list to user-friendly format that includes time information"""
    enhanced_reminders = []
    user_tz = pytz.timezone(user_timezone)
    
    for reminder in reminders:
        # Convert UTC time to user's timezone
        utc_time = reminder['scheduled_time'].replace(tzinfo=pytz.UTC)
        local_time = utc_time.astimezone(user_tz)
        
        # Format date/time nicely
        now = datetime.now(user_tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        
        if local_time.date() == today:
            date_str = "today"
        elif local_time.date() == tomorrow:
            date_str = "tomorrow"
        else:
            date_str = local_time.strftime("%A, %B %d")
            
        time_str = local_time.strftime("%I:%M %p").lstrip("0")
        
        # Combine content with time information
        content = reminder['content']
        enhanced_content = f"{content} ({date_str} at {time_str})"
        
        # Keep track of both the enhanced content and the original reminder
        enhanced_reminders.append({
            'content': content,
            'enhanced_content': enhanced_content,
            'original': reminder
        })
    
    return enhanced_reminders

def process_cancel_request(text: str, user_id: str) -> str:
    """Process a request to cancel a reminder"""
    try:
        # Get all pending reminders first
        all_reminders = get_user_reminders(user_id, status="pending")
        if not all_reminders:
            return "You don't have any active reminders to cancel."
            
        # Get user's timezone
        timezone = get_user_timezone(user_id) or "UTC"
        user_tz = pytz.timezone(timezone)
        now = datetime.now(user_tz)
        today = now.date()
        tomorrow = today + timedelta(days=1)
        
        # Enhanced reminders with date/time info for better matching
        enhanced_reminders = modify_reminder_list_for_display(all_reminders, timezone)
        
        # Create two lists: one for display to the model with enhanced content (includes date/time)
        # and one that preserves the original reminder data
        enhanced_reminder_list = [r['enhanced_content'] for r in enhanced_reminders]
        
        # Create the prompt with enhanced reminders
        system_prompt = get_reminder_cancellation_extraction_prompt(datetime.now().strftime('%Y-%m-%d'))
        user_prompt = f"""Current reminders: {enhanced_reminder_list}
        
        Request: {text}"""
        
        # Use AI to extract cancellation details
        response = openai.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            temperature=0.1,
            max_tokens=100
        )
        
        # Track token usage if available
        if hasattr(response, 'usage'):
            reminders_log_token_usage(
                user_id,
                MODEL,
                getattr(response.usage, 'prompt_tokens', 0),
                getattr(response.usage, 'completion_tokens', 0),
                getattr(response.usage, 'prompt_tokens', 0) + getattr(response.usage, 'completion_tokens', 0)
            )
        
        # Parse the response
        result = response.choices[0].message.content.strip()
        json_start = result.find('{')
        json_end = result.rfind('}') + 1
        
        if json_start >= 0 and json_end > json_start:
            json_str = result[json_start:json_end]
            cancel_data = json.loads(json_str)
            
            # Handle different cancellation types
            if cancel_data["type"] == "recent":
                # Cancel most recent reminder
                last_reminder = get_last_created_reminder(user_id)
                if last_reminder and last_reminder['status'] == 'pending':
                    if cancel_reminder(last_reminder['id'], user_id):
                        # Capitalize the first letter of each word in the reminder content
                        content = ' '.join(word.capitalize() for word in last_reminder['content'].split())
                        return f"✅ Cancelled your reminder: {content}"
                return "I couldn't find your most recent reminder. Please say \"cancel my reminder about [content]\" to cancel a specific reminder."
                
            elif cancel_data["type"] == "timeperiod":
                # Convert reminders to user's timezone and group them
                reminders_by_date = {}
                for i, reminder in enumerate(all_reminders):
                    reminder_time = reminder['scheduled_time'].replace(tzinfo=pytz.UTC)
                    local_time = reminder_time.astimezone(user_tz)
                    reminder_date = local_time.date()
                    if reminder_date not in reminders_by_date:
                        reminders_by_date[reminder_date] = []
                    # Store both the reminder and its index
                    reminders_by_date[reminder_date].append((i, reminder))
                
                target_date = None
                if cancel_data["timeperiod"] == "today":
                    target_date = today
                elif cancel_data["timeperiod"] == "tomorrow":
                    target_date = tomorrow
                elif cancel_data["timeperiod"] == "all":
                    # Cancel all reminders
                    cancelled_count = 0
                    for reminder in all_reminders:
                        if cancel_reminder(reminder['id'], user_id):
                            cancelled_count += 1
                    
                    if cancelled_count > 0:
                        return f"✅ Cancelled all {cancelled_count} reminder{'s' if cancelled_count > 1 else ''}."
                    else:
                        return "Sorry, I had trouble cancelling the reminders. Please try again."
                
                # Check if the timeperiod cancellation also has specific matches (for date-specific cancellations)
                if cancel_data.get("matches") and len(cancel_data["matches"]) > 0:
                    # We have specific matches, treat it like a content cancellation
                    cancelled_count = 0
                    cancelled_content = None
                    
                    for index in cancel_data["matches"]:
                        if 0 <= index < len(enhanced_reminders):
                            # Get the original reminder from our enhanced list
                            original_reminder = enhanced_reminders[index]['original']
                            if cancel_reminder(original_reminder['id'], user_id):
                                cancelled_count += 1
                                # Save the content of the first cancelled reminder
                                if not cancelled_content:
                                    cancelled_content = original_reminder['content']
                    
                    if cancelled_count == 1 and cancelled_content:
                        # If we cancelled exactly one reminder, show its content
                        content = ' '.join(word.capitalize() for word in cancelled_content.split())
                        return f"✅ Cancelled your reminder: {content}"
                    elif cancelled_count > 0:
                        # If we cancelled multiple reminders, just show the count
                        return f"✅ Cancelled {cancelled_count} reminder{'s' if cancelled_count > 1 else ''}."
                    else:
                        return "Sorry, I had trouble cancelling the reminder. Please try again."
                
                # Regular timeperiod handling (today/tomorrow)
                if target_date:
                    if target_date not in reminders_by_date:
                        return f"You don't have any reminders scheduled for {'today' if target_date == today else 'tomorrow'}."
                    
                    # If there's only one reminder on the target date, cancel it directly
                    if len(reminders_by_date[target_date]) == 1:
                        idx, reminder = reminders_by_date[target_date][0]
                        if cancel_reminder(reminder['id'], user_id):
                            content = ' '.join(word.capitalize() for word in reminder['content'].split())
                            return f"✅ Cancelled your reminder: {content}"
                    
                    # Otherwise cancel all reminders for the target date
                    cancelled_count = 0
                    for _, reminder in reminders_by_date[target_date]:
                        if cancel_reminder(reminder['id'], user_id):
                            cancelled_count += 1
                    
                    if cancelled_count > 0:
                        return f"✅ Cancelled {cancelled_count} reminder{'s' if cancelled_count > 1 else ''} scheduled for {'today' if target_date == today else 'tomorrow'}."
                    else:
                        return "Sorry, I had trouble cancelling the reminders. Please try again."
                        
            elif cancel_data["type"] == "all":
                # Cancel all reminders
                cancelled_count = 0
                for reminder in all_reminders:
                    if cancel_reminder(reminder['id'], user_id):
                        cancelled_count += 1
                
                if cancelled_count > 0:
                    return f"✅ Cancelled {cancelled_count} reminder{'s' if cancelled_count > 1 else ''}."
                else:
                    return "Sorry, I had trouble cancelling the reminders. Please try again."
                    
            elif cancel_data["type"] == "content":
                # Cancel reminders matching the content
                if not cancel_data.get("matches"):
                    return "I couldn't find any reminders matching that description. Please say \"cancel my reminder about [content]\" to cancel a specific reminder."
                
                # If there's exactly one match, cancel it and return its original content
                if len(cancel_data["matches"]) == 1:
                    index = cancel_data["matches"][0]
                    if 0 <= index < len(enhanced_reminders):
                        original_reminder = enhanced_reminders[index]['original']
                        original_content = enhanced_reminders[index]['content']  # Get the original content without date/time
                        
                        if cancel_reminder(original_reminder['id'], user_id):
                            # Use the original reminder content, not the query text
                            content = ' '.join(word.capitalize() for word in original_content.split())
                            return f"✅ Cancelled your reminder: {content}"
                
                # If there are multiple matches, cancel them all
                cancelled_count = 0
                for index in cancel_data["matches"]:
                    if 0 <= index < len(enhanced_reminders):
                        # Get the original reminder from our enhanced list
                        original_reminder = enhanced_reminders[index]['original']
                        if cancel_reminder(original_reminder['id'], user_id):
                            cancelled_count += 1
                
                if cancelled_count > 0:
                    # For multiple reminders, just show the count
                    return f"✅ Cancelled {cancelled_count} reminder{'s' if cancelled_count > 1 else ''}."
                else:
                    return "Sorry, I had trouble cancelling the reminder. Please try again."
            
            return "I couldn't understand which reminder to cancel. Could you be more specific?"
            
        return "I couldn't understand which reminder to cancel. Could you be more specific?"
        
    except Exception as e:
        logging.error(f"❌ Error processing cancellation request: {e}")
        return "Sorry, I had trouble processing your cancellation request. Please try again." 