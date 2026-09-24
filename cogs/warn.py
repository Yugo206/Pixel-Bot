import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiomysql
import logging
import re
from datetime import datetime, timezone
import time
from dotenv import load_dotenv
load_dotenv()
from utils.database import compter_warns, connexion, increment_warn, decrement_warn
from utils.sanctions import apply_warn_sanction, get_modo_channel
from utils.config import get_config
from utils.autorisations import refuser, repondre, verifier_mp, verifier_staff
from utils.staff import est_owner
from utils.views import Modale, VuePersistante, copie_vue, liberer_clic, reserver_clic

logger = logging.getLogger(__name__)

MSG_DEJA_TRAITE_CONTESTATION = "⚠️ Cette contestation a déjà été traitée par un autre modérateur."

_WARN_ID_RE = re.compile(r"ID du warn\s*:\s*(\d+)")

# Expiration automatique des warns (dégressivité de la sanction) : le plus ancien
# warn d'un membre expire 30 jours après sa pose, et au plus un warn expire tous
# les 30 jours pour un même membre (voir _echeance_expiration et
# check_warn_expirations plus bas). Ex. : 3 warns le même jour J sont retirés à
# J+30, J+60 et J+90 ; 3 warns vieux de 100 jours au premier démarrage du bot, à
# J (ce jour-là), J+30 et J+60.
WARN_EXPIRATION = 30 * 24 * 3600
JOURS_EXPIRATION = WARN_EXPIRATION // 86400


def _extract_warn_id(message: discord.Message) -> int | None:
    """Retrouve l'id du warn concerné depuis le footer de l'embed du message cliqué
    (voir ContestationView, rendue sans état pour rester persistante après un
    redémarrage — elle ne peut donc pas garder `warn` sur l'instance)."""
    if not message.embeds:
        return None
    footer_text = message.embeds[0].footer.text
    if not footer_text:
        return None
    match = _WARN_ID_RE.search(footer_text)
    return int(match.group(1)) if match else None


def _date_pose_warn(created_at, created_at_iso) -> int:
    """Date de pose d'un warn (epoch, secondes). created_at peut être NULL sur
    d'anciennes lignes : repli sur created_at_iso, sinon 0 — le warn est alors
    traité comme très ancien (donc expirable) plutôt que de rester bloqué pour
    toujours faute de date."""
    if created_at is not None:
        return int(created_at)
    if created_at_iso:
        try:
            dt = datetime.fromisoformat(created_at_iso)
        except ValueError:
            return 0
        # Le bot a toujours enregistré de l'UTC (datetime.now(timezone.utc)) : une
        # date sans fuseau est lue comme UTC, pas comme l'heure locale de la machine.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def _ordre_expiration(warn) -> tuple[int, int]:
    """Clé de tri d'un warn (id, created_at, created_at_iso, ...) du plus ancien au
    plus récent. Tri fait en Python plutôt qu'en SQL : un ORDER BY created_at
    placerait en premier un warn à created_at NULL même s'il est récent d'après
    created_at_iso, et il expirerait avant de plus anciens que lui."""
    return _date_pose_warn(warn[1], warn[2]), warn[0]


def _echeance_expiration(created_at, created_at_iso, derniere_expiration) -> int:
    """Date (epoch) d'expiration du plus ancien warn restant d'un membre : 30 jours
    après sa pose, et jamais moins de 30 jours après la précédente expiration
    automatique de ce membre (warn_expirations.derniere_expiration, None si aucune).

    Seul endroit où ce calcul est fait : la boucle d'expiration et /warns
    l'utilisent tous les deux, pour que la date affichée aux modérateurs soit
    toujours celle que la boucle appliquera."""
    return max(_date_pose_warn(created_at, created_at_iso), derniere_expiration or 0) + WARN_EXPIRATION


def _tronquer(texte: str | None, limite: int = 1024) -> str | None:
    """Texte coupé à `limite` caractères (champ d'embed : 1024 au plus)."""
    if texte and len(texte) > limite:
        return texte[:limite - 1] + "…"
    return texte


async def _marquer_contestation(message: discord.Message, statut: str | None = None,
                                couleur: discord.Color | None = None) -> None:
    """Désactive les boutons du message de contestation (salon modération) et y
    ajoute la décision prise, pour que le staff voie d'un coup d'œil qu'elle est
    traitée et par qui. Sans `statut`, désactive seulement les boutons (cas d'une
    contestation déjà traitée par quelqu'un d'autre : sa décision est déjà
    affichée)."""
    kwargs = {"view": copie_vue(RefuseroracceptercontestationView, message, None)}
    if statut and message.embeds:
        embed = message.embeds[0].copy()
        embed.add_field(name="Statut", value=statut, inline=False)
        if couleur is not None:
            embed.color = couleur
        kwargs["embed"] = embed
    try:
        await message.edit(**kwargs)
    except discord.HTTPException as e:
        logger.warning(f"[warn:contestation] Message {message.id} non mis à jour : {e}")


