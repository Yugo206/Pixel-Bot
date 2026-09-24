import aiomysql
import logging
import re
import time
import discord
from discord.ext import commands
import asyncio
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from cogs.warn import ContestationView, JOURS_EXPIRATION
from utils.database import compter_warns, connexion, increment_warn
from utils.sanctions import apply_warn_sanction, get_modo_channel
from utils.config import get_config
from utils.transcript import archiver_ticket
from utils.autorisations import (
    MSG_DEJA_TRAITE, MSG_ERREUR, est_staff, refuser, repondre, verifier_mp, verifier_serveur,
    verifier_staff,
)
from utils.views import Modale, VuePersistante, copie_vue, liberer_clic, reserver_clic

logger = logging.getLogger(__name__)

# Membres dont un ticket est en train d'être créé (voir TicketCreateView) : deux
# choix rapprochés dans le menu ne créent pas deux tickets.
_CREATION_EN_COURS: set[int] = set()

# Footer du MP d'avis envoyé à la fermeture (voir FermerView) : relie l'avis du
# membre à son ticket dans le message transmis au staff.
_TICKET_AVIS_RE = re.compile(r"^Ticket : (.+)$")


def _owner_mention() -> str:
    # Lu à chaque appel (et non mis en cache dans une constante de module) : ce
    # fichier est importé directement par start.py (pour demander_confirmation_
    # moderateur), donc avant load_config() dans main() — une lecture au niveau
    # module figerait cette valeur à None pour toute la durée du process.
    owner_id = get_config("OWNER_ID")
    return f"<@{owner_id}>" if owner_id else "un administrateur"


def _tronquer(texte: str | None, limite: int = 1024) -> str | None:
    """Texte coupé à `limite` caractères (champ d'embed : 1024 au plus)."""
    if texte and len(texte) > limite:
        return texte[:limite - 1] + "…"
    return texte


def _ticket_de_l_avis(message: discord.Message | None) -> str:
    """« sur le ticket `ticket-bob` » d'après le footer du MP d'avis, ou "" pour
    un ancien MP sans ce footer."""
    if message is None or not message.embeds or not message.embeds[0].footer.text:
        return ""
    match = _TICKET_AVIS_RE.match(message.embeds[0].footer.text)
    return f" sur le ticket `{match.group(1)}`" if match else ""


async def _verifier_auteur_ticket(interaction: discord.Interaction) -> bool:
    """Réservé à l'auteur du ticket dans lequel se trouve le bouton (flux
    partenariat) : un modérateur ou un membre invité dans le thread ne doit pas
    pouvoir remplir la demande à sa place."""
    if not await verifier_serveur(interaction):
        return False
    try:
        async with connexion() as conn:
            async with conn.cursor() as c:
                await c.execute("SELECT membre_id FROM ticket WHERE thread_id = %s", (interaction.channel_id,))
                row = await c.fetchone()
    except aiomysql.Error as e:
        logger.error(f"[tickets] Erreur DB en vérifiant l'auteur du ticket {interaction.channel_id} : {e}")
        return await refuser(interaction, MSG_ERREUR)
    if row is None:
        return await refuser(interaction, "❌ Ce ticket est introuvable (fermé ou supprimé).")
    if row[0] != interaction.user.id:
        return await refuser(interaction, "❌ Seul l'auteur du ticket peut remplir cette demande.")
    return True


