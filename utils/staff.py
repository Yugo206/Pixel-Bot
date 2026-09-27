"""Qui fait partie du staff, pour les fonctionnalités réservées aux modérateurs
(transcriptions de tickets et /archive, /stats-staff). Centralisé ici pour que
la génération d'une archive (qui repère le premier message d'un modérateur) et
les commandes qui la consultent appliquent exactement la même définition.
Contient aussi le format des durées affiché par /archive et /stats-staff, pour
qu'un même délai de réponse s'écrive pareil dans les deux commandes.
"""
import discord

from utils.config import get_config


def est_owner(user_id: int) -> bool:
    """Vrai si user_id est l'OWNER_ID configuré (table `config`)."""
    owner_id = get_config("OWNER_ID")
    return bool(owner_id) and str(user_id) == owner_id.strip()


def est_moderateur(member: discord.abc.User) -> bool:
    """Vrai pour un membre du serveur ayant le rôle ROLE_MODO_ID, ou la permission
    "Gérer les messages" — la même que celle exigée par /warn, /warns et /unwarn
    (cogs/warn.py), pour qu'un admin sans le rôle modo ne soit pas exclu. Un
    discord.User (membre parti du serveur, ou message privé) n'a pas de rôles :
    toujours faux."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_messages:
        return True
    role_modo_id = get_config("ROLE_MODO_ID")
    return bool(role_modo_id) and member.get_role(int(role_modo_id)) is not None


def formater_duree(secondes: float) -> str:
    """Durée lisible en français : « 45 s », « 12 min », « 2 h 05 », « 3 j 4 h ».
    Arrondie vers le bas à l'unité affichée, comme le délai restant de /daily
    (cogs/profile.py) : « 12 min » signifie entre 12 et 13 minutes."""
    s = max(int(secondes), 0)
    if s < 60:
        return f"{s} s"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        heures, reste = divmod(s, 3600)
        return f"{heures} h {reste // 60:02d}"
    jours, reste = divmod(s, 86400)
    heures = reste // 3600
    return f"{jours} j {heures} h" if heures else f"{jours} j"
