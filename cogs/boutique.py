import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiomysql
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv
load_dotenv()
from utils.database import connexion
from utils import cache
from utils.config import get_config
from utils.transactions import log_transaction
from utils.views import TimedView

logger = logging.getLogger(__name__)

# Fuseau du « jour » de shop.limite_jour (voir achats_jour dans
# utils/setupdatabase.py) : le compteur repart à zéro à minuit heure de Paris,
# celle des membres du serveur, et non à minuit UTC (1h ou 2h du matin pour eux).
FUSEAU_LIMITE_JOUR = ZoneInfo("Europe/Paris")

# Un select Discord accepte au plus 25 options, et un embed au plus 25 champs :
# au-delà, Discord refuse l'envoi et la boutique ne s'afficherait plus du tout.
MAX_OBJETS_BOUTIQUE = 25

# shop.limite_jour = 0 : objet toujours affiché mais plus achetable (voir README).
_ARTICLE_INDISPONIBLE = "❌ Cet article n'est pas disponible à l'achat."

# Erreurs MariaDB qui annulent l'achat sans que rien n'ait été engagé, et qu'un
# nouvel essai résout : interblocage (1213) et, depuis MariaDB 11.6.2
# (innodb_snapshot_isolation), écriture sur une ligne modifiée par un autre
# depuis la lecture de la transaction (1020, « Record has changed since last
# read »). Voir _effectuer_achat.
_ERREURS_A_REJOUER = {1213, 1020}
_ESSAIS_ACHAT = 3


def _jour_courant():
    return datetime.now(FUSEAU_LIMITE_JOUR).date()


def _decrire_limite(limite_jour, gabarit: str, indisponible: str) -> str:
    """Mention de shop.limite_jour dans l'affichage de la boutique : rien si
    illimité (NULL), `indisponible` si 0, `gabarit` rempli avec la limite sinon."""
    if limite_jour is None:
        return ""
    return indisponible if limite_jour == 0 else gabarit.format(limite_jour)


def _decrire_type(item_type: int, duration) -> str:
    if item_type == 1:
        return "Rôle temporaire" if duration is not None else "Rôle permanent"
    if item_type == 2:
        return "Objet d'inventaire"
    if item_type == 3:
        return "XP"
    return "Inconnu"


async def _trouver_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    """Rôle vendu par un objet de type 1 (shop.valeur) : None s'il a été supprimé
    du serveur depuis sa mise en vente."""
    role = guild.get_role(role_id)
    if role is None:
        try:
            role = await guild.fetch_role(role_id)
        except discord.HTTPException:
            role = None
    return role


async def _achats_role(cursor, user_id: int, role_id: int) -> tuple[int | None, list[int]]:
    """Achats temporaires de `role_id` en cours pour ce membre (temp_roles, origin
    'shop_purchase') : (échéance epoch la plus tardive, ids des lignes).
    Échéance None s'il n'en a aucun — un rôle qu'il possède quand même lui vient
    alors d'ailleurs (achat permanent, attribution par le staff...)."""
    await cursor.execute(
        "SELECT id, end_time FROM temp_roles "
        "WHERE user_id = %s AND role_id = %s AND origin = 'shop_purchase'",
        (user_id, role_id)
    )
    lignes = await cursor.fetchall()
    return max((fin for _, fin in lignes), default=None), [id_ligne for id_ligne, _ in lignes]


def _refus_role(role: discord.Role, duration, possede: bool, fin_achat: int | None) -> str | None:
    """Message de refus si acheter ce rôle ne servirait à rien, None sinon.

    Seul un rôle possédé SANS achat temporaire en cours est refusé : il est tenu
    durablement, le racheter en permanent ne changerait rien, et en temporaire le
    ferait même retirer à l'échéance par check_temp_roles. Un rôle tenu d'un
    achat temporaire peut en revanche être prolongé, ou rendu permanent (voir
    _achat_en_transaction)."""
    if not possede or fin_achat is not None:
        return None
    if duration is None:
        return f"❌ Tu possèdes déjà le rôle **{role.name}**."
    return (
        f"❌ Tu possèdes déjà le rôle **{role.name}** de façon permanente : l'acheter "
        "en temporaire le ferait retirer à la fin de la durée."
    )


