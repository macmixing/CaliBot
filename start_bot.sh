#!/bin/bash
# Simple startup script that waits for MySQL to be fully ready
cd /home/ec2-user/ai-assistant

# Wait for MySQL to be genuinely ready (not just started)
echo "Waiting for MySQL to be fully ready..."
for i in {1..30}; do
    if mysql -u"$DB_USER" -p"$DB_PASSWORD" -e "SELECT 1" &>/dev/null; then
        echo "MySQL is ready!"
        break
    fi
    echo "MySQL not ready yet (attempt $i/30), waiting..."
    sleep 2
done

# Start the bot in the same way you do manually
source /home/ec2-user/ai-assistant/venv/bin/activate
python3 bot.py
