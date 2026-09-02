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
from utils.config import load_config, get_config
from cogs.tickets import demander_confirmation_moderateur

Token = os.getenv("DISCORD_TOKEN")
if not Token:
    raise RuntimeError("DISCORD_TOKEN non défini")

intents = discord.Intents.none()
intents.guilds = True  # cœur : accès aux serveurs, salons, threads
intents.guild_messages = True  # on_message (XP, suivi des tickets, préfixe !)
intents.message_content = True  # lecture du contenu (commandes préfixées, wait_for)
intents.members = True  # nécessaire pour on_member_join/on_member_remove et fetch_member
# Intents.default() active aussi reactions/typing/voice/invites/webhooks/emojis/
# scheduled_events/auto_moderation etc., qu'aucun cog n'utilise (vérifié : pas
# d'écouteur on_reaction/on_typing, pas de PyNaCl/voix, pas de wait_for en DM).
# Les laisser désactivées réduit le volume d'évènements gateway à traiter — utile
# vu le quota RAM/CPU limité sur alwaysdata.

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    # Cache interne des messages (édition/suppression) inutilisé par le bot
    # (aucun on_message_edit/on_message_delete, aucun get_message) : le désactiver
    # évite de garder ~1000 objets Message en mémoire pour rien.
    max_messages=None,
)


@tasks.loop(seconds=120)
async def ticket_watcher():
    await bot.wait_until_ready()

    now = int(time.time())
    pool = get_pool()

    # Une seule connexion pour lister les tickets : suffisant, rapide, et on la relâche
    # tout de suite après (voir plus bas, chaque étape reprend sa propre connexion —
    # sinon un seul appel Discord lent pendant la boucle garde une connexion du pool
    # occupée pour rien, alors que le pool est volontairement restreint, voir
    # utils/database.py).
    try:
        async with pool.acquire() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                SELECT thread_id, last_message, warn_12h, closed_at, statut, modo_id
                FROM ticket
                """)
                tickets = await cur.fetchall()
    except Exception as e:
        # Le corps de la boucle par ticket est déjà protégé (try/except plus bas),
        # mais sans ce garde-fou une erreur DB transitoire ici lèverait hors de
        # ticket_watcher() : discord.ext.tasks arrête alors la boucle
        # définitivement (pas de relance auto), silencieusement — plus aucune
        # relance d'inactivité ni fermeture automatique de ticket jusqu'au
        # redémarrage du bot.
        logger.error(f"[ticket_watcher] Erreur lors de la liste des tickets, on réessaiera au prochain passage : {e}")
        return

    for thread_id, last_msg, warn_12h, closed_at, statut, modo_id in tickets:
        try:
            thread = bot.get_channel(thread_id)
            if not thread:
                try:
                    thread = await bot.fetch_channel(thread_id)
                except discord.NotFound:
                    # Le thread n'existe plus : on nettoie l'entrée orpheline.
                    async with pool.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("DELETE FROM ticket WHERE thread_id = %s", (thread_id,))
                        await conn.commit()
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
                    async with pool.acquire() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute("DELETE FROM ticket WHERE thread_id = %s", (thread_id,))
                        await conn.commit()
                continue

            # ---------------------------------------------------------
            # 🕒 TICKET ACTIF
            # last_msg = timestamp du dernier message du membre dans le ticket
            # (voir on_message dans cogs/events.py, qui remet aussi warn_12h à
            # NULL dès qu'il répond).
            # ---------------------------------------------------------
            if last_msg is None:
                continue

            inactivity = now - last_msg

            # ---------------------------------------------------------
            # ⚠️ AVERTISSEMENT APRÈS 24 HEURES D'INACTIVITÉ
            # UPDATE conditionné à "AND warn_12h IS NULL" + vérification du
            # rowcount AVANT d'envoyer le message : réserve la ligne de façon
            # atomique. Sans ça, deux passages (ou deux process du bot lancés
            # en même temps) qui lisent tous les deux warn_12h avant que l'un
            # des deux commit envoient chacun leur propre avertissement — le
            # bug qui faisait apparaître le message plusieurs fois pour un
            # même ticket.
            # ---------------------------------------------------------
            if inactivity >= 24 * 3600 and not warn_12h:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE ticket SET warn_12h = 1 WHERE thread_id = %s AND warn_12h IS NULL",
                            (thread_id,)
                        )
                        gagne = cur.rowcount == 1
                    await conn.commit()

                if gagne:
                    await thread.send("⚠️ Ticket inactif depuis 24h.")

            # ---------------------------------------------------------
            # 🔒 FERMETURE AUTOMATIQUE 48H APRÈS L'AVERTISSEMENT
            # (24h + 48h = 72h d'inactivité totale). Même principe de
            # réservation atomique que ci-dessus.
            # ---------------------------------------------------------
            if inactivity >= 72 * 3600:
                async with pool.acquire() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "UPDATE ticket SET statut = 3, closed_at = %s WHERE thread_id = %s AND statut != 3",
                            (now, thread_id)
                        )
                        gagne = cur.rowcount == 1
                    await conn.commit()

                if not gagne:
                    continue

                await thread.send("🔒 Ticket fermé pour inactivité.")
                await thread.edit(archived=True, locked=True)

                # MP au modérateur assigné pour savoir si tout s'est bien passé
                # (pas de message dans le ticket, déjà archivé/verrouillé à ce
                # stade) — voir demander_confirmation_moderateur dans cogs/tickets.py.
                if modo_id:
                    await demander_confirmation_moderateur(bot, thread, modo_id)
        except Exception as e:
            logger.error(f"[ticket_watcher] Erreur sur le ticket {thread_id} : {e}")


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

    guild_id = get_config("GUILD_ID")
    channel_id = get_config("CHANNEL_MODO_ID")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT id, user_id, role_id FROM temp_roles WHERE origin = 'staff_test' AND end_time <= %s",
                (now,)
            )
            rows = await cur.fetchall()

            for row_id, user_id, role_id in rows:
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
                            await cur.execute("DELETE FROM temp_roles WHERE id = %s", (row_id,))
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
                    await cur.execute("DELETE FROM temp_roles WHERE id = %s", (row_id,))
                except Exception as e:
                    logger.error(f"[staff_test_watcher] Erreur pour l'utilisateur {user_id} : {e}")

        await conn.commit()


@tasks.loop(seconds=60)
async def cycle_status():
    activities = [
        discord.Game("Anime Pixel Party"),
        discord.Activity(type=discord.ActivityType.watching, name="La version 2.0.0"),
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
    "cogs.creermessage",
    "cogs.warn",
    "cogs.recrutement",
]


async def main():
    # Le pool de connexions MariaDB doit exister avant tout chargement de cog
    # (plusieurs cogs font des requêtes dès leur mise en place).
    pool = await create_pool()

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
        await load_config(pool)  # ✅ Charge la table `config` avant les cogs (voir utils/config.py)

        # OWNER_ID vient maintenant de la table `config` (voir _migrate_env_to_config
        # dans utils/setupdatabase.py) : il ne peut donc être lu qu'une fois init_db()
        # + load_config() passés, contrairement à avant où l'alerte MP pouvait couvrir
        # tout main(). Les erreurs survenant avant ce point (création du pool,
        # migration du schéma) ne sont donc plus notifiées par MP, seulement loguées.
        owner_id = get_config("OWNER_ID")
        if owner_id:
            logging.getLogger().addHandler(DiscordErrorHandler(bot, int(owner_id)))
        else:
            logger.warning("OWNER_ID non défini (table config) : les alertes d'erreur par MP sont désactivées.")

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
