"""
Time handling functionality for AI Buddy
Handles time queries and timezone conversions
"""

import logging
import json
import pytz
from datetime import datetime
from typing import Optional, Dict, Tuple

# Import OpenAI for location resolution
import openai
from config import MODEL

# Import timezone utilities from reminder system
from .reminder_handler import extract_timezone_from_location, get_user_timezone, get_any_reminder_timezone

# Common location to flag emoji mappings
LOCATION_FLAGS = {
    # Countries
    'japan': '🇯🇵',
    'china': '🇨🇳',
    'korea': '🇰🇷',
    'india': '🇮🇳',
    'australia': '🇦🇺',
    'new zealand': '🇳🇿',
    'united kingdom': '🇬🇧',
    'uk': '🇬🇧',
    'france': '🇫🇷',
    'germany': '🇩🇪',
    'italy': '🇮🇹',
    'spain': '🇪🇸',
    'russia': '🇷🇺',
    'canada': '🇨🇦',
    'mexico': '🇲🇽',
    'brazil': '🇧🇷',
    'argentina': '🇦🇷',
    
    # US States/Cities
    'new york': '🗽',
    'los angeles': '🌴',
    'san francisco': '🌉',
    'seattle': '🌧️',
    'hawaii': '🌺',
    'las vegas': '🎰',
    'miami': '🏖️',
    'chicago': '🌆',
    'boston': '🏛️',
    'washington dc': '🏛️',
    'texas': '🤠',
    'california': '🏄',
    'florida': '🐊',
    
    # Major International Cities
    'tokyo': '🇯🇵',
    'beijing': '🇨🇳',
    'seoul': '🇰🇷',
    'hong kong': '🇭🇰',
    'singapore': '🇸🇬',
    'dubai': '🇦🇪',
    'paris': '🇫🇷',
    'london': '🇬🇧',
    'rome': '🇮🇹',
    'madrid': '🇪🇸',
    'berlin': '🇩🇪',
    'moscow': '🇷🇺',
    'sydney': '🇦🇺',
    'melbourne': '🇦🇺',
    'auckland': '🇳🇿',
    'toronto': '🇨🇦',
    'vancouver': '🇨🇦',
    'mexico city': '🇲🇽',
    'sao paulo': '🇧🇷',
    'buenos aires': '🇦🇷'
}

# Add a global messaging function for Discord patching
reminders_send_message = lambda recipient, content, **kwargs: (_ for _ in ()).throw(NotImplementedError('reminders_send_message must be patched by the Discord bot.'))

# Add a global token usage logger for Discord patching (if not already present)
reminders_log_token_usage = lambda user_id, model, prompt_tokens, completion_tokens, purpose=None, chat_guid=None: None

def _get_location_flag(location: str) -> str:
    """Get flag emoji for a location"""
    location_lower = location.lower()
    
    # First try exact match
    if location_lower in LOCATION_FLAGS:
        return LOCATION_FLAGS[location_lower]
        
    # Then try partial match
    for key, flag in LOCATION_FLAGS.items():
        if key in location_lower or location_lower in key:
            return flag
            
    # Default to world emoji if no match found
    return '🌎'

def process_time_query(text: str, user_id: str = None) -> Tuple[str, Optional[Dict]]:
    """
    Process a time-related query and generate a response
    
    Args:
        text (str): The user's query text
        user_id (str, optional): The user ID for context
        
    Returns:
        Tuple[str, Optional[Dict]]: Response text and optional metadata
    """
    try:
        # Get user's timezone from database
        timezone = get_user_timezone(user_id)
        if not timezone:
            timezone = get_any_reminder_timezone(user_id)
            
        # Parse the query type
        query_type, locations = _parse_time_query(text)
        
        # If no timezone and this is a current time query, use reminder system to get location
        if not timezone and query_type == 'current_time':
            # Import here to avoid circular imports
            from messaging.imessage import send_imessage
            from .reminder_handler import AWAITING_LOCATION
            
            # Create placeholder reminder data
            reminder_data = {
                'content': 'timezone setup',
                'time': None,
                'needs_timezone': True,
                'timezone': None,
                'is_time_query': True  # Flag to identify this is from time query
            }
            
            # Extract recipient and service from user_id
            recipient = user_id.split(';-;')[-1] if ';-;' in user_id else user_id
            service = user_id.split(';-;')[0] if ';-;' in user_id else None
            service_type = "SMS" if service and service.lower() == "sms" else "iMessage"
            
            # Ask for location using reminder system's flow
            reminders_send_message(recipient, "I want to make sure I give you the right time ⏰, so I just need to know where you are ��—mind telling me?", user_id=user_id, service=service_type)
            
            # Store reminder data while waiting for location
            AWAITING_LOCATION[user_id] = reminder_data
            return "", None  # Return empty string instead of None
            
        if query_type == 'current_time':
            return _handle_current_time(timezone or 'UTC'), None
            
        elif query_type == 'location_time':
            return _handle_location_time(locations[0]), None
            
        elif query_type == 'time_difference':
            return _handle_time_difference(locations[0], locations[1]), None
            
        else:
            return "I couldn't understand your time query. You can ask about current time, time in a specific location, or time difference between locations.", None
            
    except Exception as e:
        logging.error(f"❌ Error processing time query: {e}")
        return "I had trouble processing your time query. Please try again.", None

