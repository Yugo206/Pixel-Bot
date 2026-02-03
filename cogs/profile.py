import discord
from discord.ext import commands
from discord import app_commands
from PIL import Image, ImageDraw, ImageFont
import io
import sqlite3
import os

from cogs.setupdatabase import DB_PATH

class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    def get_level(self, xp: int):
        level = 1
        xp_needed = 10

        while xp >= xp_needed:
            xp -= xp_needed
            xp_needed *= 2
            level += 1

        return level

    @app_commands.command(name="profil", description="Afficher ton profil")
    async def profil(self, interaction: discord.Interaction):
        await interaction.response.defer()
        embed = discord.Embed(title="Profil", description="Ton profil contient ton **argent**, ton **XP** et tes **niveaux**", color=discord.Color.green())
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = ?", (interaction.user.id,))
        result = cursor.fetchone()
        argent = result[0] if result else 0
        embed.add_field(name="Argent :", value=f"{argent} €", inline=False)
        cursor.execute("SELECT xp FROM utilisateurs WHERE user_id = ?", (interaction.user.id,))
        result1 = cursor.fetchone()
        xp = result1[0] if result1 else 0
        embed.add_field(name="Experience :", value=f"{xp}", inline=False)
        nv = self.get_level(xp)
        embed.add_field(name="Niveau :", value=f"{nv}", inline=False)
        await interaction.followup.send(embed=embed)

async def setup(bot):
    await bot.add_cog(Profile(bot))
    print()

