import time
import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import logging
import os
import signal
from dotenv import load_dotenv

load_dotenv()

# Niveau configurable via .env (LOG_LEVEL=DEBUG/INFO/WARNING/...) pour rester
# modulable selon l'environnement (dev en local, prod sur alwaysdata/VPS...).
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

from utils.database import create_pool, close_pool, get_pool
from utils.error_handler import DiscordErrorHandler
from utils.setupdatabase import init_db

Token = os.getenv("DISCORD_TOKEN")
if not Token:
    raise RuntimeError("DISCORD_TOKEN non défini")

# Pour les alertes d'erreur en MP (voir utils/error_handler.py). Optionnel :
# sans OWNER_ID, le bot tourne normalement mais sans alerte proactive.
OWNER_ID = os.getenv("OWNER_ID")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # nécessaire pour on_member_join/on_member_remove et fetch_member

bot = commands.Bot(command_prefix="!", intents=intents)


@tasks.loop(seconds=120)
async def ticket_watcher():
    await bot.wait_until_ready()

    now = int(time.time())
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
            SELECT thread_id, last_message, warn_12h, closed_at, statut
            FROM ticket
            """)
            tickets = await cur.fetchall()

            for thread_id, last_msg, warn_12h, closed_at, statut in tickets:
                try:
                    thread = bot.get_channel(thread_id)
                    if not thread:
                        try:
                            thread = await bot.fetch_channel(thread_id)
                        except discord.NotFound:
                            # Le thread n'existe plus : on nettoie l'entrée orpheline.
                            await cur.execute("DELETE FROM ticket WHERE thread_id = %s", (thread_id,))
                            continue
                        except discord.HTTPException:
                            continue

                    # ---------------------------------------------------------
                    # ⛔ TICKET DÉJÀ FERMÉ
                    # Si le ticket est marqué comme fermé (statut = 3),
                    # on vérifie simplement si 24h se sont écoulées depuis
                    # la fermeture pour supprimer le thread automatiquement.
                    # ---------------------------------------------------------
                    if statut == 3:
                        if closed_at and now >= closed_at + (24 * 3600):
                            await thread.delete(reason="Ticket fermé depuis plus de 24h.")
                            await cur.execute("DELETE FROM ticket WHERE thread_id = %s", (thread_id,))
                        continue

                    # ---------------------------------------------------------
                    # 🕒 TICKET ACTIF
                    # last_msg = timestamp du dernier message dans le ticket.
                    # ---------------------------------------------------------
                    if last_msg is None:
                        continue

                    inactivity = now - last_msg

                    # ---------------------------------------------------------
                    # ⚠️ AVERTISSEMENT APRÈS 12 HEURES
                    # ---------------------------------------------------------
                    if inactivity >= 12 * 3600 and not warn_12h:
                        await thread.send("⚠️ Ticket inactif depuis 12h.")
                        await cur.execute(
                            "UPDATE ticket SET warn_12h = 1 WHERE thread_id = %s",
                            (thread_id,)
                        )

                    # ---------------------------------------------------------
                    # 🔒 FERMETURE AUTOMATIQUE APRÈS 24 HEURES
                    # ---------------------------------------------------------
                    if inactivity >= 24 * 3600:
                        await thread.send("🔒 Ticket fermé pour inactivité.")
                        await thread.edit(archived=True, locked=True)
                        await cur.execute("""
                        UPDATE ticket
                        SET statut = 3, closed_at = %s
                        WHERE thread_id = %s
                        """, (now, thread_id))
                except Exception as e:
                    logger.error(f"[ticket_watcher] Erreur sur le ticket {thread_id} : {e}")

        await conn.commit()


# ---------------------------------------------------------
# 👮 SURVEILLANCE DES PÉRIODES DE TEST STAFF
# Vérifie si les 7 jours de test d'un staff sont terminés.
# Si oui, le bot envoie un message demandant si le membre
# doit rester staff ou être retiré du staff.
# ---------------------------------------------------------
@tasks.loop(minutes=30)
async def staff_test_watcher():
    await bot.wait_until_ready()

    now = int(time.time())
    pool = get_pool()

    guild_id = os.getenv("GUILD_ID")
    channel_id = os.getenv("CHANNEL_MODO_ID")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
            SELECT user_id, role_id, end_time
            FROM role_temp
            """)
            rows = await cur.fetchall()

            for user_id, role_id, end_time in rows:
                if now < end_time:
                    continue

                try:
                    if not guild_id:
                        continue

                    guild = bot.get_guild(int(guild_id))
                    if guild is None:
                        guild = await bot.fetch_guild(int(guild_id))

                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            await cur.execute("DELETE FROM role_temp WHERE user_id = %s", (user_id,))
                            continue

                    if not channel_id:
                        continue

                    staff_channel = bot.get_channel(int(channel_id))
                    if staff_channel is None:
                        staff_channel = await bot.fetch_channel(int(channel_id))

                    if not staff_channel:
                        continue

                    embed = discord.Embed(
                        title="Fin de période de test",
                        description=f"La période de test de {member.mention} est terminée.\n Voulez‑vous **le garder dans le staff** ou **retirer son rôle** ?",
                        color=discord.Color.orange()
                    )

                    await staff_channel.send(embed=embed)

                    # On supprime l'entrée pour éviter de redemander
                    await cur.execute("DELETE FROM role_temp WHERE user_id = %s", (user_id,))
                except Exception as e:
                    logger.error(f"[staff_test_watcher] Erreur pour l'utilisateur {user_id} : {e}")

        await conn.commit()


