# cogs/visite.py
import discord
from discord.ext import commands
from discord import app_commands
import json
from datetime import datetime

class VisiteGuidee(discord.ui.View):
    def __init__(self, etape=1):
        super().__init__(timeout=None)
        self.etape = etape
        self.max_etape = 5
        self.update_buttons()

    def update_buttons(self):
        self.clear_items()
        if self.etape == 1:
            self.add_item(discord.ui.Button(label="Commencer ✅", style=discord.ButtonStyle.success, custom_id="start"))
            self.add_item(discord.ui.Button(label="Passer ❌", style=discord.ButtonStyle.danger, custom_id="skip"))
        elif self.etape == self.max_etape:
            self.add_item(discord.ui.Button(label="⬅️ Précédent", style=discord.ButtonStyle.secondary, custom_id="prev"))
            self.add_item(discord.ui.Button(label="Terminer ✅", style=discord.ButtonStyle.success, custom_id="end"))
        else:
            self.add_item(discord.ui.Button(label="⬅️ Précédent", style=discord.ButtonStyle.secondary, custom_id="prev"))
            self.add_item(discord.ui.Button(label="Suivant ➡️", style=discord.ButtonStyle.primary, custom_id="next"))

    def get_embed(self):
        if self.etape == 1:
            return discord.Embed(title="Bienvenue sur Pixel Party !", description="Salut ! Souhaite tu une visite du serveur ? Clique sur **Commencer ✅** pour commencer ou **Passer ❌** pour ignorer.", color=discord.Color.blurple())
        elif self.etape == 2:
            return discord.Embed(title="Les règles", description="Les regles du serveur sont dans <#1381641879199809626>. Pense à les lire pour garder une bonne ambiance !", color=discord.Color.gold())
        elif self.etape == 3:
            return discord.Embed(title="Jeux et notifications", description="Tu peut sélectionner les jeux auquels tu joue, tes plateformes et les notifications que tu souhaite recevoir dans <#1434616794043256952>.", color=discord.Color.green())
        elif self.etape == 4:
            return discord.Embed(title="Argent et experience", description="Sur ce serveur, tu peut gagner de l'argent et de l'experience en parlant et en etant actif. Tu peut les dépenser pour débloquer des rôles, des evenements exclusifs et bien d'autre. Les commandes d'experiece et d'argent sont : `/argent`, `/niveau`,`/boutique`, et bien d'autres.", color=discord.Color.green())
        elif self.etape == 5:
            return discord.Embed(title="Rôles", description="Certains rôles offres des avantages comme **Super Pixel** (x1,5 xp) et **Pixel Doré** (salons privées et x2 xp)", color=discord.Color.green())
        else:
            return discord.Embed(title="✅ Fin de la visite", description="Passe un bon moment sur le serveur ! Tu peut refaire la visite à tout moment avec `/visite`", color=discord.Color.teal())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Gère les boutons via custom_id
        cid = interaction.data.get("custom_id")
        if cid == "start":
            new_view = VisiteGuidee(2)
            await interaction.response.edit_message(embed=new_view.get_embed(), view=new_view)
            return False
        elif cid == "skip":
            embed = discord.Embed(title="Visite passée", description="Tu pourras refaire la visite quand tu veux via la commande `/visite` !", color=discord.Color.red())
            await interaction.response.edit_message(embed=embed, view=None)
            return False
        elif cid == "prev":
            self.etape = max(1, self.etape - 1)
            new_view = VisiteGuidee(self.etape)
            await interaction.response.edit_message(embed=new_view.get_embed(), view=new_view)
            return False
        elif cid == "next":
            self.etape = min(self.max_etape, self.etape + 1)
            new_view = VisiteGuidee(self.etape)
            await interaction.response.edit_message(embed=new_view.get_embed(), view=new_view)
            return False
        elif cid == "end":
            # ajoute rôle si possible
            with open("config.json", "r", encoding="utf-8") as f:
                cfg = json.load(f)
            guild = interaction.client.get_guild(cfg.get("GUILD_ID"))
            role = discord.utils.get(guild.roles, id=cfg.get("ROLE_VISITE")) if guild else None
            member = guild.get_member(interaction.user.id) if guild else None
            if member and role:
                try:
                    await member.add_roles(role)
                    embed = discord.Embed(title="✅ Fin de la visite", description=f"Merci d’avoir suivi la visite, {interaction.user.mention} ! Tu as reçu le rôle **{role.name}** 🎉. Passe un bon moment sur le serveur !", color=discord.Color.green())
                except Exception:
                    embed = discord.Embed(title="✅ Fin de la visite", description="Merci d’avoir suivi la visite 👋 ! Tu peux la refaire à tout moment en faisant `/visite` !", color=discord.Color.teal())
            else:
                embed = discord.Embed(title="✅ Fin de la visite", description="Merci d’avoir suivi la visite 👋 ! Tu peux la refaire à tout moment en faisant `/visite` !", color=discord.Color.teal())
            await interaction.response.edit_message(embed=embed, view=None)
            return False
        return True

class VisiteCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="visite", description="Fait un rapide tour pour découvrir les fonctionnalitées du serveur")
    async def visite(self, interaction: discord.Interaction):
        member = interaction.user
        await interaction.response.send_message("Visite envoyé en privé !", ephemeral=True)
        embed = discord.Embed(title="👋 Bienvenue !", description=f"Salut {member.mention} ! Prêt pour la visite du serveur ?", color=discord.Color.blurple())
        view = VisiteGuidee(1)
        try:
            await member.send(embed=embed, view=view)
        except discord.Forbidden:
            pass

async def setup(bot):
    await bot.add_cog(VisiteCog(bot))