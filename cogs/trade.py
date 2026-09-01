# cogs/trade.py
import os
from dotenv import load_dotenv
load_dotenv()


import discord
from discord.ext import commands
from discord import app_commands


class TradeView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter le trade", style=discord.ButtonStyle.green, custom_id="trade:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Vue sans état pour rester persistante après un redémarrage : on retrouve
        # l'auteur du trade et l'annonce directement depuis l'interaction plutôt
        # que depuis des attributs stockés à la construction (perdus au redémarrage).
        annonce = interaction.message
        trader = annonce.mentions[0] if annonce.mentions else None

        if trader is None:
            await interaction.response.send_message("❌ Impossible de retrouver l'auteur de ce trade.", ephemeral=True)
            return

        if interaction.user.id == trader.id:
            await interaction.response.send_message("Tu ne peux pas accepter ton propre trade !", ephemeral=True)
            return

        await interaction.response.send_message(f"Tu as accepté le trade de {trader.mention}, contacte-le en MP.", ephemeral=True)

        link = f"https://discord.com/channels/{annonce.guild.id}/{annonce.channel.id}/{annonce.id}"
        try:
            await trader.send(f"{interaction.user.mention} a accepté [ton trade]({link}). Contacte-le en MP !")
        except discord.Forbidden:
            pass

        button.disabled = True
        await annonce.edit(view=self)


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

    channel_id = os.getenv("CHANNEL_TRADE_ID")
    channel = interaction.guild.get_channel(int(channel_id)) if channel_id else None
    if channel is None and channel_id:
        try:
            channel = await interaction.guild.fetch_channel(int(channel_id))
        except discord.HTTPException:
            channel = None

    if channel is None:
        await interaction.response.send_message("❌ Le salon de trade n'est pas configuré (CHANNEL_TRADE_ID).", ephemeral=True)
        return

    embed = discord.Embed(title="Nouveau trade !", description="Un nouveau trade est disponible", color=discord.Color.blue())
    embed.add_field(name="Brainrot", value=brainrot, inline=False)
    embed.add_field(name="Argent par seconde", value=argent, inline=False)
    embed.add_field(name="Note", value=note or "Aucune note", inline=False)

    await channel.send(f"Annonce de {interaction.user.mention} 🟢", embed=embed, view=TradeView())

    await interaction.response.send_message("Ton annonce a été envoyée !", ephemeral=True)


class TradeAnnonceModal(discord.ui.Modal, title="Nouvelle annonce de trade"):
    brainrot = discord.ui.TextInput(label="Quel brainrot veux-tu trade ?", required=True, max_length=100)
    argent = discord.ui.TextInput(label="Combien d'argent /s fait ton brainrot ?", required=True, max_length=100)
    note = discord.ui.TextInput(label="Note supplémentaire", style=discord.TextStyle.paragraph, required=False, max_length=500)

    async def on_submit(self, interaction: discord.Interaction):
        await envoyer_annonce_trade(interaction, self.brainrot.value, self.argent.value, self.note.value or None)


class TradePanelView(discord.ui.View):
    """Panneau persistant posté via /creer-message : un clic ouvre directement le
    formulaire d'annonce, sans avoir à taper /trade-brainrot."""
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Créer une annonce de trade", style=discord.ButtonStyle.green, custom_id="trade:creer:annonce")
    async def creer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TradeAnnonceModal())


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="trade-brainrot", description="Fais une annonce pour trade ton brainrot")
    @app_commands.describe(brainrot="Quel brainrot veux-tu trade ?", argent="Combien d'argent /s fait ton brainrot ?", note="Note supplémentaire")
    async def trade_brainrot(self, interaction: discord.Interaction, brainrot: str, argent: str, note: str | None = None):
        await envoyer_annonce_trade(interaction, brainrot, argent, note)


async def setup(bot):
    await bot.add_cog(Trade(bot))
