import discord
from discord import app_commands
from discord.ext import commands, tasks
import aiomysql
import logging
import os
import re
from datetime import datetime, timezone
import time
from dotenv import load_dotenv
load_dotenv()
from utils.database import get_pool, increment_warn, decrement_warn
from utils.sanctions import apply_warn_sanction, get_modo_channel

logger = logging.getLogger(__name__)

_WARN_ID_RE = re.compile(r"ID du warn\s*:\s*(\d+)")


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

            async with pool.acquire() as conn:
                # Réclame la contestation de façon atomique avant de toucher au
                # compteur : si un double clic (ou deux modérateurs) arrivent en
                # même temps, un seul des deux DELETE obtient rowcount == 1 et
                # décrémente réellement le warn — l'autre voit juste que c'est déjà
                # traité au lieu de décrémenter deux fois.
                async with conn.cursor() as cur:
                    await cur.execute("DELETE FROM contestations WHERE message_id = %s", (interaction.message.id,))
                    gagne = cur.rowcount == 1

                if gagne:
                    # Même transaction que le DELETE ci-dessus (un seul commit) : si
                    # le décrément ou la suppression du warn échoue, la ligne de
                    # contestation qu'on vient de réclamer n'est pas perdue pour rien.
                    await decrement_warn(conn, membre_id)

                    if warn_id is not None:
                        async with conn.cursor() as cur:
                            await cur.execute("DELETE FROM warns WHERE id = %s", (warn_id,))

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
                    description="Ton warn a été retiré",
                    color=discord.Color.green()
                )
                embed.add_field(name="Modérateur :", value=interaction.user.mention, inline=False)

                try:
                    await membre.send(embed=embed)
                except discord.Forbidden:
                    pass

            await interaction.followup.send("Sanction retirée ✅", ephemeral=True)
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

    def cog_unload(self):
        self.check_tempbans.cancel()

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

            guild_id = os.getenv("GUILD_ID")
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

    @app_commands.command(name="warn", description="Avertit un membre")
    @app_commands.checks.has_permissions(manage_messages=True)
    async def warn(self, interaction: discord.Interaction, user: discord.Member, raison: str):
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
            description="Tu t'es mal comporté sur Pixel Party, donc un avertissement vient de tomber.",
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

        pool = get_pool()
        try:
            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    # LIMIT 25 : correspond à la limite Discord de 25 champs par embed
                    # (voir la boucle add_field ci-dessous), pas une limite arbitraire.
                    await c.execute(
                        "SELECT id, modo_id, raison, created_at FROM warns "
                        "WHERE user_id = %s ORDER BY created_at DESC LIMIT 25",
                        (user.id,)
                    )
                    rows = await c.fetchall()
        except aiomysql.Error as e:
            logger.critical(f"[warns] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        embed = discord.Embed(title=f"📋 Avertissements de {user.display_name}", color=discord.Color.orange())
        if not rows:
            embed.description = "Aucun avertissement."
        else:
            for warn_id, modo_id, raison, created_at in rows:
                embed.add_field(
                    name=f"#{warn_id} — <t:{created_at}:d>",
                    value=f"Par <@{modo_id}>\n{raison or 'Pas de raison précisée'}",
                    inline=False
                )
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
                    await interaction.followup.send("❌ Avertissement introuvable.", ephemeral=True)
                    return

                (user_id,) = row

                async with conn.cursor() as c:
                    # Réclame le warn de façon atomique avant de décrémenter le
                    # compteur (même principe que
                    # RefuseroracceptercontestationView.accepter plus haut) : si
                    # /unwarn est lancé deux fois sur le même id en même temps, un
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
            await interaction.followup.send("❌ Cet avertissement a déjà été retiré entre-temps.", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Avertissement #{warn_id} retiré (membre : <@{user_id}>).", ephemeral=True)


async def setup(bot):
    await bot.add_cog(Warn(bot))
