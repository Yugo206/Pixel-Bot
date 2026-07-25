import aiomysql
import time
import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from cogs.warn import ContestationView
from utils.database import get_pool
from utils.sanctions import apply_warn_sanction, get_modo_channel


class AvisModal(discord.ui.Modal, title="Ton avis"):
    avis = discord.ui.TextInput(
        label="Laisse ton avis",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500
    )

    def __init__(self, bot, view, message):
        super().__init__()
        self.bot = bot
        self.view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        channel = await get_modo_channel(self.bot)
        if channel is None:
            await interaction.response.send_message("❌ Impossible de trouver le salon de modération.", ephemeral=True)
            return

        await channel.send(
            f"Avis de {interaction.user.mention} :\n{self.avis.value}"
        )

        for child in self.view.children:
            if child.custom_id == "ticket:explique":
                child.disabled = True

        await self.message.edit(view=self.view)
        await interaction.response.send_message("Merci pour ton avis !", ephemeral=True)


class AvisView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Comment as-tu trouvé le staff ?",
        custom_id="ticket:select",
        options=[
            discord.SelectOption(label="Très agréable", value="Très agréable"),
            discord.SelectOption(label="Bonne", value="Bonne"),
            discord.SelectOption(label="Moyenne", value="Moyenne"),
            discord.SelectOption(label="Mauvaise", value="Mauvaise"),
            discord.SelectOption(label="Détestable", value="Détestable"),
        ]
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        bot = interaction.client
        advisor = await get_modo_channel(bot)
        if advisor is None:
            await interaction.response.send_message("ERREUR : Ouvre un ticket sur Pixel Party pour resoudre le probleme", ephemeral=True)
            return

        await advisor.send(
            f"Avis de {interaction.user.mention} : {select.values[0]}"
        )

        select.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Merci pour ton avis !", ephemeral=True)

    @discord.ui.button(
        label="Explique-nous !",
        style=discord.ButtonStyle.blurple,
        custom_id="ticket:explique"
    )
    async def explique(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        modal = AvisModal(
            bot=bot,
            view=self,
            message=interaction.message
        )
        await interaction.response.send_modal(modal)


class ModoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Prendre en chage", style=discord.ButtonStyle.blurple, custom_id="ticket:prendre")
    async def prendre(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT thread_id, membre_id, message_ticket_id FROM ticket WHERE modo_message_id = %s",
                        (interaction.message.id,)
                    )
                    result = await c.fetchone()
        except aiomysql.Error as e:
            print(e)
            await interaction.followup.send("ERREUR DB : Contacte <@1377571267108143194> pour resoudre le probleme", ephemeral=True)
            return

        if result is None:
            await interaction.followup.send("ERREUR DB : Aucun ticket trouvé", ephemeral=True)
            return

        thread_id, membre_id, message_ticket_id = result
        if thread_id is None or membre_id is None or message_ticket_id is None:
            await interaction.followup.send("ERREUR DB : Contacte <@1377571267108143194> pour resoudre le probleme", ephemeral=True)
            return

        try:
            thread = interaction.guild.get_channel(thread_id) or await interaction.guild.fetch_channel(thread_id)
            message_ticket = await thread.fetch_message(message_ticket_id)
        except discord.NotFound:
            await interaction.followup.send("❌ Le ticket ou son message d'origine n'existe plus.", ephemeral=True)
            return

        await interaction.followup.send(f"Tu a pris le ticket. Le lien est ici : {thread.mention}.", ephemeral=True)

        if message_ticket.embeds:
            embed = message_ticket.embeds[0]
            embed.set_field_at(2, name="Modérateur : ", value=interaction.user.mention)
            embed.set_field_at(4, name="Statue", value="Actif")
            await message_ticket.edit(embed=embed)

        button.disabled = True
        await interaction.message.edit(view=self)

        messs = await thread.send(f"{interaction.user.mention}")
        await messs.delete()

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET modo_id = %s, statut = %s WHERE thread_id = %s",
                        (interaction.user.id, 2, thread_id))
                await conn.commit()
        except aiomysql.Error as e:
            print(e)


class SatisfactionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        options=[
            discord.SelectOption(label="Super bien !", description="Le ticket s'est bien passé", emoji="🙂"),
            discord.SelectOption(label="Mal", description="Le membre a insulté / n'a pas respecté le staff", emoji="😕"),
            discord.SelectOption(label="Pas de reponse",
                                 description="Tu as mentionné plusieurs fois le membre, mais pas de réponses.",
                                 emoji="🚫")
        ],
        custom_id="ticket:satisfaction"
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        # On defer immédiatement : la suite (DB, timeout/ban, DM) peut dépasser les 3s
        # accordées par Discord pour répondre à l'interaction.
        await interaction.response.defer()

        selected_value = select.values[0]
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT membre_id FROM ticket WHERE thread_id = %s",
                          (interaction.channel.id,))
                rpw = await c.fetchone()

        if rpw is None:
            await interaction.followup.send("❌ Impossible de retrouver ce ticket en base de données.", ephemeral=True)
            return

        bot = interaction.client
        membre = bot.get_user(rpw[0])
        if membre is None:
            try:
                membre = await bot.fetch_user(rpw[0])
            except discord.NotFound:
                await interaction.followup.send("❌ Le membre de ce ticket est introuvable.", ephemeral=True)
                return

        # Cas positif : rien à faire sauf désactiver le select
        if selected_value == "Super bien !":
            await self._disable_and_respond(interaction)
            return

        # Cas négatifs : "Mal" ou "Pas de reponse"
        warn_count = 0
        warn_id = None

        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT warn FROM utilisateurs WHERE user_id = %s", (membre.id,))
                    result = await c.fetchone()

                    iso_time = datetime.now(timezone.utc).isoformat()

                    if result is None:
                        await c.execute("INSERT INTO utilisateurs (user_id, warn) VALUES (%s, 1)", (membre.id,))
                        warn_count = 1
                    elif result[0] is None:
                        await c.execute("UPDATE utilisateurs SET warn = 1 WHERE user_id = %s", (membre.id,))
                        warn_count = 1
                    else:
                        warn_count = result[0] + 1
                        await c.execute("UPDATE utilisateurs SET warn = %s WHERE user_id = %s", (warn_count, membre.id))

                    await c.execute(
                        "INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso) VALUES (%s, %s, %s, %s, %s)",
                        (membre.id, interaction.user.id, "Non respect des conditions d'ouverture de ticket",
                         int(time.time()), iso_time)
                    )

                    warn_id = c.lastrowid

                await conn.commit()

        except aiomysql.Error as e:
            print(f"Erreur SQL: {e}")
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        # Appliquer les sanctions selon le nombre de warns
        channel = await get_modo_channel(bot, interaction.guild)
        await apply_warn_sanction(interaction.guild, membre, channel, warn_count)

        # Créer l'embed d'avertissement
        embed = self._create_warn_embed(selected_value, interaction.user)

        # Envoyer le message au membre
        try:
            view = ContestationView(membre, bot, warn_id)
            await membre.send(embed=embed, view=view)
        except discord.Forbidden:
            print(f"Impossible d'envoyer un DM à {membre}")
        except Exception as e:
            print(f"Erreur envoi DM: {e}")

        await self._disable_and_respond(interaction)

    def _create_warn_embed(self, selected_value: str, modo: discord.User) -> discord.Embed:
        """Crée l'embed d'avertissement selon le type de problème."""
        if selected_value == "Mal":
            embed = discord.Embed(
                title="Tu viens d'être averti",
                description=f"Tu t'es mal comporté dans ton ticket, donc tu viens de recevoir un avertissement par {modo.mention}.",
                color=discord.Color.red()
            )
        else:  # "Pas de reponse"
            embed = discord.Embed(
                title="Tu viens d'être averti",
                description=f"Tu n'as pas répondu dans ton ticket, donc tu viens de recevoir un avertissement par {modo.mention}.",
                color=discord.Color.orange()
            )

        embed.add_field(name="C'est une erreur ?", value="Va vite ouvrir un ticket et conteste cet avertissement")
        embed.set_footer(text="Pixel Party")
        return embed

    async def _disable_and_respond(self, interaction: discord.Interaction):
        """Désactive le select sur le message d'origine (déjà deferred)."""
        for child in self.children:
            child.disabled = True

        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException as e:
            print(f"Erreur lors de la désactivation: {e}")


class FermerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        thread = interaction.channel
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT raison, membre_id FROM ticket WHERE thread_id = %s",
                          (thread.id,))
                content = await c.fetchone()

        if content is None or content[0] is None:
            await interaction.followup.send("Probleme DB")
            return

        raison, membre_id = content
        bot = interaction.client
        membre = await bot.fetch_user(membre_id)

        role = discord.utils.get(interaction.user.roles, id=int(os.getenv("ROLE_MODO_ID")))
        if role:
            await interaction.followup.send("Comment s'est passé votre ticket ?", view=SatisfactionView(), ephemeral=True)
        else:
            await interaction.followup.send("Ticket fermé avec succès", ephemeral=True)

        embed = discord.Embed(title="Ticket fermé", description="Ce ticket est fermé. Vous ne pouvez plus ecrire.")
        embed.add_field(name="Fermé par :", value=interaction.user.mention)
        embed.add_field(name="Raison ititiale du ticket : ", value=raison)
        button.disabled = True
        await interaction.message.edit(embed=embed, view=self)

        embed2 = discord.Embed(title="Donne-nous ton avis sur ton ticket !",
                               description="Afin d'ameliorer le systeme de ticket ou de rendre le staff plus efficace, nous souhaitons receuillir ton avis sur ce ticket.")
        try:
            await membre.send(embed=embed2, view=AvisView())
        except discord.Forbidden:
            pass

        ts = int((datetime.now(timezone.utc) + timedelta(seconds=86400)).timestamp())
        await thread.send(f"Ce ticket as été fermé par {interaction.user.mention}. Il se supprimera <t:{ts}:R>")
        await thread.edit(locked=True, archived=True)

        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET statut = 3, closed_at = %s WHERE thread_id = %s",
                        (int(time.time()), thread.id)
                    )
                await conn.commit()
        except aiomysql.Error as e:
            print(e)


