"""Vue de base réutilisable pour les messages temporaires (non persistants) —
voir TimedView. Toute vue enregistrée globalement au démarrage via bot.add_view
(voir cogs/events.py : tickets, recrutement, trade, contestations) reste en
dehors de ce mécanisme : elle a besoin de timeout=None pour survivre à un
redémarrage du bot et ne doit pas en hériter.
"""
import discord


class TimedView(discord.ui.View):
    """Désactive automatiquement tous les composants de la vue après `timeout`
    secondes (5 minutes par défaut) : sans ça, les boutons/select restent
    visuellement cliquables indéfiniment sur un message temporaire (carte de
    profil, boutique, sélection d'un don...) alors que la vue ne réagit plus
    une fois expirée côté discord.py — l'utilisateur voit "Cette interaction a
    échoué" plutôt qu'un composant clairement désactivé, et le bot garde en
    mémoire une vue qui ne sert plus à rien jusqu'à expiration.

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

    def __init__(self, *, timeout: float = 300):
        super().__init__(timeout=timeout)
        self.message: discord.Message | None = None

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
