"""Vérifications d'autorisation communes à toutes les commandes et à tous les
composants (boutons, menus, modales) du bot, pour qu'une même règle — « seul le
staff peut prendre un ticket », « ce menu n'appartient qu'à celui qui a lancé la
commande » — s'applique partout de la même façon et avec le même message.

Discord n'empêche personne de cliquer sur un bouton visible : un message public
(/profil, /boutique, une annonce de trade, le message d'un ticket...) est
cliquable par tous ceux qui le voient, et une vue persistante (voir
cogs/events.py) répond à n'importe quel message qui porte son custom_id. Chaque
action doit donc revérifier elle-même QUI clique et OÙ, avant tout effet :
  - les vues temporaires (utils/views.py, TimedView) refusent tout autre
    utilisateur que leur auteur ;
  - les vues persistantes vérifient le rôle attendu (staff, propriétaire du
    ticket, destinataire du MP) dans leur interaction_check ;
  - les commandes slash passent par verifier_commande() (voir start.py), qui
    refuse les messages privés et tout serveur autre que GUILD_ID.

Chaque fonction verifier_*() répond elle-même à l'interaction (message
éphémère) quand elle refuse, puis renvoie False : l'appelant n'a plus qu'à
s'arrêter (`if not await verifier_staff(interaction): return`), sans jamais
laisser l'utilisateur face à « Cette interaction a échoué ».
"""
import logging

import discord

from utils.config import get_config
from utils.staff import est_moderateur, est_owner

logger = logging.getLogger(__name__)

MSG_PAS_A_TOI = "❌ Ce menu ne t'appartient pas : utilise la commande toi-même pour avoir le tien."
MSG_STAFF = "❌ Seul le staff peut utiliser ce bouton."
MSG_SERVEUR = "❌ Ce bot n'est utilisable que sur le serveur Pixel Party."
MSG_MP = "❌ Ce bouton ne fonctionne qu'en message privé."
MSG_DEJA_TRAITE = "⚠️ Cette action a déjà été traitée."
MSG_ERREUR = "❌ Une erreur inattendue est survenue, réessaie dans un instant."


def est_staff(user: discord.abc.User) -> bool:
    """Modérateur du serveur (voir utils/staff.py) ou propriétaire du bot
    (OWNER_ID). Un discord.User (message privé, membre parti) n'est staff que
    s'il est le propriétaire : il n'a pas de rôles à vérifier."""
    return est_owner(user.id) or est_moderateur(user)


def serveur_autorise(guild_id: int | None) -> bool:
    """Vrai si guild_id est le serveur configuré (GUILD_ID, table `config`).
    Faux hors serveur (message privé). Sans GUILD_ID configuré, n'importe quel
    serveur est accepté : un bot mal configuré reste utilisable, l'alerte des
    réglages manquants au démarrage (voir start.py) prévient déjà l'owner."""
    if guild_id is None:
        return False
    attendu = get_config("GUILD_ID")
    if not attendu:
        return True
    return str(guild_id) == attendu.strip()


async def serveur_principal(bot) -> discord.Guild | None:
    """Le serveur configuré (GUILD_ID), depuis le cache ou l'API. None si
    GUILD_ID est absent ou si le bot n'y a pas accès."""
    raw_id = get_config("GUILD_ID")
    if not raw_id:
        return None
    guild = bot.get_guild(int(raw_id))
    if guild is None:
        try:
            guild = await bot.fetch_guild(int(raw_id))
        except discord.HTTPException:
            return None
    return guild


async def membre_du_serveur(bot, user_id: int) -> discord.Member | None:
    """Le membre `user_id` du serveur configuré, ou None s'il n'en fait pas (ou
    plus) partie. Sert aux actions faites depuis un message privé (formulaire
    de recrutement, contestation d'un warn), où l'interaction ne porte aucun
    serveur : sans cette vérification, un ancien membre pourrait encore agir
    sur le serveur qu'il a quitté."""
    guild = await serveur_principal(bot)
    if guild is None:
        return None
    membre = guild.get_member(user_id)
    if membre is None:
        try:
            membre = await guild.fetch_member(user_id)
        except discord.HTTPException:
            return None
    return membre


async def repondre(interaction: discord.Interaction, message: str) -> None:
    """Répond en éphémère, que l'interaction ait déjà reçu une réponse (defer,
    modale ouverte...) ou non. N'échoue jamais : un jeton expiré ou une
    interaction déjà consommée ne doit pas masquer l'erreur d'origine."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except discord.HTTPException:
        pass


async def refuser(interaction: discord.Interaction, message: str) -> bool:
    """Répond `message` en éphémère et renvoie False, pour écrire
    `return await refuser(interaction, ...)` dans un interaction_check."""
    await repondre(interaction, message)
    return False


async def verifier_serveur(interaction: discord.Interaction) -> bool:
    """Refuse toute interaction hors du serveur configuré (MP compris)."""
    if serveur_autorise(interaction.guild_id):
        return True
    return await refuser(interaction, MSG_SERVEUR)


async def verifier_staff(interaction: discord.Interaction, message: str = MSG_STAFF) -> bool:
    """Refuse toute interaction d'un non-staff, ou venue d'ailleurs que du
    serveur configuré (MP compris) : les boutons du staff (salon modération,
    tickets) n'existent que sur le serveur, et leurs actions s'appuient sur
    interaction.guild — le même custom_id recopié en MP plantait au lieu
    d'être refusé."""
    if not serveur_autorise(interaction.guild_id):
        return await refuser(interaction, MSG_SERVEUR)
    if est_staff(interaction.user):
        return True
    return await refuser(interaction, message)


async def verifier_mp(interaction: discord.Interaction) -> bool:
    """Refuse une interaction qui ne vient pas d'un message privé avec le bot
    (sondage de satisfaction, contestation d'un warn, formulaire de
    recrutement) : ces messages ne sont envoyés qu'en MP, un même custom_id
    ailleurs ne peut venir que d'un message détourné."""
    if interaction.guild_id is None:
        return True
    return await refuser(interaction, MSG_MP)


async def verifier_auteur(interaction: discord.Interaction, auteur_id: int | None,
                          message: str = MSG_PAS_A_TOI) -> bool:
    """Refuse toute interaction d'un autre utilisateur que `auteur_id`. Sans
    auteur connu (None), laisse passer : c'est à l'appelant de décider."""
    if auteur_id is None or interaction.user.id == auteur_id:
        return True
    return await refuser(interaction, message)


async def verifier_commande(interaction: discord.Interaction) -> bool:
    """Vérification globale de toutes les commandes slash (voir PixelTree dans
    start.py) : uniquement sur le serveur configuré, jamais en message privé.
    Les commandes sont déjà déclarées « serveur uniquement » auprès de Discord
    (allowed_contexts), mais un client peut garder un ancien enregistrement en
    cache, et rien n'empêche d'ajouter le bot à un autre serveur."""
    if interaction.guild_id is None:
        return await refuser(interaction, "❌ Les commandes du bot ne sont utilisables que sur le serveur.")
    if not serveur_autorise(interaction.guild_id):
        logger.warning(
            f"[autorisations] Commande refusée sur un serveur non autorisé "
            f"({interaction.guild_id}) pour {interaction.user.id}."
        )
        return await refuser(interaction, MSG_SERVEUR)
    return True
