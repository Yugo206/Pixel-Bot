import os
import sqlite3

DB_PATH = os.path.expanduser("~/botdata/database.db")

def db_test():
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("INSERT INTO warns(user_id, guild_id, modo_id, raison, created_at, created_at_iso) VALUES(1, 1, 1, 'test', 1700000000, '2026-01-19T15:00:00Z');")
        conn.commit()
        conn.close()
    except sqlite3.OperationalError as e:
        print(e)

db_test()


