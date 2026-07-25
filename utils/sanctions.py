import os
import sqlite3
import time
from datetime import timedelta

import discord

from utils.setupdatabase import DB_PATH

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

        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute(
                "INSERT INTO temp_bans (user_id, unban_at) VALUES (?, ?)",
                (membre.id, unban_at)
            )
            conn.commit()

        if channel:
            await channel.send(f"🔨 {membre} banni pour **{sanction['label']}**.\nRaison : {reason}")