@tasks.loop(seconds=10)
async def cycle_status():
    activities = [
        discord.Game("Anime Pixel Party"),
        discord.Activity(type=discord.ActivityType.watching, name="La version 1.1.2"),
        discord.Activity(type=discord.ActivityType.listening, name="Les membres de Pixel Party"),
    ]

    activity = activities[cycle_status.current_loop % len(activities)]
    await bot.change_presence(activity=activity)


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ Tu n'as pas la permission d'utiliser cette commande."
    elif isinstance(error, app_commands.CommandOnCooldown):
        message = f"⏳ Cette commande est en cooldown, réessaie dans {error.retry_after:.0f}s."
    else:
        logger.error(f"[Erreur commande] /{interaction.command.name if interaction.command else '?'} : {error}")
        message = "❌ Une erreur inattendue est survenue en exécutant cette commande."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


@bot.event
async def on_ready():
    logger.info(f"Connecté en tant que {bot.user}")
    if not hasattr(bot, "synced"):
        await bot.tree.sync()
        bot.synced = True

    if not ticket_watcher.is_running():
        ticket_watcher.start()

    if not cycle_status.is_running():
        cycle_status.start()

    if not staff_test_watcher.is_running():
        staff_test_watcher.start()


COGS = [
    "cogs.boutique",
    "cogs.profile",
    "cogs.tickets",
    "cogs.events",
    "cogs.trade",
    "cogs.visite",
    "cogs.setupticket",
    "cogs.warn",
    "cogs.recrutement",
]


async def main():
    # Le pool de connexions MariaDB doit exister avant tout chargement de cog
    # (plusieurs cogs font des requêtes dès leur mise en place).
    pool = await create_pool()

    if OWNER_ID:
        logging.getLogger().addHandler(DiscordErrorHandler(bot, int(OWNER_ID)))
    else:
        logger.warning("OWNER_ID non défini : les alertes d'erreur par MP sont désactivées.")

    # Arrêt propre sur SIGTERM (systemd, pm2, docker stop, redéploiement...)
    # et SIGINT (Ctrl+C) : ferme proprement la connexion Discord, ce qui
    # laisse le `finally` ci-dessous fermer aussi le pool MariaDB au lieu de
    # couper le process brutalement. add_signal_handler n'existe pas sur
    # l'event loop Windows : on l'ignore silencieusement dans ce cas, Ctrl+C
    # reste géré via le KeyboardInterrupt tout en bas du fichier.
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.close()))
        except NotImplementedError:
            pass

    try:
        await init_db(pool)  # ✅ Crée la DB et toutes les tables avant les cogs

        async with bot:
            for cog in COGS:
                try:
                    await bot.load_extension(cog)
                    logger.info(f"[Cog] {cog} chargé.")
                except Exception as e:
                    logger.critical(f"[Cog] ERREUR {cog} : {e}", exc_info=True)

            await bot.start(Token)
    finally:
        await close_pool()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot arrêté.")
