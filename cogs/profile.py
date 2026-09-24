import logging
import time
import discord
import aiomysql
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
load_dotenv()


from utils.database import connexion
from utils import cache
from utils.config import get_config
from utils.profile_card import generate_profile_card
from utils.transactions import log_transaction
from utils.views import Modale, TimedView
from cogs.boutique import build_boutique_display, build_inventaire_embed

logger = logging.getLogger(__name__)

# Colonnes autorisées pour le classement (voir _construire_classement) : whitelist
# revalidée juste avant l'interpolation SQL (même principe que ajouter_rarete dans
# utils/database.py), même si les seules valeurs possibles viennent déjà des
# boutons de ClassementView.
COLONNES_CLASSEMENT = {"argent", "xp"}

# Récompense et délai de /daily, configurables via la table `config` (voir
# utils/config.py) comme le reste des réglages autrefois en .env. Lu au niveau
# module : ce cog n'est jamais importé avant load_config() dans start.py
# (contrairement à cogs/tickets.py, importé directement — voir _owner_mention).
DAILY_REWARD = int(get_config("DAILY_REWARD", "50"))
DAILY_COOLDOWN = int(get_config("DAILY_COOLDOWN", str(24 * 3600)))

# Jeux/plateformes détectables via rôle pour le bouton "Personnaliser mon profil"
# (voir PersonnaliserButton). Chaque entrée : le rôle qui déclenche la proposition
# ("role_env" = clé de la table `config`, optionnelle — absente = jeu désactivé
# sur ce serveur, voir utils/config.py), et les questions fixes posées dans le
# modal ((clé DB, libellé affiché), ...).
# iPhone/Android volontairement exclus : ce sont des rôles d'appareil, pas de jeu,
# aucune question évidente à poser dessus.
JEUX_PLATEFORMES = [
    {"id": "pc", "label": "🖥️ PC", "role_env": "ROLE_PC",
     "questions": [("pc_pseudo", "Pseudo Steam / Battle.net / Epic")]},
    {"id": "xbox", "label": "🎮 Xbox", "role_env": "ROLE_XBOX",
     "questions": [("xbox_gamertag", "Gamertag Xbox")]},
    {"id": "playstation", "label": "🎮 PlayStation", "role_env": "ROLE_PLAYSTATION",
     "questions": [("psn", "Pseudo PSN")]},
    {"id": "nintendo", "label": "🎮 Nintendo", "role_env": "ROLE_NINTENDO",
     "questions": [("switch_code", "Code ami Switch")]},
    {"id": "fortnite", "label": "🔫 Fortnite", "role_env": "ROLE_FORTNITE",
     "questions": [("fortnite_niveau", "Ton niveau Fortnite")]},
    {"id": "minecraft", "label": "⛏️ Minecraft", "role_env": "ROLE_MINECRAFT",
     "questions": [("minecraft_pseudo", "Pseudo Minecraft")]},
    {"id": "brawlstars", "label": "⭐ Brawl Stars", "role_env": "ROLE_BRAWLSTARS",
     "questions": [("brawlstars_tag", "Tag Brawl Stars (#XXXXXXX)")]},
    {"id": "gta", "label": "🚗 GTA", "role_env": "ROLE_GTA",
     "questions": [("gta_pseudo", "Pseudo GTA / Rockstar Social Club")]},
    {"id": "roblox", "label": "🧱 Roblox", "role_env": "ROLE_ROBLOX",
     # Le label d'un TextInput de modal est limité à 45 caractères côté Discord :
     # l'ancien libellé ("Tu joues souvent ? (rarement / parfois / souvent)", 49
     # caractères) faisait échouer l'envoi du modal (HTTPException), donc le clic
     # sur "🧱 Roblox" ne faisait jamais rien de visible pour le membre.
     "questions": [("roblox_pseudo", "Pseudo Roblox"),
                   ("roblox_frequence", "Fréquence (rarement / parfois / souvent)")]},
]

# Reverse-map clé DB -> libellé, pour afficher les réponses sur /profil sans reparcourir
# JEUX_PLATEFORMES à chaque fois.
CLE_LABELS = {cle: label for jeu in JEUX_PLATEFORMES for cle, label in jeu["questions"]}