class AvisModal(Modale, title="Ton avis"):
    avis = discord.ui.TextInput(
        label="Laisse ton avis",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500
    )

    def __init__(self, message: discord.Message):
        super().__init__()
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        # Un seul avis détaillé par ticket, même si le formulaire a été ouvert
        # deux fois (deux onglets, double clic) avant l'envoi.
        if not reserver_clic(interaction, "ticket:explique"):
            await repondre(interaction, "⚠️ Tu as déjà donné ton avis sur ce ticket, merci !")
            return

        channel = await get_modo_channel(interaction.client)
        if channel is None:
            liberer_clic(interaction, "ticket:explique")
            logger.error("[tickets:avis] Salon de modération (CHANNEL_MODO_ID) introuvable.")
            await repondre(interaction, "❌ Ton avis n'a pas pu être transmis au staff, réessaie plus tard.")
            return

        try:
            await channel.send(
                f"Avis de {interaction.user.mention}{_ticket_de_l_avis(self.message)} :\n{self.avis.value}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            liberer_clic(interaction, "ticket:explique")
            raise

        await interaction.response.edit_message(
            view=copie_vue(AvisView, interaction.message or self.message, {"ticket:explique"})
        )
        await interaction.followup.send("Merci pour ton avis !", ephemeral=True)


class AvisView(VuePersistante):
    """Sondage envoyé en MP à l'auteur d'un ticket à sa fermeture (voir
    FermerView). Uniquement utilisable en message privé : seul le destinataire
    du MP y a accès."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_mp(interaction)

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
        if not reserver_clic(interaction):
            await repondre(interaction, "⚠️ Tu as déjà donné ta note pour ce ticket, merci !")
            return

        advisor = await get_modo_channel(interaction.client)
        if advisor is None:
            liberer_clic(interaction)
            logger.error("[tickets:avis] Salon de modération (CHANNEL_MODO_ID) introuvable.")
            await repondre(interaction, "❌ Ton avis n'a pas pu être transmis au staff, réessaie plus tard.")
            return

        try:
            await advisor.send(
                f"Avis de {interaction.user.mention}{_ticket_de_l_avis(interaction.message)} : {select.values[0]}",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            liberer_clic(interaction)
            raise

        await interaction.response.edit_message(view=copie_vue(AvisView, interaction.message, {"ticket:select"}))
        await interaction.followup.send("Merci pour ton avis !", ephemeral=True)

    @discord.ui.button(
        label="Explique-nous !",
        style=discord.ButtonStyle.blurple,
        custom_id="ticket:explique"
    )
    async def explique(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AvisModal(interaction.message))


class ModoView(VuePersistante):
    """Bouton « Prendre en charge » du message posté dans le salon modération à
    l'ouverture d'un ticket. Réservé au staff."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_staff(interaction, "❌ Seul le staff peut prendre un ticket en charge.")

    @discord.ui.button(label="Prendre en charge", style=discord.ButtonStyle.blurple, custom_id="ticket:prendre")
    async def prendre(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        vue_desactivee = copie_vue(ModoView, interaction.message, None)
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT thread_id, membre_id, message_ticket_id, modo_id, statut FROM ticket WHERE modo_message_id = %s",
                        (interaction.message.id,)
                    )
                    result = await c.fetchone()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:prendre] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send(f"❌ Erreur de base de données, contacte {_owner_mention()} pour résoudre le problème.", ephemeral=True)
            return

        if result is None:
            await interaction.followup.send("❌ Ce ticket n'existe plus (fermé et supprimé).", ephemeral=True)
            await interaction.edit_original_response(view=vue_desactivee)
            return

        thread_id, membre_id, message_ticket_id, modo_id, statut = result
        if membre_id == interaction.user.id:
            await interaction.followup.send("❌ Tu ne peux pas prendre en charge ton propre ticket.", ephemeral=True)
            return
        if statut == 3:
            await interaction.followup.send("❌ Ce ticket est déjà fermé.", ephemeral=True)
            await interaction.edit_original_response(view=vue_desactivee)
            return
        if modo_id is not None:
            await interaction.followup.send(f"⚠️ Ce ticket est déjà pris en charge par <@{modo_id}>.", ephemeral=True)
            await interaction.edit_original_response(view=vue_desactivee)
            return

        # Réservation atomique (UPDATE conditionné + rowcount) AVANT toute action
        # Discord : deux modérateurs qui cliquent en même temps ne peuvent pas
        # prendre tous les deux le ticket (le second est prévenu, et n'est ni
        # ajouté au thread ni affiché comme modérateur du ticket).
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET modo_id = %s, statut = 2 "
                        "WHERE modo_message_id = %s AND modo_id IS NULL AND statut = 1",
                        (interaction.user.id, interaction.message.id)
                    )
                    gagne = c.rowcount == 1
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:prendre] Erreur DB update : {e}", exc_info=True)
            await interaction.followup.send(f"❌ Erreur de base de données, contacte {_owner_mention()} pour résoudre le problème.", ephemeral=True)
            return

        if not gagne:
            await interaction.followup.send("⚠️ Ce ticket vient d'être pris en charge par un autre modérateur.", ephemeral=True)
            await interaction.edit_original_response(view=vue_desactivee)
            return

        try:
            thread = interaction.guild.get_thread(thread_id) or await interaction.guild.fetch_channel(thread_id)
        except discord.NotFound:
            await interaction.followup.send("❌ Le ticket n'existe plus sur Discord.", ephemeral=True)
            await interaction.edit_original_response(view=vue_desactivee)
            return

        # Mention supprimée aussitôt : ajoute le modérateur au thread privé.
        try:
            messs = await thread.send(f"{interaction.user.mention}")
            await messs.delete()
        except discord.HTTPException as e:
            logger.warning(f"[tickets:prendre] Impossible d'ajouter {interaction.user.id} au ticket {thread_id} : {e}")

        if message_ticket_id is not None:
            try:
                message_ticket = await thread.fetch_message(message_ticket_id)
                if message_ticket.embeds and len(message_ticket.embeds[0].fields) >= 5:
                    embed = message_ticket.embeds[0]
                    embed.set_field_at(2, name="Modérateur : ", value=interaction.user.mention)
                    embed.set_field_at(4, name="Statut", value="Actif")
                    await message_ticket.edit(embed=embed)
            except discord.HTTPException as e:
                logger.warning(f"[tickets:prendre] Message d'accueil du ticket {thread_id} non mis à jour : {e}")

        kwargs = {"view": vue_desactivee}
        if interaction.message.embeds:
            embed_modo = interaction.message.embeds[0].copy()
            embed_modo.color = discord.Color.green()
            embed_modo.add_field(name="Pris en charge par", value=interaction.user.mention, inline=False)
            kwargs["embed"] = embed_modo
        await interaction.edit_original_response(**kwargs)
        await interaction.followup.send(f"Tu as pris le ticket. Le lien est ici : {thread.mention}.", ephemeral=True)


