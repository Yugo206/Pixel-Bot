"""Vues de base réutilisables :
  - TimedView pour les messages temporaires (carte de profil, boutique,
    sélection d'un don...), réservés à l'utilisateur qui les a ouverts ;
  - VuePersistante pour les vues enregistrées globalement au démarrage via
    bot.add_view (voir cogs/events.py : tickets, recrutement, trade,
    contestations), qui ont besoin de timeout=None pour survivre à un
    redémarrage du bot ;
  - Modale, base des formulaires, pour qu'une erreur pendant l'envoi se
    traduise par un message clair plutôt que « Cette interaction a échoué ».
Plus quelques outils pour les boutons à usage unique (voir reserver_clic et
copie_vue plus bas).
"""
import logging
from collections import OrderedDict

import discord

from utils.autorisations import MSG_ERREUR, repondre, verifier_auteur

logger = logging.getLogger(__name__)


async def _signaler_erreur(interaction: discord.Interaction, error: Exception, origine: str) -> None:
    """Journalise l'erreur (alerte MP à l'owner, voir utils/error_handler.py)
    et prévient l'utilisateur en éphémère, au lieu de le laisser face à
    « Cette interaction a échoué » sans savoir si son action a été prise en
    compte."""
    logger.error(f"[{origine}] Erreur pour {interaction.user.id} : {error}", exc_info=error)
    await repondre(interaction, MSG_ERREUR)


