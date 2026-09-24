"""/archive : transcriptions HTML des tickets fermés, réservées au staff.

Les archives sont générées à la fermeture de chaque ticket (utils/transcript.py)
et conservées dans `ticket_archives`, qui survit à la suppression du thread et de
sa ligne `ticket` (24h après la fermeture, voir ticket_watcher dans start.py).
Un modérateur ne voit que les tickets qu'il a pris en charge ou auxquels il a
participé ; l'owner voit tout, et peut cibler un modérateur en particulier.
"""
import asyncio
import io
import logging
import re
import unicodedata
import zlib
from datetime import datetime
from zoneinfo import ZoneInfo

import aiomysql
import discord
from discord import app_commands
from discord.ext import commands

from utils.database import connexion
from utils.staff import est_moderateur, est_owner, formater_duree
from utils.transcript import FUSEAU
from utils.views import TimedView

logger = logging.getLogger(__name__)

# Archives listées par /archive, les plus récemment fermées d'abord. Reste sous les
# 25 options d'un select et les 25 champs d'un embed ; au-delà, la liste deviendrait
# de toute façon illisible sans recherche.
NB_ARCHIVES = 20

PARIS = ZoneInfo(FUSEAU)

# Tickets pris en charge par ce modérateur, ou dont il fait partie des participants
# (a écrit dans le thread, ou l'a fermé — voir utils/transcript.py).
_FILTRE_MODO = (
    "(modo_id = %s OR thread_id IN "
    "(SELECT thread_id FROM ticket_archive_participants WHERE user_id = %s))"
)
# Jamais la colonne `html` (LONGBLOB, jusqu'à plusieurs Mo par ligne) dans la liste :
# elle n'est lue que pour l'archive choisie, et seulement si elle peut être envoyée.
_COLONNES_LISTE = "thread_id, nom, membre_id, modo_id, raison, closed_at, html_taille"
_COLONNES_DETAIL = (
    "nom, membre_id, modo_id, raison, created_at, first_response_at, first_response_by, "
    "closed_at, closed_by, nb_messages, html_closed_at, html_taille"
)


def _extrait(texte: str | None, longueur: int) -> str:
    """Texte sur une seule ligne, tronqué avec « … » (raison d'un ticket dans la
    liste, libellés du select limités à 100 caractères par Discord)."""
    texte = " ".join((texte or "").split())
    return texte if len(texte) <= longueur else texte[:longueur - 1] + "…"


def _nom_fichier(nom: str | None, closed_at: int) -> str:
    """Nom du fichier envoyé, en ASCII sans caractère spécial : le nom d'un thread
    de ticket reprend le pseudo du membre (accents, emojis...), que certains
    systèmes de fichiers ou navigateurs gèrent mal."""
    base = unicodedata.normalize("NFKD", nom or "").encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", base).strip("-")[:60] or "ticket"
    return f"{base}_{datetime.fromtimestamp(closed_at, PARIS):%Y-%m-%d}.html"


def _embed_archive(nom, membre_id, modo_id, raison, created_at, first_response_at,
                   first_response_by, closed_at, closed_by, nb_messages) -> discord.Embed:
    embed = discord.Embed(title=f"📄 {nom or 'Ticket'}", color=discord.Color.blurple())
    embed.add_field(name="👤 Membre", value=f"<@{membre_id}>")
    embed.add_field(name="🛡️ Pris en charge par", value=f"<@{modo_id}>" if modo_id else "Personne")
    embed.add_field(name="💬 Messages", value=str(nb_messages))
    embed.add_field(name="📝 Raison", value=_extrait(raison, 1024) or "—", inline=False)
    embed.add_field(name="📂 Ouvert", value=f"<t:{created_at}:f>")
    embed.add_field(name="🔒 Fermé", value=f"<t:{closed_at}:f>")
    embed.add_field(name="🙋 Fermé par", value=f"<@{closed_by}>" if closed_by else "Automatiquement")
    if first_response_at is not None:
        premiere_reponse = (
            f"{formater_duree(first_response_at - created_at)} après l'ouverture, "
            f"par <@{first_response_by}>"
        )
    else:
        premiere_reponse = "Aucune réponse d'un modérateur"
    embed.add_field(name="⏱️ Première réponse", value=premiere_reponse, inline=False)
    return embed


