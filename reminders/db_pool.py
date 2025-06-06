import aiomysql
import os
from config import DB_HOST, DB_USER, DB_PASSWORD, DB_NAME

db_pool = None

async def create_db_pool():
    global db_pool
    db_host = os.environ.get('DB_HOST', DB_HOST)
    db_user = os.environ.get('DB_USER', DB_USER)
    db_password = os.environ.get('DB_PASSWORD', DB_PASSWORD)
    db_name = os.environ.get('DB_NAME', DB_NAME)
    db_port = int(os.environ.get('DB_PORT', 3306))
    print("[aiomysql] Attempting to connect to database.", flush=True)
    try:
        db_pool = await aiomysql.create_pool(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_password,
            db=db_name,
            autocommit=True,
            charset="utf8mb4"
        )
    except Exception as e:
        import traceback, sys
        print(f"[aiomysql ERROR] Could not connect: {e}", flush=True)
        traceback.print_exc(file=sys.stdout)
        raise
    print(f"[db_pool.py] db_pool after creation: {db_pool} id={id(db_pool)} type={type(db_pool)}", flush=True)
    return db_pool
