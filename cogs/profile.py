import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
load_dotenv()


from utils.database import get_pool
from utils import cache

# Colonnes autorisées pour /classement : whitelist revalidée juste avant l'interpolation
# SQL (même principe que ajouter_rarete dans utils/database.py), même si les choix sont
# déjà imposés côté client par app_commands.choices.
COLONNES_CLASSEMENT = {"argent", "xp"}

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
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT argent, xp FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                result = await cursor.fetchone()
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

    @app_commands.command(name="argent", description="Afficher ton solde d'argent")
    async def argent(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                result = await cursor.fetchone()
        argent = result[0] if result and result[0] is not None else 0
        embed = discord.Embed(
            title="💰 Argent",
            description=f"Tu as **{argent} €**.",
            color=discord.Color.green()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="niveau", description="Afficher ton niveau et ton XP")
    async def niveau(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        pool = get_pool()
        # Via le cache mémoire (utils/cache.py) : même valeur que celle utilisée par
        # on_message pour calculer les niveaux, sans refaire un aller-retour DB si elle
        # est déjà en cache.
        xp = await cache.get_xp(pool, interaction.user.id)
        nv = self.get_level(xp)
        embed = discord.Embed(
            title="✨ Niveau",
            description=f"Tu es **niveau {nv}** avec **{xp} XP**.",
            color=discord.Color.green()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="classement", description="Afficher le top 10 du serveur")
    @app_commands.describe(type="Classer par argent ou par expérience")
    @app_commands.choices(type=[
        app_commands.Choice(name="Argent", value="argent"),
        app_commands.Choice(name="Expérience", value="xp"),
    ])
    async def classement(self, interaction: discord.Interaction, type: app_commands.Choice[str]):
        if not interaction.response.is_done():
            await interaction.response.defer()

        colonne = type.value
        if colonne not in COLONNES_CLASSEMENT:
            await interaction.followup.send("❌ Choix invalide.", ephemeral=True)
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # colonne : validée juste au-dessus contre COLONNES_CLASSEMENT, sûre à interpoler.
                await cursor.execute(
                    f"SELECT user_id, {colonne} FROM utilisateurs ORDER BY {colonne} DESC LIMIT 10"
                )
                rows = await cursor.fetchall()

        if colonne == "argent":
            titre, unite = "💰 Classement — Argent", "€"
        else:
            titre, unite = "✨ Classement — Expérience", "XP"

        if not rows:
            embed = discord.Embed(title=titre, description="Personne à classer pour le moment.", color=discord.Color.green())
        else:
            medailles = ["🥇", "🥈", "🥉"]
            lignes = []
            for i, (user_id, valeur) in enumerate(rows):
                rang = medailles[i] if i < len(medailles) else f"**{i + 1}.**"
                lignes.append(f"{rang} <@{user_id}> — {valeur or 0} {unite}")
            embed = discord.Embed(title=titre, description="\n".join(lignes), color=discord.Color.green())

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Profile(bot))
