import aiomysql
import logging
import time
import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from cogs.warn import ContestationView
from utils.database import get_pool, increment_warn
from utils.sanctions import apply_warn_sanction, get_modo_channel

logger = logging.getLogger(__name__)

# ID à contacter en cas d'erreur DB critique (avant : codé en dur dans le fichier).
OWNER_ID = os.getenv("OWNER_ID")


def _owner_mention() -> str:
    return f"<@{OWNER_ID}>" if OWNER_ID else "un administrateur"


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
            await interaction.response.send_message("❌ Erreur : ouvre un ticket sur Pixel Party pour résoudre le problème.", ephemeral=True)
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

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.blurple, custom_id="ticket:prendre")
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
            logger.critical(f"[tickets:prendre] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send(f"❌ Erreur de base de données, contacte {_owner_mention()} pour résoudre le problème.", ephemeral=True)
            return

        if result is None:
            await interaction.followup.send("❌ Erreur de base de données : aucun ticket trouvé.", ephemeral=True)
            return

        thread_id, membre_id, message_ticket_id = result
        if thread_id is None or membre_id is None or message_ticket_id is None:
            await interaction.followup.send(f"❌ Erreur de base de données, contacte {_owner_mention()} pour résoudre le problème.", ephemeral=True)
            return

        try:
            thread = interaction.guild.get_channel(thread_id) or await interaction.guild.fetch_channel(thread_id)
            message_ticket = await thread.fetch_message(message_ticket_id)
        except discord.NotFound:
            await interaction.followup.send("❌ Le ticket ou son message d'origine n'existe plus.", ephemeral=True)
            return

        await interaction.followup.send(f"Tu as pris le ticket. Le lien est ici : {thread.mention}.", ephemeral=True)

        if message_ticket.embeds:
            embed = message_ticket.embeds[0]
            embed.set_field_at(2, name="Modérateur : ", value=interaction.user.mention)
            embed.set_field_at(4, name="Statut", value="Actif")
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
            logger.critical(f"[tickets:prendre] Erreur DB update : {e}", exc_info=True)


class SatisfactionView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        options=[
            discord.SelectOption(label="Super bien !", description="Le ticket s'est bien passé", emoji="🙂"),
            discord.SelectOption(label="Mal", description="Le membre a insulté / n'a pas respecté le staff", emoji="😕"),
            discord.SelectOption(label="Pas de réponse",
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
        # Résolu comme discord.Member (et non via bot.get_user/fetch_user) : nécessaire
        # pour que apply_warn_sanction (utils/sanctions.py) puisse le timeout si le
        # palier de warns l'exige — .timeout() n'existe pas sur un simple discord.User.
        membre = interaction.guild.get_member(rpw[0])
        if membre is None:
            try:
                membre = await interaction.guild.fetch_member(rpw[0])
            except discord.NotFound:
                # A quitté le serveur : on retombe sur un discord.User pour pouvoir
                # quand même enregistrer le warn et tenter un DM. apply_warn_sanction
                # gère ce cas si le palier atteint est un timeout (impossible hors serveur).
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
        warn_id = None

        try:
            # Incrément atomique (voir utils/database.py) : évite que ce warn et un
            # /warn (ou une autre satisfaction de ticket) posés au même moment sur ce
            # membre ne s'écrasent l'un l'autre.
            warn_count = await increment_warn(membre.id)

            iso_time = datetime.now(timezone.utc).isoformat()

            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso) VALUES (%s, %s, %s, %s, %s)",
                        (membre.id, interaction.user.id, "Non respect des conditions d'ouverture de ticket",
                         int(time.time()), iso_time)
                    )

                    warn_id = c.lastrowid

                await conn.commit()

        except aiomysql.Error as e:
            logger.critical(f"[tickets:avis] Erreur SQL : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        # Appliquer les sanctions selon le nombre de warns
        channel = await get_modo_channel(bot, interaction.guild)
        await apply_warn_sanction(interaction.guild, membre, channel, warn_count)

        # Créer l'embed d'avertissement
        embed = self._create_warn_embed(selected_value, interaction.user, warn_id)

        # Envoyer le message au membre
        try:
            await membre.send(embed=embed, view=ContestationView())
        except discord.Forbidden:
            logger.warning(f"Impossible d'envoyer un DM à {membre}")
        except Exception as e:
            logger.error(f"Erreur envoi DM : {e}")

        await self._disable_and_respond(interaction)

    def _create_warn_embed(self, selected_value: str, modo: discord.User, warn_id: int | None) -> discord.Embed:
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
        # Footer parsé par ContestationView (cogs/warn.py) pour retrouver le warn
        # concerné sans avoir besoin de le stocker sur l'instance de la vue.
        embed.set_footer(text=f"ID du warn : {warn_id}")
        return embed

    async def _disable_and_respond(self, interaction: discord.Interaction):
        """Désactive le select sur le message d'origine (déjà deferred)."""
        for child in self.children:
            child.disabled = True

        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException as e:
            logger.error(f"Erreur lors de la désactivation : {e}")


class ConfirmationClotureView(discord.ui.View):
    """Envoyée en MP au modérateur assigné quand un ticket est fermé automatiquement
    pour inactivité (voir ticket_watcher dans start.py). Contrairement à
    SatisfactionView (déclenchée par un clic sur "Fermer le ticket"), il n'y a ici
    aucune interaction d'origine à qui répondre ephemeral : le lien vers le ticket
    concerné passe par `ticket.mod_dm_message_id` plutôt que par le salon."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Comment s'est passé ce ticket ?",
        options=[
            discord.SelectOption(label="Super bien !", description="Le ticket s'est bien passé", emoji="🙂"),
            discord.SelectOption(label="Mal", description="Le membre a mal agi, il faut reprendre la main dessus", emoji="😕"),
            discord.SelectOption(label="Pas de réponse", description="Le membre n'a jamais répondu, il faut relancer", emoji="🚫"),
        ],
        custom_id="ticket:confirmation_cloture_auto"
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        await interaction.response.defer(ephemeral=True)

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "SELECT thread_id FROM ticket WHERE mod_dm_message_id = %s",
                    (interaction.message.id,)
                )
                row = await c.fetchone()

        if row is None:
            await interaction.followup.send(
                "❌ Ce ticket n'est plus associé à ce message (déjà traité, ou supprimé).",
                ephemeral=True
            )
            return

        thread_id = row[0]

        for child in self.children:
            child.disabled = True
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException as e:
            logger.error(f"[tickets:confirmation_cloture] Erreur désactivation select : {e}")

        # Cas positif : rien à rouvrir, on s'arrête là.
        if select.values[0] == "Super bien !":
            await interaction.followup.send("Merci pour ton retour !", ephemeral=True)
            return

        # Cas négatifs ("Mal" / "Pas de reponse") : le ticket est rouvert pour que
        # le modérateur reprenne la main dessus.
        try:
            thread = interaction.client.get_channel(thread_id) or await interaction.client.fetch_channel(thread_id)
        except discord.NotFound:
            await interaction.followup.send(
                "❌ Le ticket a été supprimé entre-temps, impossible de le rouvrir.",
                ephemeral=True
            )
            return

        await thread.edit(archived=False, locked=False)
        await thread.send(
            f"🔓 Ce ticket a été rouvert par {interaction.user.mention} suite à la fermeture automatique pour inactivité."
        )

        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET statut = 2, closed_at = NULL WHERE thread_id = %s",
                        (thread_id,)
                    )
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:confirmation_cloture] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send(
                "⚠️ Ticket rouvert sur Discord mais erreur DB à l'enregistrement, contacte "
                f"{_owner_mention()}.",
                ephemeral=True
            )
            return

        await interaction.followup.send(f"Ticket rouvert : {thread.mention}", ephemeral=True)


async def demander_confirmation_moderateur(bot, thread: discord.Thread, modo_id: int):
    """MP le modérateur assigné à un ticket fermé automatiquement pour inactivité,
    pour lui demander si tout s'est bien passé (et lui permettre de rouvrir le
    ticket sinon). Appelé par ticket_watcher (start.py) après la fermeture."""
    try:
        modo = bot.get_user(modo_id) or await bot.fetch_user(modo_id)
    except discord.NotFound:
        logger.warning(f"[tickets:confirmation] Modérateur {modo_id} introuvable (ticket {thread.id}).")
        return

    embed = discord.Embed(
        title="Ticket fermé automatiquement",
        description=(
            f"Le ticket {thread.mention} a été fermé pour inactivité (72h sans réponse "
            "du membre). Comment s'est-il passé ?"
        ),
        colour=discord.Colour.orange()
    )

    try:
        message = await modo.send(embed=embed, view=ConfirmationClotureView())
    except discord.Forbidden:
        logger.warning(f"[tickets:confirmation] MP impossible à {modo_id} (ticket {thread.id}) : DMs fermés.")
        return

    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "UPDATE ticket SET mod_dm_message_id = %s WHERE thread_id = %s",
                    (message.id, thread.id)
                )
            await conn.commit()
    except aiomysql.Error as e:
        logger.critical(f"[tickets:confirmation] Erreur DB : {e}", exc_info=True)


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
            await interaction.followup.send("❌ Problème de base de données.")
            return

        raison, membre_id = content
        bot = interaction.client
        try:
            membre = await bot.fetch_user(membre_id)
        except discord.NotFound:
            # Compte supprimé entre-temps : on ne peut pas lui envoyer l'avis, mais
            # ça ne doit pas empêcher la fermeture effective du ticket (archivage,
            # statut en DB) — sinon le bouton reste actif et échoue à chaque clic.
            membre = None

        role_modo_id = os.getenv("ROLE_MODO_ID")
        role = discord.utils.get(interaction.user.roles, id=int(role_modo_id)) if role_modo_id else None
        if role:
            await interaction.followup.send("Comment s'est passé ton ticket ?", view=SatisfactionView(), ephemeral=True)
        else:
            await interaction.followup.send("Ticket fermé avec succès", ephemeral=True)

        embed = discord.Embed(title="Ticket fermé", description="Ce ticket est fermé. Tu ne peux plus écrire dedans.")
        embed.add_field(name="Fermé par :", value=interaction.user.mention)
        embed.add_field(name="Raison initiale du ticket : ", value=raison)
        button.disabled = True
        await interaction.message.edit(embed=embed, view=self)

        embed2 = discord.Embed(title="Donne-nous ton avis sur ton ticket !",
                               description="Afin d'améliorer le système de ticket et l'efficacité du staff, nous aimerions recueillir ton avis sur ce ticket.")
        if membre is not None:
            try:
                await membre.send(embed=embed2, view=AvisView())
            except discord.Forbidden:
                pass

        ts = int((datetime.now(timezone.utc) + timedelta(seconds=86400)).timestamp())
        await thread.send(f"Ce ticket a été fermé par {interaction.user.mention}. Il sera supprimé <t:{ts}:R>")
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
            logger.critical(f"[tickets:fermer] Erreur DB : {e}", exc_info=True)


class TicketCreateView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(placeholder="Sélectionne une option", custom_id="ticket:create", options=[
        discord.SelectOption(label="Partenariat", description="Pour proposer ou discuter d'un partenariat entre serveur/projet", emoji="🤝"),
        discord.SelectOption(label="Support technique", description="Pour signaler un bug ou demander de l'aide concernant le serveur ou un bot", emoji="🛠️"),
        discord.SelectOption(label="Demande de rôle", description="Pour demander un rôle spécial, une vérification ou un grade particulier", emoji="🗒️"),
        discord.SelectOption(label="Signaler un membre", description="Pour signaler un comportement inapproprié, du spam ou un non-respect des règles", emoji="🚨"),
        discord.SelectOption(label="Contester une sanction", description="Pour discuter d'un mute, kick ou ban que tu juges injustifié", emoji="⚖️"),
        discord.SelectOption(label="Question générale", description="Pour poser des questions sur le serveur, les évènements, ou son fonctionnement", emoji="❓"),
        discord.SelectOption(label="Problème lié aux économies du serveur", description="Pour toute question concernant un achat ou un don", emoji="💰"),
        discord.SelectOption(label="Suggestions pour le serveur", description="Pour proposer des idées ou améliorations pour le serveur", emoji="💡"),
        discord.SelectOption(label="Autre / privé", description="Pour toute autre demande nécessitant une discussion privée avec le staff", emoji="🔒"),
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
        embed.add_field(name="Fermer le ticket", value="Tu peux fermer ton ticket à tout moment en cliquant sur ce bouton", inline=False)
        embed.add_field(name="Raison du ticket : ", value=raison)
        embed.add_field(name="Modérateur :", value="Personne")
        embed.add_field(name="Demandé par :", value=interaction.user.mention)
        embed.add_field(name="Statut : ", value="En attente d'un modérateur")
        message = await thread.send(f"Bienvenue {interaction.user.mention} sur ton ticket", embed=embed, view=view)
        await interaction.followup.send(f"Ticket créé avec succès dans {thread.mention}", ephemeral=True)

        channel = await get_modo_channel(interaction.client, interaction.guild)
        messsages = None
        if channel is None:
            logger.warning("Aucun salon de modération trouvé (CHANNEL_MODO_ID non configuré ou introuvable).")
        else:
            embed2 = discord.Embed(title="Ticket ouvert !", description="Clique sur le bouton ci-dessous pour accéder au ticket et le prendre en charge.", colour=discord.Colour.blue())
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
            logger.critical(f"[tickets:create] Erreur DB : {e}", exc_info=True)

        if raison == "Partenariat":
            embed_partenariat_intro = discord.Embed(title="Bienvenue sur ton ticket partenariat !",
                                         description="Afin de faciliter le travail du staff et te faire gagner du temps, nous souhaitons récuperer les informations du partenariat.",
                                         colour=discord.Colour.blue())
            embed_partenariat_intro.add_field(name="Étape 1 : Conditions", value="Ces conditions sont obligatoires. Même sans l'aide du bot, elles doivent être acceptées, sinon le partenariat est impossible.", inline=False)
            embed_partenariat_intro.add_field(name="Étape 2 : Ton serveur", value="Fais une courte description de ce qu'est ton serveur.", inline=False)
            embed_partenariat_intro.add_field(name="Étape 3 : Mentions", value="Indique quelle mention tu souhaites entre ton serveur et le nôtre.", inline=False)
            embed_partenariat_intro.add_field(name="Étape 4 : Ta pub", value="Donne la publicité de ton serveur avec le lien. Si tu n'as pas de pub, envoie juste le lien.", inline=False)
            embed_partenariat_intro.add_field(name="Étape 5 : Notre pub & finalisation", value="Le bot envoie la pub du serveur. Le staff viendra ensuite pour publier les annonces.", inline=False)
            embed_partenariat_intro.add_field(name="Alors, prêt à commencer ?", value="Clique sur le bouton \"Démarrer\" ci-dessous")
            await thread.send(embed=embed_partenariat_intro, view=PartenariatCommencerView())


class PartenariatCommencerView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Démarrer", style=discord.ButtonStyle.green, custom_id="Partenariat:Commencer")
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
                "• Ping <@1418958299927412879> seulement (sauf exception du staff) \n"
                "• Tu dois mettre ta pub en premier.\n"
                "• Invitation expirée ou message supprimé = partenariat annulé"
            ),
            inline=False
        )
        embed.set_footer(text="En faisant un partenariat, tu t'engages à respecter ces règles")
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

        # Enregistrées en DB (plutôt que gardées uniquement sur l'instance de
        # MentionPartenariatView) : cette vue est enregistrée globalement au
        # démarrage (bot.add_view, voir cogs/events.py) pour rester persistante, ce
        # qui écraserait des attributs stockés sur l'instance par une instance vide
        # si le bot redémarre avant que l'utilisateur ait cliqué.
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET partenariat_description = %s, partenariat_pub = %s WHERE thread_id = %s",
                        (description, pub, thread.id)
                    )
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:partenariat] Erreur DB : {e}", exc_info=True)

        await thread.send(
            embed=discord.Embed(
                title="Choix de la mention",
                description="Choisis la mention souhaitée",
                colour=discord.Colour.blurple()
            ),
            view=MentionPartenariatView()
        )


class MentionPartenariatView(discord.ui.View):
    """Vue persistante et sans état : la description/pub collectées plus tôt dans
    le flux sont retrouvées dans `ticket` via le thread (voir
    ConditionsPartenariatView.accepter, qui les y enregistre) plutôt que stockées
    sur l'instance."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(options=[
        discord.SelectOption(label="Aucune mention", description="Aucune mention sur ton serveur", emoji="🚫"),
        discord.SelectOption(label="Mention \"Here\"", description="Mention here sur ton serveur", emoji="🧑‍🧒"),
        discord.SelectOption(label="Mention \"Partenariat\"", description="Mention partenariat sur ton serveur", emoji="🧑‍🧑‍🧒"),
        discord.SelectOption(label="Mention \"Everyone\"", description="Mention everyone sur ton serveur", emoji="🧑‍🧑‍🧒‍🧒")
    ], custom_id="partenariat:mention")
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        mention = select.values[0]
        channel = interaction.message.channel

        description = pub = None
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT partenariat_description, partenariat_pub FROM ticket WHERE thread_id = %s",
                        (channel.id,)
                    )
                    row = await c.fetchone()
            if row is not None:
                description, pub = row
        except aiomysql.Error as e:
            logger.critical(f"[tickets:mention] Erreur DB : {e}", exc_info=True)

        await interaction.response.send_message(f"Mention choisie : {mention}")
        embed = discord.Embed(title="Informations collectées !",
                              description="Toutes les informations de ton serveur ont été récupérées. S'il en manque, le staff te les demandera.")
        embed.add_field(name="Description du serveur", value=description or "Non renseignée", inline=False)
        embed.add_field(name="Publicité", value=pub or "Non renseignée", inline=False)
        embed.add_field(name="Mention souhaitée", value=mention, inline=False)
        embed.add_field(name="Tu pourrais te demander : je fais quoi maintenant ?",
                        value="Tu attends que le staff traite ta demande. Reste toujours disponible pour aller le plus vite. En attendant, je t'envoie la pub de Pixel Party.", inline=False)
        await channel.send(embed=embed)
        await channel.send("# **🎮 Pixel Party | Serveur Multigaming Fun & Actif !** \n ## **Tu cherches un endroit pour jouer, discuter et rigoler ? Rejoins Pixel Party !** \n 🔥 Jeux populaires : Fortnite • Brawl Stars • Minecraft • Roblox \n 🎉 Événements : cache-cache, défilés de mode, défis d’armes, tournois… \n 🏅 Rôles spéciaux à débloquer : VIP, Nintendo, PS5, etc. \n 🗨️ Une vraie communauté chill pour se faire des potes \n 💬 Que tu sois joueur Switch, PC, mobile ou console… t’es le/la bienvenu(e) ! \n 🔗 Rejoins-nous maintenant en cliquant [ici](https://discord.gg/cnWz7fXAex)")


class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(Tickets(bot))
