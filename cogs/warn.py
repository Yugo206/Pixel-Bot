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
from utils.database import connexion, get_pool, increment_warn, decrement_warn
from utils.sanctions import apply_warn_sanction, get_modo_channel
from utils.config import get_config

logger = logging.getLogger(__name__)

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


class RaisonrefuserModal(discord.ui.Modal, title="Raison"):
    raison = discord.ui.TextInput(
        label="Raison du refus",
        placeholder="Je trouve que ce warn est mérité car ...",
        min_length=10,
        max_length=1092,
        style=discord.TextStyle.paragraph,
        required=True
    )

    def __init__(self, membre, message_id):
        super().__init__()
        self.membre = membre
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            "Refus envoyé au membre ❌",
            ephemeral=True
        )

        embed = discord.Embed(
            title="Contestation refusée",
            description="Ta contestation a été refusée",
            color=discord.Color.red()
        )
        embed.add_field(name="Modérateur", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison", value=self.raison.value, inline=False)

        try:
            await self.membre.send(embed=embed)
        except discord.Forbidden:
            pass

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute("DELETE FROM contestations WHERE message_id = %s", (self.message_id,))
            await conn.commit()


class RefuseroracceptercontestationView(discord.ui.View):
    """Vue persistante et sans état : les infos de la contestation (membre, warn concerné)
    sont retrouvées dans la table `contestations` à partir de l'id du message cliqué,
    au lieu d'être stockées sur l'instance (ce qui casse dès qu'elle est enregistrée
    globalement via bot.add_view)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="warn:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT membre_id, warn_id FROM contestations WHERE message_id = %s",
                        (interaction.message.id,)
                    )
                    row = await cur.fetchone()

            if row is None:
                await interaction.response.send_message("❌ Contestation introuvable (déjà traitée ?).", ephemeral=True)
                return

            membre_id, warn_id = row
            guild = interaction.guild
            membre = guild.get_member(membre_id)
            if membre is None:
                try:
                    membre = await guild.fetch_member(membre_id)
                except discord.NotFound:
                    membre = None

            # On defer avant de toucher la DB (et on n'édite le message qu'une fois
            # la suppression du warn effectivement commit) : si la DB échoue, le
            # modérateur voit une erreur et peut réessayer, au lieu de voir la
            # contestation comme traitée alors que le warn est toujours là.
            await interaction.response.defer()

            warn_retire = False
            async with pool.acquire() as conn:
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

            if not gagne:
                await interaction.followup.send("❌ Cette contestation a déjà été traitée.", ephemeral=True)
                return

            for b in self.children:
                b.disabled = True
            await interaction.edit_original_response(view=self)

            if membre is not None:
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

                try:
                    await membre.send(embed=embed)
                except discord.Forbidden:
                    pass

            if warn_retire:
                await interaction.followup.send("Sanction retirée ✅", ephemeral=True)
            else:
                await interaction.followup.send(
                    "Contestation clôturée ✅ — ce warn n'existait plus (déjà expiré ou retiré "
                    "via /unwarn), le compteur du membre n'a pas été modifié.",
                    ephemeral=True
                )
        except aiomysql.Error as e:
            logger.critical(f"[warn:accepter] Erreur DB : {e}", exc_info=True)
            if interaction.response.is_done():
                await interaction.followup.send("❌ Une erreur de base de données est survenue, réessaie.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Une erreur de base de données est survenue, réessaie.", ephemeral=True)
        except Exception as e:
            logger.error(f"[warn:accepter] {e}")
            if interaction.response.is_done():
                await interaction.followup.send("❌ Une erreur inattendue est survenue, réessaie.", ephemeral=True)
            else:
                await interaction.response.send_message("❌ Une erreur inattendue est survenue, réessaie.", ephemeral=True)

    @discord.ui.button(label="Refuser", style=discord.ButtonStyle.red, custom_id="warn:refuser")
    async def refuser(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT membre_id FROM contestations WHERE message_id = %s",
                        (interaction.message.id,)
                    )
                    row = await cur.fetchone()

            if row is None:
                await interaction.response.send_message("❌ Contestation introuvable (déjà traitée ?).", ephemeral=True)
                return

            membre_id = row[0]
            guild = interaction.guild
            membre = guild.get_member(membre_id)
            if membre is None:
                try:
                    membre = await guild.fetch_member(membre_id)
                except discord.NotFound:
                    membre = None

            if membre is None:
                await interaction.response.send_message("❌ Ce membre n'est plus sur le serveur.", ephemeral=True)
                return

            await interaction.response.send_modal(RaisonrefuserModal(membre, interaction.message.id))
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(view=self)
        except Exception as e:
            logger.error(f"[warn:refuser] {e}")


class ContestationModal(discord.ui.Modal, title="Contestation"):
    raison = discord.ui.TextInput(
        style=discord.TextStyle.paragraph,
        placeholder="Je trouve ce warn injuste car ...",
        min_length=100,
        max_length=1092,
        label="Explique pourquoi tu trouves ce warn injuste",
        required=True
    )

    def __init__(self, bot, membre, warn):
        super().__init__()
        self.bot = bot
        self.membre = membre
        self.warn = warn  # tuple (id,) ou None — seul l'id est utilisé ci-dessous

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("Merci, tu recevras une réponse sous 24h.", ephemeral=True)

        channel = await get_modo_channel(self.bot)
        if channel is None:
            return

        embed = discord.Embed(title="Contestation", color=discord.Color.green(), description="Nouvelle contestation !")
        embed.add_field(name="Membre :", value=interaction.user.mention, inline=False)
        embed.add_field(name="Raison : ", value=self.raison.value, inline=False)

        msg = await channel.send(embed=embed, view=RefuseroracceptercontestationView())

        warn_id = self.warn[0] if self.warn else None
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as c:
                await c.execute(
                    "INSERT INTO contestations (message_id, membre_id, warn_id) VALUES (%s, %s, %s)",
                    (msg.id, self.membre.id, warn_id)
                )
            await conn.commit()


class ContestationView(discord.ui.View):
    """Vue persistante et sans état : ce bouton n'apparaît que dans le MP du membre
    averti, donc le membre concerné est toujours interaction.user ; l'id du warn
    est retrouvé depuis le footer de l'embed (voir _extract_warn_id) plutôt que
    stocké sur l'instance, ce qui casserait dès l'enregistrement global via
    bot.add_view (voir cogs/events.py) — même principe que
    RefuseroracceptercontestationView ci-dessus."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Contestation", style=discord.ButtonStyle.red, custom_id="contest", emoji="❌")
    async def contest(self, interaction: discord.Interaction, button: discord.ui.Button):
        warn_id = _extract_warn_id(interaction.message)
        warn = (warn_id,) if warn_id is not None else None
        await interaction.response.send_modal(ContestationModal(interaction.client, interaction.user, warn))
        button.disabled = True
        await interaction.message.edit(view=self)


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
            pool = get_pool()

            async with pool.acquire() as conn:
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
                    user = await self.bot.fetch_user(user_id)
                    await guild.unban(user, reason="Fin du ban temporaire")
                except (discord.NotFound, discord.Forbidden):
                    pass

                async with pool.acquire() as conn:
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
    @app_commands.checks.has_permissions(manage_messages=True)
    # Raison bornée à 1000 caractères : elle s'affiche dans un champ d'embed (MP au
    # membre, limite Discord de 1024) ; au-delà, le MP échouait alors que le warn
    # était déjà enregistré.
    async def warn(self, interaction: discord.Interaction, user: discord.Member,
                   raison: app_commands.Range[str, 1, 1000]):
        await interaction.response.defer(ephemeral=True)

        if interaction.guild is None:
            embed = discord.Embed(
                title="Les messages privés...",
                description="Cette commande n'est pas disponible en MP. Utilise-la directement sur le serveur !",
                color=discord.Color.red()
            )
            await interaction.followup.send(embed=embed)
            return

        modo = interaction.user
        membre = user

        try:
            timestamp = int(time.time())
            iso_time = datetime.now(timezone.utc).isoformat()

            pool = get_pool()
            async with pool.acquire() as conn:
                # Incrément atomique (voir utils/database.py) : évite que deux warns
                # posés au même moment sur le même membre ne s'écrasent l'un
                # l'autre. Fait dans la même transaction que l'INSERT INTO warns
                # ci-dessous (un seul commit) : si l'un des deux échoue, l'autre est
                # annulé plutôt que de désynchroniser le compteur de l'historique
                # des warns.
                warn_count = await increment_warn(conn, membre.id)

                async with conn.cursor() as c:
                    await c.execute(
                        """
                        INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (membre.id, modo.id, raison, timestamp, iso_time)
                    )
                    warn_id = c.lastrowid

                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[warn] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        channel = await get_modo_channel(self.bot, interaction.guild)
        await apply_warn_sanction(interaction.guild, membre, channel, warn_count)

        await interaction.followup.send(
            "Le membre vient d'être averti en MP, merci !",
            ephemeral=True
        )

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

        try:
            await membre.send(embed=embed, view=ContestationView())
        except discord.Forbidden:
            pass

    @app_commands.command(name="warns", description="Affiche l'historique des avertissements d'un membre")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warns(self, interaction: discord.Interaction, user: discord.Member):
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
    @app_commands.checks.has_permissions(manage_messages=True)
    async def unwarn(self, interaction: discord.Interaction, warn_id: int):
        await interaction.response.defer(ephemeral=True)

        pool = get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute("SELECT user_id FROM warns WHERE id = %s", (warn_id,))
                    row = await c.fetchone()

                if row is None:
                    await interaction.followup.send("❌ Avertissement introuvable (déjà retiré ou expiré ?).", ephemeral=True)
                    return

                (user_id,) = row

                async with conn.cursor() as c:
                    # Réclame le warn de façon atomique avant de décrémenter le
                    # compteur (même principe que
                    # RefuseroracceptercontestationView.accepter plus haut) : si
                    # /unwarn est lancé deux fois sur le même id en même temps, ou
                    # pendant que check_warn_expirations fait expirer ce warn, un
                    # seul des deux DELETE obtient rowcount == 1 et décrémente
                    # réellement le compteur.
                    await c.execute("DELETE FROM warns WHERE id = %s", (warn_id,))
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