async def _proposer_achat(interaction: discord.Interaction, item_name: str) -> None:
    """Premier temps d'un achat, au choix d'un objet dans AchatSelect : n'écrit
    rien en base. Écarte d'emblée, avec un message clair, les achats voués à
    l'échec (solde, limite du jour, rôle déjà possédé), puis envoie la demande
    de confirmation (ConfirmationAchatView). Purement informatif : tout est
    revérifié au clic sur "Confirmer" (voir _achat_en_transaction), des minutes
    ayant pu passer entre-temps."""
    membre = interaction.user
    try:
        async with connexion() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(
                    "SELECT name, price, type, valeur, duration, limite_jour FROM shop WHERE name = %s",
                    (item_name,)
                )
                objet = await cursor.fetchone()

                if not objet:
                    await interaction.followup.send("❌ Objet introuvable.", ephemeral=True)
                    return

                name, price, item_type, valeur, duration, limite_jour = objet

                await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (membre.id,))
                row = await cursor.fetchone()
                argent = row[0] if row and row[0] is not None else 0

                await cursor.execute(
                    "SELECT nb FROM achats_jour WHERE user_id = %s AND item = %s AND jour = %s",
                    (membre.id, name, _jour_courant())
                )
                row = await cursor.fetchone()
                achats_du_jour = row[0] if row else 0

                fin_achat = None
                if item_type == 1:
                    fin_achat, _ = await _achats_role(cursor, membre.id, int(valeur))
    except aiomysql.Error as e:
        logger.critical(f"Erreur SQL achat : {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
        return

    if item_type not in (1, 2, 3):
        await interaction.followup.send("❌ Type d'objet inconnu dans la boutique.", ephemeral=True)
        return

    if limite_jour == 0:
        await interaction.followup.send(_ARTICLE_INDISPONIBLE, ephemeral=True)
        return

    role = None
    possede = False
    if item_type == 1:
        role = await _trouver_role(interaction.guild, int(valeur))
        if role is None:
            await interaction.followup.send("❌ Rôle introuvable.", ephemeral=True)
            return
        possede = membre.get_role(role.id) is not None
        refus = _refus_role(role, duration, possede, fin_achat)
        if refus:
            await interaction.followup.send(refus, ephemeral=True)
            return

    if limite_jour is not None and achats_du_jour >= limite_jour:
        await interaction.followup.send(
            f"❌ Tu as atteint la limite d'achats du jour pour **{name}** ({achats_du_jour}/{limite_jour}). "
            "Reviens demain !",
            ephemeral=True
        )
        return

    if argent < price:
        await interaction.followup.send(
            f"❌ Tu n'as pas assez d'argent.\n💰 Prix : {price} € | 💸 Ton solde : {argent} €",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🛒 Confirmer l'achat",
        description=f"Tu es sur le point d'acheter **{name}**.",
        color=discord.Color.orange()
    )
    type_str = _decrire_type(item_type, duration)
    if role is not None:
        type_str += f" : {role.mention}"
    embed.add_field(name="🏷️ Type", value=type_str, inline=False)
    embed.add_field(name="💰 Prix", value=f"{price} €")
    embed.add_field(name="💸 Solde actuel", value=f"{argent} €")
    embed.add_field(name="🧾 Solde après achat", value=f"{argent - price} €")
    embed.add_field(
        name="📅 Achats aujourd'hui",
        value="illimité" if limite_jour is None else f"{achats_du_jour}/{limite_jour}"
    )
    if item_type == 1:
        if duration is not None:
            duree = f"{duration} jours"
            # Même règle que _achat_en_transaction : la durée s'ajoute à tout
            # achat temporaire encore en cours.
            if fin_achat is not None and fin_achat > time.time():
                duree += f"\nS'ajoute à ton rôle actuel (fin prévue <t:{fin_achat}:f>)"
            embed.add_field(name="⏳ Durée", value=duree)
        elif possede and fin_achat is not None:
            embed.add_field(name="♾️ Permanent", value="Ton rôle temporaire actuel ne te sera plus retiré.")

    view = ConfirmationAchatView((name, price, item_type, valeur, duration), auteur=membre.id)
    view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)