class SatisfactionView(VuePersistante):
    """Proposée en éphémère au modérateur qui ferme un ticket (voir FermerView) :
    un retour négatif pose un avertissement à l'auteur du ticket."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_staff(interaction)

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
        # Un seul retour par fermeture : sans ça, deux choix rapprochés
        # posaient deux avertissements au membre.
        if not reserver_clic(interaction):
            await repondre(interaction, MSG_DEJA_TRAITE)
            return

        # On defer immédiatement : la suite (DB, timeout/ban, DM) peut dépasser les 3s
        # accordées par Discord pour répondre à l'interaction.
        await interaction.response.defer()

        selected_value = select.values[0]
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT membre_id FROM ticket WHERE thread_id = %s",
                              (interaction.channel_id,))
                    rpw = await c.fetchone()
        except aiomysql.Error as e:
            liberer_clic(interaction)
            logger.critical(f"[tickets:avis] Erreur SQL : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        if rpw is None:
            await interaction.followup.send("❌ Impossible de retrouver ce ticket en base de données.", ephemeral=True)
            return
        if rpw[0] == interaction.user.id:
            await self._desactiver(interaction, "Ticket fermé.")
            return

        # Cas positif : rien à faire sauf désactiver le select
        if selected_value == "Super bien !":
            await self._desactiver(interaction, "Merci pour ton retour !")
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

        # Cas négatifs : "Mal" ou "Pas de reponse"
        warn_id = None

        try:
            iso_time = datetime.now(timezone.utc).isoformat()

            async with connexion() as conn:
                # Incrément atomique (voir utils/database.py) : évite que ce warn et
                # un /warn (ou une autre satisfaction de ticket) posés au même
                # moment sur ce membre ne s'écrasent l'un l'autre. Fait dans la même
                # transaction que l'INSERT INTO warns ci-dessous (un seul commit) :
                # si l'un des deux échoue, l'autre est annulé plutôt que de
                # désynchroniser le compteur de l'historique des warns.
                await increment_warn(conn, membre.id)

                async with conn.cursor() as c:
                    await c.execute(
                        "INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso) VALUES (%s, %s, %s, %s, %s)",
                        (membre.id, interaction.user.id, "Non respect des conditions d'ouverture de ticket",
                         int(time.time()), iso_time)
                    )

                    warn_id = c.lastrowid

                # Palier de sanction d'après les warns réellement en cours (voir
                # compter_warns), celui qu'on vient d'ajouter compris.
                warn_count = await compter_warns(conn, membre.id)
                await conn.commit()

        except aiomysql.Error as e:
            liberer_clic(interaction)
            logger.critical(f"[tickets:avis] Erreur SQL : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        # MP envoyé AVANT la sanction (comme /warn, cogs/warn.py) : un ban retire
        # tout serveur en commun, et ce MP ne pourrait plus partir ensuite.
        embed = self._create_warn_embed(selected_value, interaction.user, warn_id)
        mp_envoye = True
        try:
            await membre.send(embed=embed, view=copie_vue(ContestationView, None, set()))
        except discord.HTTPException:
            mp_envoye = False
            logger.warning(f"Impossible d'envoyer un DM à {membre}")

        # Appliquer les sanctions selon le nombre de warns
        channel = await get_modo_channel(bot, interaction.guild)
        await apply_warn_sanction(interaction.guild, membre, channel, warn_count)

        resultat = f"⚠️ {membre.mention} a reçu un avertissement (#{warn_id}, {warn_count} en cours)."
        if not mp_envoye:
            resultat += "\nSes messages privés sont fermés : il n'a pas été prévenu."
        await self._desactiver(interaction, resultat)

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

        # Même mention que le MP de /warn (cogs/warn.py) : ce warn expire aussi
        # automatiquement (check_warn_expirations).
        embed.description += f"\n⌛ Il expirera au plus tôt dans {JOURS_EXPIRATION} jours."
        embed.add_field(name="C'est une erreur ?", value="Clique sur le bouton ci-dessous pour contester cet avertissement")
        # Footer parsé par ContestationView (cogs/warn.py) pour retrouver le warn
        # concerné sans avoir besoin de le stocker sur l'instance de la vue.
        embed.set_footer(text=f"ID du warn : {warn_id}")
        return embed

    async def _desactiver(self, interaction: discord.Interaction, contenu: str):
        """Désactive le select sur le message d'origine (déjà deferred) et y
        affiche le résultat du choix."""
        try:
            await interaction.edit_original_response(
                content=contenu, view=copie_vue(SatisfactionView, interaction.message, None)
            )
        except discord.HTTPException as e:
            logger.error(f"Erreur lors de la désactivation : {e}")


class ConfirmationClotureView(VuePersistante):
    """Envoyée en MP au modérateur assigné quand un ticket est fermé automatiquement
    pour inactivité (voir ticket_watcher dans start.py). Contrairement à
    SatisfactionView (déclenchée par un clic sur "Fermer le ticket"), il n'y a ici
    aucune interaction d'origine à qui répondre ephemeral : le lien vers le ticket
    concerné passe par `ticket.mod_dm_message_id` plutôt que par le salon."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_mp(interaction)

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
        message_id = interaction.message.id

        # Réservation atomique : la réponse n'est prise en compte qu'une fois
        # (double choix, ou select resté actif après un redémarrage), et
        # uniquement par le modérateur assigné au ticket. Lecture et réservation
        # dans deux transactions, comme ModoView : dans la même, un second choix
        # simultané lisait la ligne avant la réservation du premier, et son
        # UPDATE échouait alors en erreur 1020 (« Record has changed since last
        # read », MariaDB 11.6+) au lieu de simplement ne rien réserver.
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT thread_id, modo_id FROM ticket WHERE mod_dm_message_id = %s",
                        (message_id,)
                    )
                    row = await c.fetchone()
            gagne = False
            if row is not None and row[1] == interaction.user.id:
                async with connexion() as conn:
                    async with conn.cursor() as c:
                        await c.execute(
                            "UPDATE ticket SET mod_dm_message_id = NULL WHERE mod_dm_message_id = %s AND modo_id = %s",
                            (message_id, interaction.user.id)
                        )
                        gagne = c.rowcount == 1
                    await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:confirmation_cloture] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Erreur de base de données, réessaie dans un instant.", ephemeral=True)
            return

        if row is not None and row[1] != interaction.user.id:
            await interaction.followup.send("❌ Tu n'es pas le modérateur de ce ticket.", ephemeral=True)
            return

        vue_desactivee = copie_vue(ConfirmationClotureView, interaction.message, None)
        if not gagne:
            await interaction.followup.send(
                "❌ Ce ticket n'est plus associé à ce message (déjà traité, ou supprimé).",
                ephemeral=True
            )
            await interaction.edit_original_response(view=vue_desactivee)
            return

        thread_id = row[0]

        # Cas positif : rien à rouvrir, on s'arrête là.
        if select.values[0] == "Super bien !":
            await interaction.edit_original_response(view=vue_desactivee)
            await interaction.followup.send("Merci pour ton retour !", ephemeral=True)
            return

        # Cas négatifs ("Mal" / "Pas de reponse") : le ticket est rouvert pour que
        # le modérateur reprenne la main dessus.
        try:
            thread = interaction.client.get_channel(thread_id) or await interaction.client.fetch_channel(thread_id)
            await thread.edit(archived=False, locked=False)
        except discord.NotFound:
            await interaction.edit_original_response(view=vue_desactivee)
            await interaction.followup.send(
                "❌ Le ticket a été supprimé entre-temps, impossible de le rouvrir.",
                ephemeral=True
            )
            return
        except discord.HTTPException as e:
            # Réservation rendue : le modérateur peut réessayer.
            logger.warning(f"[tickets:confirmation_cloture] Réouverture du ticket {thread_id} impossible : {e}")
            try:
                async with connexion() as conn:
                    async with conn.cursor() as c:
                        await c.execute(
                            "UPDATE ticket SET mod_dm_message_id = %s WHERE thread_id = %s",
                            (message_id, thread_id)
                        )
                    await conn.commit()
            except aiomysql.Error:
                pass
            await interaction.followup.send("❌ Impossible de rouvrir le ticket pour le moment, réessaie.", ephemeral=True)
            return

        await interaction.edit_original_response(view=vue_desactivee)

        try:
            # last_message et warn_12h sont remis à zéro comme si le membre venait de
            # répondre (voir on_message dans cogs/events.py) : sinon ticket_watcher
            # relit l'ancien timestamp d'inactivité au prochain passage (120s) et
            # referme aussitôt le ticket qu'on vient de rouvrir.
            now_ts = int(time.time())
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET statut = 2, closed_at = NULL, closed_by = NULL, last_message = %s, warn_12h = NULL WHERE thread_id = %s",
                        (now_ts, thread_id)
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

        # On rattache un FermerView tout neuf au message de réouverture : l'ancien
        # message "Gestionnaire de ticket" a déjà son bouton désactivé (voir
        # FermerView.create) et son id n'est pas conservé en base, donc sans ça le
        # ticket rouvert n'a plus aucun moyen de le refermer depuis Discord.
        try:
            await thread.send(
                f"🔓 Ce ticket a été rouvert par {interaction.user.mention} suite à la fermeture automatique pour inactivité.",
                view=copie_vue(FermerView, None, set())
            )
        except discord.HTTPException as e:
            logger.warning(f"[tickets:confirmation_cloture] Message de réouverture non envoyé ({thread_id}) : {e}")

        await interaction.followup.send(f"Ticket rouvert : {thread.mention}", ephemeral=True)