class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(placeholder="Selectionne une option", custom_id="ticket:create", options=[
        discord.SelectOption(label="Partenariat", description="Pour proposer ou discuter d'un partenariat entre serveur/projet", emoji="🤝"),
        discord.SelectOption(label="Support technique", description="Pour signer un bug ou demander de l'aide concernant le serveur ou un bot", emoji="🛠️"),
        discord.SelectOption(label="Demande de rôle", description="Pour demander un rôle spécial, une vérification ou un grade particulier", emoji="🗒️"),
        discord.SelectOption(label="Signaler un membre", description="pour signaler un comportement inapproprié du spam ou un non-respect des règles", emoji="🚨"),
        discord.SelectOption(label="Contester une sanction", description="pour discuter d'un mute, kick ou ban que vous jugez injustifié", emoji="⚖️"),
        discord.SelectOption(label="Question générale", description="pour poser des questions sur le serveur, les évènements, ou son fonctionnement", emoji="❓"),
        discord.SelectOption(label="Problème lié aux économies du serveur", description="pour toute question concernant un achat ou un don", emoji="💰"),
        discord.SelectOption(label="Suggestions pour le serveur", description="pour proposer des idées ou amélioration pour le serveur", emoji="💡"),
        discord.SelectOption(label="Autre / privé", description="pour toute autre demande nécessitant une discussion en privé avec le staff", emoji="🔒"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)
        thread = await interaction.channel.create_thread(
            name=f"ticket-{interaction.user.name}",
            invitable=True
        )

        messs = await thread.send(interaction.user.mention)
        await messs.delete()
        raison = select.values[0]
        view = FermerView()
        embed = discord.Embed(title="Gestionnaire de ticket", description=f"Bienvenue {interaction.user.name} sur ton ticket !", colour=discord.Colour.blue())
        embed.add_field(name="Fermer le ticket", value="Tu peut fermer ton ticket à tout moment en cliquant sur ce boutton", inline=False)
        embed.add_field(name="Raison du ticket : ", value=raison)
        embed.add_field(name="Modérateur :", value="Personne")
        embed.add_field(name="Demandé par :", value=interaction.user.mention)
        embed.add_field(name="Statue : ", value="En attente d'un moderateur")
        message = await thread.send(f"Bienvenue {interaction.user.mention} sur ton ticket", embed=embed, view=view)
        await interaction.followup.send(f"Ticket crée avec succès dans {thread.mention}", ephemeral=True)

        channel = await get_modo_channel(interaction.client, interaction.guild)
        messsages = None
        if channel is None:
            print("PAS DE CHANNEL DE MODERATION TROUVÉ (CHANNEL_MODO_ID) !")
        else:
            embed2 = discord.Embed(title="Ticket ouvert !", description="Clique sur le boutton ci-dessous pour acceder au ticket et le prendre en charge.", colour=discord.Colour.blue())
            messsages = await channel.send(embed=embed2, view=ModoView())

        await interaction.message.edit(view=TicketCreateView())

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO ticket (thread_id, membre_id, statut, raison, modo_message_id, message_ticket_id) VALUES (%s, %s, %s, %s, %s, %s)",
                        (thread.id, interaction.user.id, 1, raison, messsages.id if messsages else None, message.id)
                    )
                await conn.commit()
        except aiomysql.Error as e:
            print(e)

        if raison == "Partenariat":
            embed_partenariat_intro = discord.Embed(title="Bienvenue sur ton ticket partenariat !",
                                         description="Afin de faciliter le travail du staff et te faire gagner du temps, nous souhaitons récuperer les informations du partenariat.",
                                         colour=discord.Colour.blue())
            embed_partenariat_intro.add_field(name="Etape 1 : Conditions", value="Ces conditions sont obligatoires et mêmes sans le bot ces condition doivent etre accepté sinon partenariat impossible.", inline=False)
            embed_partenariat_intro.add_field(name="Etape 2 : Ton sevreur", value="Fait une courte descripions de ce qu'est ton serveur.", inline=False)
            embed_partenariat_intro.add_field(name="Etape 3 : Mentions", value="Donne quelle mention veux que ton serveur et notre serveur.", inline=False)
            embed_partenariat_intro.add_field(name="Etape 3 : Ta pub", value="Tu donne la publicité de ton serveur avec le lien. Si tu n'a pas de pub, envoie juste le lien.", inline=False)
            embed_partenariat_intro.add_field(name="Etape 5 : Notre pub & finalistion", value="Le bot envoie la pu du serveur. Le staff viendra ensuite pour publier les annonces.", inline=False)
            embed_partenariat_intro.add_field(name="Alors, pret a commencer ?", value=" Clique sur le boutton \"Demarrer\" ci-dessous")
            await thread.send(embed=embed_partenariat_intro, view=PartenariatCommencerView())


class PartenariatCommencerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Demarrer", style=discord.ButtonStyle.green, custom_id="Partenariat:Commencer")
    async def demarrer(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = discord.Embed(
            title="🤝 Conditions de partenariat",
            description=(
                "Avant de demander un partenariat, merci de lire **attentivement** les conditions ci-dessous.\n"
                "Toute demande ne respectant pas ces règles sera **refusée automatiquement**."
            ),
            colour=discord.Colour.blurple()
        )

        embed.add_field(
            name="📌 Conditions obligatoires",
            value=(
                "• Serveur **actif** (minimum **10 membres**)\n"
                "• Serveur créé depuis **au moins 7 jours**\n"
                "• Contenu **légal et respectueux**\n"
                "• Partenariat **réciproque obligatoire**"
            ),
            inline=False
        )

        embed.add_field(
            name="⭐ Critères de qualité",
            value=(
                "• Thématique compatible (Gaming / Tech / Communauté)\n"
                "• Serveur bien organisé\n"
                "• Pas de spam, fake giveaways ou pubs abusives\n"
                "• Lien d'invitation **permanent**"
            ),
            inline=False
        )

        embed.add_field(
            name="📝 Informations à fournir",
            value=(
                "• Nom du serveur\n"
                "• Thématique\n"
                "• Nombre de membres\n"
                "• Lien d'invitation\n"
                "• Texte du partenariat prêt à poster"
            ),
            inline=False
        )

        embed.add_field(
            name="⚠️ Règles importantes",
            value=(
                "• Ping <@1418958299927412879> seulement (sauf exeption du staff) \n"
                "• Tu doit mettre ta pub en premier.\n"
                "• Invitation expirée ou message supprimé = partenariat annulé"
            ),
            inline=False
        )
        embed.set_footer(text="En faisant un partenariat, tu t'engage a respecter ces règles")
        view = ConditionsPartenariatView()
        await interaction.response.send_message(embed=embed, view=view)
        button.disabled = True
        await interaction.message.edit(view=self)


class ConditionsPartenariatView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="partenariat:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        bot = interaction.client
        thread = interaction.channel

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Description de ton serveur",
                description="Envoie une description de ton serveur.",
                colour=discord.Colour.blurple()
            )
        )
        button.disabled = True
        await interaction.message.edit(view=self)

        def check(m):
            return m.author == interaction.user and m.channel == thread

        description = None
        try:
            desc_msg = await bot.wait_for("message", timeout=240, check=check)
            description = desc_msg.content
        except asyncio.TimeoutError:
            await thread.send("⏱️ Temps écoulé, on continue.")

        await thread.send(
            embed=discord.Embed(
                title="Publicité de ton serveur",
                description="Envoie maintenant ta publicité.",
                colour=discord.Colour.blurple()
            )
        )

        pub = None
        try:
            pub_msg = await bot.wait_for("message", timeout=240, check=check)
            pub = pub_msg.content
        except asyncio.TimeoutError:
            await thread.send("⏱️ Pas de pub reçue.")

        await thread.send(
            embed=discord.Embed(
                title="Choix de la mention",
                description="Choisis la mention souhaitée",
                colour=discord.Colour.blurple()
            ),
            view=MentionPartenariatView(description, pub)
        )