async def _achat_en_transaction(cursor, membre: discord.Member, objet: tuple) -> tuple[bool, str]:
    """Corps de l'achat confirmé (voir _effectuer_achat) : renvoie (succès,
    message pour le membre). N'appelle jamais commit ni rollback lui-même :
    _effectuer_achat tranche selon le résultat, en un seul endroit."""
    name, price, item_type, valeur, duration = objet
    now = int(time.time())

    # Déduction atomique et conditionnelle : n'a d'effet que si le solde est
    # suffisant, ce qui évite les doubles achats en cas de clics rapides.
    # Toute première requête de la transaction : le verrou qu'elle pose sur la
    # ligne `utilisateurs` du membre jusqu'au commit sérialise ses achats
    # simultanés (deux confirmations cliquées en même temps), et les lectures
    # qui suivent voient l'état laissé par l'achat précédent. Une lecture faite
    # AVANT elle figerait un instantané antérieur à ce verrou (REPEATABLE READ) :
    # solde périmé dans les messages, et erreur 1020 dès qu'une écriture touche
    # une ligne modifiée entre-temps (voir _ERREURS_A_REJOUER).
    await cursor.execute(
        "UPDATE utilisateurs SET argent = argent - %s WHERE user_id = %s AND argent >= %s",
        (price, membre.id, price)
    )
    if cursor.rowcount == 0:
        await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (membre.id,))
        row = await cursor.fetchone()
        if row is not None or price > 0:
            argent = row[0] if row and row[0] is not None else 0
            return False, f"❌ Tu n'as pas assez d'argent.\n💰 Prix : {price} € | 💸 Ton solde : {argent} €"
        # Objet gratuit pour un membre encore absent de `utilisateurs` (jamais
        # crédité ni passé par le système d'XP) : rien à débiter, mais sa ligne
        # doit exister pour l'XP d'un objet de type 3 et pour porter le verrou
        # décrit ci-dessus.
        await cursor.execute("INSERT IGNORE INTO utilisateurs (user_id) VALUES (%s)", (membre.id,))

    # Relu en base : l'objet a pu être retiré ou modifié depuis l'affichage de la
    # confirmation. On ne vend jamais autre chose que ce que le membre a validé
    # (le débit ci-dessus est alors annulé avec le reste, voir _effectuer_achat).
    await cursor.execute(
        "SELECT price, type, valeur, duration, limite_jour FROM shop WHERE name = %s",
        (name,)
    )
    actuel = await cursor.fetchone()
    if not actuel:
        return False, f"❌ **{name}** n'est plus en vente : achat annulé, rien n'a été débité."
    if actuel[0] != price:
        return False, (
            f"❌ Le prix de **{name}** a changé depuis l'affichage ({price} € → {actuel[0]} €) : "
            "achat annulé, rien n'a été débité. Rechoisis-le dans la boutique pour l'acheter au nouveau prix."
        )
    if tuple(actuel[1:4]) != (item_type, valeur, duration):
        return False, (
            f"❌ **{name}** a été modifié depuis l'affichage : achat annulé, rien n'a été débité. "
            "Rechoisis-le dans la boutique pour voir ce qu'il contient désormais."
        )
    # Lue ici plutôt que figée à l'affichage : c'est la limite en vigueur au
    # moment de l'achat qui compte, et la réservation ci-dessous la vérifie de
    # toute façon atomiquement.
    limite_jour = actuel[4]
    if limite_jour == 0:
        return False, _ARTICLE_INDISPONIBLE

    role = None
    if item_type == 1:
        role = await _trouver_role(membre.guild, int(valeur))
        if role is None:
            return False, "❌ Rôle introuvable."

    if limite_jour is not None:
        # Réservation atomique d'un achat dans la limite du jour : la ligne est
        # d'abord garantie (INSERT IGNORE ne fait rien si elle existe), puis
        # l'UPDATE conditionnel ne l'incrémente que sous la limite — rowcount
        # fiable ici grâce à FOUND_ROWS (voir utils/database.py), comme le débit.
        # Les lignes des jours passés sont purgées par check_temp_roles.
        jour = _jour_courant()
        await cursor.execute(
            "INSERT IGNORE INTO achats_jour (user_id, item, jour, nb) VALUES (%s, %s, %s, 0)",
            (membre.id, name, jour)
        )
        await cursor.execute(
            "UPDATE achats_jour SET nb = nb + 1 WHERE user_id = %s AND item = %s AND jour = %s AND nb < %s",
            (membre.id, name, jour, limite_jour)
        )
        if cursor.rowcount != 1:
            return False, (
                f"❌ Tu as atteint la limite d'achats du jour pour **{name}** ({limite_jour}/{limite_jour}). "
                "Reviens demain !"
            )

    if item_type == 1:
        # Rôles relus depuis Discord : `membre` (interaction.user) les fige au
        # moment du clic, avant l'attente éventuelle du verrou du débit — un achat
        # du même rôle validé entre-temps n'y figurerait pas.
        possede = (await membre.guild.fetch_member(membre.id)).get_role(role.id) is not None
        # Lecture simple, puis suppression par id : une lecture verrouillante
        # (FOR UPDATE) poserait des verrous d'intervalle sur l'index
        # (user_id, role_id), qui bloquent les INSERT d'autres membres et les
        # faisaient s'interbloquer. Elle voit de toute façon tout achat précédent
        # de ce membre, validé avant que le débit ne lui rende la main.
        fin_achat, anciennes = await _achats_role(cursor, membre.id, role.id)
        refus = _refus_role(role, duration, possede, fin_achat)
        if refus:
            return False, refus

        # Une seule ligne temp_roles par (membre, rôle) : les anciennes sont
        # remplacées, pas complétées. check_temp_roles retire le rôle dès la
        # PREMIÈRE ligne échue, donc en ajouter une seconde ne prolongeait rien,
        # et en laisser une sous un rôle devenu permanent le ferait retirer quand
        # même. Nouvelle ligne (nouvel id) plutôt qu'un UPDATE de l'ancienne :
        # check_temp_roles, s'il traite justement celle-ci (échue), la verrouille
        # et la relit avant de retirer le rôle (voir check_temp_roles).
        for id_ligne in anciennes:
            await cursor.execute("DELETE FROM temp_roles WHERE id = %s", (id_ligne,))

        expires_at = None
        if duration is not None:
            # Prolongation d'un achat temporaire en cours : la durée s'ajoute à
            # son échéance au lieu de repartir de maintenant.
            debut = max(now, fin_achat) if fin_achat is not None else now
            expires_at = debut + (duration * 86400)
            await cursor.execute(
                "INSERT INTO temp_roles (user_id, role_id, end_time, origin) VALUES (%s, %s, %s, 'shop_purchase')",
                (membre.id, role.id, expires_at)
            )

        await log_transaction(cursor, membre.id, "achat", -price, f"Achat : {name}")

        # Ajouté en dernier : c'est la seule étape qu'un rollback ne peut pas
        # défaire, donc plus rien côté base ne doit pouvoir échouer après elle
        # (hormis le commit). Si elle échoue (HTTPException, ex: rôle placé
        # au-dessus de celui du bot), l'exception remonte et _effectuer_achat
        # annule tout le reste.
        await membre.add_roles(role)
        if expires_at is not None:
            logger.info(f"[DB] Rôle temporaire ajouté : user={membre.id}, role={role.id}, expires={expires_at}")

        message = f"🛒 **Achat réussi !**\n\n🎭 Rôle : **{role.name}**\n💰 Prix : **{price} €**"
        if expires_at is not None:
            message += f"\n⏳ Jusqu'au : <t:{expires_at}:f>"
        return True, message

    elif item_type == 2:
        # Upsert en une requête plutôt que SELECT puis INSERT/UPDATE : la lecture
        # simple verrait l'instantané du début de transaction, antérieur à un
        # achat du même objet validé pendant qu'on attendait le verrou du débit,
        # et l'INSERT échouerait alors sur la clé déjà créée par ce dernier.
        await cursor.execute(
            "INSERT INTO inventaire (user_id, item_id, quantite) VALUES (%s, %s, 1) "
            "ON DUPLICATE KEY UPDATE quantite = quantite + 1",
            (membre.id, valeur)
        )
        await log_transaction(cursor, membre.id, "achat", -price, f"Achat : {name}")
        return True, f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**"

    elif item_type == 3:
        await cursor.execute(
            "UPDATE utilisateurs SET xp = xp + %s WHERE user_id = %s",
            (valeur, membre.id)
        )
        await log_transaction(cursor, membre.id, "achat", -price, f"Achat : {name}")
        return True, f"🛒 **Achat réussi !**\n\n📦 Objet : **{name}**\n💰 Prix : **{price} €**"

    return False, "❌ Type d'objet inconnu dans la boutique."


