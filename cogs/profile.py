import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
load_dotenv()


from utils.database import get_pool
from utils import cache

# Colonnes autorisées pour /classement : whitelist revalidée juste avant l'interpolation
# SQL (même principe que ajouter_rarete dans utils/database.py), même si les choix sont
# déjà imposés côté client par app_commands.choices.
COLONNES_CLASSEMENT = {"argent", "xp"}

# Jeux/plateformes détectables via rôle pour le bouton "Personnaliser mon profil"
# (voir PersonnaliserButton). Chaque entrée : le rôle qui déclenche la proposition
# (variable d'env, optionnelle — absente = jeu désactivé sur ce serveur), et les
# questions fixes posées dans le modal ((clé DB, libellé affiché), ...).
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
     "questions": [("roblox_pseudo", "Pseudo Roblox"),
                   ("roblox_frequence", "Tu joues souvent ? (rarement / parfois / souvent)")]},
]

# Reverse-map clé DB -> libellé, pour afficher les réponses sur /profil sans reparcourir
# JEUX_PLATEFORMES à chaque fois.
CLE_LABELS = {cle: label for jeu in JEUX_PLATEFORMES for cle, label in jeu["questions"]}


def _jeux_disponibles(member: discord.Member) -> list:
    """Renvoie les jeux/plateformes de JEUX_PLATEFORMES configurés (variable d'env
    définie) et dont `member` possède le rôle correspondant."""
    disponibles = []
    for jeu in JEUX_PLATEFORMES:
        role_id_raw = os.getenv(jeu["role_env"])
        if not role_id_raw:
            continue
        role = member.guild.get_role(int(role_id_raw))
        if role is not None and role in member.roles:
            disponibles.append(jeu)
    return disponibles


class JeuModal(discord.ui.Modal):
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
        pool = get_pool()
        async with pool.acquire() as conn:
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
        await interaction.followup.send("✅ Ton profil a été mis à jour !", ephemeral=True)


class JeuButton(discord.ui.Button):
    def __init__(self, jeu: dict):
        super().__init__(label=jeu["label"], style=discord.ButtonStyle.blurple)
        self.jeu = jeu

    async def callback(self, interaction: discord.Interaction):
        cles = [cle for cle, _ in self.jeu["questions"]]
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                placeholders = ",".join(["%s"] * len(cles))
                await cursor.execute(
                    f"SELECT cle, valeur FROM profil_extra WHERE user_id = %s AND cle IN ({placeholders})",
                    (interaction.user.id, *cles)
                )
                valeurs_existantes = dict(await cursor.fetchall())
        await interaction.response.send_modal(JeuModal(self.jeu, valeurs_existantes))


class PersonnalisationView(discord.ui.View):
    def __init__(self, jeux: list):
        super().__init__(timeout=180)
        for jeu in jeux:
            self.add_item(JeuButton(jeu))


class PersonnaliserButton(discord.ui.View):
    """Bouton attaché à /profil. Agit toujours sur qui clique (interaction.user), pas
    sur le propriétaire du profil affiché — même principe que AchatSelect dans
    cogs/boutique.py, partagé entre tous les viewers d'un même message public."""
    def __init__(self):
        super().__init__(timeout=180)

    @discord.ui.button(label="🎮 Personnaliser mon profil", style=discord.ButtonStyle.blurple)
    async def personnaliser(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild is None:
            await interaction.response.send_message(
                "❌ Cette fonctionnalité n'est disponible que sur le serveur.", ephemeral=True
            )
            return

        jeux = _jeux_disponibles(interaction.user)
        if not jeux:
            await interaction.response.send_message(
                "Tu n'as aucun rôle jeu/plateforme pour l'instant. Choisis-en d'abord via "
                "l'accueil du serveur pour pouvoir personnaliser ton profil !",
                ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Choisis quel jeu/plateforme tu veux renseigner :",
            view=PersonnalisationView(jeux),
            ephemeral=True
        )


class Profile(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
    def get_level(self, xp: int):
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
        embed = discord.Embed(title="Profil", description="Ton profil contient ton **argent**, ton **XP** et tes **niveaux**", color=discord.Color.green())
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT argent, xp FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                result = await cursor.fetchone()
                await cursor.execute("SELECT cle, valeur FROM profil_extra WHERE user_id = %s", (interaction.user.id,))
                extra = await cursor.fetchall()
        argent = result[0] if result and result[0] is not None else 0
        xp = result[1] if result and result[1] is not None else 0
        embed.add_field(name="Argent :", value=f"{argent} €", inline=False)
        embed.add_field(name="Experience :", value=f"{xp}", inline=False)
        nv = self.get_level(xp)
        embed.add_field(name="Niveau :", value=f"{nv}", inline=False)
        if extra:
            texte = "\n".join(f"**{CLE_LABELS.get(cle, cle)}** : {valeur}" for cle, valeur in extra)
            embed.add_field(name="🎮 Jeux & plateformes :", value=texte, inline=False)
        view = PersonnaliserButton() if interaction.guild is not None else None
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view)

    @app_commands.command(name="argent", description="Afficher ton solde d'argent")
    async def argent(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT argent FROM utilisateurs WHERE user_id = %s", (interaction.user.id,))
                result = await cursor.fetchone()
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

    @app_commands.command(name="niveau", description="Afficher ton niveau et ton XP")
    async def niveau(self, interaction: discord.Interaction):
        if not interaction.response.is_done():
            await interaction.response.defer()
        pool = get_pool()
        # Via le cache mémoire (utils/cache.py) : même valeur que celle utilisée par
        # on_message pour calculer les niveaux, sans refaire un aller-retour DB si elle
        # est déjà en cache.
        xp = await cache.get_xp(pool, interaction.user.id)
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

    @app_commands.command(name="classement", description="Afficher le top 10 du serveur")
    @app_commands.describe(type="Classer par argent ou par expérience")
    @app_commands.choices(type=[
        app_commands.Choice(name="Argent", value="argent"),
        app_commands.Choice(name="Expérience", value="xp"),
    ])
    async def classement(self, interaction: discord.Interaction, type: app_commands.Choice[str]):
        if not interaction.response.is_done():
            await interaction.response.defer()

        colonne = type.value
        if colonne not in COLONNES_CLASSEMENT:
            await interaction.followup.send("❌ Choix invalide.", ephemeral=True)
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                # colonne : validée juste au-dessus contre COLONNES_CLASSEMENT, sûre à interpoler.
                await cursor.execute(
                    f"SELECT user_id, {colonne} FROM utilisateurs ORDER BY {colonne} DESC LIMIT 10"
                )
                rows = await cursor.fetchall()

        if colonne == "argent":
            titre, unite = "💰 Classement — Argent", "€"
        else:
            titre, unite = "✨ Classement — Expérience", "XP"

        if not rows:
            embed = discord.Embed(title=titre, description="Personne à classer pour le moment.", color=discord.Color.green())
        else:
            medailles = ["🥇", "🥈", "🥉"]
            lignes = []
            for i, (user_id, valeur) in enumerate(rows):
                rang = medailles[i] if i < len(medailles) else f"**{i + 1}.**"
                lignes.append(f"{rang} <@{user_id}> — {valeur or 0} {unite}")
            embed = discord.Embed(title=titre, description="\n".join(lignes), color=discord.Color.green())

        if interaction.response.is_done():
            await interaction.followup.send(embed=embed)
        else:
            await interaction.response.send_message(embed=embed)

async def setup(bot):
    await bot.add_cog(Profile(bot))
