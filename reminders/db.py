import logging
import pymysql
from datetime import datetime
import pytz
from config import (
    DB_HOST, DB_USER, DB_PASSWORD, DB_NAME
)

def get_db_connection():
    """Get a connection to the MySQL database"""
    return pymysql.connect(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME,
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor
    )

def save_reminder(user_id, content, scheduled_time, timezone=None, status='pending'):
    """Save a reminder to the database"""
    try:
        # Ensure scheduled_time has timezone info
        if not scheduled_time.tzinfo:
            logging.error("❌ Scheduled time must have timezone information")
            return False
            
        # Convert to UTC for storage
        formatted_utc = scheduled_time.astimezone(pytz.UTC)
        logging.info(f"⏰ Saving reminder with UTC time: {formatted_utc}")
        
        # For relative times (like "in 30 minutes"), we don't want to set original_timezone
        # For absolute times, we want to preserve the timezone the reminder was set in
        original_timezone = None
        if timezone and timezone != 'UTC':
            original_timezone = timezone
        else:
            # Try to get the timezone from the scheduled_time
            if scheduled_time.tzinfo and str(scheduled_time.tzinfo) != 'UTC':
                original_timezone = str(scheduled_time.tzinfo)
        
        # If we still don't have an original_timezone, default to UTC
        if not original_timezone:
            original_timezone = 'UTC'
            
        logging.info(f"⏰ Saving reminder with timezone: {timezone}, original_timezone: {original_timezone}")
        
        with get_db_connection() as conn:
            cursor = conn.cursor()
            
            # Insert the reminder with both timezone and original_timezone
            # Explicitly use UTC_TIMESTAMP() for created_at
            cursor.execute("""
                INSERT INTO reminders 
                (user_id, content, scheduled_time, timezone, original_timezone, status, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, UTC_TIMESTAMP())
            """, (user_id, content, formatted_utc, timezone or 'UTC', original_timezone, status))
            
            conn.commit()
            
            # Verify the saved time
            cursor.execute("""
                SELECT scheduled_time, timezone, original_timezone 
                FROM reminders 
                WHERE id = LAST_INSERT_ID()
            """)
            saved_reminder = cursor.fetchone()
            
            if saved_reminder:
                saved_time, saved_tz, saved_orig_tz = saved_reminder
                logging.info(f"✅ Saved reminder with time: {saved_time}, timezone: {saved_tz}, original_timezone: {saved_orig_tz}")
                return True
            else:
                logging.error("❌ Failed to verify saved reminder")
                return False
                
    except Exception as e:
        logging.error(f"❌ Error saving reminder: {e}")
        return False

def get_user_timezone(user_id):
    """Get the user's timezone from their most recent reminder"""
    try:
        with get_db_connection() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                query = """
                SELECT timezone 
                FROM reminders 
                WHERE user_id = %s 
                AND timezone IS NOT NULL 
                AND timezone != 'UTC'
                ORDER BY created_at DESC 
                LIMIT 1
                """
                
                cursor.execute(query, (user_id,))
                result = cursor.fetchone()
                
                if result and result['timezone']:
                    logging.info(f"✅ Found user timezone: {result['timezone']}")
                    return result['timezone']
                
                return None
    except Exception as e:
        logging.error(f"❌ Error getting user timezone: {e}")
        return None

