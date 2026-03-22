import discord
from discord.ext import commands
from dotenv import load_dotenv
import os
import sqlite3
load_dotenv()
from utils.setupdatabase import DB_PATH
import time


class RaisonModal(discord.ui.Modal, title="Raison du refus"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Cette personne n'est pas accepté dans le staff car ...",
        required=True,
        label="Pourquoi refuser cette candidature ?",
        max_length=500,
        min_length=10
    )
    def __init__(self, ctx, membre: discord.Member):
        super().__init__()
        self.ctx = ctx
        self.membre = membre

    async def on_submit(self, interaction: discord.Interaction):
        raison = self.raison.value
        membre = self.membre
        message = self.ctx
        if message is None:
            await interaction.response.send_message("Erreur : message introuvable.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"La candidature de {membre.mention} a été refusée par {interaction.user.mention} avec raison : {self.raison.value}"
        )

        view = Accepterview()
        for item in view.children:
            item.disabled = True

        await message.edit(view=view)
        try:

                embed = discord.Embed(title="Candidature refusée",
                                      description="Malheureusement, ta candidature a été refusé pour devenir modérateur sur Pixel Party. N'hésite pas à retenter ta chance plus tard !",
                                      color=discord.Color.red())
                icon = interaction.guild.icon.url if interaction.guild.icon else None
                embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
                embed.add_field(name="Raison du refus :", value=raison, inline=False)
                await membre.send(embed=embed)
                with sqlite3.connect(DB_PATH) as conn:
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM role_special WHERE user_id = ?", (membre.id,))
                    conn.commit()
        except sqlite3.Error as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Erreur de base de donnée : {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Erreur de base de donnée : {e}", ephemeral=True)


class Accepterview(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, emoji="✅", custom_id="recrutement:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        try:
            with (sqlite3.connect(DB_PATH) as conn):
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = ?",
                               (interaction.message.id,))
                row = cursor.fetchone()
                if row is None:
                    await interaction.followup.send("Candidature introuvable.")
                    return
                user_id = row[0]
                guild = interaction.guild
                membre = guild.get_member(user_id)
                if membre is None:
                    membre = await guild.fetch_member(user_id)
                cursor.execute("DELETE FROM role_special WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError as e:
            await interaction.followup.send(f"Erreur de base de donnée : {e}")
            return
        embed = discord.Embed(title="Candidature acceptée",
                              description=f"Félicitations {membre.mention} ! Tu viens d'être accepté pour devenir modérateur sur Pixel Party !", color=discord.Color.green())
        embed.add_field(name="Les étapes suivantes :", value="Tu va passer en modérateur test, tu aura accès à des salons privés et tu passera une période de test pour montrer tes compétences et ton activité. Ensuite, en fonction de ta performance, tu rentrera officiellement dans le staff ou tu reviendra membre.", inline=False)
        embed.add_field(name="Durée :", value="La période de test dure 1 semaine", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        await membre.send(embed=embed)
        role_id = int(os.getenv("ROLE_RECRUTEMENT"))
        role = interaction.guild.get_role(role_id)
        await membre.add_roles(role)
        with (sqlite3.connect(DB_PATH) as conn):
            cursor = conn.cursor()
            time_end = int(time.time()) + 7 * 24 * 3600
            cursor.execute("INSERT INTO role_temp (user_id, role_id, end_time) VALUES (?, ?, ?)", (user_id, role.id, int(time_end)))
            conn.commit()
        await interaction.followup.send(f"La candidature de {membre.mention} vient d'être acceptée par {interaction.user.mention} ✅")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, emoji="❌", custom_id="recrutement:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        print("Interaction reussi")
        try:
            with (sqlite3.connect(DB_PATH) as conn):
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = ?", (interaction.message.id,))
                row = cursor.fetchone()
                if row is None:
                    await interaction.response.send_message("Candidature introuvable.")
                    return
                user_id = row[0]
                membre = interaction.guild.get_member(user_id)
                if membre is None:
                    membre = await interaction.guild.fetch_member(user_id)
                await interaction.response.send_modal(RaisonModal(interaction.message, membre))
        except sqlite3.Error as e:
            if not interaction.response.is_done():
                await interaction.response.send_message(f"Erreur : {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"Erreur : {e}", ephemeral=True)

class RecrutementModal(discord.ui.Modal, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi devenir modérateur ?", placeholder="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question2 = discord.ui.TextInput(label="Réaction face à une insulte ?", placeholder="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question3 = discord.ui.TextInput(label="Si un ami enfreint une règle ?", placeholder="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question4 = discord.ui.TextInput(label="Temps disponible par semaine ?", placeholder="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True)
    question5 = discord.ui.TextInput(label="C'est quoi un mauvais modérateur ?", placeholder="Selon toi, c’est quoi un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True, min_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            embed = discord.Embed(
                title="Formulaire reçu",
                description=f"Merci {interaction.user.mention} pour tes réponses!",
                color=discord.Color.green()
            )

            embed.add_field(name="Pourquoi devenir modérateur ?", value=self.question1.value, inline=False)
            embed.add_field(name="Réaction face à une insulte ?", value=self.question2.value, inline=False)
            embed.add_field(name="Si un ami enfreint une règle ?", value=self.question3.value, inline=False)
            embed.add_field(name="Temps disponible ?", value=self.question4.value, inline=False)
            embed.add_field(name="Mauvais modérateur ?", value=self.question5.value, inline=False)

            await interaction.response.send_message("✅ Formulaire envoyé !", embed=embed, ephemeral=True)

            channel_id = int(os.getenv("CHANNEL_MODO_ID"))
            channel = interaction.client.get_channel(channel_id)

            if channel is None:
                channel = await interaction.client.fetch_channel(channel_id)

            msg = await channel.send(embed=embed, view=Accepterview())

            # DB
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO role_special (user_id, status, message_accepter_id) VALUES (?, ?, ?)",
                    (interaction.user.id, 1, msg.id)
                )
                conn.commit()

            print("✅ Formulaire traité correctement")

        except Exception as e:
            print("💥 ERREUR MODAL :", e)
            await interaction.followup.send(f"Erreur : {e}", ephemeral=True)


class FormulaireBouton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Remplir le formulaire", style=discord.ButtonStyle.green, custom_id="recrutement:remplir:formulaire")
    async def remplir_formulaire(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = RecrutementModal()
        await interaction.response.send_modal(modal)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

class ConditionsSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    # ajoute un boutton pour commencer le recrutement après la description des rôles
    @discord.ui.button(label="Commencer", style=discord.ButtonStyle.green, custom_id="recrutement:commenciation")
    async def commencer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            with sqlite3.connect(DB_PATH) as conn:
                cur = conn.cursor()
                cur.execute("SELECT status FROM role_special WHERE user_id = ?", (interaction.user.id,))
                row = cur.fetchone()
        except sqlite3.Error as e:
            await interaction.followup.send(f"Erreur de base de donnée : {e}", ephemeral=True)
            return

        if row and row[0] == None:
            await interaction.followup.send("Tu as déjà commencé le recrutement !", ephemeral=True)
            return
        embed = discord.Embed(title="Commencer le recrutement", description=f"Bienvenue dans le système de recrutement, {interaction.user.name}!", colour=discord.Colour.blue())
        embed.add_field(name="Etape 1 :", value="Remplis le formulaire ci-dessous pour donner tes informations au staff", inline=False)
        embed.add_field(name="Etape 2 :", value="Tu passe un entretien vocal avec un administrateur", inline=False)
        embed.add_field(name="Etape 3 : ", value="Tu rentre (ou non) dans le staff et tu est en phase de **test**", inline=False)
        embed.add_field(name="Ensuite ?", value="En fonction de ton activité et de tes compétences, a la fin de la periode de test, tu rentre officielement dans le staff ou tu reviens membre.", inline=False)
        embed.add_field(name="Tu est prêt.e ?", value="Selectionne le rôle staff que tu souhaite avoir mais attention : après avoir cliqué, tu t'engage et pas de retour possible. Tout abus sera sanctionné", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        embed2 = discord.Embed(title="La suite dans les messages privés", description="Afin de faciliter pour la persistance des messages pour mieux t'y retrouver, la suite du recrutement est envoyé dans les messages privés", colour=discord.Colour.blue())
        try:
            await interaction.followup.send(embed=embed2, ephemeral=True)
            await interaction.user.send(embed=embed, view=FormulaireBouton())
        except discord.Forbidden:
            await interaction.followup.send("Tu n'a pas activé les messages privés !", embed=embed, ephemeral=True)
            await interaction.followup.send("Tu n'a pas activé les messages privés ! Active-les, c'est **obligatoire** !", ephemeral=True)

class RecrutementCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    @commands.command(name="setup_recrutement")
    async def setup_recrutement(self, ctx):
        await ctx.message.delete()
        embed = discord.Embed(title="Système de recrutement pour devenir modérateur",
                              description="Ici tu verra toutes les informations pour devenir **modérateur**",
                              color=discord.Color.green())
        embed.add_field(name="Ton rôle :",
                        value="Retirer et sanctionner les membres ou contenu qui ne respectent pas les Tos ou le reglement du serveur.",
                        inline=False)
        embed.add_field(name="Conditions :",
                        value="Être assez actif et serieux sur le serveur, minimum d'ancienneté requis et passage des tests **obligatoire**",
                        inline=False)
        embed.add_field(name="Etapes de recrutement :",
                        value="Remplis le formulaire en cliquant sur le boutton. Si tu est accepté, un entretien vocal sera fait avec toi et tu passera modérateur test.",
                        inline=False)
        embed.add_field(name="Evolutions :",
                        value="Tu as la possibilité de monter en grade. Au debut, tu est **Modérateur test**, si tu remplis bien ton rôle tu passe **Modérateur** et une futur promotion se fera en fonction de ton activité.",
                        inline=False)
        embed.add_field(name="Avantages : ",
                        value="Tu est au coeur du serveur, accès a des salons privés et tu participe aux décisions concernant l'avenir du serveur.",
                        inline=False)
        embed.add_field(name="Tu est sûr.e de toi ?", value="Clique sur le boutton pour commencer le recrutement",
                        inline=False)
        await ctx.channel.send(embed=embed, view=ConditionsSelect())

async def setup(bot):
    await bot.add_cog(RecrutementCog(bot))