async def _effectuer_achat(membre: discord.Member, objet: tuple) -> str:
    """Second temps d'un achat, au clic sur "Confirmer" (ConfirmationAchatView) :
    tout est revérifié et appliqué dans UNE transaction (voir
    _achat_en_transaction). Rien n'est engagé — ni débit, ni compteur du jour —
    tant que tout n'a pas réussi : connexion() annule tout ce qui n'a pas été
    validé. Renvoie le message à afficher au membre."""
    for essai in range(1, _ESSAIS_ACHAT + 1):
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    succes, message = await _achat_en_transaction(cursor, membre, objet)
                if succes:
                    await conn.commit()
            break
        except aiomysql.OperationalError as e:
            # Ces erreurs ne peuvent venir que d'une requête SQL, donc d'avant
            # add_roles (dernière étape) : rejouer l'achat ne peut ni attribuer
            # le rôle ni débiter deux fois.
            if e.args and e.args[0] in _ERREURS_A_REJOUER and essai < _ESSAIS_ACHAT:
                logger.warning(f"[achat] Transaction rejouée ({essai}/{_ESSAIS_ACHAT}) après l'erreur {e.args[0]} : {e}")
                continue
            logger.critical(f"Erreur SQL achat : {e}", exc_info=True)
            return "❌ Une erreur est survenue avec la base de données."
        except aiomysql.Error as e:
            logger.critical(f"Erreur SQL achat : {e}", exc_info=True)
            return "❌ Une erreur est survenue avec la base de données."
        except discord.HTTPException as e:
            # Seule étape Discord de la transaction : l'attribution du rôle (ou
            # la relecture du membre). Tout le reste a été annulé avec elle.
            logger.warning(f"[achat] Achat de {objet[0]} annulé pour user={membre.id} : {e}")
            if isinstance(e, discord.Forbidden):
                return (
                    "❌ Je n'ai pas la permission de te donner ce rôle : achat annulé, rien n'a été "
                    "débité. Préviens un membre du staff."
                )
            if isinstance(e, discord.NotFound):
                return "❌ Ce rôle (ou ton compte) est introuvable sur le serveur : achat annulé, rien n'a été débité."
            return "❌ Discord n'a pas pu t'attribuer le rôle : achat annulé, rien n'a été débité. Réessaie dans un instant."

    if succes and objet[2] == 3:
        # Écriture SQL directe sur xp en dehors du cache (utils/cache.py) :
        # on invalide plutôt que de tenter de le mettre à jour ici, pour ne
        # pas dupliquer la logique — la prochaine lecture (au message
        # suivant) revient chercher la vraie valeur en base.
        cache.invalidate_xp(membre.id)
    return message


