# cogs/creermessage.py
import discord
from discord.ext import commands
from discord import app_commands

from cogs.tickets import TicketCreateView
from cogs.recrutement import ConditionsSelect
from cogs.trade import TradePanelView


class CreerMessageCog(commands.Cog):
    """Regroupe les messages préconçus (ticket, recrutement, trade...) sous une
    seule commande /creer-message, plutôt qu'une commande dédiée par message
    (remplace les anciennes !setup_ticket et !setup_recrutement)."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="creer-message",
        description="Poste un message préconçu (ticket, recrutement, trade...) dans ce salon"
    )
    @app_commands.describe(message="Quel message veux-tu poster ?")
    @app_commands.choices(message=[
        app_commands.Choice(name="Ticket", value="ticket"),
        app_commands.Choice(name="Recrutement", value="recrutement"),
        app_commands.Choice(name="Trade-brainrot", value="trade"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    async def creer_message(self, interaction: discord.Interaction, message: app_commands.Choice[str]):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Cette commande n'est pas disponible en MP. Utilise-la directement sur le serveur !",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        if message.value == "ticket":
            embed = discord.Embed(
                title="Tu as un problème, une question ou un partenariat à proposer ?",
                description="Viens en parler au staff en ouvrant un ticket",
                color=discord.Color.green()
            )
            embed.add_field(name="Tickets abusifs", value="Tout ticket abusif sera sanctionné", inline=False)
            await interaction.channel.send(embed=embed, view=TicketCreateView())

        elif message.value == "recrutement":
            embed = discord.Embed(
                title="Système de recrutement pour devenir modérateur",
                description="Tu trouveras ici toutes les informations pour devenir **modérateur**.",
                color=discord.Color.green()
            )
            embed.add_field(
                name="Ton rôle :",
                value="Faire respecter le règlement et les conditions d'utilisation du serveur, et sanctionner les membres ou contenus qui les enfreignent.",
                inline=False
            )
            embed.add_field(
                name="Conditions :",
                value="Être actif et sérieux sur le serveur. Une ancienneté minimale est requise, ainsi que la réussite des tests, **obligatoires**.",
                inline=False
            )
            embed.add_field(
                name="Étapes de recrutement :",
                value="Remplis le formulaire en cliquant sur le bouton ci-dessous. Si ta candidature est acceptée, un entretien vocal sera organisé avec toi, puis tu passeras modérateur test.",
                inline=False
            )
            embed.add_field(
                name="Évolutions :",
                value="Tu peux monter en grade au fil du temps. Tu commences **Modérateur test** ; si tu remplis bien ton rôle, tu deviens **Modérateur**, et une future promotion pourra suivre selon ton activité.",
                inline=False
            )
            embed.add_field(
                name="Avantages : ",
                value="Tu es au cœur du serveur : accès à des salons privés, et participation aux décisions concernant son avenir.",
                inline=False
            )
            embed.add_field(
                name="Tu es sûr.e de toi ?",
                value="Clique sur le bouton ci-dessous pour commencer le recrutement.",
                inline=False
            )
            await interaction.channel.send(embed=embed, view=ConditionsSelect())

        elif message.value == "trade":
            embed = discord.Embed(
                title="Trade ton brainrot !",
                description="Tu veux échanger ou vendre un brainrot ? Clique sur le bouton ci-dessous pour créer ton annonce.",
                color=discord.Color.blue()
            )
            await interaction.channel.send(embed=embed, view=TradePanelView())

        else:
            await interaction.followup.send("❌ Message inconnu.", ephemeral=True)
            return

        await interaction.followup.send("✅ Message envoyé dans ce salon !", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CreerMessageCog(bot))
