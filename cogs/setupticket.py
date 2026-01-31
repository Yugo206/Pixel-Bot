import asyncio
import discord
from discord.ext import commands
from cogs.tickets import TicketCreateView

try:

    class ticketsconf(commands.Cog):
        def __init__(self, bot):
            self.bot = bot


        @commands.command()
        @commands.has_permissions(administrator=True)
        async def setup_ticket(self, ctx):

            print("line 26")
            channel = ctx.channel
            embed = discord.Embed(title="Tu as un problème, question ou partenariat ? ?", description="Viens en parler au staff en ouvrant un ticket", color=discord.Color.green())
            embed.add_field(name="Tikets abusifs", value="Tout ticket abusif sera sanctionné", inline=False)
            view = TicketCreateView(self.bot)
            await channel.send(embed=embed, view=view)
    async def setup(bot):
        await bot.add_cog(ticketsconf(bot))
except Exception as e:
    print(e)