# Plus grand montant qu'un don peut transférer : plafond de la colonne
# utilisateurs.argent (INT signé). Au-delà, le débit ne pourrait de toute façon
# jamais passer, et le montant ne tiendrait pas dans la colonne du destinataire.
MONTANT_DON_MAX = 2_147_483_647


def _refus_destinataire(donneur: discord.abc.User, destinataire: discord.abc.User) -> str | None:
    """Message de refus si `destinataire` ne peut pas recevoir de don de
    `donneur`, None sinon. Vérifié AVANT de demander le montant (voir
    DestinataireSelect) : le membre ne remplit pas un formulaire pour rien."""
    if destinataire.id == donneur.id:
        return "❌ Tu ne peux pas te donner de l'argent à toi-même."
    if destinataire.bot:
        return "❌ Impossible de donner de l'argent à un bot."
    return None


def _jeux_disponibles(member: discord.Member) -> list:
    """Renvoie les jeux/plateformes de JEUX_PLATEFORMES configurés (clé présente
    dans la table `config`) et dont `member` possède le rôle correspondant."""
    disponibles = []
    for jeu in JEUX_PLATEFORMES:
        role_id_raw = get_config(jeu["role_env"])
        if not role_id_raw:
            continue
        role = member.guild.get_role(int(role_id_raw))
        if role is not None and role in member.roles:
            disponibles.append(jeu)
    return disponibles


async def _solde(user_id: int) -> int:
    """Solde actuel de `user_id`, 0 s'il n'a encore aucune ligne."""
    async with connexion() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (user_id,))
            row = await cursor.fetchone()
    return row[0] if row and row[0] is not None else 0


def _msg_solde_insuffisant(solde: int) -> str:
    return f"❌ Tu n'as pas assez d'argent.\n💸 Ton solde : {solde} €"


async def _effectuer_don(interaction: discord.Interaction, destinataire: discord.abc.User, montant: int) -> None:
    """Logique de transfert d'argent, partagée entre /donner et le select "Actions"
    attaché à /profil (voir MontantDonModal ci-dessous). Suppose que l'interaction
    est déjà déférée en ephemeral — envoie elle-même la réponse finale (succès ou
    erreur) via followup."""
    refus = _refus_destinataire(interaction.user, destinataire)
    if refus:
        await interaction.followup.send(refus, ephemeral=True)
        return

    try:
        # Solde vérifié avant toute écriture : un don refusé faute d'argent ne
        # doit rien laisser derrière lui (pas même la ligne du destinataire
        # créée ci-dessous). Revérifié de toute façon par le débit conditionnel.
        solde = await _solde(interaction.user.id)
        if solde < montant:
            await interaction.followup.send(_msg_solde_insuffisant(solde), ephemeral=True)
            return

        # Ligne du destinataire créée à part, validée avant le transfert (même
        # raison que pour /daily) : deux dons simultanés à un membre encore
        # absent de `utilisateurs` s'interbloquaient en la créant.
        async with connexion() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("INSERT IGNORE INTO utilisateurs (user_id) VALUES (%s)", (destinataire.id,))
            await conn.commit()

        async with connexion() as conn:
            async with conn.cursor() as cursor:
                # Les deux lignes verrouillées d'abord, toujours dans le même ordre
                # (id croissant) : sans ça, A qui donne à B pendant que B donne à A
                # verrouillaient chacun sa propre ligne puis attendaient celle de
                # l'autre, et l'un des deux dons échouait en erreur 1213
                # (« Deadlock found »). Ici, le second attend simplement le premier.
                await cursor.execute(
                    "SELECT user_id FROM utilisateurs WHERE user_id IN (%s, %s) ORDER BY user_id FOR UPDATE",
                    (interaction.user.id, destinataire.id)
                )
                # Déduction atomique et conditionnelle (même principe que l'achat en
                # boutique, voir AchatSelect.callback dans cogs/boutique.py) : n'a
                # d'effet que si le solde de l'expéditeur est suffisant, ce qui évite
                # un double envoi en cas de double clic/appel rapide.
                await cursor.execute(
                    "UPDATE utilisateurs SET argent = argent - %s WHERE user_id = %s AND argent >= %s",
                    (montant, interaction.user.id, montant)
                )
                debite = cursor.rowcount == 1

                if debite:
                    # Upsert plutôt qu'un simple UPDATE, par sécurité : si la ligne
                    # du destinataire avait disparu entre-temps, le don serait
                    # débité chez l'expéditeur sans jamais être crédité.
                    await cursor.execute(
                        "INSERT INTO utilisateurs (user_id, argent) VALUES (%s, %s) "
                        "ON DUPLICATE KEY UPDATE argent = COALESCE(argent, 0) + %s",
                        (destinataire.id, montant, montant)
                    )
                    await log_transaction(cursor, interaction.user.id, "don_envoye", -montant, f"Don à {destinataire}")
                    await log_transaction(cursor, destinataire.id, "don_recu", montant, f"Don de {interaction.user}")
                    await conn.commit()

        if not debite:
            # Solde dépensé entre-temps (autre don, achat simultané...).
            await interaction.followup.send(_msg_solde_insuffisant(await _solde(interaction.user.id)), ephemeral=True)
            return
    except aiomysql.Error as e:
        logger.critical(f"[donner] Erreur DB : {e}", exc_info=True)
        await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
        return

    await interaction.followup.send(f"✅ Tu as donné **{montant} €** à {destinataire.mention}.", ephemeral=True)
    try:
        await destinataire.send(f"💸 {interaction.user.mention} t'a donné **{montant} €** sur Pixel Party !")
    except discord.HTTPException:  # MP fermés : le don est fait quand même
        pass


