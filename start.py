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
                try:
                    thread = await bot.fetch_channel(thread_id)
                except Exception:
                    continue

            # ---------------------------------------------------------
            # ⛔ TICKET DÉJÀ FERMÉ
            # Si le ticket est marqué comme fermé (statut = 3),
            # on vérifie simplement si 24h se sont écoulées depuis
            # la fermeture pour supprimer le thread automatiquement.
            # ---------------------------------------------------------
            if statut == 3:
                if closed_at:
                    # Vérifie si la date actuelle dépasse la date
                    # de fermeture + 24 heures
                    if now >= closed_at + (24 * 3600):
                        await thread.delete(reason="Ticket fermé depuis plus de 24h.")
                        cur.execute(
                            "DELETE FROM ticket WHERE thread_id = ?",
                            (thread_id,)
                        )
                continue

            # ---------------------------------------------------------
            # 🕒 TICKET ACTIF
            # Si le ticket n'est pas fermé, on vérifie son activité.
            # last_msg = timestamp du dernier message dans le ticket.
            # ---------------------------------------------------------
            if last_msg is None:
                continue

            # Temps d'inactivité du ticket
            inactivity = now - last_msg

            # ---------------------------------------------------------
            # ⚠️ AVERTISSEMENT APRÈS 12 HEURES
            # Si aucun message n'a été envoyé depuis 12h,
            # on envoie un avertissement dans le thread.
            # warn_12h empêche d'envoyer le message plusieurs fois.
            # ---------------------------------------------------------
            if inactivity >= 12 * 3600 and not warn_12h:
                await thread.send("⚠️ Ticket inactif depuis 12h.")
                cur.execute(
                    "UPDATE ticket SET warn_12h = 1 WHERE thread_id = ?",
                    (thread_id,)
                )

            # ---------------------------------------------------------
            # 🔒 FERMETURE AUTOMATIQUE APRÈS 24 HEURES
            # Si aucune activité pendant 24h, le ticket est fermé :
            # - on archive
            # - on verrouille
            # - on stocke la date de fermeture
            # ---------------------------------------------------------
            if inactivity >= 24 * 3600:
                await thread.send("🔒 Ticket fermé pour inactivité.")
                await thread.edit(archived=True, locked=True)
                cur.execute("""
                UPDATE ticket
                SET statut = 3, closed_at = ?
                WHERE thread_id = ?
                """, (now, thread_id))

        conn.commit()
        conn.close()

    # ---------------------------------------------------------
    # 👮 SURVEILLANCE DES PÉRIODES DE TEST STAFF
    # Vérifie si les 7 jours de test d'un staff sont terminés.
    # Si oui, le bot envoie un message demandant si le membre
    # doit rester staff ou être retiré du staff.
    # ---------------------------------------------------------
    @tasks.loop(minutes=30)
    async def staff_test_watcher():
        await bot.wait_until_ready()

        now = time.time()
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()

        # On récupère les membres avec un rôle temporaire
        cur.execute("""
        SELECT user_id, role_id, end_time
        FROM role_temp
        """)
        rows = cur.fetchall()

        for user_id, role_id, end_time in rows:

            # Si la période de test est terminée
            if now >= end_time:

                guild_id = int(os.getenv("GUILD_ID"))
                guild = bot.get_guild(guild_id)
                if guild is None:
                    guild = await bot.fetch_guild(guild_id)

                member = guild.get_member(user_id)
                if member is None:
                    member = await guild.fetch_member(user_id)

                if not member:
                    continue

                role = guild.get_role(role_id)
                channel_id = int(os.getenv("CHANNEL_MODO_ID"))
                staff_channel = bot.get_channel(channel_id)
                if staff_channel is None:
                    staff_channel = await bot.fetch_channel(channel_id)

                if not staff_channel:
                    continue

                embed = discord.Embed(
                    title="Fin de période de test",
                    description=f"La période de test de {member.mention} est terminée.\n Voulez‑vous **le garder dans le staff** ou **retirer son rôle** ?",
                    color=discord.Color.orange()
                )

                try:
                    await staff_channel.send(embed=embed)
                except Exception as e:
                    print("ERREUR ENVOI STAFF :", e)

                # On supprime l'entrée pour éviter de redemander
                cur.execute(
                    "DELETE FROM role_temp WHERE user_id = ?",
                    (user_id,)
                )

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
        if not hasattr(bot, "synced"):
            await bot.tree.sync()
            bot.synced = True

        if not ticket_watcher.is_running():
            ticket_watcher.start()

        if not cycle_status.is_running():
            cycle_status.start()

        if not staff_test_watcher.is_running():
            staff_test_watcher.start()


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
                    "cogs.getdb",
                    "cogs.recrutement",
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
