import discord
from discord.ext import commands
from dotenv import load_dotenv
import os
import sqlite3
load_dotenv()
from utils.setupdatabase import DB_PATH

class Accepterview(discord.ui.View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, emoji="✅")
    async def accepter(self, interaction: discord.Interaction):
        membre = None
        await interaction.response.send_message(f"La candidature de {membre.mention} viens d'être accepté ✅ \n Un entretien vocal prevu avec ")


class RecrutementModal(discord.ui.Modal, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True)
    question2 = discord.ui.TextInput(label="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question3 = discord.ui.TextInput(label="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True)
    question4 = discord.ui.TextInput(label="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True)
    question5 = discord.ui.TextInput(label="Selon toi, c’est quoi un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        embed = discord.Embed(title="Formulaire reçu", description=f"Merci {interaction.user.name} pour tes réponses!", color=discord.Color.green())
        embed.add_field(name="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", value=self.question1.value, inline=False)
        embed.add_field(name="Un membre insulte un autre membre. Que fais-tu ?", value=self.question2.value, inline=False)
        embed.add_field(name="Si un de tes amis enfreint une règle, que fais-tu ?", value=self.question3.value, inline=False)
        embed.add_field(name="Combien de temps peux-tu consacrer au serveur par semaine ?", value=self.question4.value, inline=False)
        embed.add_field(name="Selon toi, c’est quoi un mauvais modérateur ?", value=self.question5.value, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("UPDATE role_special SET status = 1 WHERE user_id = ?", (interaction.user.id,))

class FormulaireBouton(discord.ui.View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="Remplir le formulaire", style=discord.Colour.green())
    async def remplir_formulaire(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RecrutementModal())

class CommencerView(discord.ui.View):
    def __init__(self):
        super().__init__()

    @discord.ui.button(label="Commencer", style=discord.Colour.green())
    async def commencer(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        embed = discord.Embed(title="Sélection du rôle", description=f"Bienvenue {interaction.user.name}! Selectionne le rôle staff que tu souhaites.", colour=discord.Colour.blue())
        try:
            await interaction.user.send(embed=embed, view=RoleSelectView())
            await interaction.followup.send("Regarde tes messages privés!", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Tu n'a pas activé les messages privés ! Active-les, c'est **obligatoire** pour devenir staff !", ephemeral=True)

class RoleSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(placeholder="Selectionne le rôle que tu souhaites", options=[
        discord.SelectOption(label="Modérateur", description="Devenir modérateur", emoji="🗒️", value="1"),
        discord.SelectOption(label="Community manager", description="Devenir community manager", emoji="🤝", value="2"),
        discord.SelectOption(label="Parrain", description="Devenir parrain", emoji="🔍", value="3"),
    ], custom_id="recrutement:role_select")
    async def role_select_callback(self, interaction: discord.Interaction, selection: discord.ui.Select):
        role = int(selection.values[0])
        if role == 1:
            embed = discord.Embed(title="Formulaire pour Modérateur", description="Remplis le formulaire ci-dessous pour postuler en tant que modérateur",
                                  color=discord.Color.green())
            embed.add_field(name="Question 1 :", value="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", inline=False)
            embed.add_field(name="Question 2 :", value="Un membre insulte un autre membre. Que fais-tu ?", inline=False)
            embed.add_field(name="Question 3 :", value="Si un de tes amis enfreint une règle, que fais-tu ?", inline=False)
            embed.add_field(name="Question 4 :", value="Combien de temps peux-tu consacrer au serveur par semaine ?", inline=False)
            embed.add_field(name="Question 5 :", value="Selon toi, c’est quoi un mauvais modérateur ?", inline=False)
            embed.add_field(name="", value="", inline=False)
            await interaction.response.send_message(embed=embed, view=FormulaireBouton())
        elif role == 2:
            embed = discord.Embed(title="Formulaire pour Community Manager",
                                  description="Remplis le formulaire ci-dessous pour postuler en tant que community manager", color=discord.Color.green())
            embed.add_field(name="Question 1 :", value="", inline=False)
            embed.add_field(name="Question 2 :", value="", inline=False)
            embed.add_field(name="Question 3 :", value="", inline=False)
            embed.add_field(name="Question 4 :", value="", inline=False)
            embed.add_field(name="Question 5 :", value="", inline=False)
            embed.add_field(name="", value="", inline=False)
            await interaction.response.send_message(embed=embed, view=FormulaireBouton())
        elif role == 3:
            embed = discord.Embed(title="Formulaire pour Parrain",
                                  description="Remplis le formulaire ci-dessous pour postuler en tant que parrain",
                                  colour=discord.Colour.green())
            embed.add_field(name="Question 1 :", value="", inline=False)
            embed.add_field(name="Question 2 :", value="", inline=False)
            embed.add_field(name="Question 3 :", value="", inline=False)
            embed.add_field(name="Question 4 :", value="", inline=False)
            embed.add_field(name="Question 5 :", value="", inline=False)
            embed.add_field(name="", value="", inline=False)
            await interaction.response.send_message(embed=embed, view=FormulaireBouton())
            with sqlite3.connect(DB_PATH) as con:
                c = con.cursor()
                c.execute("INSERT INTO role_special (user_id, role_id) VALUES (?, ?)", (interaction.user.id, role))
                con.commit()

class ConditionsSelect(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(placeholder="Selectionne une option", options=[
        discord.SelectOption(label="Modérateur", description="Voir les informations pour devenir modérateur", emoji="🗒️", value="1"),
        discord.SelectOption(label="Community manager", description="Voir les informations pour devenir community manager", emoji="🤝", value="2"),
        discord.SelectOption(label="Parrain", description="Voir les informations pour devenir parrain", emoji="🔍", value="3"),
    ], custom_id="recrutement:infos")
    async def select_callback(self, interaction: discord.Interaction, selection: discord.SelectOption):
        role = int(selection.values[0])
        if role == 1:
            embed = discord.Embed(title="Système de recrutement pour devenir modérateur", description="Ici tu verra toutes les informations pour devenir **modérateur**",
                                  color=discord.Color.green())
            embed.add_field(name="Ton rôle :", value="Retirer et sanctionner les membres ou contenu qui ne respectent pas les Tos ou le reglement du serveur.", inline=False)
            embed.add_field(name="Conditions :", value="Être assez actif et serieux sur le serveur, minimum d'ancienneté requis et passage des tests **obligatoire**", inline=False)
            embed.add_field(name="Etapes de recrutement :", value="Remplis le formulaire en cliquant sur le boutton. Si tu est accepté, un entretien vocal sera fait avec toi et tu passera modérateur test.", inline=False)
            embed.add_field(name="Evolutions :", value="Tu as la possibilité de monter en grade. Au debut, tu est **Modérateur test**, si tu remplis bien ton rôle tu passe **Modérateur** et une futur promotion se fera en fonction de ton activité.", inline=False)
            embed.add_field(name="Avantages : ", value="Tu est au coeur du serveur, accès a des salons privés et tu participe aux décisions concernant l'avenir du serveur.", inline=False)
            embed.add_field(name="Note :", value="Si tu veux devenir modérateur, soit sur de toi car le processus est long et le besoin **n'est pas urgent** nous n'hesiterons pas à ne pas te recruter, te derank voir te **bannir** en cas d'abus.", inline=False)
            embed.add_field(name="Tu est sûr.e de toi ?", value="Clique sur le boutton pour commencer le recrutement", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        if role == 2:
            embed = discord.Embed(title="Système de recrutement pour devenir community manager",
                                  description="Ici tu verra toutes les informations pour devenir **community manager**", color=discord.Color.green())
            embed.add_field(name="Ton rôle :",
                            value="Chercher des serveurs de partenariat et faire des partenariat pour faire grandir le serveur", inline=False)
            embed.add_field(name="Conditions :",
                            value="Être assez actif et serieux sur le serveur, minimum d'ancienneté requis et passage des tests **obligatoire**", inline=False)
            embed.add_field(name="Etapes de recrutement :",
                            value="Remplis le formulaire en cliquant sur le boutton. Si tu est accepté, un entretien vocal sera fait avec toi et tu passera community manager test.", inline=False)
            embed.add_field(name="Evolutions :",
                            value="Tu as la possibilité de monter en grade. Au debut, tu est **Community manager test**, si tu remplis bien ton rôle tu passe **Community manager** et une futur promotion se fera en fonction de ton activité.", inline=False)
            embed.add_field(name="Avantages : ",
                            value="Tu t'occupe de l'image du serveur et contrôle qui sont les serveurs de confince et tu fait grandir le serveur", inline=False)
            embed.add_field(name="Tu est sûr.e de toi ?",
                            value="Clique sur le boutton pour continuer le recrutement", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
        if role == 3:
            embed = discord.Embed(title="Système de recrutement pour devenir parrain",
                                  description="Ici tu verra toutes les informations pour devenir **parrainr**",
                                  colour=discord.Colour.green())
            embed.add_field(name="Ton rôle :",
                            value="Chercher des serveurs de pub et faire mettre reguliererment la publicité du serveur pour faire grandir le serveur",
                            inline=False)
            embed.add_field(name="Conditions :",
                            value="Passage des tests **obligatoire**",
                            inline=False)
            embed.add_field(name="Etapes de recrutement :",
                            value="Remplis le formulaire en cliquant sur le boutton. Si tu est accepté, un entretien vocal sera fait avec toi et tu passera parrain test.",
                            inline=False)
            embed.add_field(name="Evolutions :",
                            value="Tu as la possibilité de monter en grade. Au debut, tu est **Parrain test**, si tu remplis bien ton rôle tu passe **Parrain** et une futur promotion se fera en fonction de ton activité.",
                            inline=False)
            embed.add_field(name="Avantages : ",
                            value="Tu fait venir des membres et donc tu fait grandir le serveur",
                            inline=False)
            embed.add_field(name="Tu est sûr.e de toi ?",
                            value="Clique sur le boutton pour continuer le recrutement", inline=False)
            await interaction.response.send_message(embed=embed, ephemeral=True)
    # ajoute un boutton pour commencer le recrutement après la description des rôles
    @discord.ui.button(label="Commencer le recrutement", style=discord.Colour.green(), custom_id="recrutement:commenciation")
    async def commencer(self, interaction: discord.Interaction):
        print("COMMENCEMENT DU RECRUTEMENT")
        embed = discord.Embed(title="Commencer le recrutement", description=f"Bienvenue dans le système de recrutement, {interaction.user.name}!", colour=discord.Colour.blue())
        embed.add_field(name="Etape 1 :", value="Remplis le formulaire ci-dessous pour donner tes informations au staff", inline=False)
        embed.add_field(name="Etape 2 :", value="Tu passe un entretien vocal avec un administrateur", inline=False)
        embed.add_field(name="Etape 3 : ", value="Tu rentre (ou non) dans le staff et tu est en phase de **test**", inline=False)
        embed.add_field(name="Ensuite ?", value="En fonction de ton activité et de tes compétences, a la fin de la periode de test, tu rentre officielement dans le staff ou tu reviens membre.", inline=False)
        embed.add_field(name="Tu est prêt.e ?", value="Selectionne le rôle staff que tu souhaite avoir mais attention : après avoir cliqué, tu t'engage et pas de retour possible. Tout abus sera sanctionné", inline=False)
        embed2 = discord.Embed(title="La suite dans les messages privés", description="Afin de faciliter pour la persistance des messages pour mieux t'y retrouver, la suite du recrutement est envoyé dans les messages privés", colour=discord.Colour.blue())
        try:
            await interaction.response.send_message(embed=embed2)
            await interaction.user.send(embed=embed)
        except discord.Forbidden:
            await interaction.response.send_message("Tu n'a pas activé les messages privés !", embed=embed)
            await interaction.followup.send("Tu n'a pas activé les messages privés !. Active-les, c'est **obligatoire** !")

class RecrutementCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    @commands.command(name="setup-recrutement")
    async def setup_recrutement(self, ctx):
        print("190:17")
        await ctx.message.delete()
        print("192:16")
        embed = discord.Embed(title="Recrutement",
                              color=discord.Color.green(),
                              description="Regarde ci-dessous les options de recrutement et si l'une d'entre elles t'interesse, clique sur le menu ci-dessous et découvre les details du rôle.")
        embed.add_field(name="Modérateur",
                        value="Ton rôle : sanctionner les abus sur le serveur et rendre actif le serveur.",
                        inline=False)
        embed.add_field(name="Community manager",
                        value="Ton rôle : rechercher et faire des partenariats pour Pixel Party dans le but de faire grandir le serveur.",
                        inline=False)
        embed.add_field(name="Parrain",
                        value="Ton rôle : Mettre notre publicité sur d'autres serveurs pour faire grandir le serveur.",
                        inline=False)
        print("205:16")
        await ctx.channel.send(embed=embed)

async def setup(bot):
    await bot.add_cog(RecrutementCog(bot))