class ArchiveSelect(discord.ui.Select):
    """Choix d'une archive dans la liste de /archive : envoie sa transcription en
    fichier HTML (éphémère), avec ses métadonnées."""

    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(
            placeholder="📄 Choisir un ticket",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        # thinking=True : lecture de la base, décompression puis envoi d'un fichier
        # de plusieurs Mo peuvent prendre quelques secondes.
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self._envoyer_archive(interaction, int(self.values[0]))
        except aiomysql.Error as e:
            logger.critical(f"[archive] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
        finally:
            # Réinitialise le select (voir TimedView) : sans ça, Discord garde le
            # ticket choisi affiché comme sélectionné et il ne peut pas être
            # redemandé tant que le message n'a pas été réédité. Via rafraichir()
            # plutôt qu'en réponse au clic : on garde ainsi l'indicateur
            # « réfléchit… » ci-dessus pendant l'envoi.
            await self.view.rafraichir()

    async def _envoyer_archive(self, interaction: discord.Interaction, thread_id: int):
        user = interaction.user
        proprio = est_owner(user.id)
        # Revérifié à chaque choix : le rôle a pu être retiré depuis l'affichage
        # de la liste (qui reste utilisable 5 minutes).
        if not (proprio or est_moderateur(user)):
            await interaction.followup.send("❌ Les archives sont réservées aux modérateurs.", ephemeral=True)
            return

        # Limite d'envoi fournie par Discord pour cette interaction (niveau de boost
        # du serveur compris).
        limite = interaction.filesize_limit
        compresse = None
        async with connexion() as conn:
            async with conn.cursor() as c:
                if proprio:
                    await c.execute(
                        f"SELECT {_COLONNES_DETAIL} FROM ticket_archives WHERE thread_id = %s",
                        (thread_id,)
                    )
                else:
                    await c.execute(
                        f"SELECT {_COLONNES_DETAIL} FROM ticket_archives WHERE thread_id = %s AND {_FILTRE_MODO}",
                        (thread_id, user.id, user.id)
                    )
                ligne = await c.fetchone()
                html_taille = ligne[-1] if ligne else None
                # La page ne quitte la base que si elle peut effectivement être
                # envoyée sur ce serveur (limite selon le niveau de boost).
                if html_taille is not None and html_taille <= limite:
                    await c.execute("SELECT html FROM ticket_archives WHERE thread_id = %s", (thread_id,))
                    resultat = await c.fetchone()
                    compresse = resultat[0] if resultat else None

        if ligne is None:
            await interaction.followup.send("❌ Cette archive est introuvable ou ne t'est pas accessible.", ephemeral=True)
            return

        nom, closed_at, html_closed_at = ligne[0], ligne[7], ligne[-2]
        embed = _embed_archive(*ligne[:-2])
        envoi = {"embed": embed, "ephemeral": True}

        contenu = None
        corrompue = False
        if compresse is not None:
            try:
                # Décompression dans un thread : plusieurs Mo, la boucle du bot
                # ne doit pas se figer pendant ce temps.
                contenu = await asyncio.to_thread(zlib.decompress, compresse)
            except zlib.error as e:
                logger.error(f"[archive] Transcription du ticket {thread_id} illisible : {e}")
                corrompue = True
            del compresse

        if contenu is not None:
            envoi["file"] = discord.File(io.BytesIO(contenu), filename=_nom_fichier(nom, closed_at))
            embed.set_footer(
                text="Transcription à ouvrir dans un navigateur. Pièces jointes intégrées au fichier ; "
                     "avatars et emojis chargés depuis Internet."
            )
            if html_closed_at != closed_at:
                # Ticket rouvert puis refermé, page non régénérée la dernière fois
                # (voir _UPSERT_ARCHIVE dans utils/transcript.py).
                embed.description = (
                    f"⚠️ Transcription de la fermeture du <t:{html_closed_at}:f> : celle de la "
                    "dernière fermeture n'a pas pu être générée, les messages suivants manquent."
                )
        elif corrompue:
            embed.description = "⚠️ Transcription illisible (données corrompues en base)."
        elif html_taille is not None and html_taille > limite:
            embed.description = (
                f"⚠️ Transcription trop volumineuse pour être envoyée sur ce serveur "
                f"({html_taille / 1_048_576:.1f} Mo, limite de {limite / 1_048_576:.0f} Mo)."
            )
        else:
            embed.description = "⚠️ Transcription indisponible (échec de génération)."

        try:
            await interaction.followup.send(**envoi)
        except discord.HTTPException as e:
            logger.error(f"[archive] Envoi de la transcription du ticket {thread_id} impossible : {e}", exc_info=True)
            await interaction.followup.send("❌ Impossible d'envoyer cette transcription.", ephemeral=True)


class ArchiveView(TimedView):
    def __init__(self, options: list[discord.SelectOption], *, auteur: int):
        super().__init__(auteur=auteur)
        self.add_item(ArchiveSelect(options))


class Archive(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="archive",
        description="Consulter les transcriptions des tickets fermés (réservé au staff)"
    )
    @app_commands.describe(moderateur="Réservé à l'owner : archives d'un modérateur en particulier (défaut : toutes)")
    # Masque la commande aux membres sans « Gérer les messages » (même permission
    # que /warn, cf. utils/staff.py). Ce n'est qu'un réglage par défaut, qu'un
    # admin peut ouvrir à d'autres rôles depuis les paramètres d'intégration du
    # serveur : la vraie vérification est faite au début de la commande.
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def archive(self, interaction: discord.Interaction, moderateur: discord.Member | None = None):
        proprio = est_owner(interaction.user.id)
        if not (proprio or est_moderateur(interaction.user)):
            await interaction.response.send_message(
                "❌ Cette commande est réservée aux modérateurs.", ephemeral=True
            )
            return
        if moderateur is not None and moderateur.id != interaction.user.id and not proprio:
            await interaction.response.send_message(
                "❌ Seul l'owner peut consulter les archives d'un autre modérateur.", ephemeral=True
            )
            return

        # Éphémère de bout en bout : le contenu des tickets ne doit pas s'afficher
        # dans un salon public.
        await interaction.response.defer(ephemeral=True)

        # None = toutes les archives (owner sans paramètre).
        if moderateur is not None:
            cible = moderateur.id
        elif proprio:
            cible = None
        else:
            cible = interaction.user.id

        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    if cible is None:
                        await c.execute(
                            f"SELECT {_COLONNES_LISTE} FROM ticket_archives "
                            "ORDER BY closed_at DESC LIMIT %s",
                            (NB_ARCHIVES,)
                        )
                    else:
                        await c.execute(
                            f"SELECT {_COLONNES_LISTE} FROM ticket_archives WHERE {_FILTRE_MODO} "
                            "ORDER BY closed_at DESC LIMIT %s",
                            (cible, cible, NB_ARCHIVES)
                        )
                    rows = await c.fetchall()
        except aiomysql.Error as e:
            logger.critical(f"[archive] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        if cible is None:
            portee, vide = "Tous les tickets", "Aucun ticket archivé pour l'instant."
        elif cible == interaction.user.id:
            portee = "Tes tickets (pris en charge ou avec ta participation)"
            vide = "Aucune archive : tu n'as pris en charge ni participé à aucun ticket fermé."
        else:
            portee = f"Tickets de {moderateur.mention} (pris en charge ou avec sa participation)"
            vide = f"Aucune archive pour {moderateur.mention} : aucun ticket fermé pris en charge ou avec sa participation."

        if not rows:
            await interaction.followup.send(
                f"📭 {vide}\n-# Seuls les tickets fermés depuis la mise en place des archives sont conservés.",
                ephemeral=True
            )
            return

        embed = discord.Embed(
            title="🗂️ Archives des tickets",
            description=f"{portee}.\nChoisis un ticket ci-dessous pour recevoir sa transcription.",
            color=discord.Color.blurple()
        )
        options = []
        for i, (thread_id, nom, membre_id, modo_id, raison, closed_at, html_taille) in enumerate(rows, start=1):
            disponible = html_taille is not None
            # Taille d'un champ bornée (nom ≤ 60, raison ≤ 50, ~200 caractères au
            # total) : 20 champs restent sous les 6000 caractères d'un embed.
            valeur = (
                f"👤 <@{membre_id}> — {_extrait(raison, 50) or 'Sans raison'}\n"
                f"🛡️ {f'<@{modo_id}>' if modo_id else 'Non pris en charge'} · 🔒 <t:{closed_at}:f>"
            )
            if not disponible:
                valeur += "\n⚠️ Transcription indisponible"
            embed.add_field(name=f"{i}. {_extrait(nom, 60) or 'Ticket'}", value=valeur, inline=False)

            # Les mentions ne s'affichent pas dans un select : nom du membre en clair
            # s'il est encore sur le serveur.
            membre = interaction.guild.get_member(membre_id)
            description = " · ".join(filter(None, [membre.display_name if membre else None, _extrait(raison, 100)]))
            options.append(discord.SelectOption(
                label=_extrait(f"{i}. {_extrait(nom, 70)} ({datetime.fromtimestamp(closed_at, PARIS):%d/%m/%Y %H:%M})", 100),
                value=str(thread_id),
                description=_extrait(description, 100) or None,
                emoji="📄" if disponible else "⚠️",
            ))
        embed.set_footer(text=f"{len(rows)} ticket(s) fermé(s), les plus récents d'abord ({NB_ARCHIVES} au maximum)")

        view = ArchiveView(options, auteur=interaction.user.id)
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(Archive(bot))
