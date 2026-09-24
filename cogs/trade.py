# cogs/trade.py
from dotenv import load_dotenv
load_dotenv()


import logging
import re

import discord
from discord.ext import commands
from discord import app_commands
from utils.config import get_config
from utils.autorisations import repondre, verifier_serveur
from utils.views import Modale, VuePersistante, copie_vue, liberer_clic, reserver_clic

logger = logging.getLogger(__name__)

# Auteur de l'annonce, lu dans le contenu du message (« Annonce de <@id> 🟢 ») :
# message.mentions ne suffit pas, il est vide si la mention n'a pas notifié
# (membre parti, mentions désactivées) alors que l'id reste dans le texte.
_AUTEUR_TRADE_RE = re.compile(r"<@!?([0-9]+)>")


class TradeView(VuePersistante):
    """Bouton « Accepter le trade » des annonces du salon de trade. Utilisable
    une seule fois, par n'importe quel membre du serveur sauf l'auteur."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_serveur(interaction)

    @discord.ui.button(label="Accepter le trade", style=discord.ButtonStyle.green, custom_id="trade:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Vue sans état pour rester persistante après un redémarrage : on retrouve
        # l'auteur du trade et l'annonce directement depuis l'interaction plutôt
        # que depuis des attributs stockés à la construction (perdus au redémarrage).
        annonce = interaction.message
        match = _AUTEUR_TRADE_RE.search(annonce.content or "")
        if match is None:
            await interaction.response.send_message("❌ Impossible de retrouver l'auteur de ce trade.", ephemeral=True)
            return
        trader_id = int(match.group(1))

        if interaction.user.id == trader_id:
            await interaction.response.send_message("❌ Tu ne peux pas accepter ton propre trade !", ephemeral=True)
            return

        # Un seul preneur par annonce : deux membres qui cliquent en même temps
        # ne reçoivent pas tous les deux la confirmation.
        if not reserver_clic(interaction):
            await repondre(interaction, "⚠️ Ce trade a déjà été accepté par quelqu'un d'autre.")
            return

        try:
            contenu = f"Annonce de <@{trader_id}> 🔴 — acceptée par {interaction.user.mention}"
            await interaction.response.edit_message(
                content=contenu, view=copie_vue(TradeView, annonce, None),
                allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException:
            liberer_clic(interaction)
            raise

        link = annonce.jump_url
        mp_envoye = True
        try:
            trader = interaction.client.get_user(trader_id) or await interaction.client.fetch_user(trader_id)
            await trader.send(f"{interaction.user.mention} a accepté [ton trade]({link}). Contacte-le en MP !")
        except discord.HTTPException:
            mp_envoye = False

        message = f"Tu as accepté le trade de <@{trader_id}>, contacte-le en MP."
        if not mp_envoye:
            message += "\n⚠️ Ses MP sont fermés : il n'a pas été prévenu, écris-lui directement."
        await interaction.followup.send(message, ephemeral=True)


async def envoyer_annonce_trade(interaction: discord.Interaction, brainrot: str, argent: str, note: str | None):
    """Construit et poste l'annonce de trade dans CHANNEL_TRADE_ID. Partagé entre
    /trade-brainrot et le bouton du panneau posté via /creer-message (TradePanelView)
    pour ne pas dupliquer la logique entre les deux points d'entrée."""
    if interaction.guild is None:
        await interaction.response.send_message(
            "❌ Cette commande n'est pas disponible en MP. Utilise-la directement sur le serveur !",
            ephemeral=True
        )
        return

    channel_id = get_config("CHANNEL_TRADE_ID")
    channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
    if channel is None and channel_id:
        try:
            channel = await interaction.guild.fetch_channel(int(channel_id))
        except discord.HTTPException:
            channel = None

    if channel is None:
        await interaction.response.send_message("❌ Le salon de trade n'est pas configuré (CHANNEL_TRADE_ID).", ephemeral=True)
        return
    if not isinstance(channel, discord.abc.Messageable):
        # CHANNEL_TRADE_ID pointant vers une catégorie ou un forum : l'envoi
        # plantait (« Cette interaction a échoué ») au lieu d'un message clair.
        logger.error(f"[trade] CHANNEL_TRADE_ID ({channel.id}) ne désigne pas un salon textuel.")
        await interaction.response.send_message(
            "❌ Le salon de trade configuré n'est pas un salon textuel, préviens un modérateur.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title="Nouveau trade !", description="Un nouveau trade est disponible", color=discord.Color.blue())
    embed.add_field(name="Brainrot", value=brainrot, inline=False)
    embed.add_field(name="Argent par seconde", value=argent, inline=False)
    embed.add_field(name="Note", value=note or "Aucune note", inline=False)

    try:
        # Mention affichée sans notifier : l'auteur n'a pas à être notifié de
        # sa propre annonce (le texte saisi, lui, est dans l'embed, où une
        # mention ne notifie jamais personne).
        await channel.send(
            f"Annonce de {interaction.user.mention} 🟢", embed=embed, view=copie_vue(TradeView, None, set()),
            allowed_mentions=discord.AllowedMentions.none()
        )
    except discord.HTTPException as e:
        logger.error(f"[trade] Annonce non publiée dans {channel.id} : {e}")
        await interaction.response.send_message(
            "❌ Ton annonce n'a pas pu être publiée (salon de trade inaccessible), préviens un modérateur.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(f"Ton annonce a été envoyée dans {channel.mention} !", ephemeral=True)


class TradeAnnonceModal(Modale, title="Nouvelle annonce de trade"):
    brainrot = discord.ui.TextInput(label="Quel brainrot veux-tu trade ?", required=True, max_length=100)
    argent = discord.ui.TextInput(label="Combien d'argent /s fait ton brainrot ?", required=True, max_length=100)
    note = discord.ui.TextInput(label="Note supplémentaire", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await envoyer_annonce_trade(interaction, self.brainrot.value, self.argent.value, self.note.value or None)


class TradePanelView(VuePersistante):
    """Panneau persistant posté via /creer-message : un clic ouvre directement le
    formulaire d'annonce, sans avoir à taper /trade-brainrot."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_serveur(interaction)

    @discord.ui.button(label="Créer une annonce de trade", style=discord.ButtonStyle.green, custom_id="trade:creer:annonce")
    async def creer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeAnnonceModal())


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="trade-brainrot", description="Fais une annonce pour trade ton brainrot")
    @app_commands.describe(brainrot="Quel brainrot veux-tu trade ?", argent="Combien d'argent /s fait ton brainrot ?", note="Note supplémentaire")
    # Mêmes limites que le formulaire du panneau (TradeAnnonceModal) : sans
    # elles, un texte de plus de 1024 caractères faisait échouer l'envoi de
    # l'embed de l'annonce (limite Discord d'un champ).
    async def trade_brainrot(self, interaction: discord.Interaction,
                             brainrot: app_commands.Range[str, 1, 100],
                             argent: app_commands.Range[str, 1, 100],
                             note: app_commands.Range[str, 1, 500] | None = None):
        await envoyer_annonce_trade(interaction, brainrot, argent, note)


async def setup(bot):
    await bot.add_cog(Trade(bot))