class TimedView(discord.ui.View):
    """Désactive automatiquement tous les composants de la vue après `timeout`
    secondes (5 minutes par défaut) : sans ça, les boutons/select restent
    visuellement cliquables indéfiniment sur un message temporaire (carte de
    profil, boutique, sélection d'un don...) alors que la vue ne réagit plus
    une fois expirée côté discord.py — l'utilisateur voit "Cette interaction a
    échoué" plutôt qu'un composant clairement désactivé, et le bot garde en
    mémoire une vue qui ne sert plus à rien jusqu'à expiration.

    `auteur` : id de l'utilisateur à qui appartient la vue (celui qui a lancé
    la commande). Tout autre utilisateur est refusé avant le moindre effet —
    indispensable dès que le message est public (carte de /profil, /boutique) :
    sans ça, n'importe quel membre qui voit le message peut utiliser les menus
    à la place de son auteur (donner de l'argent « depuis » le profil d'un
    autre, acheter depuis sa boutique...). None uniquement pour une vue qui
    doit rester ouverte à tous.

    `self.message` doit être renseigné par l'appelant juste après l'envoi (le
    message renvoyé par interaction.followup.send(), ou obtenu via
    interaction.original_response() quand la vue accompagne
    interaction.response.send_message() — jamais interaction.message, qui ne
    peut pas être édité si le message est éphémère).

    Les select de la vue doivent se réinitialiser après un choix : sans ça,
    Discord affiche l'option choisie comme sélectionnée en permanence et un même
    choix ne peut pas être refait tant que le message n'a pas été réédité. Le
    plus sûr est de répondre au choix par interaction.response.edit_message(
    view=self.view) (voir AchatSelect dans cogs/boutique.py, ProfilActionsSelect
    dans cogs/profile.py) : le jeton du clic est neuf. Quand la réponse doit
    être autre chose (une modale, voir DestinataireSelect dans cogs/profile.py),
    rafraichir() réédite self.message à la place."""

    def __init__(self, *, auteur: int | None, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.auteur = auteur
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await verifier_auteur(interaction, self.auteur)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _signaler_erreur(interaction, error, type(self).__name__)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        await self.rafraichir()

    async def rafraichir(self) -> None:
        """Réédite self.message avec la vue dans son état actuel (composants
        désactivés à l'expiration, choix d'un select vidé — voir plus haut).
        Sans effet si ce n'est plus possible : le jeton qui permet d'éditer un
        message envoyé en réponse à une interaction expire 15 min après l'envoi,
        alors que chaque clic repousse l'expiration de la vue — l'erreur
        remonterait sinon jusqu'au propriétaire en MP (voir
        utils/error_handler.py)."""
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


class VuePersistante(discord.ui.View):
    """Base des vues enregistrées au démarrage (bot.add_view, voir
    cogs/events.py). Une seule instance répond aux clics de TOUS les messages
    qui portent ses custom_id : elle ne doit donc jamais garder d'état propre à
    un message (bouton désactivé, choix fait...). Pour modifier les boutons d'un
    message précis, construire une nouvelle instance avec copie_vue() plutôt
    que de modifier self, sinon l'état fuit vers les autres messages.

    Chaque sous-classe vérifie dans interaction_check QUI peut cliquer (staff,
    propriétaire du ticket, destinataire du MP...) : voir utils/autorisations.py."""

    def __init__(self):
        super().__init__(timeout=None)

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await _signaler_erreur(interaction, error, type(self).__name__)


class Modale(discord.ui.Modal):
    """Base des formulaires du bot : même traitement d'erreur que les vues
    ci-dessus. S'utilise comme discord.ui.Modal
    (`class MaModale(Modale, title="...")`)."""

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await _signaler_erreur(interaction, error, type(self).__name__)


# ---------------------------------------------------------------------------
# Boutons à usage unique
# ---------------------------------------------------------------------------
# Désactiver un bouton après un clic ne suffit pas à empêcher un double
# traitement : deux clics rapprochés (double-clic, deux modérateurs en même
# temps) sont tous deux transmis au bot avant que le message ne soit réédité, et
# discord.py ne regarde pas l'état `disabled` avant d'appeler le callback. Quand
# l'action laisse une trace en base, la protection la plus sûre reste un UPDATE
# ou DELETE conditionnel dont on vérifie le rowcount (voir ModoView dans
# cogs/tickets.py). Sinon, reserver_clic() ci-dessous sert de verrou en mémoire.
_CLICS_RESERVES: "OrderedDict[tuple[int, str], None]" = OrderedDict()
_MAX_CLICS_RESERVES = 5000


def composant_desactive(message: discord.Message | None, custom_id: str) -> bool:
    """Vrai si le composant `custom_id` est désactivé dans le message tel que
    Discord l'a transmis avec l'interaction (état persistant, qui survit à un
    redémarrage du bot contrairement à _CLICS_RESERVES)."""
    if message is None:
        return False
    for ligne in message.components:
        for composant in getattr(ligne, "children", [ligne]):
            if getattr(composant, "custom_id", None) == custom_id:
                return bool(getattr(composant, "disabled", False))
    return False


def reserver_clic(interaction: discord.Interaction, cle: str | None = None) -> bool:
    """Réserve l'action `cle` (par défaut le custom_id cliqué) sur le message de
    l'interaction. True pour le premier clic seulement ; False si l'action a
    déjà été réservée (clic en double) ou si le bouton est déjà désactivé sur le
    message. Appeler liberer_clic() si l'action échoue, pour permettre de
    réessayer."""
    message = interaction.message
    custom_id = (interaction.data or {}).get("custom_id", "")
    cle = cle or custom_id
    if message is None:
        return True
    # `cle` aussi : depuis une modale, custom_id est celui de la modale, et la
    # clé nomme alors le bouton qui l'a ouverte (voir AvisModal, cogs/tickets.py).
    if any(composant_desactive(message, c) for c in {custom_id, cle} if c):
        return False
    k = (message.id, cle)
    if k in _CLICS_RESERVES:
        return False
    _CLICS_RESERVES[k] = None
    # Borne la mémoire : les réservations les plus anciennes concernent des
    # messages dont les boutons sont depuis longtemps désactivés sur Discord
    # (composant_desactive prend alors le relais).
    while len(_CLICS_RESERVES) > _MAX_CLICS_RESERVES:
        _CLICS_RESERVES.popitem(last=False)
    return True


def liberer_clic(interaction: discord.Interaction, cle: str | None = None) -> None:
    """Annule une réservation faite par reserver_clic() (action échouée)."""
    if interaction.message is None:
        return
    cle = cle or (interaction.data or {}).get("custom_id", "")
    _CLICS_RESERVES.pop((interaction.message.id, cle), None)


def copie_vue(cls: type[discord.ui.View], message: discord.Message | None,
              desactiver: set[str] | None = None, *args, **kwargs) -> discord.ui.View:
    """Nouvelle instance de la vue persistante `cls`, destinée uniquement à
    rééditer `message` : reprend les boutons déjà désactivés sur ce message,
    désactive en plus ceux de `desactiver` (custom_id ; None = tous), et ne
    touche jamais à l'instance partagée enregistrée par bot.add_view (voir
    VuePersistante).

    La copie est aussitôt arrêtée (stop()) : discord.py ne l'enregistre alors
    pas pour ce message, et les prochains clics continuent d'arriver à
    l'instance partagée, qui fait toutes les vérifications."""
    vue = cls(*args, **kwargs)
    for item in vue.children:
        custom_id = getattr(item, "custom_id", None)
        if custom_id is None:
            continue
        if desactiver is None or custom_id in desactiver or composant_desactive(message, custom_id):
            item.disabled = True
    vue.stop()
    return vue