def update_user_timezone(user_id, new_timezone):
    """Update the timezone for all pending reminders and most recent cancelled reminder for this user"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                # First update all pending reminders EXCEPT relative time reminders (with timezone='UTC')
                pending_query = """
                UPDATE reminders 
                SET timezone = %s 
                WHERE user_id = %s AND status = 'pending' AND timezone != 'UTC'
                """
                cursor.execute(pending_query, (new_timezone, user_id))
                pending_affected = cursor.rowcount
                
                # Then update the most recent cancelled reminder (used for timezone storage)
                cancelled_query = """
                UPDATE reminders 
                SET timezone = %s 
                WHERE user_id = %s 
                AND status = 'cancelled'
                AND id = (
                    SELECT id FROM (
                        SELECT id 
                        FROM reminders 
                        WHERE user_id = %s 
                        AND status = 'cancelled'
                        ORDER BY created_at DESC 
                        LIMIT 1
                    ) as sub
                )
                """
                cursor.execute(cancelled_query, (new_timezone, user_id, user_id))
                cancelled_affected = cursor.rowcount
                
                conn.commit()
                
                total_affected = pending_affected + cancelled_affected
                logging.info(f"✅ Updated timezone to {new_timezone} for {pending_affected} pending and {cancelled_affected} cancelled reminders")
                return total_affected
    except Exception as e:
        logging.error(f"❌ Error updating user timezone: {e}")
        return 0

def get_due_reminders():
    """Get all reminders that are due to be sent"""
    try:
        # Get current time in UTC
        now_utc = datetime.now(pytz.UTC)
        
        # Create a fresh connection each time
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                # Get all pending reminders
                query = """
                SELECT id, user_id, content, scheduled_time, timezone,
                       TIMEDIFF(NOW(), scheduled_time) as time_since_due
                FROM reminders 
                WHERE status = 'pending' 
                ORDER BY scheduled_time ASC
                """
                
                cursor.execute(query)
                all_reminders = cursor.fetchall()
                
                # Filter reminders that are due based on their timezone
                due_reminders = []
                for reminder in all_reminders:
                    try:
                        # Get reminder's timezone (default to UTC if not set)
                        reminder_tz = pytz.timezone(reminder['timezone']) if reminder['timezone'] else pytz.UTC
                        
                        # Get the scheduled time in UTC (it's stored in UTC)
                        scheduled_utc = reminder['scheduled_time'].replace(tzinfo=pytz.UTC)
                        
                        # Compare times in UTC - no need to convert to local time
                        # A reminder is due if its UTC time is less than or equal to now UTC
                        if scheduled_utc <= now_utc:
                            due_reminders.append(reminder)
                            
                    except Exception as e:
                        logging.error(f"❌ Error checking reminder {reminder['id']}: {e}")
                        continue
                
                return due_reminders
                
        finally:
            conn.close()
            
    except Exception as e:
        logging.error(f"❌ Error getting due reminders: {e}")
        return []

def mark_reminder_sent(reminder_id):
    """Mark a reminder as sent with retry mechanism"""
    max_retries = 3
    base_delay = 1  # seconds
    
    for attempt in range(max_retries):
        try:
            with get_db_connection() as conn:
                with conn.cursor() as cursor:
                    query = "UPDATE reminders SET status = 'sent' WHERE id = %s"
                    cursor.execute(query, (reminder_id,))
                    conn.commit()
                    
                    return True
        except Exception as e:
            if attempt < max_retries - 1:  # Don't sleep on the last attempt
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logging.warning(f"⚠️ Attempt {attempt + 1}/{max_retries} failed to mark reminder {reminder_id} as sent. Retrying in {delay} seconds...")
                time.sleep(delay)
            else:
                logging.error(f"❌ Error marking reminder as sent after {max_retries} attempts: {e}")
                return False

def get_user_reminders(user_id: str, status: str = None) -> list:
    """Get all reminders for a user, optionally filtered by status"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                if status:
                    query = """
                    SELECT id, content, scheduled_time, timezone, original_timezone, status, created_at
                    FROM reminders 
                    WHERE user_id = %s AND status = %s
                    ORDER BY scheduled_time ASC
                    """
                    cursor.execute(query, (user_id, status))
                else:
                    query = """
                    SELECT id, content, scheduled_time, timezone, original_timezone, status, created_at
                    FROM reminders 
                    WHERE user_id = %s
                    ORDER BY scheduled_time ASC
                    """
                    cursor.execute(query, (user_id,))
                
                reminders = cursor.fetchall()
                logging.info(f"📋 Found {len(reminders)} reminders for {user_id}")
                return reminders
    except Exception as e:
        logging.error(f"❌ Error getting user reminders: {e}")
        return []

def get_reminder_by_content(user_id: str, content: str) -> list:
    """Search for reminders by content (fuzzy match)"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                SELECT id, content, scheduled_time, timezone, status, created_at
                FROM reminders 
                WHERE user_id = %s 
                AND content LIKE %s
                AND status = 'pending'
                ORDER BY scheduled_time ASC
                """
                cursor.execute(query, (user_id, f"%{content}%"))
                reminders = cursor.fetchall()
                logging.info(f"🔍 Found {len(reminders)} reminders matching '{content}' for {user_id}")
                return reminders
    except Exception as e:
        logging.error(f"❌ Error searching reminders by content: {e}")
        return []

def get_last_created_reminder(user_id: str) -> dict:
    """Get the most recently created reminder for a user"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                SELECT id, content, scheduled_time, timezone, status, created_at
                FROM reminders 
                WHERE user_id = %s
                ORDER BY created_at DESC
                LIMIT 1
                """
                cursor.execute(query, (user_id,))
                reminder = cursor.fetchone()
                if reminder:
                    logging.info(f"📝 Found last created reminder for {user_id}")
                return reminder
    except Exception as e:
        logging.error(f"❌ Error getting last created reminder: {e}")
        return None

def cancel_reminder(reminder_id: int, cancelled_by: str) -> bool:
    """Cancel a specific reminder"""
    try:
        with get_db_connection() as conn:
            with conn.cursor() as cursor:
                query = """
                UPDATE reminders 
                SET status = 'cancelled', 
                    cancelled_at = UTC_TIMESTAMP(),
                    cancelled_by = %s
                WHERE id = %s AND status = 'pending'
                """
                cursor.execute(query, (cancelled_by, reminder_id))
                conn.commit()
                
                if cursor.rowcount > 0:
                    logging.info(f"✅ Cancelled reminder {reminder_id}")
                    return True
                else:
                    logging.info(f"ℹ️ No pending reminder found with ID {reminder_id}")
                    return False
    except Exception as e:
        logging.error(f"❌ Error cancelling reminder: {e}")
        return False

def get_any_reminder_timezone(user_id):
    """Get timezone from any existing reminder (including cancelled ones)"""
    try:
        with get_db_connection() as conn:
            with conn.cursor(pymysql.cursors.DictCursor) as cursor:
                # Query to get timezone from any reminder for this user
                query = """
                    SELECT timezone 
                    FROM reminders 
                    WHERE user_id = %s 
                    AND timezone IS NOT NULL 
                    AND timezone != 'UTC'
                    ORDER BY created_at DESC
                    LIMIT 1
                """
                
                cursor.execute(query, (user_id,))
                result = cursor.fetchone()
                
                if result and result['timezone']:
                    logging.info(f"✅ Found timezone from any reminder: {result['timezone']}")
                    return result['timezone']
                
                return None
    except Exception as e:
        logging.error(f"❌ Error getting any reminder timezone: {e}")
        return None 