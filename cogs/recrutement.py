import discord
from discord.ext import commands
from dotenv import load_dotenv
import logging
import aiomysql
load_dotenv()
from utils.database import get_pool
from utils.sanctions import get_modo_channel
from utils.config import get_config
import time

logger = logging.getLogger(__name__)


class RaisonModal(discord.ui.Modal, title="Raison du refus"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Cette candidature n'est pas retenue car ...",
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
            await interaction.response.send_message("❌ Erreur : message introuvable.", ephemeral=True)
            return

        await interaction.response.send_message(
            f"La candidature de {membre.mention} a été refusée par {interaction.user.mention}.\nRaison : {self.raison.value}"
        )

        view = Accepterview()
        for item in view.children:
            item.disabled = True

        await message.edit(view=view)

        embed = discord.Embed(title="Candidature refusée",
                              description="Ta candidature pour devenir modérateur sur Pixel Party n'a malheureusement pas été retenue. N'hésite pas à retenter ta chance plus tard !",
                              color=discord.Color.red())
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        embed.add_field(name="Raison du refus :", value=raison, inline=False)

        # L'envoi du DM ne doit pas empêcher le nettoyage de la candidature en base :
        # sinon un membre ayant fermé ses MP reste bloqué en "candidature en cours" à vie.
        try:
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("DELETE FROM role_special WHERE user_id = %s", (membre.id,))
                await conn.commit()
        except aiomysql.Error as e:
            await interaction.followup.send(f"❌ Erreur de base de données : {e}", ephemeral=True)


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
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = %s",
                                   (interaction.message.id,))
                    row = await cursor.fetchone()
                    if row is None:
                        await interaction.followup.send("❌ Candidature introuvable.")
                        return
                    user_id = row[0]
                    guild = interaction.guild
                    membre = guild.get_member(user_id)
                    if membre is None:
                        try:
                            membre = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            membre = None
                    # Nettoyage fait dans tous les cas (même si le membre a quitté le
                    # serveur) : sinon sa candidature reste bloquée "en cours" à vie
                    # (voir ConditionsSelect.commencer, qui refuse toute nouvelle
                    # candidature tant que cette ligne existe).
                    await cursor.execute("DELETE FROM role_special WHERE user_id = %s", (user_id,))
                await conn.commit()
        except aiomysql.Error as e:
            await interaction.followup.send(f"❌ Erreur de base de données : {e}")
            return

        if membre is None:
            await interaction.followup.send("❌ Ce membre n'est plus sur le serveur, sa candidature a été retirée.")
            return

        embed = discord.Embed(title="Candidature acceptée",
                              description=f"Félicitations {membre.mention} ! Ta candidature pour devenir modérateur sur Pixel Party est acceptée !", color=discord.Color.green())
        embed.add_field(name="Les étapes suivantes :", value="Tu vas passer modérateur test : tu auras accès à des salons privés et tu traverseras une période d'essai pour montrer tes compétences et ton implication. À l'issue de cette période, selon ta performance, tu rejoindras officiellement le staff ou tu redeviendras simple membre.", inline=False)
        embed.add_field(name="Durée :", value="La période de test dure une semaine.", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)

        # L'échec du DM ne doit pas empêcher l'ajout du rôle ni le suivi de la période de test.
        try:
            await membre.send(embed=embed)
        except discord.Forbidden:
            pass

        raw_role_id = get_config("ROLE_RECRUTEMENT")
        if not raw_role_id:
            await interaction.followup.send(
                "❌ Configuration manquante : ROLE_RECRUTEMENT n'est pas défini dans la table `config`, "
                "le rôle de test n'a pas pu être attribué."
            )
            return

        role_id = int(raw_role_id)
        role = interaction.guild.get_role(role_id)
        if role is not None:
            try:
                await membre.add_roles(role)
            except discord.Forbidden:
                await interaction.followup.send(f"❌ Impossible d'ajouter le rôle à {membre.mention} (permissions insuffisantes).")
                return

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                time_end = int(time.time()) + 7 * 24 * 3600
                await cursor.execute(
                    "INSERT INTO temp_roles (user_id, role_id, end_time, origin) VALUES (%s, %s, %s, 'staff_test')",
                    (user_id, role.id if role else role_id, int(time_end))
                )
            await conn.commit()

        await interaction.followup.send(f"La candidature de {membre.mention} a été acceptée par {interaction.user.mention} ✅")

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, emoji="❌", custom_id="recrutement:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT user_id FROM role_special WHERE message_accepter_id = %s", (interaction.message.id,))
                    row = await cursor.fetchone()

            if row is None:
                await interaction.response.send_message("❌ Candidature introuvable.", ephemeral=True)
                return

            user_id = row[0]
            membre = interaction.guild.get_member(user_id)
            if membre is None:
                membre = await interaction.guild.fetch_member(user_id)
        except aiomysql.Error as e:
            await interaction.response.send_message(f"❌ Erreur : {e}", ephemeral=True)
            return
        except discord.NotFound:
            # Même nettoyage que Accepterview.accepter dans ce cas : sinon, avec la
            # contrainte UNIQUE(user_id) sur role_special, ce membre ne pourrait
            # plus jamais repostuler même en revenant sur le serveur.
            try:
                pool = get_pool()
                async with pool.acquire() as conn:
                    async with conn.cursor() as cursor:
                        await cursor.execute("DELETE FROM role_special WHERE user_id = %s", (user_id,))
                    await conn.commit()
            except aiomysql.Error as e:
                await interaction.response.send_message(f"❌ Erreur de base de données : {e}", ephemeral=True)
                return
            await interaction.response.send_message(
                "❌ Ce membre n'est plus sur le serveur, sa candidature a été retirée.", ephemeral=True
            )
            return

        await interaction.response.send_modal(RaisonModal(interaction.message, membre))

