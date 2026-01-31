# main.py
import sqlite3
import time
import discord
from discord.ext import commands, tasks
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from cogs.tickets import TicketCreateView
DB_PATH = os.path.expanduser("~/botdata/database.db")
from os import getenv
from discord.ext.commands import command
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

    @tasks.loop(seconds=30)
    async def ticket_watcher(bot):
        await bot.wait_until_ready()

        while not bot.is_closed():
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
                if last_msg == None:
                    continue
                inactivity = now - last_msg

                # ⚠️ 12h WARNING
                if statut == 2 and inactivity > 24 * 3600 and not warn_12h:
                    await thread.send("⚠️ Ticket inactif depuis 12h.")
                    cur.execute(
                        "UPDATE ticket SET warn_12h = 1 WHERE thread_id = ?",
                        (thread_id,)
                    )

                # 🔒 24h FERMETURE
                if statut == 2 and inactivity > 24 * 3600:
                    await thread.send("🔒 Ticket fermé pour inactivité.")
                    await thread.edit(archived=True, locked=True)

                    cur.execute("""
                    UPDATE ticket
                    SET statut = 3, closed_at = ?
                    WHERE thread_id = ?
                    """, (now, thread_id))

                # 🗑️ 24h APRÈS FERMETURE → SUPPRESSION
                if statut == 3 and closed_at and now - closed_at > 24 * 3600:
                    await thread.delete()
                    cur.execute(
                        "DELETE FROM ticket WHERE thread_id = ?",
                        (thread_id,)
                    )

            conn.commit()
            conn.close()
            await asyncio.sleep(120)  # toutes les 5 minutes
        @cycle_status.before_loop
        async def before_ticket_watcher():
            await bot.wait_until_ready()


    @tasks.loop(seconds=30)
    async def cycle_status():
        await bot.change_presence(activity=discord.Game("Anime Pixel Party"))
        await asyncio.sleep(10)

        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="La version 1.1.2"
            )
        )
        await asyncio.sleep(10)

        await bot.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening,
                name="Les membres de Pixel Party"
            )
        )


    @cycle_status.before_loop
    async def before_cycle_status():
        await bot.wait_until_ready()


    @bot.event
    async def on_ready():
        print(f"Connecté en tant que {bot.user} (id: {bot.user.id})")
        synced = await bot.tree.sync()
        print(f"Commandes slash synchronisées : {len(synced)}")





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
                    "cogs.partenariat",
                    "cogs.warn"
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