class JeuModal(Modale):
    def __init__(self, jeu: dict, valeurs_existantes: dict):
        super().__init__(title=f"Personnalisation — {jeu['label']}")
        self.champs = []
        for cle, label in jeu["questions"]:
            champ = discord.ui.TextInput(
                label=label,
                required=False,
                max_length=100,
                default=valeurs_existantes.get(cle)
            )
            self.champs.append((cle, champ))
            self.add_item(champ)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    for cle, champ in self.champs:
                        valeur = champ.value.strip() if champ.value else ""
                        if valeur:
                            await cursor.execute(
                                "INSERT INTO profil_extra (user_id, cle, valeur) VALUES (%s, %s, %s) "
                                "ON DUPLICATE KEY UPDATE valeur = VALUES(valeur)",
                                (interaction.user.id, cle, valeur)
                            )
                        else:
                            # Champ vidé volontairement : on supprime plutôt que de garder une
                            # valeur vide, pour que le champ disparaisse de /profil.
                            await cursor.execute(
                                "DELETE FROM profil_extra WHERE user_id = %s AND cle = %s",
                                (interaction.user.id, cle)
                            )
                await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[profil:personnaliser] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send(
                "❌ Une erreur est survenue avec la base de données : ton profil n'a pas été modifié.",
                ephemeral=True
            )
            return
        await interaction.followup.send("✅ Ton profil a été mis à jour !", ephemeral=True)


class JeuButton(discord.ui.Button):
    def __init__(self, jeu: dict):
        super().__init__(label=jeu["label"], style=discord.ButtonStyle.blurple)
        self.jeu = jeu

    async def callback(self, interaction: discord.Interaction):
        cles = [cle for cle, _ in self.jeu["questions"]]
        # connexion() : ces valeurs pré-remplissent la modale et sont réécrites
        # telles quelles à la validation. Lues sur une image figée, elles
        # remettaient en place une réponse que le membre venait de changer.
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    placeholders = ",".join(["%s"] * len(cles))
                    await cursor.execute(
                        f"SELECT cle, valeur FROM profil_extra WHERE user_id = %s AND cle IN ({placeholders})",
                        (interaction.user.id, *cles)
                    )
                    valeurs_existantes = dict(await cursor.fetchall())
        except aiomysql.Error as e:
            logger.critical(f"[profil:personnaliser] Erreur DB : {e}", exc_info=True)
            await interaction.response.send_message(
                "❌ Une erreur est survenue avec la base de données, réessaie dans un instant.", ephemeral=True
            )
            return
        await interaction.response.send_modal(JeuModal(self.jeu, valeurs_existantes))


class PersonnalisationView(TimedView):
    def __init__(self, jeux: list, *, auteur: int):
        super().__init__(auteur=auteur)
        for jeu in jeux:
            self.add_item(JeuButton(jeu))