class ConfirmationAchatView(TimedView):
    """Demande de confirmation envoyée par _proposer_achat. Message éphémère :
    seul le membre qui a choisi l'objet la voit et peut cliquer."""
    def __init__(self, objet: tuple, *, auteur: int):
        super().__init__(auteur=auteur)
        # (name, price, type, valeur, duration) tels qu'affichés au membre :
        # l'achat est annulé au clic sur Confirmer si l'objet ne correspond plus.
        self.objet = objet
        # Posé avant le moindre await : un second clic arrivé avant que les
        # boutons désactivés ne s'affichent (double clic) est ignoré au lieu de
        # lancer un second achat.
        self.traitee = False

    def _desactiver(self) -> None:
        self.traitee = True
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="✅ Confirmer", style=discord.ButtonStyle.green)
    async def confirmer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.traitee:
            await interaction.response.defer()
            return
        self._desactiver()
        await interaction.response.edit_message(view=self)

        try:
            message = await _effectuer_achat(interaction.user, self.objet)
        except Exception:
            # Filet de sécurité : sans lui, le membre resterait devant des boutons
            # désactivés sans savoir si l'achat a eu lieu. Toute erreur sortie de
            # la transaction l'a fait annuler (voir connexion() dans
            # utils/database.py) : rien n'a été débité.
            logger.exception(f"[achat] Erreur inattendue pour user={interaction.user.id}")
            message = "❌ Erreur inattendue : achat annulé, rien n'a été débité."
        # Arrêtée seulement maintenant : une vue arrêtée ne reçoit plus les clics,
        # et un double clic arrivé entre-temps resterait sans réponse ("Échec de
        # l'interaction") au lieu d'être ignoré ci-dessus. Plus rien à désactiver
        # à l'expiration (TimedView.on_timeout).
        self.stop()
        try:
            await interaction.edit_original_response(content=message, embed=None, view=self)
        except discord.HTTPException:
            # Le résultat doit parvenir au membre même si la confirmation n'est
            # plus éditable (message éphémère masqué entre-temps) : l'achat, lui,
            # a déjà été engagé ou annulé.
            await interaction.followup.send(message, ephemeral=True)

    @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.red)
    async def annuler(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.traitee:
            await interaction.response.defer()
            return
        self._desactiver()
        self.stop()
        await interaction.response.edit_message(content="❌ Achat annulé.", embed=None, view=self)


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
                    description=f"{price} €" + _decrire_limite(limite_jour, " · {}/jour", " · indisponible"),
                    value=name
                )
                for name, price, item_type, valeur, duration, limite_jour in items
            ]

        super().__init__(
            placeholder="🛒 Choisis un objet",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        # Réinitialisé en guise de réponse au clic (voir TimedView), plutôt que
        # désactivé comme avant la demande de confirmation : choisir un objet
        # n'achète plus rien à lui seul, il faut pouvoir en choisir un autre ou le
        # même à nouveau (confirmation annulée, second achat...). La suite arrive
        # en message éphémère (followup).
        await interaction.response.edit_message(view=self.view)
        if self.values[0] == "__empty__":
            await interaction.followup.send("❌ La boutique est vide.", ephemeral=True)
            return
        await _proposer_achat(interaction, self.values[0])


class BoutiqueView(TimedView):
    """Select d'achat de /boutique. Réservé à qui a ouvert la boutique : le
    message est public, et sans ça n'importe quel membre du salon pouvait
    acheter depuis la boutique d'un autre."""
    def __init__(self, items, *, auteur: int):
        super().__init__(auteur=auteur)
        self.add_item(AchatSelect(items))


async def build_boutique_display(auteur: int) -> tuple[discord.Embed, discord.ui.View | None]:
    """Construit l'embed + le select d'achat de /boutique pour le membre
    `auteur`, seul à pouvoir utiliser le select. Extrait de
    BoutiqueCog.boutique() pour être réutilisable depuis le bouton "🛒 Boutique"
    attaché à /profil (voir ProfilActionsView dans cogs/profile.py), sans dupliquer
    la requête ni la mise en forme. Vue None si la boutique n'a pas pu être lue :
    afficher « Boutique vide » sur une erreur de base de données serait trompeur."""
    embed = discord.Embed(title="🛍 Boutique", color=discord.Color.green())

    try:
        async with connexion() as conn:
            async with conn.cursor() as cursor:
                # ORDER BY : ordre stable, pour que la troncature ci-dessous écarte
                # toujours les mêmes objets.
                await cursor.execute("SELECT name, price, type, valeur, duration, limite_jour FROM shop ORDER BY name")
                items = await cursor.fetchall()
    except aiomysql.Error as e:
        logger.critical(f"Erreur SQL boutique : {e}", exc_info=True)
        embed.description = "❌ La boutique est momentanément indisponible, réessaie dans un instant."
        embed.color = discord.Color.red()
        return embed, None

    if len(items) > MAX_OBJETS_BOUTIQUE:
        logger.warning(
            f"[boutique] {len(items)} objets en vente : seuls les {MAX_OBJETS_BOUTIQUE} premiers "
            "sont affichés (limite Discord)."
        )
        embed.set_footer(text=f"Seuls les {MAX_OBJETS_BOUTIQUE} premiers objets sont affichés.")
        items = items[:MAX_OBJETS_BOUTIQUE]

    if not items:
        embed.description = "❌ Boutique vide"
    else:
        for name, price, item_type, valeur, duration, limite_jour in items:
            desc = f" **Prix :** {price} €\n **Type :** {_decrire_type(item_type, duration)}"
            if duration is not None and item_type == 1:
                desc += f"\n **Durée :** {duration} jours"
            desc += _decrire_limite(limite_jour, "\n **Limite :** {} par jour", "\n **Indisponible à l'achat**")

            embed.add_field(name=name, value=desc, inline=False)

    return embed, BoutiqueView(items, auteur=auteur)


async def build_inventaire_embed(user_id: int) -> discord.Embed:
    """Construit l'embed d'inventaire de `user_id` — l'ancienne commande
    /inventaire, désormais affichée via le select "Actions" de /profil (voir
    ProfilActionsSelect dans cogs/profile.py). Placé ici, à côté de
    build_boutique_display, pour garder le même sens d'import (profile ->
    boutique) et éviter un import circulaire.

    Laisse remonter aiomysql.Error : afficher un inventaire vide sur une erreur
    DB serait trompeur, c'est à l'appelant de signaler l'erreur."""
    embed = discord.Embed(title="🎒 Inventaire", color=discord.Color.green())

    async with connexion() as conn:
        async with conn.cursor() as cursor:
            # `inventaire.item_id` correspond à `shop.valeur` pour les objets de
            # type 2 (voir _achat_en_transaction ci-dessus) : shop.name n'est pas
            # stocké dans inventaire pour ne pas dupliquer une donnée qui peut
            # changer (renommage) ou disparaître (objet retiré de la boutique)
            # après l'achat. LEFT JOIN pour rester tolérant à ce second cas.
            await cursor.execute(
                "SELECT i.item_id, i.quantite, s.name FROM inventaire i "
                "LEFT JOIN shop s ON s.type = 2 AND s.valeur = i.item_id "
                "WHERE i.user_id = %s AND i.quantite > 0 "
                "ORDER BY i.item_id",
                (user_id,)
            )
            rows = await cursor.fetchall()

    if not rows:
        embed.description = "Ton inventaire est vide."
        return embed

    lignes = [
        f"**{name or f'Objet #{item_id} (retiré de la boutique)'}** — x{quantite}"
        for item_id, quantite, name in rows
    ]
    # Description d'embed limitée à 4096 caractères : au-delà, Discord refuse
    # l'envoi. Il faudrait des dizaines d'objets différents pour l'atteindre,
    # mais on tronque plutôt que de ne rien afficher du tout.
    affichees, taille = [], 0
    for ligne in lignes:
        taille += len(ligne) + 1
        if taille > 4000:
            break
        affichees.append(ligne)
    if len(affichees) < len(lignes):
        affichees.append(f"… et {len(lignes) - len(affichees)} autre(s) objet(s)")
    embed.description = "\n".join(affichees)
    return embed


class BoutiqueCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.check_temp_roles.start()

    def cog_unload(self):
        self.check_temp_roles.cancel()

    @tasks.loop(minutes=5)
    async def check_temp_roles(self):
        """Retire automatiquement les rôles temporaires achetés en boutique une
        fois expirés, et purge les compteurs d'achats des jours passés."""
        try:
            now = int(time.time())

            async with connexion() as conn:
                async with conn.cursor() as c:
                    # Purge des compteurs de shop.limite_jour (voir achats_jour dans
                    # utils/setupdatabase.py) : seules les lignes du jour servent
                    # encore. Faite ici plutôt que pendant les achats : un DELETE par
                    # plage y posait des verrous d'intervalle partagés entre
                    # membres, qui faisaient s'interbloquer deux achats simultanés.
                    await c.execute("DELETE FROM achats_jour WHERE jour < %s", (_jour_courant(),))
                    await c.execute(
                        "SELECT id, user_id, role_id FROM temp_roles WHERE origin = 'shop_purchase' AND end_time <= %s",
                        (now,)
                    )
                    expired = await c.fetchall()
                # Chaque rôle est ensuite traité dans sa propre transaction.
                await conn.commit()

                if not expired:
                    return

                guild_id = get_config("GUILD_ID")
                guild = None
                if guild_id:
                    guild = self.bot.get_guild(int(guild_id))
                    if guild is None:
                        try:
                            guild = await self.bot.fetch_guild(int(guild_id))
                        except discord.HTTPException:
                            guild = None

                for row_id, user_id, role_id in expired:
                    async with conn.cursor() as c:
                        # Relue en la verrouillant jusqu'au commit : un achat qui
                        # prolonge ou rend permanent ce rôle remplace cette ligne
                        # (voir _achat_en_transaction). S'il a été validé depuis la
                        # liste ci-dessus, elle a disparu et le rôle, payé, ne doit
                        # pas être retiré ; s'il est en cours, il attend ce commit
                        # avant de la remplacer, puis rajoute le rôle.
                        await c.execute("SELECT 1 FROM temp_roles WHERE id = %s FOR UPDATE", (row_id,))
                        if await c.fetchone() is None:
                            await conn.rollback()
                            continue
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
        embed, view = await build_boutique_display(interaction.user.id)
        if view is None:
            await interaction.followup.send(embed=embed)
            return
        view.message = await interaction.followup.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(BoutiqueCog(bot))
