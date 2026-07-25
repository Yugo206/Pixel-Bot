import warnings

import aiomysql
import pymysql
from dotenv import load_dotenv

load_dotenv()

TABLES = {
    "utilisateurs": [
        "user_id BIGINT PRIMARY KEY",
        "argent INT DEFAULT 0",
        "xp INT DEFAULT 0",
        "niveau INT DEFAULT 0",
        "nb_tickets_open INT DEFAULT 0",
        "warn INT DEFAULT 0",
        "commun INT DEFAULT 0",
        "rare INT DEFAULT 0",
        "epique INT DEFAULT 0",
        "mytique INT DEFAULT 0",
        "legendaire INT DEFAULT 0",
        "secret INT DEFAULT 0",
    ],

    "inventaire": [
        "user_id BIGINT NOT NULL",
        "item_id INT NOT NULL",
        "quantite INT NOT NULL DEFAULT 0",
        "PRIMARY KEY(user_id, item_id)"
    ],

    "role_temp": [
        "user_id BIGINT NOT NULL",
        "role_id BIGINT NOT NULL",
        "end_time BIGINT NOT NULL"
    ],

    "shop": [
        # VARCHAR (et non TEXT) car cette colonne est la clé primaire : InnoDB a besoin
        # d'une longueur fixe pour indexer une clé.
        "name VARCHAR(100) PRIMARY KEY",
        "price INT NOT NULL",
        "type INT NOT NULL",
        # BIGINT : "valeur" peut contenir un id de rôle Discord (snowflake), qui dépasse
        # largement la plage d'un INT 32 bits.
        "valeur BIGINT NOT NULL",
        "duration INT"
    ],

    "temp_bans": [
        "user_id BIGINT",
        "unban_at BIGINT"
    ],

    "ticket": [
        "ticket_id INT PRIMARY KEY AUTO_INCREMENT",
        "thread_id BIGINT NOT NULL",
        "membre_id BIGINT NOT NULL",
        "modo_id BIGINT",
        "statut INT NOT NULL",
        "raison TEXT NOT NULL",
        "last_message BIGINT",
        "warn_12h INT",
        "closed_at BIGINT",
        "modo_message_id BIGINT",
        "message_ticket_id BIGINT"
    ],
    "role_special": [
        "id INT NOT NULL PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "role_id BIGINT",
        "status INT NOT NULL DEFAULT 0",
        "message_accepter_id BIGINT",
    ],

    "warns": [
        "id INT PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "modo_id BIGINT NOT NULL",
        "raison TEXT",
        "created_at BIGINT",
        "created_at_iso VARCHAR(64)"
    ],

    "contestations": [
        "message_id BIGINT PRIMARY KEY",
        "membre_id BIGINT NOT NULL",
        "warn_id INT",
        "warn_raison TEXT",
        "warn_created_at BIGINT",
    ],

    "shop_temp_roles": [
        "id INT PRIMARY KEY AUTO_INCREMENT",
        "user_id BIGINT NOT NULL",
        "role_id BIGINT NOT NULL",
        "end_time BIGINT NOT NULL",
    ],
}


async def init_db(pool: aiomysql.Pool):
    """Crée les tables manquantes et ajoute les colonnes manquantes sur celles qui existent déjà.
    Suppose que la base (DB_NAME) existe déjà sur le serveur MariaDB."""
    async with pool.acquire() as conn:
        async with conn.cursor() as c:
            for table, columns in TABLES.items():
                # 1️⃣ Création de la table (le "IF NOT EXISTS" fait émettre un warning
                # MariaDB anodin quand la table existe déjà : on le supprime volontairement).
                create_sql = f"CREATE TABLE IF NOT EXISTS {table} ({', '.join(columns)})"
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", category=pymysql.Warning)
                    await c.execute(create_sql)

                # 2️⃣ Colonnes existantes
                await c.execute(
                    "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s",
                    (table,)
                )
                existing_columns = {row[0] for row in await c.fetchall()}

                # 3️⃣ Ajout des colonnes manquantes
                for col in columns:
                    if col.startswith("PRIMARY KEY"):
                        continue

                    col_name = col.split()[0]
                    if col_name not in existing_columns:
                        await c.execute(f"ALTER TABLE {table} ADD COLUMN {col}")

        await conn.commit()