class MontantDonModal(Modale, title="Donner de l'argent"):
    montant = discord.ui.TextInput(label="Montant (€)", placeholder="Ex: 50", max_length=10)

    def __init__(self, destinataire: discord.abc.User):
        super().__init__()
        self.destinataire = destinataire

    async def on_submit(self, interaction: discord.Interaction):
        # Espaces tolérés comme séparateurs de milliers (« 1 000 ») ; pas le
        # point, qui pourrait être une virgule décimale (« 1.5 » donnerait 15).
        valeur = "".join(self.montant.value.split())
        if not valeur.isascii() or not valeur.isdigit() or int(valeur) <= 0:
            await interaction.response.send_message(
                "❌ Montant invalide : indique un nombre entier positif (ex : 50).", ephemeral=True
            )
            return
        if int(valeur) > MONTANT_DON_MAX:
            await interaction.response.send_message(
                f"❌ Montant trop élevé : {MONTANT_DON_MAX} € au maximum par don.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        await _effectuer_don(interaction, self.destinataire, int(valeur))


class DestinataireSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Choisis le membre à qui donner de l'argent", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        destinataire = self.values[0]
        refus = _refus_destinataire(interaction.user, destinataire)
        if refus:
            # Refusé avant la modale : inutile de demander un montant pour un
            # don impossible (soi-même, un bot). La réponse au clic réinitialise
            # le select (voir TimedView), le refus suit en éphémère.
            await interaction.response.edit_message(view=self.view)
            await interaction.followup.send(refus, ephemeral=True)
            return
        await interaction.response.send_modal(MontantDonModal(destinataire))
        # Réinitialise le select (voir TimedView) : sans ça, Discord garde le
        # membre choisi affiché comme sélectionné et le même choix ne peut pas
        # être refait (ex: modale annulée, on veut retenter avec le même membre).
        # Via rafraichir() : la modale occupe déjà la réponse à ce clic.
        await self.view.rafraichir()


class DestinataireView(TimedView):
    def __init__(self, *, auteur: int):
        super().__init__(auteur=auteur)
        self.add_item(DestinataireSelect())


async def _construire_historique(user_id: int) -> discord.Embed:
    """Embed des 20 dernières transactions de `user_id` (select "Actions" de
    /profil). Laisse remonter aiomysql.Error, signalée par l'appelant."""
    async with connexion() as conn:
        async with conn.cursor() as cursor:
            # LIMIT 20 : voir table `transactions` (utils/setupdatabase.py) —
            # un pur journal, jamais relu pour recalculer un solde.
            await cursor.execute(
                "SELECT type, montant, detail, created_at FROM transactions "
                "WHERE user_id = %s ORDER BY created_at DESC, id DESC LIMIT 20",
                (user_id,)
            )
            rows = await cursor.fetchall()

    embed = discord.Embed(title="📜 Historique des transactions", color=discord.Color.green())
    if not rows:
        embed.description = "Aucune transaction pour l'instant."
        return embed

    lignes = []
    for _type, montant, detail, created_at in rows:
        signe = "+" if montant > 0 else ""
        # Détail tronqué à 150 caractères : au plus ~40 caractères fixes par ligne
        # (date, montant, séparateurs) + 150 + le saut de ligne, soit ≤ 3800 pour
        # 20 lignes — sous la limite Discord de 4096 pour une description d'embed,
        # même si un type de transaction journalise un détail long (la colonne
        # en accepte 255).
        detail = detail or ""
        if len(detail) > 150:
            detail = detail[:149] + "…"
        lignes.append(f"<t:{created_at}:d> — **{signe}{montant} €** — {detail}")
    embed.description = "\n".join(lignes)
    return embed


async def _construire_classement(colonne: str, user_id: int) -> discord.Embed:
    """Embed du top 10 par `colonne` (argent ou xp), suivi de la position de
    `user_id` s'il n'y figure pas. Laisse remonter aiomysql.Error, signalée par
    l'appelant."""
    if colonne not in COLONNES_CLASSEMENT:
        raise ValueError(f"Colonne de classement non autorisée : {colonne}")

    async with connexion() as conn:
        async with conn.cursor() as cursor:
            # colonne : validée juste au-dessus contre COLONNES_CLASSEMENT, sûre à interpoler.
            await cursor.execute(
                f"SELECT user_id, {colonne} FROM utilisateurs ORDER BY {colonne} DESC LIMIT 10"
            )
            rows = await cursor.fetchall()

            position = None
            if rows and user_id not in {uid for uid, _ in rows}:
                await cursor.execute(f"SELECT {colonne} FROM utilisateurs WHERE user_id = %s", (user_id,))
                row = await cursor.fetchone()
                valeur_membre = row[0] if row and row[0] is not None else 0
                # Même calcul que le rang XP de /profil : les ex æquo partagent la
                # même position.
                await cursor.execute(
                    f"SELECT COUNT(*) + 1 FROM utilisateurs WHERE {colonne} > %s", (valeur_membre,)
                )
                (position,) = await cursor.fetchone()

    if colonne == "argent":
        titre, unite = "💰 Classement — Argent", "€"
    else:
        titre, unite = "✨ Classement — Expérience", "XP"

    if not rows:
        return discord.Embed(title=titre, description="Personne à classer pour le moment.", color=discord.Color.green())

    medailles = ["🥇", "🥈", "🥉"]
    lignes = []
    for i, (uid, valeur) in enumerate(rows):
        rang = medailles[i] if i < len(medailles) else f"**{i + 1}.**"
        lignes.append(f"{rang} <@{uid}> — {valeur or 0} {unite}")
    if position is not None:
        lignes.append(f"\n📍 Ta position : **{position}e** — {valeur_membre} {unite}")
    return discord.Embed(title=titre, description="\n".join(lignes), color=discord.Color.green())


class ClassementView(TimedView):
    """Boutons du classement (select "Actions" de /profil) : basculent entre
    argent et XP en rééditant le même message, le bouton du classement affiché
    étant désactivé."""
    def __init__(self, colonne: str, *, auteur: int):
        super().__init__(auteur=auteur)
        self._marquer_actif(colonne)

    def _marquer_actif(self, colonne: str) -> None:
        self.bouton_argent.disabled = colonne == "argent"
        self.bouton_xp.disabled = colonne == "xp"

    async def _basculer(self, interaction: discord.Interaction, colonne: str) -> None:
        await interaction.response.defer()
        try:
            embed = await _construire_classement(colonne, interaction.user.id)
        except aiomysql.Error as e:
            logger.critical(f"[classement] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return
        self._marquer_actif(colonne)
        # Via le jeton de ce clic plutôt que self.message.edit() : celui qui a
        # servi à envoyer self.message expire 15 min après l'envoi, alors que
        # chaque clic repousse l'expiration de la vue (TimedView).
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="💰 Argent", style=discord.ButtonStyle.green)
    async def bouton_argent(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._basculer(interaction, "argent")

    @discord.ui.button(label="✨ XP", style=discord.ButtonStyle.blurple)
    async def bouton_xp(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._basculer(interaction, "xp")


async def _donnees_profil(user_id: int) -> tuple[int, int, int]:
    """(argent, xp, rang) affichés sur la carte de /profil. Lève aiomysql.Error."""
    async with connexion() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (user_id,))
            result = await cursor.fetchone()
    argent = result[0] if result and result[0] is not None else 0
    # Via le cache mémoire (utils/cache.py), comme /niveau : sinon un nouveau
    # membre voit 0 XP ici puis une valeur différente (40, la valeur de
    # seed du cache) dès qu'il utilise /niveau, uniquement selon l'ordre
    # dans lequel il tape les deux commandes.
    xp = await cache.get_xp(user_id)

    # Rang XP : utilisateurs.xp est toujours à jour en base (bump_xp dans
    # on_message, cogs/events.py, écrit en DB dans le même handler), donc
    # une lecture directe ici est fiable sans passer par le cache.
    async with connexion() as conn:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT COUNT(*) + 1 FROM utilisateurs WHERE xp > %s", (xp,))
            (rang,) = await cursor.fetchone()
    return argent, xp, rang


async def _contenu_profil(membre: discord.Member | discord.User, argent: int, xp: int, rang: int) -> dict:
    """Contenu du message /profil : la carte ({"file": ...}), ou un embed texte
    ({"embed": ...}) si elle n'a pas pu être générée."""
    try:
        return {"file": await generate_profile_card(membre, argent, xp, rang)}
    except Exception as e:
        # Avatar illisible (erreur passagère du CDN Discord) ou rendu Pillow
        # en échec : le profil s'affiche quand même, en texte, plutôt que de
        # laisser la commande sans réponse.
        logger.error(f"[profil] Carte de profil non générée pour {membre.id} : {e}", exc_info=True)
        embed = discord.Embed(title=f"Profil de {membre.display_name}", color=discord.Color.green())
        embed.set_thumbnail(url=membre.display_avatar.url)
        embed.add_field(name="💰 Argent", value=f"{argent} €")
        embed.add_field(name="✨ Niveau", value=f"{Profile.get_level(xp)} ({xp} XP)")
        # Même format que la carte (voir utils/profile_card.py).
        embed.add_field(name="🏆 Rang", value=f"#{rang} sur le serveur")
        return {"embed": embed}


def _resume_actualisation(gain_argent: int, gain_xp: int) -> str:
    """Ce qui a changé depuis le dernier affichage de la carte (« +15 XP · -30 € »)."""
    morceaux = []
    if gain_xp:
        morceaux.append(f"{gain_xp:+} XP")
    if gain_argent:
        morceaux.append(f"{gain_argent:+} €")
    if not morceaux:
        return "🔄 Profil actualisé : rien de nouveau depuis le dernier affichage."
    return f"🔄 Profil actualisé : {' · '.join(morceaux)} depuis le dernier affichage."


class ProfilActionsSelect(discord.ui.Select):
    """Select "Actions" attaché à /profil (historique, inventaire, classement,
    don — les deux du milieu remplacent les anciennes commandes /inventaire et
    /classement —, et actualisation de la carte). Réservé à qui a lancé /profil
    (voir ProfilActionsView) : la carte est publique, mais un autre membre qui
    clique dessus est renvoyé vers sa propre commande. Les résultats
    s'affichent en éphémère."""
    def __init__(self):
        super().__init__(
            placeholder="💰 Actions",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Historique des transactions", value="historique", emoji="📜"),
                discord.SelectOption(label="Inventaire", value="inventaire", emoji="🎒"),
                discord.SelectOption(label="Classement", value="classement", emoji="🏆"),
                discord.SelectOption(label="Donner de l'argent", value="donner", emoji="💸"),
                discord.SelectOption(label="Actualiser mon profil", value="actualiser", emoji="🔄"),
            ],
            row=0,
        )

    async def _actualiser(self, interaction: discord.Interaction) -> None:
        """Régénère la carte sur place, puis dit en éphémère ce qui a changé
        depuis son dernier affichage (XP, argent)."""
        # Mise à jour différée du message de la carte : la regénérer (avatar,
        # rendu Pillow) peut dépasser les 3 secondes laissées pour répondre.
        await interaction.response.defer()
        try:
            argent, xp, rang = await _donnees_profil(interaction.user.id)
        except aiomysql.Error as e:
            logger.critical(f"[profil_actions] Erreur DB (actualiser) : {e}", exc_info=True)
            await interaction.edit_original_response(view=self.view)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        contenu = await _contenu_profil(interaction.user, argent, xp, rang)
        # Carte en fichier joint ou texte de secours en embed : chacun remplace
        # l'autre, sans quoi l'ancienne carte resterait affichée à côté.
        if "file" in contenu:
            await interaction.edit_original_response(attachments=[contenu["file"]], embed=None, view=self.view)
        else:
            await interaction.edit_original_response(attachments=[], embed=contenu["embed"], view=self.view)

        vue = self.view
        message = _resume_actualisation(argent - vue.argent, xp - vue.xp)
        vue.argent, vue.xp = argent, xp
        await interaction.followup.send(message, ephemeral=True)

    async def callback(self, interaction: discord.Interaction):
        choix = self.values[0]
        if choix == "actualiser":
            await self._actualiser(interaction)
            return

        # Réinitialise d'abord ce select sur la carte /profil (voir TimedView),
        # en guise de réponse au clic : sans ça, Discord le garde affiché sur le
        # choix fait et le même choix ne peut pas être refait (ex: reconsulter
        # l'historique juste après, ou redonner de l'argent une seconde fois).
        # Le résultat suit en message éphémère (followup).
        await interaction.response.edit_message(view=self.view)
        if choix == "donner":
            view = DestinataireView(auteur=interaction.user.id)
            view.message = await interaction.followup.send(
                "Choisis à qui donner de l'argent :", view=view, ephemeral=True
            )
            return

        try:
            if choix == "historique":
                embed = await _construire_historique(interaction.user.id)
                await interaction.followup.send(embed=embed, ephemeral=True)
            elif choix == "inventaire":
                embed = await build_inventaire_embed(interaction.user.id)
                await interaction.followup.send(embed=embed, ephemeral=True)
            elif choix == "classement":
                embed = await _construire_classement("argent", interaction.user.id)
                view = ClassementView("argent", auteur=interaction.user.id)
                view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except aiomysql.Error as e:
            logger.critical(f"[profil_actions] Erreur DB ({choix}) : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)


class ProfilActionsView(TimedView):
    """Boutons + select attachés à /profil : personnalisation, historique/
    inventaire/classement/don via ProfilActionsSelect, et accès rapide à la
    boutique. Réservés au membre qui a lancé /profil (`auteur`) : la carte est
    publique, et sans cette vérification n'importe quel membre du salon pouvait
    utiliser les menus du profil d'un autre (donner de l'argent « depuis » sa
    carte, ouvrir sa boutique...).

    `argent` et `xp` : valeurs affichées sur la carte, pour dire au membre ce
    qu'il a gagné quand il l'actualise (voir ProfilActionsSelect)."""
    def __init__(self, *, auteur: int, argent: int, xp: int):
        super().__init__(auteur=auteur)
        self.argent = argent
        self.xp = xp
        self.add_item(ProfilActionsSelect())

    @discord.ui.button(label="🎮 Personnaliser mon profil", style=discord.ButtonStyle.blurple, row=1)
    async def personnaliser(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Cette fonctionnalité n'est disponible que sur le serveur.", ephemeral=True
            )
            return

        jeux = _jeux_disponibles(interaction.user)
        if not jeux:
            await interaction.response.send_message(
                "❌ Tu n'as aucun rôle jeu/plateforme pour l'instant. Choisis-en d'abord via "
                "l'accueil du serveur pour pouvoir personnaliser ton profil !",
                ephemeral=True
            )
            return

        view = PersonnalisationView(jeux, auteur=interaction.user.id)
        await interaction.response.send_message(
            "Choisis quel jeu/plateforme tu veux renseigner :",
            view=view,
            ephemeral=True
        )
        view.message = await interaction.original_response()

    @discord.ui.button(label="🛒 Boutique", style=discord.ButtonStyle.green, row=1)
    async def boutique(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Cette fonctionnalité n'est disponible que sur le serveur.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        embed, view = await build_boutique_display(interaction.user.id)
        if view is None:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        view.message = await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def get_level(xp: int):
        level = 1
        xp_needed = 10

        while xp >= xp_needed:
            xp -= xp_needed
            xp_needed *= 2
            level += 1
        return level

    @app_commands.command(name="profil", description="Afficher ton profil")
    async def profil(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            argent, xp, rang = await _donnees_profil(interaction.user.id)
        except aiomysql.Error as e:
            logger.critical(f"[profil] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        view = ProfilActionsView(auteur=interaction.user.id, argent=argent, xp=xp)
        contenu = await _contenu_profil(interaction.user, argent, xp, rang)
        if interaction.response.is_done():
            message = await interaction.followup.send(view=view, **contenu)
        else:
            await interaction.response.send_message(view=view, **contenu)
            message = await interaction.original_response()
        view.message = message

    @app_commands.command(name="argent", description="Afficher ton solde d'argent")
    async def argent(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                    result = await cursor.fetchone()
        except aiomysql.Error as e:
            logger.critical(f"[argent] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return
        argent = result[0] if result and result[0] is not None else 0
        embed = discord.Embed(
            title="💰 Argent",
            description=f"Tu as **{argent} €**.",
            color=discord.Color.green()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="donner", description="Donne de l'argent à un autre membre")
    @app_commands.describe(user="Le membre à qui donner", montant="Combien d'argent donner")
    async def donner(self, interaction: discord.Interaction, user: discord.Member,
                     montant: app_commands.Range[int, 1, MONTANT_DON_MAX]):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        await _effectuer_don(interaction, user, montant)

    @app_commands.command(name="niveau", description="Afficher ton niveau et ton XP")
    async def niveau(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        # Via le cache mémoire (utils/cache.py) : même valeur que celle utilisée par
        # on_message pour calculer les niveaux, sans refaire un aller-retour DB si elle
        # est déjà en cache.
        xp = await cache.get_xp(interaction.user.id)
        nv = self.get_level(xp)
        embed = discord.Embed(
            title="✨ Niveau",
            description=f"Tu es **niveau {nv}** avec **{xp} XP**.",
            color=discord.Color.green()
        )
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

    @app_commands.command(name="daily", description="Récupère ta récompense quotidienne")
    async def daily(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

        now = int(time.time())
        try:
            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    # Garantit d'abord l'existence de la ligne (un membre n'ayant
                    # jamais gagné d'XP/argent n'en a pas encore — même principe que
                    # ajouter_rarete dans utils/database.py), sans toucher à argent ni
                    # last_daily si elle existe déjà : INSERT IGNORE ne fait rien dans
                    # ce cas.
                    await cursor.execute("INSERT IGNORE INTO utilisateurs (user_id) VALUES (%s)", (interaction.user.id,))
                # Validé à part : dans la même transaction que l'UPDATE ci-dessous,
                # deux /daily simultanés d'un nouveau membre gardaient chacun le
                # verrou partagé posé par l'INSERT IGNORE sur la clé déjà créée par
                # l'autre, puis s'interbloquaient en voulant la modifier (erreur
                # 1213, « Deadlock found ») au lieu de simplement ne créditer
                # qu'une fois.
                await conn.commit()

            async with connexion() as conn:
                async with conn.cursor() as cursor:
                    # UPDATE conditionné par le cooldown, comme la déduction d'achat en
                    # boutique ou le flag warn_12h de ticket_watcher (start.py) — pas un
                    # upsert avec condition dans le SET : le pool est ouvert avec
                    # client_flag=CLIENT.FOUND_ROWS (voir utils/database.py), qui rend
                    # rowcount ambigu entre "ligne insérée" et "clé dupliquée mais
                    # valeurs inchangées" pour un INSERT ... ON DUPLICATE KEY UPDATE.
                    # Ici la clause WHERE porte directement la condition de succès, donc
                    # rowcount reste fiable : elle ne matche que si le cooldown est
                    # écoulé, auquel cas last_daily change forcément de valeur.
                    await cursor.execute(
                        "UPDATE utilisateurs SET argent = argent + %s, last_daily = %s "
                        "WHERE user_id = %s AND (last_daily IS NULL OR last_daily <= %s)",
                        (DAILY_REWARD, now, interaction.user.id, now - DAILY_COOLDOWN)
                    )
                    gagne = cursor.rowcount == 1

                    if not gagne:
                        # Lecture simple : connexion() garantit qu'elle voit la
                        # réclamation qui fait échouer l'UPDATE ci-dessus, même
                        # faite sur une autre connexion. Sur une image figée
                        # d'avant, last_daily pouvait valoir NULL et le calcul
                        # du délai plus bas plantait (ou, depuis MariaDB 11.6,
                        # l'UPDATE lui-même échouait en erreur 1020).
                        await cursor.execute("SELECT last_daily FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                        (last_daily,) = await cursor.fetchone()
                    else:
                        await log_transaction(cursor, interaction.user.id, "daily", DAILY_REWARD, "Récompense quotidienne")

                    await conn.commit()
        except aiomysql.Error as e:
            logger.critical(f"[daily] Erreur DB : {e}", exc_info=True)
            await interaction.followup.send("❌ Une erreur est survenue avec la base de données.", ephemeral=True)
            return

        if gagne:
            await interaction.followup.send(
                f"✅ Tu as reçu ta récompense quotidienne : **{DAILY_REWARD} €** !",
                ephemeral=True
            )
        else:
            restant = last_daily + DAILY_COOLDOWN - now
            heures, reste = divmod(max(restant, 0), 3600)
            minutes = reste // 60
            await interaction.followup.send(
                f"❌ Tu as déjà réclamé ta récompense aujourd'hui. Reviens dans **{heures}h{minutes:02d}**.",
                ephemeral=True
            )

async def setup(bot):
    await bot.add_cog(Profile(bot))
