# cogs/partenariat.py
import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import json

class Partenariat_commencerView(discord.ui.View):
    def __init__(self, membre, salon):
        super().__init__(timeout=None)
        self.membre = membre
        self.salon = salon

    @discord.ui.button(label="Commencer", style=discord.ButtonStyle.success)
    async def commencer(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(title="Bienvenue !", description="Bienvenue dans le formulaire de partenariat.", color=discord.Color.blue())
        embed.add_field(name="Étape 1 : Présentation", value="Présente-toi ou ton serveur / projet pour commencer le partenariat.", inline=False)
        embed.add_field(name="Étape 2 : Publicité", value="Crée ou donne une publicité pour donner envie aux membres de rejoindre ton serveur", inline=False)
        embed.add_field(name="Derniere étape : Mention", value="Donne une mention selon le nombre de membres sur ton serveur", inline=False)

        # thread dans le salon PARTENARIAT_PARENT
        with open("config.json", "r", encoding="utf-8") as f:
            cfg = json.load(f)
        parent_channel = interaction.guild.get_channel(cfg.get("PARTENARIAT_PARENT"))
        if parent_channel is None:
            await interaction.response.send_message("Erreur: salon de partenariats introuvable.", ephemeral=True)
            return

        thread = await parent_channel.create_thread(name=f"Fil de partenariat avec {self.membre.name}", type=discord.ChannelType.public_thread)
        await thread.send(f"Thread créé pour {self.membre.mention}")

        view = EtapesView(self.membre, self.salon, thread)
        view.update_state(1)
        await interaction.response.edit_message(embed=embed, view=view)

# ---------- ETAPES VIEW ----------
class EtapesView(discord.ui.View):
    def __init__(self, membre, salon, thread):
        super().__init__(timeout=None)
        self.membre = membre
        self.salon = salon
        self.thread = thread
        self.etape = 1

        # boutons
        self.bt1 = discord.ui.Button(label="Actuel", style=discord.ButtonStyle.success)
        self.bt2 = discord.ui.Button(label="Suivant", style=discord.ButtonStyle.primary, disabled=True)
        self.bt3 = discord.ui.Button(label="Suivant", style=discord.ButtonStyle.primary, disabled=True)

        # select
        self.select = Etape3Select(self)

        # callbacks
        self.bt1.callback = self.bt1_click
        self.bt2.callback = self.bt2_click
        self.bt3.callback = self.bt3_click

        self.add_item(self.bt1)
        self.add_item(self.bt2)
        self.add_item(self.bt3)

    def update_state(self, etape: int):
        self.etape = etape
        if etape == 1:
            self.bt1.label = "Actuel"; self.bt1.style = discord.ButtonStyle.success; self.bt1.disabled = False
            self.bt2.label = "Suivant"; self.bt2.style = discord.ButtonStyle.primary; self.bt2.disabled = True
            self.bt3.label = "Suivant"; self.bt3.style = discord.ButtonStyle.primary; self.bt3.disabled = True
        elif etape == 2:
            self.bt1.label = "Précédent"; self.bt1.style = discord.ButtonStyle.secondary; self.bt1.disabled = False
            self.bt2.label = "Actuel"; self.bt2.style = discord.ButtonStyle.success; self.bt2.disabled = False
            self.bt3.label = "Suivant"; self.bt3.style = discord.ButtonStyle.primary; self.bt3.disabled = True
        elif etape == 3:
            self.bt1.label = "Précédent"; self.bt1.style = discord.ButtonStyle.secondary; self.bt1.disabled = False
            self.bt2.label = "Précédent"; self.bt2.style = discord.ButtonStyle.secondary; self.bt2.disabled = False
            self.bt3.label = "Actuel"; self.bt3.style = discord.ButtonStyle.success; self.bt3.disabled = False
        elif etape == 4:
            # toutes les étapes finies
            self.clear_items()
            done = discord.ui.Button(label="Demande envoyée...", style=discord.ButtonStyle.secondary, disabled=True)
            self.add_item(done)
            terminer_view = TerminerPartenariatView(self.membre, self.thread, self.salon)
            # envoyer dans le thread
            asyncio.create_task(self.thread.send("✅ Toutes les étapes ont été complétées !", view=terminer_view))

    async def aller_etape_suivante(self, interaction: discord.Interaction):
        if self.etape < 4:
            self.etape += 1
            self.update_state(self.etape)
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass

    async def bt1_click(self, interaction: discord.Interaction):
        await interaction.response.send_modal(Modal_1(self))

    async def bt2_click(self, interaction: discord.Interaction):
        await interaction.response.send_modal(Modal_2(self))

    async def bt3_click(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Étape 3 — Choisis une option", description="Sélectionne une option ci-dessous pour finaliser ta demande.", color=discord.Color.blue())
        select_view = SelectOnlyView(self)
        await interaction.response.edit_message(embed=embed, view=select_view)

# ---------- MODAL 1 ----------
class Modal_1(discord.ui.Modal):
    def __init__(self, parent_view: EtapesView):
        super().__init__(title="Étape 1 — Présentation")
        self.parent_view = parent_view
        self.presentation = discord.ui.TextInput(label="Présente ton serveur :", placeholder="Mon serveur parle de...", max_length=200)
        self.add_item(self.presentation)

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent_view.thread.send(f"**Présentation :** {self.presentation.value}")
        await interaction.response.send_message("✅ Étape 1 complétée !", ephemeral=True)
        await self.parent_view.aller_etape_suivante(interaction)

# ---------- MODAL 2 ----------
class Modal_2(discord.ui.Modal):
    def __init__(self, parent_view: EtapesView):
        super().__init__(title="Étape 2 — Publicité")
        self.parent_view = parent_view
        self.pub = discord.ui.TextInput(label="Ta publicité :", style=discord.TextStyle.paragraph, placeholder="Rejoignez mon serveur car...", max_length=1000)
        self.add_item(self.pub)

    async def on_submit(self, interaction: discord.Interaction):
        await self.parent_view.thread.send("**Publicité :**")
        await self.parent_view.thread.send(f"```{self.pub.value}```")
        await interaction.response.send_message("✅ Étape 2 complétée !", ephemeral=True)
        await self.parent_view.aller_etape_suivante(interaction)

# ---------- SELECT MENU (ÉTAPE 3) ----------
class Etape3Select(discord.ui.Select):
    def __init__(self, parent_view: EtapesView):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="Aucune mention", description="Ne mentionne aucun membre du serveur"),
            discord.SelectOption(label="Mention 'partenariat'", description="Mention spéciale de partenariat (recommandé)"),
            discord.SelectOption(label="Mention 'here'", description="Mentionne tout les membres du serveur actifs"),
            discord.SelectOption(label="Mention 'everyone'", description="Mentionne tout le serveur (nécessite un serveur suffisamment grand)"),
        ]
        super().__init__(placeholder="Actuel", min_values=1, max_values=1, options=options, disabled=False)

    async def callback(self, interaction: discord.Interaction):
        valeur = self.values[0]
        await self.parent_view.thread.send(f"**Choix final :** {valeur}")
        await interaction.response.send_message(f"✅ Tu as choisi : {valeur}", ephemeral=True)
        await self.parent_view.aller_etape_suivante(interaction)