async def demander_confirmation_moderateur(bot, thread: discord.Thread, modo_id: int):
    """MP le modérateur assigné à un ticket fermé automatiquement pour inactivité,
    pour lui demander si tout s'est bien passé (et lui permettre de rouvrir le
    ticket sinon). Appelé par ticket_watcher (start.py) après la fermeture."""
    try:
        modo = bot.get_user(modo_id) or await bot.fetch_user(modo_id)
    except discord.HTTPException:
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
        message = await modo.send(embed=embed, view=copie_vue(ConfirmationClotureView, None, set()))
    except discord.HTTPException:
        logger.warning(f"[tickets:confirmation] MP impossible à {modo_id} (ticket {thread.id}) : DMs fermés.")
        return

    try:
        async with connexion() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "UPDATE ticket SET mod_dm_message_id = %s WHERE thread_id = %s",
                    (message.id, thread.id)
                )
            await conn.commit()
    except aiomysql.Error as e:
        logger.critical(f"[tickets:confirmation] Erreur DB : {e}", exc_info=True)


class FermerView(VuePersistante):
    """Bouton « Fermer le ticket ». Réservé à l'auteur du ticket et au staff
    (vérifié dans le callback, qui a besoin de la ligne `ticket`) : un membre
    invité dans le thread ne peut pas le fermer."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_serveur(interaction)

    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        thread = interaction.channel
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT raison, membre_id FROM ticket WHERE thread_id = %s",
                              (thread.id,))
                    content = await c.fetchone()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:fermer] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Erreur de base de données, réessaie dans un instant.", ephemeral=True)
            return

        if content is None:
            await interaction.followup.send(
                f"❌ Ce ticket est introuvable en base de données, contacte {_owner_mention()}.", ephemeral=True
            )
            return

        raison, membre_id = content
        staff = est_staff(interaction.user)
        if interaction.user.id != membre_id and not staff:
            await interaction.followup.send("❌ Seul l'auteur du ticket ou le staff peut le fermer.", ephemeral=True)
            return

        # Réservation atomique de la fermeture AVANT toute action Discord : un
        # double clic (ou le membre et un modérateur en même temps) ne ferme le
        # ticket qu'une fois — un seul MP d'avis, un seul message de fermeture,
        # une seule archive.
        closed_at = int(time.time())
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "UPDATE ticket SET statut = 3, closed_at = %s, closed_by = %s WHERE thread_id = %s AND statut != 3",
                        (closed_at, interaction.user.id, thread.id)
                    )
                    gagne = c.rowcount == 1
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:fermer] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Erreur de base de données, le ticket n'a pas été fermé. Réessaie.", ephemeral=True)
            return

        vue_desactivee = copie_vue(FermerView, interaction.message, None)
        if not gagne:
            await interaction.followup.send("⚠️ Ce ticket est déjà fermé.", ephemeral=True)
            try:
                await interaction.edit_original_response(view=vue_desactivee)
            except discord.HTTPException:
                pass
            return

        # Retour sur le déroulé du ticket : seulement pour un membre du staff qui
        # ferme le ticket d'un autre (il peut alors avertir le membre).
        if staff and interaction.user.id != membre_id:
            await interaction.followup.send(
                "Comment s'est passé ce ticket ?", view=copie_vue(SatisfactionView, None, set()), ephemeral=True
            )
        else:
            await interaction.followup.send("Ticket fermé avec succès", ephemeral=True)

        embed = discord.Embed(title="Ticket fermé", description="Ce ticket est fermé. Tu ne peux plus écrire dedans.")
        embed.add_field(name="Fermé par :", value=interaction.user.mention)
        embed.add_field(name="Raison initiale du ticket : ", value=_tronquer(raison) or "Non précisée")
        try:
            await interaction.edit_original_response(embed=embed, view=vue_desactivee)
        except discord.HTTPException as e:
            logger.warning(f"[tickets:fermer] Message du ticket {thread.id} non mis à jour : {e}")

        embed2 = discord.Embed(title="Donne-nous ton avis sur ton ticket !",
                               description="Afin d'améliorer le système de ticket et l'efficacité du staff, nous aimerions recueillir ton avis sur ce ticket.")
        embed2.set_footer(text=f"Ticket : {thread.name}")
        bot = interaction.client
        try:
            membre = bot.get_user(membre_id) or await bot.fetch_user(membre_id)
            await membre.send(embed=embed2, view=copie_vue(AvisView, None, set()))
        except discord.HTTPException:
            # Compte supprimé ou MP fermés : pas d'avis, la fermeture continue.
            pass

        ts = int((datetime.now(timezone.utc) + timedelta(seconds=86400)).timestamp())
        try:
            await thread.send(f"Ce ticket a été fermé par {interaction.user.mention}. Il sera supprimé <t:{ts}:R>")
            await thread.edit(locked=True, archived=True)
        except discord.HTTPException as e:
            logger.warning(f"[tickets:fermer] Verrouillage du ticket {thread.id} impossible : {e}")

        # Transcription HTML + métadonnées pour /archive et /stats-staff, avec le
        # même horodatage de fermeture que la table `ticket`. En dernier : le
        # message de fermeture ci-dessus fait partie de l'archive, et une
        # génération lente (pièces jointes à intégrer) ne retarde rien d'autre.
        # Ne lève jamais (échecs loggés dans utils/transcript.py).
        await archiver_ticket(bot, thread, closed_by=interaction.user.id, closed_at=closed_at)


class TicketCreateView(VuePersistante):
    """Menu public d'ouverture de ticket (posté via /creer-message)."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_serveur(interaction)

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
        # Réponse = réédition du menu (choix remis à zéro, voir TimedView dans
        # utils/views.py), la suite passe par des messages éphémères.
        await interaction.response.edit_message(view=copie_vue(TicketCreateView, None, set()))

        user = interaction.user
        if user.id in _CREATION_EN_COURS:
            await interaction.followup.send("⏳ Ton ticket est déjà en cours de création.", ephemeral=True)
            return
        _CREATION_EN_COURS.add(user.id)
        try:
            await self._creer_ticket(interaction, select.values[0])
        finally:
            _CREATION_EN_COURS.discard(user.id)

    async def _creer_ticket(self, interaction: discord.Interaction, raison: str):
        user = interaction.user

        # Un seul ticket ouvert à la fois par membre : évite les doublons (spam du
        # menu) et renvoie vers le ticket existant.
        try:
            async with connexion() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT thread_id FROM ticket WHERE membre_id = %s AND statut != 3 LIMIT 1", (user.id,)
                    )
                    existant = await cur.fetchone()
        except aiomysql.Error as e:
            logger.critical(f"[tickets:create] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Erreur de base de données, réessaie dans un instant.", ephemeral=True)
            return
        if existant is not None:
            await interaction.followup.send(
                f"❌ Tu as déjà un ticket ouvert : <#{existant[0]}>. Ferme-le avant d'en ouvrir un nouveau.",
                ephemeral=True
            )
            return

        # Tickets = fils privés du salon du menu : impossible depuis un fil ou un
        # forum (voir aussi /creer-message, qui refuse d'y poster ce menu).
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.followup.send(
                "❌ Impossible d'ouvrir un ticket depuis ce salon, préviens un modérateur.", ephemeral=True
            )
            return

        try:
            thread = await interaction.channel.create_thread(
                name=f"ticket-{user.name}",
                invitable=True
            )
        except discord.HTTPException as e:
            logger.error(f"[tickets:create] Création du thread impossible : {e}")
            await interaction.followup.send(
                "❌ Impossible de créer ton ticket pour le moment, préviens un modérateur.", ephemeral=True
            )
            return

        messsages = None
        try:
            messs = await thread.send(user.mention)
            await messs.delete()
            embed = discord.Embed(title="Gestionnaire de ticket", description=f"Bienvenue {user.display_name} sur ton ticket !", colour=discord.Colour.blue())
            embed.add_field(name="Fermer le ticket", value="Tu peux fermer ton ticket à tout moment en cliquant sur ce bouton", inline=False)
            embed.add_field(name="Raison du ticket : ", value=raison)
            embed.add_field(name="Modérateur :", value="Personne")
            embed.add_field(name="Demandé par :", value=user.mention)
            embed.add_field(name="Statut : ", value="En attente d'un modérateur")
            message = await thread.send(
                f"Bienvenue {user.mention} sur ton ticket", embed=embed, view=copie_vue(FermerView, None, set())
            )

            channel = await get_modo_channel(interaction.client, interaction.guild)
            if channel is None:
                logger.warning("Aucun salon de modération trouvé (CHANNEL_MODO_ID non configuré ou introuvable).")
            else:
                embed2 = discord.Embed(title="Ticket ouvert !", description="Clique sur le bouton ci-dessous pour accéder au ticket et le prendre en charge.", colour=discord.Colour.blue())
                embed2.add_field(name="Membre", value=user.mention)
                embed2.add_field(name="Raison", value=raison)
                messsages = await channel.send(embed=embed2, view=copie_vue(ModoView, None, set()))

            # last_message = ouverture : un ticket où le membre n'écrit jamais est
            # relancé puis fermé pour inactivité comme les autres (voir
            # ticket_watcher dans start.py), au lieu de rester ouvert à vie.
            async with connexion() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "INSERT INTO ticket (thread_id, membre_id, statut, raison, modo_message_id, message_ticket_id, last_message) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                        (thread.id, user.id, 1, raison, messsages.id if messsages else None, message.id, int(time.time()))
                    )
                await conn.commit()
        except Exception as e:
            # Ticket inutilisable sans sa ligne en base (ni fermeture, ni prise en
            # charge possibles) : on retire le thread et le message du staff
            # plutôt que de laisser un ticket fantôme.
            logger.critical(f"[tickets:create] Erreur à la création du ticket de {user.id} : {e}", exc_info=True)
            for a_supprimer in (thread, messsages):
                if a_supprimer is not None:
                    try:
                        await a_supprimer.delete()
                    except discord.HTTPException:
                        pass
            await interaction.followup.send("❌ Ton ticket n'a pas pu être créé, réessaie dans un instant.", ephemeral=True)
            return

        await interaction.followup.send(f"Ticket créé avec succès dans {thread.mention}", ephemeral=True)

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
            try:
                await thread.send(embed=embed_partenariat_intro, view=copie_vue(PartenariatCommencerView, None, set()))
            except discord.HTTPException as e:
                logger.warning(f"[tickets:create] Introduction partenariat non envoyée ({thread.id}) : {e}")


