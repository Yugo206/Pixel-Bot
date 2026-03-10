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

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, emoji="✅")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        membre = None
        await interaction.response.send_message("La candidature de {membre.mention} viens d'être accepté ✅ \n Un entretien vocal prevu avec ")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, emoji="❌")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        membre = None
        await interaction.response.send_message("La candidature de {membre.mention} viens d'être refusé ❌")


class RecrutementModal(discord.ui.Modal, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi devenir modérateur ?", placeholder="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True)
    question2 = discord.ui.TextInput(label="Réaction face à une insulte ?", placeholder="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question3 = discord.ui.TextInput(label="Si un ami enfreint une règle ?", placeholder="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question4 = discord.ui.TextInput(label="Temps disponible par semaine ?", placeholder="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True)
    question5 = discord.ui.TextInput(label="C'est quoi un mauvais modérateur ?", placeholder="Selon toi, c’est quoi un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        for child in self.children:
            child.disabled = True
        embed = discord.Embed(title="Formulaire reçu", description=f"Merci {interaction.user.name} pour tes réponses!", color=discord.Color.green())
        embed.add_field(name="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", value=self.question1.value, inline=False)
        embed.add_field(name="Un membre insulte un autre membre. Que fais-tu ?", value=self.question2.value, inline=False)
        embed.add_field(name="Si un de tes amis enfreint une règle, que fais-tu ?", value=self.question3.value, inline=False)
        embed.add_field(name="Combien de temps peux-tu consacrer au serveur par semaine ?", value=self.question4.value, inline=False)
        embed.add_field(name="Selon toi, c’est quoi un mauvais modérateur ?", value=self.question5.value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        channel = interaction.guild.get_channel(int(os.getenv("CHANNEL_MODO_ID"))) # remplace par ton channel
        await channel.send(
            embed=embed,
            view=Accepterview())
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE role_special SET status = 1 WHERE user_id = ?", (interaction.user.id,))

class FormulaireBouton(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Remplir le formulaire", style=discord.ButtonStyle.green, custom_id="recrutement:remplir:formulaire")
    async def remplir_formulaire(self, interaction: discord.Interaction, button: discord.ui.Button):

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
        print("COMMENCEMENT DU RECRUTEMENT")
        embed = discord.Embed(title="Commencer le recrutement", description=f"Bienvenue dans le système de recrutement, {interaction.user.name}!", colour=discord.Colour.blue())
        embed.add_field(name="Etape 1 :", value="Remplis le formulaire ci-dessous pour donner tes informations au staff", inline=False)
        embed.add_field(name="Etape 2 :", value="Tu passe un entretien vocal avec un administrateur", inline=False)
        embed.add_field(name="Etape 3 : ", value="Tu rentre (ou non) dans le staff et tu est en phase de **test**", inline=False)
        embed.add_field(name="Ensuite ?", value="En fonction de ton activité et de tes compétences, a la fin de la periode de test, tu rentre officielement dans le staff ou tu reviens membre.", inline=False)
        embed.add_field(name="Tu est prêt.e ?", value="Selectionne le rôle staff que tu souhaite avoir mais attention : après avoir cliqué, tu t'engage et pas de retour possible. Tout abus sera sanctionné", inline=False)
        embed2 = discord.Embed(title="La suite dans les messages privés", description="Afin de faciliter pour la persistance des messages pour mieux t'y retrouver, la suite du recrutement est envoyé dans les messages privés", colour=discord.Colour.blue())
        try:
            await interaction.response.send_message(embed=embed2, ephemeral=True)
            await interaction.user.send(embed=embed, view=FormulaireBouton())
        except discord.Forbidden:
            await interaction.response.send_message("Tu n'a pas activé les messages privés !", embed=embed, ephemeral=True)
            await interaction.followup.send("Tu n'a pas activé les messages privés !. Active-les, c'est **obligatoire** !")

class RecrutementCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    @commands.command(name="setup-recrutement")
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