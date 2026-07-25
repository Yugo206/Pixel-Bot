import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
from dotenv import load_dotenv
load_dotenv()


from utils.setupdatabase import DB_PATH

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
        if not interaction.response.is_done():
            await interaction.response.defer()
        embed = discord.Embed(title="Profil", description="Ton profil contient ton **argent**, ton **XP** et tes **niveaux**", color=discord.Color.green())
        with sqlite3.connect(DB_PATH) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT argent, xp FROM utilisateurs WHERE user_id = ?", (interaction.user.id,))
            result = cursor.fetchone()
        argent = result[0] if result and result[0] is not None else 0
        xp = result[1] if result and result[1] is not None else 0
        embed.add_field(name="Argent :", value=f"{argent} €", inline=False)
        embed.add_field(name="Experience :", value=f"{xp}", inline=False)
        nv = self.get_level(xp)
        embed.add_field(name="Niveau :", value=f"{nv}", inline=False)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Profile(bot))