def _parse_time_query(text: str) -> Tuple[str, list]:
    """
    Parse the type of time query and extract locations if any
    
    Args:
        text (str): The query text
        
    Returns:
        Tuple[str, list]: Query type and list of locations
    """
    text_lower = text.lower()
    
    # Check for current time queries
    if any(pattern in text_lower for pattern in [
        "what time is it",
        "what's the time",
        "current time",
        "time now"
    ]):
        if "in " not in text_lower:
            return 'current_time', []
    
    # Check for location time queries
    if "time in " in text_lower or "what time is it in " in text_lower:
        location = text_lower.split(" in ")[-1].strip()
        return 'location_time', [location]
    
    # Check for time difference queries
    if "time difference between " in text_lower:
        parts = text_lower.split("between ")[-1].split(" and ")
        if len(parts) == 2:
            return 'time_difference', [parts[0].strip(), parts[1].strip()]
            
    return 'unknown', []

def _handle_current_time(timezone: str) -> str:
    """Generate response for current time query"""
    try:
        # Get the timezone object
        tz = pytz.timezone(timezone)
        
        # Get current time in UTC
        utc_now = datetime.now(pytz.UTC)
        
        # Convert to user's timezone
        current_time = utc_now.astimezone(tz)
        
        # Format time with AM/PM
        formatted_time = current_time.strftime("%I:%M %p").lstrip("0")
        
        # Format date
        formatted_date = current_time.strftime("%A, %B %d")
        
        return f"🕐 It's {formatted_time} \n🗓️ {formatted_date} \n🌎 ({timezone})"
        
    except Exception as e:
        logging.error(f"❌ Error handling current time: {e}")
        return "I had trouble getting the current time. Please try again."

def _handle_location_time(location: str) -> str:
    """Generate response for time in specific location"""
    try:
        # Capitalize each word in the location name
        formatted_location = ' '.join(word.capitalize() for word in location.split())
        
        # Get location flag
        flag = _get_location_flag(location)
        
        # Get timezone for location
        timezone = extract_timezone_from_location(location)
        if not timezone:
            return f"I couldn't determine the timezone for {formatted_location}. Please try with a major city or specific timezone."
            
        # Get current time in that timezone
        tz = pytz.timezone(timezone)
        
        # Get current time in UTC
        utc_now = datetime.now(pytz.UTC)
        
        # Convert to target timezone
        current_time = utc_now.astimezone(tz)
        
        # Format time with AM/PM
        formatted_time = current_time.strftime("%I:%M %p").lstrip("0")
        
        # Format date
        formatted_date = current_time.strftime("%A, %B %d")
        
        return f"🕐 It's {formatted_time} \n🗓️ {formatted_date} \n🌎 {formatted_location} {flag} ({timezone})"
        
    except Exception as e:
        logging.error(f"❌ Error handling location time: {e}")
        return f"I had trouble getting the time for {formatted_location}. Please try again."

def _handle_time_difference(location1: str, location2: str) -> str:
    """Generate response for time difference between locations"""
    try:
        # Capitalize each word in the location names
        formatted_location1 = ' '.join(word.capitalize() for word in location1.split())
        formatted_location2 = ' '.join(word.capitalize() for word in location2.split())
        
        # Get location flags
        flag1 = _get_location_flag(location1)
        flag2 = _get_location_flag(location2)
        
        # Get timezones for both locations
        timezone1 = extract_timezone_from_location(location1)
        timezone2 = extract_timezone_from_location(location2)
        
        if not timezone1 or not timezone2:
            return "I couldn't determine the timezone for one or both locations. Please try with major cities or specific timezones."
            
        # Get current time in both timezones
        tz1 = pytz.timezone(timezone1)
        tz2 = pytz.timezone(timezone2)
        
        # Get current time in UTC
        utc_now = datetime.now(pytz.UTC)
        
        # Convert to target timezones
        time1 = utc_now.astimezone(tz1)
        time2 = utc_now.astimezone(tz2)
        
        # Calculate time difference in hours
        diff_hours = (time2.utcoffset() - time1.utcoffset()).total_seconds() / 3600
        
        # Format times
        time1_str = time1.strftime("%I:%M %p").lstrip("0")
        time2_str = time2.strftime("%I:%M %p").lstrip("0")
        
        # Handle positive and negative differences
        if diff_hours > 0:
            diff_text = f"{abs(diff_hours):.0f} hours ahead of"
        elif diff_hours < 0:
            diff_text = f"{abs(diff_hours):.0f} hours behind"
        else:
            diff_text = "in the same timezone as"
            
        return f"🕐 When it's {time1_str} in {formatted_location1} {flag1} \n\nit's {time2_str} in {formatted_location2} {flag2}. \n\n⏳{formatted_location2} is {diff_text} {formatted_location1}."
        
    except Exception as e:
        logging.error(f"❌ Error handling time difference: {e}")
        return "I had trouble calculating the time difference. Please try again." 