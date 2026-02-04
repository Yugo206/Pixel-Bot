import discord
import sqlite3
from discord.ext import commands
import os
from dotenv import load_dotenv
load_dotenv()
DB_PATH = os.getenv("CHEMIN_DB")
if DB_PATH is None:
    raise RuntimeError("CHEMIN_DB non défini")

if not os.path.exists(DB_PATH):
    print("Database not found")
print("DB = ", DB_PATH)


class SetupDatabaseCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    @commands.has_permissions(administrator=True)
    async def setup_database(self, ctx):
        print("database setup")
        if ctx.author.guild_permissions.administrator:
            await ctx.message.delete()
            with (sqlite3.connect(DB_PATH) as db):
                cursor = db.cursor()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "inventaire" ("user_id"	INTEGER NOT NULL,"item_id"	INTEGER NOT NULL,"quantite"	INTEGER NOT NULL DEFAULT 0,PRIMARY KEY("user_id","item_id"));""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "role-temp" (
                "user_id"	INTEGER NOT NULL,
                "role_id"	INTEGER NOT NULL,
                "end_time"	INTEGER NOT NULL);""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "shop" (
                "name"	TEXT NOT NULL,
                "price"	INTEGER NOT NULL,
                "type"	INTEGER NOT NULL,
                "valeur"	INTEGER NOT NULL,
                "duration"	INTEGER DEFAULT NULL,
                PRIMARY KEY("name")
                );""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "temp_bans" (
                "user_id"	INTEGER,
                "unban_at"	INTEGER);""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "ticket" (
                "ticket_id"	INTEGER NOT NULL,
                "thread_id"	INTEGER NOT NULL,
                "membre_id"	INTEGER NOT NULL,
                "modo_id"	INTEGER,
                "statut"	INTEGER NOT NULL,
                "raison"	INTEGER NOT NULL,
                "last_message"	INTEGER,
                "warn_12h"	INTEGER,
                "closed_at"	INTEGER,
                PRIMARY KEY("ticket_id" AUTOINCREMENT));""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "utilisateurs" (
                "user_id"	INTEGER,
                "argent"	INTEGER NOT NULL DEFAULT 0,
                "xp"	INTEGER NOT NULL DEFAULT 0,
                "niveau"	INTEGER NOT NULL DEFAULT 0,
                "nb_tickets_open"	INTEGER NOT NULL DEFAULT 0,
                "warn"	INTEGER NOT NULL DEFAULT 0,
                "commun"	INTEGER DEFAULT 0,
                "rare"	INTEGER DEFAULT 0,
                "epique"	INTEGER DEFAULT 0,
                "mytique"	INTEGER DEFAULT 0,
                "legendaire"	INTEGER DEFAULT 0,
                "secret"	INTEGER DEFAULT 0,
                PRIMARY KEY("user_id"));""")
                db.commit()
                cursor.execute("""CREATE TABLE IF NOT EXISTS "warns" (
                "id"	INTEGER,
                "user_id"	INTEGER NOT NULL,
                "modo_id"	INTEGER NOT NULL,
                "raison"	TEXT,
                "created_at"	INTEGER,
                "created_at_iso"	TEXT,
                PRIMARY KEY("id" AUTOINCREMENT));""")
                db.commit()



async def setup(bot):
    await bot.add_cog(SetupDatabaseCog(bot))

