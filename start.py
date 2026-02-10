# main.py
import sqlite3
import time
import discord
from discord.ext import commands, tasks
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from utils.setupdatabase import DB_PATH
from dotenv import load_dotenv
load_dotenv()
from utils.setupdatabase import init_db
init_db()  # ✅ Crée la DB et toutes les tables avant les cogs

try:
    # Chargement config
    try:
        Token = os.getenv("DISCORD_TOKEN")
        print(Token)
    except Exception as e:
        print(e)
    # main.py

    if not Token:
        raise RuntimeError("DISCORD_TOKEN non défini")

    intents = discord.Intents.default()
    intents.message_content = True

    bot = commands.Bot(command_prefix="!", intents=intents)


    @tasks.loop(seconds=120)
    async def ticket_watcher():
        await bot.wait_until_ready()

        now = time.time()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        cur.execute("""
        SELECT thread_id, last_message, warn_12h, closed_at, statut
        FROM ticket
        """)
        tickets = cur.fetchall()

        for thread_id, last_msg, warn_12h, closed_at, statut in tickets:
            thread = bot.get_channel(thread_id)
            if not thread:
                continue

            # ⛔ TICKET FERMÉ → ON NE TOUCHE PLUS
            if statut == 3:
                if closed_at and now - closed_at > 24 * 3600:
                    await thread.delete()
                    cur.execute(
                        "DELETE FROM ticket WHERE thread_id = ?",
                        (thread_id,)
                    )
                continue

            if last_msg is None:
                continue

            inactivity = now - last_msg

            if inactivity > 12 * 3600 and not warn_12h:
                await thread.send("⚠️ Ticket inactif depuis 12h.")
                cur.execute(
                    "UPDATE ticket SET warn_12h = 1 WHERE thread_id = ?",
                    (thread_id,)
                )

            if inactivity > 24 * 3600:
                await thread.send("🔒 Ticket fermé pour inactivité.")
                await thread.edit(archived=True, locked=True)
                cur.execute("""
                UPDATE ticket
                SET statut = 3, closed_at = ?
                WHERE thread_id = ?
                """, (now, thread_id))

        conn.commit()
        conn.close()


    @tasks.loop(seconds=10)
    async def cycle_status():
        activities = [
            discord.Game("Anime Pixel Party"),
            discord.Activity(type=discord.ActivityType.watching, name="La version 1.1.2"),
            discord.Activity(type=discord.ActivityType.listening, name="Les membres de Pixel Party"),
        ]

        activity = activities[cycle_status.current_loop % len(activities)]
        await bot.change_presence(activity=activity)


    @bot.event
    async def on_ready():
        print(f"Connecté en tant que {bot.user}")
        await bot.tree.sync()

        if not ticket_watcher.is_running():
            ticket_watcher.start()

        if not cycle_status.is_running():
            cycle_status.start()


    async def setup_hook():
            async with bot:
                COGS = [
                    "cogs.boutique",
                    "cogs.profile",
                    "cogs.tickets",
                    "cogs.events",
                    "cogs.trade",
                    "cogs.visite",
                    "cogs.setupticket",
                    "cogs.warn",
                    "cogs.getdb"
                ]

                for cog in COGS:
                    try:
                        await bot.load_extension(cog)
                        print(f"[Cog] {cog} chargé.")
                    except Exception as e:
                        print(f"[Cog] ERREUR {cog} :", e)

                await bot.start(Token)



    if __name__ == "__main__":
        try:
            asyncio.run(setup_hook())
        except KeyboardInterrupt:
            print("Bot arrêté depuis PyCharm.")

except Exception as e:
    print(e)