async def _mp_membre(bot, membre_id: int, embed: discord.Embed) -> bool:
    """MP best-effort au membre ; False s'il n'a pas pu être prévenu (MP fermés,
    plus aucun serveur en commun, compte supprimé)."""
    try:
        user = bot.get_user(membre_id) or await bot.fetch_user(membre_id)
        await user.send(embed=embed)
    except discord.HTTPException:
        return False
    return True


class RaisonrefuserModal(Modale, title="Raison du refus"):
    raison = discord.ui.TextInput(
        label="Raison du refus",
        placeholder="Je trouve que ce warn est mérité car ...",
        min_length=10,
        # 1000 : la raison s'affiche dans un champ d'embed (1024 au plus).
        max_length=1000,
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, message: discord.Message, membre_id: int):
        super().__init__()
        self.message = message
        self.membre_id = membre_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer()

        # Réservation atomique : si un autre modérateur a accepté (ou refusé)
        # la contestation pendant que ce formulaire était ouvert, ce refus
        # n'est pas envoyé au membre par-dessus sa décision.
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute("DELETE FROM contestations WHERE message_id = %s", (self.message.id,))
                    gagne = c.rowcount == 1
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[warn:refuser] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur de base de données est survenue, réessaie.", ephemeral=True)
            return

        if not gagne:
            await interaction.followup.send(MSG_DEJA_TRAITE_CONTESTATION, ephemeral=True)
            await _marquer_contestation(self.message)
            return

        embed = discord.Embed(
            title="Contestation refusée",
            description="Ta contestation a été refusée",
            color=discord.Color.red()
        )
        embed.add_field(name="Modérateur", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison", value=self.raison.value, inline=False)
        mp_envoye = await _mp_membre(interaction.client, self.membre_id, embed)

        await _marquer_contestation(
            self.message, f"❌ Refusée par {interaction.user.mention}", discord.Color.red()
        )
        message = "Refus envoyé au membre ❌"
        if not mp_envoye:
            message = "Refus enregistré ❌ (ses MP sont fermés : le membre n'a pas été prévenu)."
        await interaction.followup.send(message, ephemeral=True)


class RefuseroracceptercontestationView(VuePersistante):
    """Vue persistante et sans état : les infos de la contestation (membre, warn concerné)
    sont retrouvées dans la table `contestations` à partir de l'id du message cliqué,
    au lieu d'être stockées sur l'instance (ce qui casse dès qu'elle est enregistrée
    globalement via bot.add_view). Réservée au staff, et jamais sur sa propre
    contestation."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_staff(interaction, "❌ Seul le staff peut traiter une contestation.")

    async def _contestation(self, interaction: discord.Interaction, colonnes: str):
        """Ligne `contestations` du message cliqué, ou None après avoir répondu
        (introuvable, ou contestation du modérateur lui-même)."""
        async with connexion() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    f"SELECT {colonnes} FROM contestations WHERE message_id = %s",
                    (interaction.message.id,)
                )
                row = await cur.fetchone()

        if row is None:
            await interaction.response.edit_message(
                view=copie_vue(RefuseroracceptercontestationView, interaction.message, None)
            )
            await interaction.followup.send(MSG_DEJA_TRAITE_CONTESTATION, ephemeral=True)
            return None
        if row[0] == interaction.user.id:
            await refuser(interaction, "❌ Tu ne peux pas traiter ta propre contestation.")
            return None
        return row

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="warn:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            row = await self._contestation(interaction, "membre_id, warn_id")
            if row is None:
                return
            membre_id, warn_id = row

            # On defer avant de toucher la DB (et on n'édite le message qu'une fois
            # la suppression du warn effectivement commit) : si la DB échoue, le
            # modérateur voit une erreur et peut réessayer, au lieu de voir la
            # contestation comme traitée alors que le warn est toujours là.
            await interaction.response.defer()

            warn_retire = False
            async with connexion() as conn:
                # Réclame la contestation de façon atomique avant de toucher au
                # compteur : si un double clic (ou deux modérateurs) arrivent en
                # même temps, un seul des deux DELETE obtient rowcount == 1 et
                # décrémente réellement le warn — l'autre voit juste que c'est déjà
                # traité au lieu de décrémenter deux fois.
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM contestations WHERE message_id = %s", (interaction.message.id,))
                    gagne = cur.rowcount == 1

                # Même transaction que le DELETE ci-dessus (un seul commit) : si
                # le décrément ou la suppression du warn échoue, la ligne de
                # contestation qu'on vient de réclamer n'est pas perdue pour rien.
                if gagne and warn_id is None:
                    # Ancienne contestation, sans id de warn : impossible de savoir
                    # quelle ligne de `warns` retirer, on garde le comportement
                    # historique (compteur seul).
                    await decrement_warn(conn, membre_id)
                    warn_retire = True
                elif gagne:
                    # Le compteur n'est décrémenté que si le warn existait encore :
                    # il a pu expirer (check_warn_expirations) ou être retiré via
                    # /unwarn depuis le dépôt de la contestation — ces deux chemins
                    # ont déjà décrémenté le compteur, le refaire ici le
                    # décrémenterait deux fois pour un seul warn.
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "DELETE FROM warns WHERE id = %s AND user_id = %s",
                            (warn_id, membre_id)
                        )
                        warn_retire = cur.rowcount == 1

                    if warn_retire:
                        await decrement_warn(conn, membre_id)

                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[warn:accepter] Erreur DB : {e}", exc_info=True)
            await repondre(interaction, "❌ Une erreur de base de données est survenue, réessaie.")
            return

        if not gagne:
            await interaction.followup.send(MSG_DEJA_TRAITE_CONTESTATION, ephemeral=True)
            await _marquer_contestation(interaction.message)
            return

        await _marquer_contestation(
            interaction.message, f"✅ Acceptée par {interaction.user.mention}", discord.Color.green()
        )

        embed = discord.Embed(
            title="Contestation acceptée",
            description=(
                "Ton warn a été retiré"
                if warn_retire else
                "Ce warn avait déjà été retiré entre-temps (expiration automatique "
                "ou modérateur) : il ne compte plus dans tes avertissements."
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="Modérateur :", value=interaction.user.mention, inline=False)
        mp_envoye = await _mp_membre(interaction.client, membre_id, embed)

        if warn_retire:
            message = "Sanction retirée ✅"
        else:
            message = (
                "Contestation clôturée ✅ — ce warn n'existait plus (déjà expiré ou retiré "
                "via /unwarn), le compteur du membre n'a pas été modifié."
            )
        if not mp_envoye:
            message += "\n(Ses MP sont fermés : le membre n'a pas été prévenu.)"
        await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, custom_id="warn:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            row = await self._contestation(interaction, "membre_id")
        except aiomysql.Error as e:
            logger.critical(f"[warn:refuser] Erreur DB : {e}", exc_info=True)
            await repondre(interaction, "❌ Une erreur de base de données est survenue, réessaie.")
            return
        if row is None:
            return

        # Les boutons ne sont désactivés qu'à l'envoi du formulaire (voir
        # RaisonrefuserModal) : un formulaire fermé sans l'envoyer ne doit pas
        # rendre la contestation impossible à traiter. Un membre parti du
        # serveur peut aussi voir sa contestation refusée (le MP sera
        # simplement impossible), pour qu'elle ne reste pas en suspens.
        await interaction.response.send_modal(RaisonrefuserModal(interaction.message, row[0]))


async def _verifier_warn_contestable(user_id: int, warn_id: int | None) -> tuple[str | None, tuple | None]:
    """(message d'erreur ou None, ligne du warn ou None) pour une contestation du
    warn `warn_id` par `user_id` : le warn doit toujours exister, appartenir au
    membre, et ne pas avoir déjà une contestation en attente. Sans id (ancien MP
    sans footer), rien n'est vérifiable : on laisse passer."""
    if warn_id is None:
        return None, None
    async with connexion() as conn:
        async with conn.cursor() as c:
            await c.execute(
                "SELECT user_id, raison, created_at, created_at_iso FROM warns WHERE id = %s", (warn_id,)
            )
            warn = await c.fetchone()
            await c.execute("SELECT 1 FROM contestations WHERE warn_id = %s LIMIT 1", (warn_id,))
            en_attente = await c.fetchone() is not None
    if warn is None:
        return "ℹ️ Cet avertissement n'existe plus (expiré ou déjà retiré) : il n'y a plus rien à contester.", None
    if warn[0] != user_id:
        return "❌ Cet avertissement ne te concerne pas.", None
    if en_attente:
        return "⚠️ Tu as déjà contesté cet avertissement : le staff va te répondre en MP.", None
    return None, warn


class ContestationModal(Modale, title="Contestation"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Je trouve ce warn injuste car ...",
        min_length=100,
        # 1000 : la raison s'affiche dans un champ d'embed (1024 au plus).
        max_length=1000,
        label="Explique pourquoi tu trouves ce warn injuste",
        required=True
    )

    def __init__(self, message: discord.Message, warn_id: int | None):
        super().__init__()
        self.message = message
        self.warn_id = warn_id

    async def on_submit(self, interaction: discord.Interaction):
        # Une seule contestation par MP, même si le formulaire a été ouvert deux
        # fois avant l'envoi.
        if not reserver_clic(interaction, "contest"):
            await repondre(interaction, "⚠️ Tu as déjà contesté cet avertissement : le staff va te répondre en MP.")
            return
        await interaction.response.defer()
        try:
            envoyee = await self._envoyer(interaction)
        except Exception:
            liberer_clic(interaction, "contest")
            raise
        if not envoyee:
            liberer_clic(interaction, "contest")

    async def _envoyer(self, interaction: discord.Interaction) -> bool:
        message = interaction.message or self.message
        try:
            erreur, warn = await _verifier_warn_contestable(interaction.user.id, self.warn_id)
        except aiomysql.Error as e:
            logger.critical(f"[warn:contestation] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur de base de données est survenue, réessaie.", ephemeral=True)
            return False
        if erreur:
            await interaction.edit_original_response(view=copie_vue(ContestationView, message, None))
            await interaction.followup.send(erreur, ephemeral=True)
            return True

        channel = await get_modo_channel(interaction.client)
        if channel is None:
            logger.error("[warn:contestation] Salon de modération (CHANNEL_MODO_ID) introuvable.")
            await interaction.followup.send(
                "❌ Ta contestation n'a pas pu être transmise au staff, réessaie plus tard.", ephemeral=True
            )
            return False

        embed = discord.Embed(title="Contestation", color=discord.Color.green(), description="Nouvelle contestation !")
        embed.add_field(name="Membre :", value=f"{interaction.user.mention} (`{interaction.user.id}`)", inline=False)
        warn_raison = warn_created_at = None
        if warn is not None:
            _, warn_raison, created_at, created_at_iso = warn
            warn_created_at = _date_pose_warn(created_at, created_at_iso) or None
            date_txt = f"<t:{warn_created_at}:d>" if warn_created_at else "date inconnue"
            embed.add_field(
                name=f"Avertissement #{self.warn_id} — {date_txt}",
                value=_tronquer(warn_raison) or "Pas de raison précisée",
                inline=False
            )
        embed.add_field(name="Raison : ", value=self.raison.value, inline=False)

        msg = await channel.send(
            embed=embed, view=copie_vue(RefuseroracceptercontestationView, None, set()),
            allowed_mentions=discord.AllowedMentions.none()
        )
        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "INSERT INTO contestations (message_id, membre_id, warn_id, warn_raison, warn_created_at) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (msg.id, interaction.user.id, self.warn_id, warn_raison, warn_created_at)
                    )
                await conn.commit()
        except aiomysql.Error as e:
            # Sans sa ligne en base, le message du staff ne peut pas être traité :
            # on le retire, et le membre peut réessayer.
            logger.critical(f"[warn:contestation] Erreur DB : {e}", exc_info=True)
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            await interaction.followup.send("❌ Une erreur de base de données est survenue, réessaie.", ephemeral=True)
            return False

        await interaction.edit_original_response(view=copie_vue(ContestationView, message, None))
        await interaction.followup.send("Merci, tu recevras une réponse sous 24h.", ephemeral=True)
        return True


class ContestationView(VuePersistante):
    """Vue persistante et sans état : ce bouton n'apparaît que dans le MP du membre
    averti, donc le membre concerné est toujours interaction.user ; l'id du warn
    est retrouvé depuis le footer de l'embed (voir _extract_warn_id) plutôt que
    stocké sur l'instance, ce qui casserait dès l'enregistrement global via
    bot.add_view (voir cogs/events.py) — même principe que
    RefuseroracceptercontestationView ci-dessus."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_mp(interaction)

    @discord.ui.button(label="Contestation", style=discord.ButtonStyle.red, custom_id="contest", emoji="❌")
    async def contest(self, interaction: discord.Interaction, button: discord.ui.Button):
        warn_id = _extract_warn_id(interaction.message)
        # Vérifié avant d'ouvrir le formulaire : le membre n'écrit pas 100
        # caractères pour un warn déjà expiré ou déjà contesté.
        try:
            erreur, _ = await _verifier_warn_contestable(interaction.user.id, warn_id)
        except aiomysql.Error as e:
            logger.critical(f"[warn:contestation] Erreur DB : {e}", exc_info=True)
            await repondre(interaction, "❌ Une erreur de base de données est survenue, réessaie.")
            return
        if erreur:
            await interaction.response.edit_message(view=copie_vue(ContestationView, interaction.message, None))
            await interaction.followup.send(erreur, ephemeral=True)
            return
        # Le bouton n'est désactivé qu'à l'envoi (voir ContestationModal) : un
        # formulaire fermé par erreur ne doit pas empêcher de contester.
        await interaction.response.send_modal(ContestationModal(interaction.message, warn_id))


class Warn(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_tempbans.start()
        self.check_warn_expirations.start()

    def cog_unload(self):
        self.check_tempbans.cancel()
        self.check_warn_expirations.cancel()

    # 30s d'origine était inutilement agressif pour un débannissement automatique
    # (personne ne remarque 5 min d'écart) ; aligné sur la cadence des autres
    # boucles de nettoyage (voir check_temp_roles dans cogs/boutique.py).
    @tasks.loop(minutes=5)
    async def check_tempbans(self):
        try:
            now = int(time.time())

            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT user_id FROM temp_bans WHERE unban_at <= %s",
                        (now,)
                    )
                    bans = await c.fetchall()

            if not bans:
                return

            guild_id = get_config("GUILD_ID")
            if not guild_id:
                return
            guild = self.bot.get_guild(int(guild_id))
            if not guild:
                return

            for (user_id,) in bans:
                try:
                    await guild.unban(discord.Object(id=user_id), reason="Fin du ban temporaire")
                except discord.NotFound:
                    pass  # Déjà débanni à la main (ou compte supprimé).
                except discord.Forbidden:
                    # Plus de permission de débannir : on ne réessaie pas toutes
                    # les 5 minutes, mais le staff doit le faire à la main.
                    logger.error(
                        f"[check_tempbans] Impossible de débannir {user_id} (permissions "
                        "insuffisantes) : à débannir manuellement."
                    )
                except discord.HTTPException as e:
                    # Erreur passagère de l'API : la ligne est gardée, on réessaiera.
                    logger.warning(f"[check_tempbans] Débannissement de {user_id} reporté : {e}")
                    continue

                async with connexion() as conn:
                    async with conn.cursor() as c:
                        await c.execute(
                            "DELETE FROM temp_bans WHERE user_id = %s",
                            (user_id,)
                        )
                    await conn.commit()
        except Exception as e:
            # Sans ce garde-fou, une erreur DB transitoire ici lèverait hors de
            # check_tempbans() : discord.ext.tasks arrête alors la boucle
            # définitivement et silencieusement — plus aucun débannissement
            # automatique jusqu'au redémarrage du bot.
            logger.error(f"[check_tempbans] Erreur inattendue, on réessaiera au prochain passage : {e}")

    @check_tempbans.before_loop
    async def before_tempbans(self):
        await self.bot.wait_until_ready()

    # Toutes les heures : l'échéance se compte en jours (une heure d'écart ne se
    # voit pas) et la requête de repérage ne renvoie rien la plupart du temps.
    @tasks.loop(hours=1)
    async def check_warn_expirations(self):
        """Fait expirer les warns échus (voir WARN_EXPIRATION et _echeance_expiration).

        Ne déclenche jamais de sanction (apply_warn_sanction n'est pas appelé ici) :
        repasser par un palier en descendant (ex. 4 -> 3) ne doit pas re-mute le
        membre — seul un nouveau warn qui fait atteindre un palier sanctionne."""
        try:
            now = int(time.time())

            async with connexion() as conn:
                async with conn.cursor() as c:
                    # Une seule requête : tous les warns des membres dont le plus
                    # ancien warn est échu (triés ensuite avec _ordre_expiration).
                    # Les warns suivants servent à annoncer au membre la date de la
                    # prochaine expiration.
                    # Le HAVING n'est qu'un pré-filtre pour ne pas rapatrier tout
                    # l'historique à chaque passage : il laisse passer les membres
                    # ayant un created_at NULL, et la décision finale reste prise
                    # par _echeance_expiration (seule à gérer le repli sur
                    # created_at_iso).
                    await c.execute(
                        """
                        SELECT w.user_id, w.id, w.created_at, w.created_at_iso, e.derniere_expiration
                        FROM warns w
                        LEFT JOIN warn_expirations e ON e.user_id = w.user_id
                        WHERE w.user_id IN (
                            SELECT w2.user_id
                            FROM warns w2
                            LEFT JOIN warn_expirations e2 ON e2.user_id = w2.user_id
                            GROUP BY w2.user_id, e2.derniere_expiration
                            HAVING SUM(w2.created_at IS NULL) > 0
                                OR GREATEST(MIN(w2.created_at), COALESCE(e2.derniere_expiration, 0)) + %s <= %s
                        )
                        """,
                        (WARN_EXPIRATION, now)
                    )
                    rows = await c.fetchall()

            if not rows:
                return

            par_membre: dict[int, tuple[int | None, list]] = {}
            for user_id, warn_id, created_at, created_at_iso, derniere in rows:
                par_membre.setdefault(user_id, (derniere, []))[1].append((warn_id, created_at, created_at_iso))

            guild_id = get_config("GUILD_ID")
            guild = self.bot.get_guild(int(guild_id)) if guild_id else None

            for user_id, (derniere, warns_membre) in par_membre.items():
                warns_membre.sort(key=_ordre_expiration)
                try:
                    expire, restants, prochaine = await self._expirer_warn_membre(
                        user_id, warns_membre, derniere, now
                    )
                    if expire:
                        logger.info(
                            f"[check_warn_expirations] Warn #{warns_membre[0][0]} expiré pour {user_id} "
                            f"({restants} restant(s))"
                        )
                        await self._notifier_expiration(guild, user_id, restants, prochaine)
                except Exception as e:
                    logger.error(f"[check_warn_expirations] Erreur pour le membre {user_id} : {e}")
        except Exception as e:
            # Même piège que ticket_watcher (start.py) : une exception qui sort de
            # cette méthode arrête définitivement et silencieusement la boucle
            # discord.ext.tasks — plus aucun warn n'expirerait jusqu'au
            # redémarrage du bot.
            logger.error(f"[check_warn_expirations] Erreur inattendue, on réessaiera au prochain passage : {e}")

    @check_warn_expirations.before_loop
    async def before_warn_expirations(self):
        await self.bot.wait_until_ready()

    async def _expirer_warn_membre(self, user_id, warns_membre, derniere, now):
        """Fait expirer le plus ancien warn d'un membre s'il est échu. warns_membre :
        tous ses warns, triés par _ordre_expiration. Renvoie (expiré ?, nombre de
        warns restants, échéance du suivant ou None).

        Un seul warn par passage : la nouvelle derniere_expiration est `now`, pas
        l'échéance théorique, donc le warn suivant attend encore 30 jours pleins.
        La règle « au plus un warn retiré tous les 30 jours » vaut ainsi en temps
        réel, y compris au premier démarrage sur des warns déjà anciens ou après
        une panne du bot, où plusieurs warns sont échus en même temps.

        Une seule transaction : suppression du warn, décrément du compteur et
        nouvelle derniere_expiration."""
        warn_id, created_at, created_at_iso = warns_membre[0]
        if now < _echeance_expiration(created_at, created_at_iso, derniere):
            return False, None, None

        async with connexion() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "DELETE FROM warns WHERE id = %s AND user_id = %s",
                    (warn_id, user_id)
                )
                if c.rowcount != 1:
                    # Warn retiré entre-temps par /unwarn ou l'acceptation d'une
                    # contestation, qui ont déjà décrémenté le compteur : on ne
                    # touche à rien (connexion() annule la transaction) et le
                    # prochain passage repartira de l'état à jour.
                    return False, None, None

            await decrement_warn(conn, user_id)
            async with conn.cursor() as c:
                # GREATEST : derniere_expiration ne doit jamais reculer, même si
                # deux instances du bot tournaient en même temps.
                await c.execute(
                    "INSERT INTO warn_expirations (user_id, derniere_expiration) VALUES (%s, %s) "
                    "ON DUPLICATE KEY UPDATE derniere_expiration = "
                    "GREATEST(derniere_expiration, VALUES(derniere_expiration))",
                    (user_id, now)
                )
                # Warns réellement restants, pour le MP : le compteur
                # utilisateurs.warn peut s'en écarter (remis à zéro quand le membre
                # quitte le serveur, voir on_member_remove dans cogs/events.py).
                await c.execute("SELECT COUNT(*) FROM warns WHERE user_id = %s", (user_id,))
                (restants,) = await c.fetchone()
            await conn.commit()

        prochaine = None
        if len(warns_membre) > 1:
            _, created_at, created_at_iso = warns_membre[1]
            prochaine = _echeance_expiration(created_at, created_at_iso, now)
        return True, restants, prochaine

    async def _notifier_expiration(self, guild, user_id, restants, prochaine):
        """MP best-effort au membre dont un warn vient d'expirer. Rien si le membre
        n'est plus sur le serveur : sans serveur en commun, Discord refuse de toute
        façon le MP."""
        if guild is None:
            return
        membre = guild.get_member(user_id)
        if membre is None:
            try:
                membre = await guild.fetch_member(user_id)
            except discord.HTTPException:  # inclut NotFound (membre parti)
                return

        lignes = ["⌛ Un de tes avertissements a expiré sur **Pixel Party**."]

        if restants:
            suite = f"Il t'en reste {restants}."
            if prochaine is not None:
                suite += f" Le prochain expirera <t:{prochaine}:R>."
            lignes.append(suite)
        else:
            lignes.append("Tu n'as plus aucun avertissement 🎉")

        lignes.append(
            f"\nRappel : 1 avertissement expire tous les {JOURS_EXPIRATION} jours, "
            "en commençant par le plus ancien."
        )

        embed = discord.Embed(
            title="Avertissement expiré",
            description="\n".join(lignes),
            color=discord.Color.green()
        )
        try:
            await membre.send(embed=embed)
        except discord.HTTPException:  # inclut Forbidden (MP fermés)
            pass

    @app_commands.command(name="warn", description="Avertit un membre")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    # Raison bornée à 1000 caractères : elle s'affiche dans un champ d'embed (MP au
    # membre, limite Discord de 1024) ; au-delà, le MP échouait alors que le warn
    # était déjà enregistré.
    async def warn(self, interaction: discord.Interaction, user: discord.Member,
                   raison: app_commands.Range[str, 1, 1000]):
        await interaction.response.defer(ephemeral=True)

        modo = interaction.user
        membre = user

        # Même hiérarchie que Discord pour un kick ou un ban : on n'avertit ni
        # un bot, ni soi-même, ni le propriétaire du serveur, ni un membre dont
        # le rôle le plus haut est égal ou supérieur au sien (sauf pour le
        # propriétaire du serveur ou du bot). Sans ça, un modérateur pouvait
        # faire mute ou bannir un collègue (paliers de sanction) d'un /warn.
        guild = interaction.guild
        erreur = None
        if membre.bot:
            erreur = "❌ Tu ne peux pas avertir un bot."
        elif membre.id == modo.id:
            erreur = "❌ Tu ne peux pas t'avertir toi-même."
        elif membre.id == guild.owner_id:
            erreur = "❌ Tu ne peux pas avertir le propriétaire du serveur."
        elif (modo.id != guild.owner_id and not est_owner(modo.id)
              and isinstance(modo, discord.Member) and membre.top_role >= modo.top_role):
            erreur = "❌ Tu ne peux pas avertir un membre dont le rôle est égal ou supérieur au tien."
        if erreur:
            await interaction.followup.send(erreur, ephemeral=True)
            return

        try:
            timestamp = int(time.time())
            iso_time = datetime.now(timezone.utc).isoformat()

            async with connexion() as conn:
                # Incrément atomique (voir utils/database.py) : évite que deux warns
                # posés au même moment sur le même membre ne s'écrasent l'un
                # l'autre. Fait dans la même transaction que l'INSERT INTO warns
                # ci-dessous (un seul commit) : si l'un des deux échoue, l'autre est
                # annulé plutôt que de désynchroniser le compteur de l'historique
                # des warns.
                await increment_warn(conn, membre.id)

                async with conn.cursor() as c:
                    await c.execute(
                        """
                        INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (membre.id, modo.id, raison, timestamp, iso_time)
                    )
                    warn_id = c.lastrowid

                # Palier de sanction d'après les warns réellement en cours (voir
                # compter_warns), celui qu'on vient d'ajouter compris.
                warn_count = await compter_warns(conn, membre.id)
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[warn] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Tu viens d'être averti",
            description=(
                "Tu t'es mal comporté sur Pixel Party, donc un avertissement vient de tomber.\n"
                f"⌛ Il expirera au plus tôt dans {JOURS_EXPIRATION} jours."
            ),
            color=discord.Color.red()
        )
        embed.add_field(name="Modérateur : ", value=modo.mention, inline=False)
        embed.add_field(name="Raison : ", value=raison, inline=False)
        embed.add_field(
            name="C'est une erreur ?",
            value="Clique sur le bouton ci-dessous pour contester ta sanction"
        )
        embed.set_footer(text=f"ID du warn : {warn_id}")

        # MP envoyé AVANT la sanction : un ban (10e warn) retire tout serveur en
        # commun, et le MP de l'avertissement (avec son bouton de contestation)
        # ne pouvait alors plus être envoyé.
        mp_envoye = True
        try:
            await membre.send(embed=embed, view=copie_vue(ContestationView, None, set()))
        except discord.HTTPException:
            mp_envoye = False

        channel = await get_modo_channel(self.bot, guild)
        await apply_warn_sanction(guild, membre, channel, warn_count)

        message = f"✅ {membre.mention} a reçu l'avertissement #{warn_id} ({warn_count} en cours)."
        if not mp_envoye:
            message += "\n⚠️ Ses MP sont fermés : il n'a pas été prévenu en message privé."
        await interaction.followup.send(message, ephemeral=True)

    @app_commands.command(name="warns", description="Affiche l'historique des avertissements d'un membre")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    # discord.User (et non Member) : l'historique d'un membre parti du serveur
    # reste consultable, par exemple avant de lever un ban.
    async def warns(self, interaction: discord.Interaction, user: discord.User):
        await interaction.response.defer(ephemeral=True)

        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    # Tous les warns (quelques-uns par membre au plus) : le plus
                    # ancien, celui qui expirera en premier, se choisit avec le même
                    # tri que check_warn_expirations (_ordre_expiration).
                    await c.execute(
                        "SELECT id, created_at, created_at_iso, modo_id, raison FROM warns WHERE user_id = %s",
                        (user.id,)
                    )
                    rows = sorted(await c.fetchall(), key=_ordre_expiration)
                    await c.execute(
                        "SELECT derniere_expiration FROM warn_expirations WHERE user_id = %s",
                        (user.id,)
                    )
                    derniere = await c.fetchone()
        except aiomysql.Error as e:
            logger.critical(f"[warns] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📋 Avertissements de {user.display_name}", color=discord.Color.orange())
        if not rows:
            embed.description = "Aucun avertissement."
        else:
            _, created_at, created_at_iso, _, _ = rows[0]
            echeance = _echeance_expiration(created_at, created_at_iso, derniere[0] if derniere else None)
            if echeance <= int(time.time()):
                # Échu mais pas encore traité : check_warn_expirations ne passe
                # qu'une fois par heure.
                prochaine = "imminente (au prochain passage automatique, dans l'heure)"
            else:
                prochaine = f"<t:{echeance}:R> (<t:{echeance}:f>)"
            embed.description = (
                f"⌛ Prochaine expiration : {prochaine}\n"
                f"1 avertissement expire tous les {JOURS_EXPIRATION} jours, en commençant par le plus ancien."
            )

            # Les 25 plus récents (limite Discord de 25 champs par embed), raison
            # tronquée : 25 champs doivent tenir dans les 6000 caractères d'un embed.
            for warn_id, created_at, created_at_iso, modo_id, raison in reversed(rows[-25:]):
                # created_at peut être NULL sur d'anciennes lignes : "<t:None:d>"
                # s'afficherait tel quel dans Discord.
                date_pose = _date_pose_warn(created_at, created_at_iso)
                date_txt = f"<t:{date_pose}:d>" if date_pose else "date inconnue"
                raison = raison or "Pas de raison précisée"
                if len(raison) > 150:
                    raison = raison[:149] + "…"
                embed.add_field(
                    name=f"#{warn_id} — {date_txt}",
                    value=f"Par <@{modo_id}>\n{raison}",
                    inline=False
                )
            if len(rows) > 25:
                embed.set_footer(text=f"25 plus récents affichés sur {len(rows)}")
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="unwarn", description="Retire un avertissement précis (voir son id via /warns)")
    @app_commands.describe(warn_id="Identifiant de l'avertissement à retirer (visible via /warns)")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.guild_only()
    @app_commands.checks.has_permissions(manage_messages=True)
    async def unwarn(self, interaction: discord.Interaction, warn_id: app_commands.Range[int, 1, 2_147_483_647]):
        await interaction.response.defer(ephemeral=True)

        try:
            async with connexion() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT user_id FROM warns WHERE id = %s", (warn_id,))
                    row = await c.fetchone()

            if row is None:
                await interaction.followup.send("❌ Avertissement introuvable (déjà retiré ou expiré ?).", ephemeral=True)
                return

            (user_id,) = row
            if user_id == interaction.user.id and not est_owner(interaction.user.id):
                await interaction.followup.send(
                    "❌ Tu ne peux pas retirer tes propres avertissements : demande à un autre membre du staff.",
                    ephemeral=True
                )
                return

            # Nouvelle transaction : dans celle de la lecture, un second /unwarn
            # simultané lisait la ligne avant la suppression du premier, et son
            # DELETE échouait alors en erreur 1020 (« Record has changed since
            # last read », MariaDB 11.6+) au lieu de simplement ne rien retirer.
            async with connexion() as conn:
                async with conn.cursor() as c:
                    # Réclame le warn de façon atomique avant de décrémenter le
                    # compteur (même principe que
                    # RefuseroracceptercontestationView.accepter plus haut) : si
                    # /unwarn est lancé deux fois sur le même id en même temps, ou
                    # pendant que check_warn_expirations fait expirer ce warn, un
                    # seul des deux DELETE obtient rowcount == 1 et décrémente
                    # réellement le compteur.
                    await c.execute("DELETE FROM warns WHERE id = %s AND user_id = %s", (warn_id, user_id))
                    gagne = c.rowcount == 1

                if gagne:
                    await decrement_warn(conn, user_id)

                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[unwarn] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        if not gagne:
            await interaction.followup.send("❌ Cet avertissement a déjà été retiré (ou a expiré) entre-temps.", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Avertissement #{warn_id} retiré (membre : <@{user_id}>).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Warn(bot))
