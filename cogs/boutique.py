import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiomysql
import logging
import time
import os
from dotenv import load_dotenv
load_dotenv()
from utils.database import get_pool
from utils import cache

logger = logging.getLogger(__name__)


class AchatSelect(discord.ui.Select):
    def __init__(self, items):
        # `items` est récupéré au préalable de façon asynchrone (voir boutique()) :
        # un Select ne peut pas faire de requête réseau dans son __init__ synchrone.
        if not items:
            options = [
                discord.SelectOption(
                    label="Boutique vide",
                    description="Aucun objet disponible",
                    value="__empty__"
                )
            ]
        else:
            options = [
                discord.SelectOption(
                    label=name,
                    description=f"{price} €",
                    value=name
                )
                for name, price, item_type, valeur, duration in items
            ]

        super().__init__(
            placeholder="🛒 Choisis un objet",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        item_name = self.values[0]

        if item_name == "__empty__":
            await interaction.followup.send("❌ La boutique est vide.", ephemeral=True)
            return

        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute(
                        "SELECT name, price, type, valeur, duration FROM shop WHERE name = %s",
                        (item_name,)
                    )
                    result = await cursor.fetchone()

                    if not result:
                        await interaction.followup.send("❌ Objet introuvable.", ephemeral=True)
                        return

                    name, price, item_type, valeur, duration = result

                    # Déduction atomique et conditionnelle : n'a d'effet que si le solde est
                    # suffisant, ce qui évite les doubles achats en cas de clics rapides.
                    await cursor.execute(
                        "UPDATE utilisateurs SET argent = argent - %s WHERE user_id = %s AND argent >= %s",
                        (price, interaction.user.id, price)
                    )

                    if cursor.rowcount == 0:
                        await conn.rollback()
                        await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                        row = await cursor.fetchone()
                        argent = row[0] if row and row[0] is not None else 0
                        await interaction.followup.send(
                            f"❌ Tu n'as pas assez d'argent.\n💰 Prix : {price} € | 💸 Ton solde : {argent} €",
                            ephemeral=True
                        )
                        return

                    try:
                        if item_type == 1:
                            role = interaction.guild.get_role(int(valeur))
                            if role is None:
                                try:
                                    role = await interaction.guild.fetch_role(int(valeur))
                                except discord.HTTPException:
                                    role = None
                            if role is None:
                                await conn.rollback()
                                await interaction.followup.send("❌ Rôle introuvable.", ephemeral=True)
                                return

                            await interaction.user.add_roles(role)

                            if duration is not None:
                                expires_at = int(time.time()) + (duration * 86400)
                                await cursor.execute(
                                    "INSERT INTO temp_roles (user_id, role_id, end_time, origin) VALUES (%s, %s, %s, 'shop_purchase')",
                                    (interaction.user.id, role.id, expires_at)
                                )
                                logger.info(f"[DB] Rôle temporaire ajouté : user={interaction.user.id}, role={role.id}, expires={expires_at}")

                            await conn.commit()
                            await interaction.followup.send(
                                f"🛒 **Achat réussi !**\n\n🎭 Rôle : **{role.name}**\n💰 Prix : **{price} €**",
                                ephemeral=True
                            )

                        elif item_type == 2:
                            await cursor.execute(
                                "SELECT quantite FROM inventaire WHERE user_id = %s AND item_id = %s",
                                (interaction.user.id, valeur)
                            )
                            result_inv = await cursor.fetchone()

                            if result_inv:
                                await cursor.execute(
                                    "UPDATE inventaire SET quantite = quantite + 1 WHERE user_id = %s AND item_id = %s",
                                    (interaction.user.id, valeur)
                                )
                            else:
                                await cursor.execute(
                                    "INSERT INTO inventaire (user_id, item_id, quantite) VALUES (%s, %s, 1)",
                                    (interaction.user.id, valeur)
                                )

                            await conn.commit()
                            await interaction.followup.send(
                                f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**",
                                ephemeral=True
                            )

                        elif item_type == 3:
                            await cursor.execute(
                                "UPDATE utilisateurs SET xp = xp + %s WHERE user_id = %s",
                                (valeur, interaction.user.id)
                            )
                            await conn.commit()
                            # Écriture SQL directe sur xp en dehors du cache (utils/cache.py) :
                            # on invalide plutôt que de tenter de le mettre à jour ici, pour ne
                            # pas dupliquer la logique — la prochaine lecture (au message
                            # suivant) revient chercher la vraie valeur en base.
                            cache.invalidate_xp(interaction.user.id)
                            await interaction.followup.send(
                                f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**",
                                ephemeral=True
                            )

                        else:
                            await conn.rollback()
                            await interaction.followup.send("❌ Type d'objet inconnu dans la boutique.", ephemeral=True)
                            return

                    except discord.HTTPException as e:
                        await conn.rollback()
                        await interaction.followup.send(f"❌ Erreur lors de l'achat : {e}", ephemeral=True)
                        return

        except aiomysql.Error as e:
            logger.critical(f"Erreur SQL achat : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        self.disabled = True
        self.placeholder = "Objet acheté ✔"
        await interaction.message.edit(view=self.view)


class BoutiqueCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_temp_roles.start()

    def cog_unload(self):
        self.check_temp_roles.cancel()

    class BoutiqueView(discord.ui.View):
        def __init__(self, items):
            super().__init__(timeout=60)
            self.add_item(AchatSelect(items))

    @tasks.loop(minutes=5)
    async def check_temp_roles(self):
        """Retire automatiquement les rôles temporaires achetés en boutique une fois expirés."""
        try:
            now = int(time.time())
            pool = get_pool()

            async with pool.acquire() as conn:
                async with conn.cursor() as c:
                    await c.execute(
                        "SELECT id, user_id, role_id FROM temp_roles WHERE origin = 'shop_purchase' AND end_time <= %s",
                        (now,)
                    )
                    expired = await c.fetchall()

                if not expired:
                    return

                guild_id = os.getenv("GUILD_ID")
                guild = None
                if guild_id:
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is None:
                        try:
                            guild = await self.bot.fetch_guild(int(guild_id))
                        except discord.HTTPException:
                            guild = None

                async with conn.cursor() as c:
                    for row_id, user_id, role_id in expired:
                        try:
                            if guild is not None:
                                member = guild.get_member(user_id)
                                if member is None:
                                    try:
                                        member = await guild.fetch_member(user_id)
                                    except discord.NotFound:
                                        member = None

                                if member is not None:
                                    role = guild.get_role(role_id)
                                    if role is not None:
                                        try:
                                            await member.remove_roles(role, reason="Rôle temporaire de boutique expiré")
                                        except discord.Forbidden:
                                            logger.warning(
                                                f"[check_temp_roles] Permissions insuffisantes pour retirer le "
                                                f"rôle {role_id} à {user_id} : le rôle restera attribué en Discord "
                                                "bien que la ligne de suivi soit supprimée."
                                            )
                        except Exception as e:
                            logger.error(f"[check_temp_roles] Erreur pour user={user_id} role={role_id} : {e}")
                        finally:
                            await c.execute("DELETE FROM temp_roles WHERE id = %s", (row_id,))

                await conn.commit()
        except Exception as e:
            # Sans ce garde-fou, une erreur qui échappe au try/except par rôle
            # ci-dessus (ex: la requête de liste elle-même, ou le commit) lèverait
            # hors de check_temp_roles() : discord.ext.tasks arrête alors la boucle
            # définitivement et silencieusement — plus aucun rôle temporaire ne
            # serait jamais retiré jusqu'au redémarrage du bot.
            logger.error(f"[check_temp_roles] Erreur inattendue, on réessaiera au prochain passage : {e}")

    @check_temp_roles.before_loop
    async def before_check_temp_roles(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="boutique", description="Regarde la boutique")
    async def boutique(self, interaction: discord.Interaction):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Cette commande n'est pas disponible en MP. Utilise-la directement sur le serveur !",
                ephemeral=True
            )
            return

        await interaction.response.defer()

        embed = discord.Embed(
            title="🛍 Boutique",
            color=discord.Color.green()
        )

        items = []
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT name, price, type, valeur, duration FROM shop")
                    items = await cursor.fetchall()
        except aiomysql.Error as e:
            logger.critical(f"Erreur SQL boutique : {e}", exc_info=True)

        if not items:
            embed.description = "❌ Boutique vide"
        else:
            for name, price, item_type, valeur, duration in items:
                if item_type == 1:
                    type_str = "Rôle"
                    if duration is not None:
                        type_str += " temporaire"
                    else:
                        type_str += " permanent"
                elif item_type == 2:
                    type_str = "Objet d'inventaire"
                elif item_type == 3:
                    type_str = "XP"
                else:
                    type_str = "Inconnu"

                desc = f" **Prix :** {price} €\n **Type :** {type_str}"
                if duration is not None and item_type == 1:
                    desc += f"\n **Durée :** {duration} jours"

                embed.add_field(name=name, value=desc, inline=False)

        await interaction.followup.send(embed=embed, view=self.BoutiqueView(items))


async def setup(bot):
    await bot.add_cog(BoutiqueCog(bot))
