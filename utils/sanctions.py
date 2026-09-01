import os
import time
from datetime import timedelta

import discord

from utils.database import get_pool

# Palier d'avertissements -> sanction appliquée.
SANCTION_THRESHOLDS = {
    3: {"type": "timeout", "hours": 48, "days": 0, "label": "48h"},
    5: {"type": "timeout", "hours": 0, "days": 7, "label": "7 jour(s)"},
    10: {"type": "ban", "days": 30, "label": "30 jour(s)"},
}


async def get_modo_channel(bot, guild=None):
    """Récupère le salon de modération (CHANNEL_MODO_ID), en repassant par l'API si besoin."""
    raw_id = os.getenv("CHANNEL_MODO_ID")
    if not raw_id:
        return None
    channel_id = int(raw_id)

    channel = guild.get_channel(channel_id) if guild else bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except discord.HTTPException:
            channel = None
    return channel


async def apply_warn_sanction(guild, membre: discord.Member, channel, warn_count: int, reason_suffix: str = "avertissements"):
    """Applique la sanction correspondant au nombre d'avertissements (3 / 5 / 10) si applicable."""
    sanction = SANCTION_THRESHOLDS.get(warn_count)
    if sanction is None:
        return

    reason = f"{warn_count} {reason_suffix}"

    if sanction["type"] == "timeout":
        # Le timeout Discord n'existe que pour un membre toujours présent sur le
        # serveur : selon d'où vient l'appel, `membre` peut être un simple
        # discord.User (ex: membre ayant quitté le serveur — voir SatisfactionView
        # dans cogs/tickets.py) qui n'a pas de méthode .timeout(). On l'évite
        # proprement ici plutôt que de laisser planter chaque appelant.
        if not isinstance(membre, discord.Member):
            if channel:
                await channel.send(
                    f"⚠️ {membre} n'est plus sur le serveur, impossible de le mute ({sanction['label']})."
                )
            return

        until = discord.utils.utcnow() + timedelta(hours=sanction["hours"], days=sanction["days"])

        try:
            await membre.timeout(until, reason=reason)
        except discord.Forbidden:
            if channel:
                await channel.send(f"❌ Erreur : impossible de mute {membre.mention} (permissions insuffisantes)")
            return
        except discord.HTTPException as e:
            if channel:
                await channel.send(f"❌ Impossible de mute {membre.mention} : {e}")
            return

        embed = discord.Embed(
            title="Tu viens d'être mute",
            description=(
                f"Tu as reçu {warn_count} avertissements, tu viens donc d'être mute {sanction['label']} "
                "sur **Pixel Party**.\nPrends le temps de réfléchir pendant ton mute, ça évitera le ban 😆"
            ),
            color=discord.Color.red()
        )
        try:
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass

    elif sanction["type"] == "ban":
        unban_at = int(time.time()) + sanction["days"] * 86400

        embed = discord.Embed(
            title="Tu viens d'être ban",
            description="Tu t'es récemment mal comporté sur Pixel Party",
            color=discord.Color.red()
        )
        embed.add_field(name="Raison", value=reason, inline=False)
        embed.add_field(name="Temps", value=sanction["label"], inline=False)
        try:
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass

        try:
            await guild.ban(membre, reason=reason)
        except discord.Forbidden:
            if channel:
                await channel.send(f"❌ Erreur : impossible de bannir {membre.mention} (permissions insuffisantes)")
            return
        except discord.HTTPException as e:
            if channel:
                await channel.send(f"❌ Impossible de bannir {membre.mention} : {e}")
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "INSERT INTO temp_bans (user_id, unban_at) VALUES (%s, %s)",
                    (membre.id, unban_at)
                )
            await conn.commit()

        if channel:
            await channel.send(f"🔨 {membre} banni pour **{sanction['label']}**.\nRaison : {reason}")