class MentionPartenariatView(discord.ui.View):
    def __init__(self, description: str | None = None, pub: str | None = None):
        super().__init__(timeout=None)
        self.description = description
        self.pub = pub

    @discord.ui.select(options=[
        discord.SelectOption(label="Aucune mention", description="Aucune mention sur ton serveur", emoji="🚫"),
        discord.SelectOption(label="Mention \"Here\"", description="Mention here sur ton serveur", emoji="🧑‍🧒"),
        discord.SelectOption(label="Mention \"Partenariat\"", description="Mention partenariat sur ton serveur", emoji="🧑‍🧑‍🧒"),
        discord.SelectOption(label="Mention \"Everyone\"", description="Mention everyone sur ton serveur", emoji="🧑‍🧑‍🧒‍🧒")
    ], custom_id="partenariat:mention")
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        mention = select.values[0]
        channel = interaction.message.channel
        await interaction.response.send_message(f"Mention choisi : {mention}")
        embed = discord.Embed(title="Informations collectés !",
                              description="Toute les informations de ton serveur ont été récupérés. Si il en manque, le staff te les demandera.")
        embed.add_field(name="Description du serveur", value=self.description or "Non renseignée", inline=False)
        embed.add_field(name="Publicité", value=self.pub or "Non renseignée", inline=False)
        embed.add_field(name="Mention souhaitée", value=mention, inline=False)
        embed.add_field(name="Tu pourrait de demander : je fait quoi maintenant ?",
                        value="Tu attends que le staff traite ta demande. Reste toujours disponible pour aller le plus vite. En attendant, je t'envoie la pub de Pixel Party", inline=False)
        await channel.send(embed=embed)
        await channel.send("# **🎮 Pixel Party | Serveur Multigaming Fun & Actif !** \n ## **Tu cherches un endroit pour jouer, discuter et rigoler ? Rejoins Pixel Party !** \n 🔥 Jeux populaires : Fortnite • Brawl Stars • Minecraft • Roblox \n 🎉 Événements : cache-cache, défilés de mode, défis d’armes, tournois… \n 🏅 Rôles spéciaux à débloquer : VIP, Nintendo, PS5, etc. \n 🗨️ Une vraie communauté chill pour se faire des potes \n 💬 Que tu sois joueur switch, PC, mobile ou console… t’es le/la bienvenu(e) ! \n 🔗 Rejoins-nous maintenant en cliquant [ici](https://discord.gg/cnWz7fXAex)")


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(Tickets(bot))