class RecrutementModal(discord.ui.Modal, title="Formulaire de recrutement"):
    question1 = discord.ui.TextInput(label="Pourquoi devenir modérateur ?", placeholder="Pourquoi veux-tu devenir modérateur sur Pixel Party ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question2 = discord.ui.TextInput(label="Réaction face à une insulte ?", placeholder="Un membre insulte un autre membre. Que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question3 = discord.ui.TextInput(label="Si un ami enfreint une règle ?", placeholder="Si un de tes amis enfreint une règle, que fais-tu ?", style=discord.TextStyle.paragraph, required=True, min_length=50)
    question4 = discord.ui.TextInput(label="Temps disponible par semaine ?", placeholder="Combien de temps peux-tu consacrer au serveur par semaine ?", style=discord.TextStyle.paragraph, required=True)
    question5 = discord.ui.TextInput(label="Qu'est-ce qu'un mauvais modérateur ?", placeholder="Selon toi, qu'est-ce qu'un mauvais modérateur ?", style=discord.TextStyle.paragraph, required=True, min_length=50)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            embed = discord.Embed(
                title="Formulaire reçu",
                description=f"Merci {interaction.user.mention} pour tes réponses !",
                color=discord.Color.green()
            )

            embed.add_field(name="Pourquoi devenir modérateur ?", value=self.question1.value, inline=False)
            embed.add_field(name="Réaction face à une insulte ?", value=self.question2.value, inline=False)
            embed.add_field(name="Si un ami enfreint une règle ?", value=self.question3.value, inline=False)
            embed.add_field(name="Temps disponible par semaine ?", value=self.question4.value, inline=False)
            embed.add_field(name="Mauvais modérateur ?", value=self.question5.value, inline=False)

            await interaction.response.send_message("✅ Formulaire envoyé !", embed=embed, ephemeral=True)

            channel = await get_modo_channel(interaction.client, interaction.guild)
            if channel is None:
                await interaction.followup.send(
                    "❌ Configuration manquante : le salon de modération (CHANNEL_MODO_ID) est introuvable.",
                    ephemeral=True
                )
                return

            msg = await channel.send(embed=embed, view=Accepterview())

            # DB — contrainte UNIQUE(user_id) sur role_special (voir
            # utils/setupdatabase.py) : si ce membre a déjà une candidature en cours
            # (ex: double clic sur "Commencer" avant que la première n'ait été
            # enregistrée), l'INSERT échoue au lieu de créer un doublon silencieux.
            pool = get_pool()
            try:
                async with pool.acquire() as conn:
                    async with conn.cursor() as c:
                        await c.execute(
                            "INSERT INTO role_special (user_id, status, message_accepter_id) VALUES (%s, %s, %s)",
                            (interaction.user.id, 1, msg.id)
                        )
                    await conn.commit()
            except aiomysql.IntegrityError:
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass
                await interaction.followup.send(
                    "❌ Tu as déjà une candidature en cours (soumise en parallèle) — celle-ci n'a pas été enregistrée.",
                    ephemeral=True
                )
                return


        except Exception as e:
            logger.error(f"💥 ERREUR MODAL : {e}")
            await interaction.followup.send(f"❌ Une erreur est survenue : {e}", ephemeral=True)


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
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT status FROM role_special WHERE user_id = %s", (interaction.user.id,))
                    row = await cur.fetchone()
        except aiomysql.Error as e:
            await interaction.followup.send(f"❌ Erreur de base de données : {e}", ephemeral=True)
            return

        # ---------------------------------------------------------
        # 🔒 VERIFICATIONS AVANT RECRUTEMENT
        # ---------------------------------------------------------

        # 1. Vérifie si déjà en procédure
        if row:
            await interaction.followup.send("❌ Tu as déjà une candidature en cours !", ephemeral=True)
            return

        # 2. Vérifie ancienneté (1 mois)
        joined_at = interaction.user.joined_at
        if joined_at is None:
            await interaction.followup.send("❌ Impossible de vérifier ton ancienneté.", ephemeral=True)
            return

        if (discord.utils.utcnow() - joined_at).days < 30:
            await interaction.followup.send("❌ Tu dois être sur le serveur depuis au moins un mois.", ephemeral=True)
            return

        # 3. Vérifie les avertissements (max 3)
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT COUNT(*) FROM warns WHERE user_id = %s", (interaction.user.id,))
                    warn_count = (await cur.fetchone())[0]
        except aiomysql.Error:
            await interaction.followup.send("❌ Erreur lors de la vérification des avertissements.", ephemeral=True)
            return

        if warn_count > 3:
            await interaction.followup.send("❌ Tu as trop d'avertissements pour postuler.", ephemeral=True)
            return
        embed = discord.Embed(title="Commencer le recrutement", description=f"Bienvenue dans le système de recrutement, {interaction.user.name} !", colour=discord.Colour.blue())
        embed.add_field(name="Étape 1 :", value="Remplis le formulaire ci-dessous pour donner tes informations au staff", inline=False)
        embed.add_field(name="Étape 2 :", value="Tu passes un entretien vocal avec un administrateur", inline=False)
        embed.add_field(name="Étape 3 :", value="Tu rejoins (ou non) le staff, en phase de **test**", inline=False)
        embed.add_field(name="Ensuite ?", value="En fonction de ton activité et de tes compétences, tu rejoins officiellement le staff à la fin de la période de test, ou tu redeviens membre.", inline=False)
        embed.add_field(name="Tu es prêt.e ?", value="Sélectionne le rôle staff que tu souhaites obtenir. Attention : une fois cliqué, tu t'engages et il n'y a pas de retour en arrière possible. Tout abus sera sanctionné.", inline=False)
        icon = interaction.guild.icon.url if interaction.guild.icon else None
        embed.set_footer(text="Pixel Party - Système de recrutement", icon_url=icon)
        embed2 = discord.Embed(title="La suite dans les messages privés", description="Pour que tu puisses mieux t'y retrouver, la suite du recrutement se déroule en messages privés.", colour=discord.Colour.blue())
        try:
            await interaction.followup.send(embed=embed2, ephemeral=True)
            await interaction.user.send(embed=embed, view=FormulaireBouton())
        except discord.Forbidden:
            await interaction.followup.send(
                "Tes messages privés sont désactivés. Active-les : c'est **obligatoire** pour continuer le recrutement.",
                ephemeral=True
            )

class RecrutementCog(commands.Cog):
    # Le panneau de recrutement se poste désormais via /creer-message
    # (cogs/creermessage.py), qui reprend cet embed et ConditionsSelect ci-dessus.
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(RecrutementCog(bot))
