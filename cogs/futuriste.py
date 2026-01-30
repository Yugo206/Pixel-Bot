import discord
from discord.ext import commands
from discord import app_commands
import random
from utils.database import ajouter_rarete


class FuturCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.checks.cooldown(1, 600)
    @app_commands.command(
        name="prediction",
        description="Te donne une prédiction totalement fiable (ou pas)"
    )
    async def prediction(self, interaction: discord.Interaction):
        annees = random.randint(1, 15)

        predictions = [
            "les gens feront la queue pour acheter de l'eau en canette goût pizza 🍕",
            "Discord ajoutera un bouton 'rage quit' officiel 🔥",
            "Fortnite ressortira une saison OG pour la 12e fois 🎮",
            "les profs corrigeront les copies avec une IA mal lunée 🤖",
            "les chiens auront plus d'abonnés que les humains sur TikTok 🐶",
            "un frigo sera élu président d'un pays 🇺🇸",
            "dire 'bonjour' sera considéré comme cringe 😬",
            "Minecraft sortira enfin la version finale (peut-être) ⛏️",
            "les gens paieront pour dormir sans pub 😴",
            "ton pseudo Discord te fera honte 🫠",

            "les devoirs seront remplacés par des quêtes quotidiennes 📚",
            "les parents demanderont conseil à leurs enfants pour la technologie 👨‍👩‍👧‍👦",
            "les lunettes de soleil auront un abonnement mensuel 😎",
            "les gens applaudiront quand le Wi-Fi marche du premier coup 📶",
            "les claviers corrigeront tes fautes avant même que tu écrives ⌨️",
            "les influenceurs auront un bouton 'désinfluence' ❌",
            "les pubs dureront plus longtemps que les vidéos 📺",
            "les téléphones auront besoin de pauses écran 😴",
            "les montres connectées diront quand mentir ⌚",
            "les chaussettes disparaîtront toujours par paire 🧦",

            "les IA demanderont des vacances 🏖️",
            "les jeux vidéo auront des mises à jour plus lourdes que le jeu lui-même 💾",
            "les gens regretteront les bugs parce qu'ils étaient fun 🐛",
            "les serveurs tomberont pile pendant les événements importants 🚨",
            "les emojis remplaceront les mots dans les conversations 😶‍🌫️",
            "les consoles auront besoin d'un permis pour être allumées 🎮",
            "les gens streameront leur sommeil en direct 🛌",
            "les patch notes seront plus longs que les livres 📜",
            "les téléphones se vexeront quand tu les ignores 📱",
            "les gens demanderont à une IA de choisir leur tenue 👕",

            "les mises à jour arriveront toujours au pire moment ⏳",
            "les mots de passe auront eux-mêmes un mot de passe 🔐",
            "les micros s'activeront toujours quand il ne faut pas 🎤",
            "les bugs deviendront des fonctionnalités officielles 🧩",
            "les gens feront confiance à une IA plus qu'à eux-mêmes 🤯",
            "les jeux sortiront en accès anticipé pendant 10 ans 🚧",
            "les batteries tomberont à 1% pile quand tu en as besoin 🔋",
            "les notifications arriveront toutes en même temps 🔔",
            "les gens diront 'c'était mieux avant' à propos de 2025 ⏪"
        ]

        nb = random.random()

        if nb < 0.05:
            rarete = "SECRET !"
            color = discord.Color.dark_gray()
        elif nb < 0.1:
            rarete = "Légendaire"
            color = discord.Color.gold()
        elif nb < 0.15:
            rarete = "Mytique"
            color = discord.Color.red()
        elif nb < 0.30:
            rarete = "Epique"
            color = discord.Color.purple()
        elif nb < 0.60:
            rarete = "Rare"
            color = discord.Color.blue()
        else:
            rarete = "Commun"
            color = discord.Color.green()

        phrase = random.choice(predictions)
        prediction = f"Dans {annees} ans, {phrase}"

        embed = discord.Embed(
            title="🔮 Ta prédiction",
            description=prediction,
            color=color
        )
        embed.add_field(name="Rareté", value=rarete)
        ajouter_rarete(interaction.user.id, rarete.lower())
        await interaction.response.send_message(embed=embed)

    @prediction.error
    async def prediction_error(self, interaction: discord.Interaction, error):
        if isinstance(error, app_commands.CommandOnCooldown):
            minutes = int(error.retry_after // 60)
            secondes = int(error.retry_after % 60)

            embed = discord.Embed(
                title="PAS SI VITE !!!",
                description=(f"La boule de prédiction de Garama la sorcière est fatigué. Attends encore **{minutes} min {secondes} s** avant sa pleine forme pour une nouvelle prédiction."
                ),
                color=discord.Color.red()
            )

            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(FuturCog(bot))
