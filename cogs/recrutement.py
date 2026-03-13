import discord
from discord.ext import commands
from dotenv import load_dotenv
import os
import sqlite3
load_dotenv()
from utils.setupdatabase import DB_PATH

class Accepterview(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, emoji="✅", custom_id="recrutement:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
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
                membre = await interaction.client.fetch_user(user_id)
                cursor.execute("DELETE FROM role_special WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError as e:
            await interaction.followup.send(f"Erreur de base de donnée : {e}")
            return
        embed = discord.Embed(title="Candidature acceptée",
                              description=f"Félicitations {interaction.user.mention} ! Tu viens d'accepter une candidature pour devenir modérateur sur Pixel Party !", color=discord.Color.green())
        embed.add_field(name="", value="")
        await interaction.followup.send(f"La candidature de {membre.mention} vient d'être acceptée par {interaction.user.mention} ✅")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, emoji="❌", custom_id="recrutement:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        try:
            with (sqlite3.connect(DB_PATH) as conn):
                cursor = conn.cursor()
                cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = ?", (interaction.message.id,))
                row = cursor.fetchone()
                if row is None:
                    await interaction.response.send_message("Candidature introuvable.", ephemeral=True)
                    return
                user_id = row[0]
                membre = await interaction.client.fetch_user(user_id)
                await membre.send(f"Ta candidature a été refusé par {interaction.user.mention}, n'hésite pas à retenter ta chance plus tard !")
                cursor.execute("DELETE FROM role_special WHERE user_id = ?", (user_id,))
        except sqlite3.OperationalError as e:
            await interaction.response.send_message(f"Erreur de base de donnée : {e}", ephemeral=True)
            return
        await interaction.response.send_message(f"La candidature de {membre.mention} viens d'être refusé par {interaction.user.mention} ❌")


class RecrutementModal(discord.ui.Modal, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi devenir modérateur ?", placeholder="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True)
    question2 = discord.ui.TextInput(label="Réaction face à une insulte ?", placeholder="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question3 = discord.ui.TextInput(label="Si un ami enfreint une règle ?", placeholder="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question4 = discord.ui.TextInput(label="Temps disponible par semaine ?", placeholder="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True)
    question5 = discord.ui.TextInput(label="C'est quoi un mauvais modérateur ?", placeholder="Selon toi, c’est quoi un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Formulaire reçu", description=f"Merci {interaction.user.mention} pour tes réponses!", color=discord.Color.green())
        embed.add_field(name="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", value=self.question1.value, inline=False)
        embed.add_field(name="Un membre insulte un autre membre. Que fais-tu ?", value=self.question2.value, inline=False)
        embed.add_field(name="Si un de tes amis enfreint une règle, que fais-tu ?", value=self.question3.value, inline=False)
        embed.add_field(name="Combien de temps peux-tu consacrer au serveur par semaine ?", value=self.question4.value, inline=False)
        embed.add_field(name="Selon toi, c’est quoi un mauvais modérateur ?", value=self.question5.value, inline=False)
        await interaction.response.send_message(embed=embed)
        channel_id = int(os.getenv("CHANNEL_MODO_ID"))
        # The modal is submitted in DM, so interaction.guild can be None.
        # We must fetch the channel from the bot instead of the guild.
        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            channel = await interaction.client.fetch_channel(channel_id)
        msg = await channel.send(
            embed=embed,
            view=Accepterview())

        with sqlite3.connect(DB_PATH) as conn:
            try:
                c = conn.cursor()
                c.execute(
                    "INSERT OR REPLACE INTO role_special (user_id, status, message_accepter_id) VALUES (?, ?, ?)",
                    (interaction.user.id, 1, msg.id)
                )
                conn.commit()
            except sqlite3.OperationalError as e:
                print(e)
                await interaction.followup.send(f"Erreur de base de donnée : {e}")


class FormulaireBouton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    @discord.ui.button(label="Remplir le formulaire", style=discord.ButtonStyle.green, custom_id="recrutement:remplir:formulaire")
    async def remplir_formulaire(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
        try:
            modal = RecrutementModal()
            await interaction.response.send_modal(modal)
        except Exception as e:
            print("ERREUR MODAL :", e)

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