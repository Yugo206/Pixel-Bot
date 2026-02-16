import sqlite3
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
DB_PATH = os.getenv("CHEMIN_DB")
if DB_PATH is None:
    raise RuntimeError("CHEMIN_DB non défini")
DB_PATH = Path(DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

TABLES = {
    "utilisateurs": [
        "user_id INTEGER PRIMARY KEY",
        "argent INTEGER DEFAULT 0",
        "xp INTEGER DEFAULT 0",
        "niveau INTEGER DEFAULT 0",
        "nb_tickets_open INTEGER DEFAULT 0",
        "warn INTEGER DEFAULT 0",
        "commun INTEGER DEFAULT 0",
        "rare INTEGER DEFAULT 0",
        "epique INTEGER DEFAULT 0",
        "mytique INTEGER DEFAULT 0",
        "legendaire INTEGER DEFAULT 0",
        "secret INTEGER DEFAULT 0",
    ],

    "inventaire": [
        "user_id INTEGER NOT NULL",
        "item_id INTEGER NOT NULL",
        "quantite INTEGER NOT NULL DEFAULT 0",
        "PRIMARY KEY(user_id, item_id)"
    ],

    "role_temp": [
        "user_id INTEGER NOT NULL",
        "role_id INTEGER NOT NULL",
        "end_time INTEGER NOT NULL"
    ],

    "shop": [
        "name TEXT PRIMARY KEY",
        "price INTEGER NOT NULL",
        "type INTEGER NOT NULL",
        "valeur INTEGER NOT NULL",
        "duration INTEGER"
    ],

    "temp_bans": [
        "user_id INTEGER",
        "unban_at INTEGER"
    ],

    "ticket": [
        "ticket_id INTEGER PRIMARY KEY AUTOINCREMENT",
        "thread_id INTEGER NOT NULL",
        "membre_id INTEGER NOT NULL",
        "modo_id INTEGER",
        "statut INTEGER NOT NULL",
        "raison INTEGER NOT NULL",
        "last_message INTEGER",
        "warn_12h INTEGER",
        "closed_at INTEGER",
        "modo_message_id INTEGER"
    ],

    "warns": [
        "id INTEGER PRIMARY KEY AUTOINCREMENT",
        "user_id INTEGER NOT NULL",
        "modo_id INTEGER NOT NULL",
        "raison TEXT",
        "created_at INTEGER",
        "created_at_iso TEXT"
    ],
}

def init_db():
    with sqlite3.connect(DB_PATH) as db:
        c = db.cursor()

        for table, columns in TABLES.items():
            # 1️⃣ Création de la table
            create_sql = f"""
            CREATE TABLE IF NOT EXISTS {table} (
                {", ".join(columns)}
            )
            """
            c.execute(create_sql)

            # 2️⃣ Colonnes existantes
            c.execute(f"PRAGMA table_info({table})")
            existing_columns = {row[1] for row in c.fetchall()}

            # 3️⃣ Ajout des colonnes manquantes
            for col in columns:
                if col.startswith("PRIMARY KEY"):
                    continue

                col_name = col.split()[0]
                if col_name not in existing_columns:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

        db.commit()