# ---------- VUE TEMPORAIRE AVEC SEULEMENT LE SELECT ----------
class SelectOnlyView(discord.ui.View):
    def __init__(self, parent_view: EtapesView):
        super().__init__(timeout=None)
        self.parent_view = parent_view
        self.add_item(Etape3SelectTemp(self))

class Etape3SelectTemp(discord.ui.Select):
    def __init__(self, parent_view: SelectOnlyView):
        self.parent_view = parent_view
        options = [
            discord.SelectOption(label="Aucune mention", description="Ne mentionne aucun membre du serveur"),
            discord.SelectOption(label="Mention 'partenariat'", description="Mention spéciale de partenariat (recommandé)"),
            discord.SelectOption(label="Mention 'here'", description="Mentionne tout les membres du serveur actifs"),
            discord.SelectOption(label="Mention 'everyone'", description="Mentionne tout le serveur (nécessite un serveur suffisamment grand)"),
        ]
        super().__init__(placeholder="Fais ton choix ici", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        valeur = self.values[0]
        await self.parent_view.parent_view.thread.send(f"**Choix final :** {valeur}")
        await interaction.response.send_message(f"✅ Tu as choisi : {valeur}", ephemeral=True)
        # Passe à l'état final
        self.parent_view.parent_view.update_state(4)
        embed = discord.Embed(title="Demande envoyée", description="Merci pour ta participation ! Un modérateur te contactera bientôt.", color=discord.Color.green())
        try:
            await interaction.message.edit(embed=embed, view=self.parent_view.parent_view)
        except Exception:
            pass

# ---------- BOUTON TERMINER PARTENARIAT ----------
class TerminerPartenariatView(discord.ui.View):
    def __init__(self, membre, thread, salon):
        super().__init__(timeout=None)
        self.salon = salon
        self.membre = membre
        self.thread = thread

    @discord.ui.button(label="Terminer le partenariat", style=discord.ButtonStyle.danger)
    async def terminer(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            await self.membre.send("🤝 Ton partenariat est désormais terminé. Merci pour ta collaboration !")
        except discord.Forbidden:
            await interaction.followup.send("❌ Impossible d’envoyer un MP à l’utilisateur.", ephemeral=True)

        try:
            await self.salon.send("Partenariat terminé !")
            await interaction.response.send_message("🧹 Suppression du thread en cours...", ephemeral=True)
            await asyncio.sleep(2)
            await self.thread.delete()
        except Exception:
            await interaction.response.send_message("Erreur lors de la clôture du partenariat.", ephemeral=True)

class PartenariatCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(PartenariatCog(bot))
