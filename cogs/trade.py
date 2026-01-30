# cogs/trade.py
import discord
from discord.ext import commands
from discord import app_commands

class TradeView(discord.ui.View):
    def __init__(self, trader, annonce):
        super().__init__()
        self.trader = trader
        self.annonce = annonce

    @discord.ui.button(label="Accepter le trade", style=discord.ButtonStyle.green)
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id == self.trader.id:
            await interaction.response.send_message("Tu ne peux pas accepter ton propre trade !", ephemeral=True)
            return
        await interaction.response.send_message(f"Tu as accepté le trade de {self.trader.mention}, contacte-le en MP.", ephemeral=True)
        link = f"https://discord.com/channels/{self.annonce.guild.id}/{self.annonce.channel.id}/{self.annonce.id}"
        await self.trader.send(f"{interaction.user.mention} a accepté [ton trade]({link}). Contacte-le en MP !")
        button.disabled = True
        await self.annonce.edit(view=self)


class Trade(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="trade-brainrot", description="Fait une annonce pour trade ton brainrot")
    @app_commands.describe(brainrot="Quel brainrot veux-tu trade ?", argent="Combien d'argent /s fait ton brainrot ?", note="Note supplémentaire")
    async def trade_brainrot(self, interaction: discord.Interaction, brainrot: str, argent: str, note: str | None = None):
        embed = discord.Embed(title="Nouveau trade !", description="Un nouveau trade est disponible", color=discord.Color.blue())
        embed.add_field(name="Brainrot", value=brainrot, inline=False)
        embed.add_field(name="Argent par seconde", value=argent, inline=False)
        embed.add_field(name="Note", value=note or "Aucune note", inline=False)

        channel = interaction.guild.get_channel(1431330774291976237)  # remplace par ton channel
        view = TradeView(interaction.user, None)
        annonce = await channel.send(f"Annonce de {interaction.user.mention} 🟢", embed=embed, view=view)
        view.annonce = annonce

        await interaction.response.send_message("Ton annonce a été envoyée !", ephemeral=True)

async def setup(bot):
    await bot.add_cog(Trade(bot))
