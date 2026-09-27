# cogs/creermessage.py
import logging
from urllib.parse import urlparse

import discord
from discord.ext import commands
from discord import app_commands

from cogs.tickets import TicketCreateView
from cogs.recrutement import ConditionsSelect
from cogs.trade import TradePanelView
from utils.views import Modale, copie_vue

logger = logging.getLogger(__name__)


def _message_erreur_envoi(e: discord.HTTPException) -> str:
    """Message clair pour un envoi refusé par Discord, au lieu du texte brut de
    l'exception."""
    if isinstance(e, discord.Forbidden):
        return "❌ Je n'ai pas la permission d'envoyer des messages (ou des embeds) dans ce salon."
    return "❌ Discord a refusé le message : vérifie l'URL de l'image et la longueur du texte."


class MessagePersonnaliseModal(Modale, title="Message personnalisé"):
    titre = discord.ui.TextInput(label="Titre", required=True, max_length=256)
    texte = discord.ui.TextInput(
        label="Texte du message", style=discord.TextStyle.paragraph, required=True, max_length=4000
    )
    couleur = discord.ui.TextInput(
        label="Couleur en hexadécimal (optionnel)", required=False, max_length=7, placeholder="#5865F2"
    )
    image = discord.ui.TextInput(
        label="URL d'une image (optionnel)", required=False, max_length=300, placeholder="https://..."
    )

    async def on_submit(self, interaction: discord.Interaction):
        # Revérifié à l'envoi : le formulaire a pu rester ouvert pendant qu'un
        # administrateur perdait ses droits.
        if not interaction.permissions.administrator:
            await interaction.response.send_message(
                "❌ Il faut être administrateur pour poster ce message.", ephemeral=True
            )
            return

        couleur_embed = discord.Color.blue()
        # strip() d'abord : un champ optionnel qui ne contient que des espaces
        # compte comme vide, au lieu d'être refusé comme couleur invalide.
        couleur = (self.couleur.value or "").strip()
        if couleur:
            try:
                valeur = int(couleur.lstrip("#"), 16)
                # int() seul n'exclut pas les valeurs hors plage RGB (ex: 7 chiffres
                # hexa sans #) : discord.Color ne fait lui-même aucune vérification
                # de plage, ce qui ferait échouer l'envoi de l'embed plus loin avec
                # une erreur Discord peu claire au lieu du message ci-dessous.
                if not (0 <= valeur <= 0xFFFFFF):
                    raise ValueError("Valeur hors de la plage RGB (000000-FFFFFF).")
                couleur_embed = discord.Color(valeur)
            except ValueError:
                await interaction.response.send_message(
                    "❌ Couleur invalide : utilise un code hexadécimal à 6 chiffres, par exemple `#5865F2`.",
                    ephemeral=True
                )
                return

        image = self.image.value.strip() if self.image.value else ""
        if image:
            # Discord n'accepte qu'une URL http(s) : sans cette vérification,
            # une faute de frappe faisait échouer l'envoi avec une erreur brute.
            url = urlparse(image)
            if url.scheme not in ("http", "https") or not url.netloc:
                await interaction.response.send_message(
                    "❌ URL d'image invalide : elle doit commencer par `https://`.", ephemeral=True
                )
                return

        embed = discord.Embed(title=self.titre.value, description=self.texte.value, color=couleur_embed)
        if image:
            embed.set_image(url=image)

        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.channel.send(embed=embed)
        except discord.HTTPException as e:
            logger.warning(f"[creer-message] Message personnalisé refusé dans {interaction.channel_id} : {e}")
            await interaction.followup.send(_message_erreur_envoi(e), ephemeral=True)
            return

        await interaction.followup.send("✅ Message envoyé dans ce salon !", ephemeral=True)


class CreerMessageCog(commands.Cog):
    """Regroupe les messages préconçus (ticket, recrutement, trade...) sous une
    seule commande /creer-message, plutôt qu'une commande dédiée par message
    (remplace les anciennes !setup_ticket et !setup_recrutement)."""

    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="creer-message",
        description="Poste un message préconçu, ou personnalisé, dans ce salon"
    )
    @app_commands.describe(message="Quel message veux-tu poster ?")
    @app_commands.choices(message=[
        app_commands.Choice(name="Ticket", value="ticket"),
        app_commands.Choice(name="Recrutement", value="recrutement"),
        app_commands.Choice(name="Trade-brainrot", value="trade"),
        app_commands.Choice(name="Personnalisé", value="personnalise"),
    ])
    @app_commands.default_permissions(administrator=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(administrator=True)
    async def creer_message(self, interaction: discord.Interaction, message: app_commands.Choice[str]):
        # Les tickets sont des fils privés créés dans le salon du message : un
        # fil ou un salon vocal ne peut pas en contenir, et chaque ouverture de
        # ticket y échouait.
        if message.value == "ticket" and not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Le message de ticket doit être posté dans un salon textuel (pas dans un fil ou un forum).",
                ephemeral=True
            )
            return

        # Un message personnalisé demande du texte libre (titre, texte...) : on
        # passe par un Modal, qui doit être la toute première réponse à
        # l'interaction (impossible après un defer(), contrairement aux autres
        # choix ci-dessous qui n'ont besoin d'aucune saisie).
        if message.value == "personnalise":
            await interaction.response.send_modal(MessagePersonnaliseModal())
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await self._poster(interaction, message.value)
        except discord.HTTPException as e:
            logger.warning(f"[creer-message] Message {message.value} refusé dans {interaction.channel_id} : {e}")
            await interaction.followup.send(_message_erreur_envoi(e), ephemeral=True)

    async def _poster(self, interaction: discord.Interaction, choix: str):
        # Vues envoyées sous forme de copies (voir copie_vue dans
        # utils/views.py) : les clics arrivent à l'instance enregistrée au
        # démarrage, qui fait les vérifications.
        if choix == "ticket":
            embed = discord.Embed(
                title="Tu as un problème, une question ou un partenariat à proposer ?",
                description="Viens en parler au staff en ouvrant un ticket",
                color=discord.Color.green()
            )
            embed.add_field(name="Tickets abusifs", value="Tout ticket abusif sera sanctionné", inline=False)
            await interaction.channel.send(embed=embed, view=copie_vue(TicketCreateView, None, set()))

        elif choix == "recrutement":
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
            await interaction.channel.send(embed=embed, view=copie_vue(ConditionsSelect, None, set()))

        elif choix == "trade":
            embed = discord.Embed(
                title="Trade ton brainrot !",
                description="Tu veux échanger ou vendre un brainrot ? Clique sur le bouton ci-dessous pour créer ton annonce.",
                color=discord.Color.blue()
            )
            await interaction.channel.send(embed=embed, view=copie_vue(TradePanelView, None, set()))

        else:
            await interaction.followup.send("❌ Message inconnu.", ephemeral=True)
            return

        await interaction.followup.send("✅ Message envoyé dans ce salon !", ephemeral=True)


async def setup(bot):
    await bot.add_cog(CreerMessageCog(bot))