class PartenariatCommencerView(VuePersistante):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _verifier_auteur_ticket(interaction)

    @discord.ui.button(label="Démarrer", style=discord.ButtonStyle.green, custom_id="Partenariat:Commencer")
    async def demarrer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not reserver_clic(interaction):
            await repondre(interaction, MSG_DEJA_TRAITE)
            return

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
        await interaction.response.edit_message(view=copie_vue(PartenariatCommencerView, interaction.message, None))
        await interaction.followup.send(embed=embed, view=copie_vue(ConditionsPartenariatView, None, set()))


class ConditionsPartenariatView(VuePersistante):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _verifier_auteur_ticket(interaction)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="partenariat:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not reserver_clic(interaction):
            await repondre(interaction, MSG_DEJA_TRAITE)
            return

        bot = interaction.client
        thread = interaction.channel

        await interaction.response.edit_message(view=copie_vue(ConditionsPartenariatView, interaction.message, None))
        await thread.send(
            embed=discord.Embed(
                title="Description de ton serveur",
                description="Envoie une description de ton serveur.",
                colour=discord.Colour.blurple()
            )
        )

        def check(m):
            return m.author.id == interaction.user.id and m.channel.id == thread.id

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
            async with connexion() as conn:
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
            view=copie_vue(MentionPartenariatView, None, set())
        )


class MentionPartenariatView(VuePersistante):
    """Vue persistante et sans état : la description/pub collectées plus tôt dans
    le flux sont retrouvées dans `ticket` via le thread (voir
    ConditionsPartenariatView.accepter, qui les y enregistre) plutôt que stockées
    sur l'instance."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await _verifier_auteur_ticket(interaction)

    @discord.ui.select(options=[
        discord.SelectOption(label="Aucune mention", description="Aucune mention sur ton serveur", emoji="🚫"),
        discord.SelectOption(label="Mention \"Here\"", description="Mention here sur ton serveur", emoji="🧑‍🧒"),
        discord.SelectOption(label="Mention \"Partenariat\"", description="Mention partenariat sur ton serveur", emoji="🧑‍🧑‍🧒"),
        discord.SelectOption(label="Mention \"Everyone\"", description="Mention everyone sur ton serveur", emoji="🧑‍🧑‍🧒‍🧒")
    ], custom_id="partenariat:mention")
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Select à usage unique (le choix déclenche la suite du flux
        # partenariat) : désactivé plutôt que réinitialisé, et réservé, pour
        # éviter un second envoi de la pub du serveur.
        if not reserver_clic(interaction):
            await repondre(interaction, MSG_DEJA_TRAITE)
            return

        mention = select.values[0]
        channel = interaction.channel
        await interaction.response.edit_message(view=copie_vue(MentionPartenariatView, interaction.message, None))

        description = pub = None
        try:
            async with connexion() as conn:
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

        await interaction.followup.send(f"Mention choisie : {mention}")
        embed = discord.Embed(title="Informations collectées !",
                              description="Toutes les informations de ton serveur ont été récupérées. S'il en manque, le staff te les demandera.")
        # Tronquées pour l'affichage (1024 caractères par champ d'embed) : un
        # message Discord peut en faire jusqu'à 4000, et l'embed entier n'était
        # alors jamais envoyé. Le texte complet reste dans le ticket.
        embed.add_field(name="Description du serveur", value=_tronquer(description) or "Non renseignée", inline=False)
        embed.add_field(name="Publicité", value=_tronquer(pub) or "Non renseignée", inline=False)
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
