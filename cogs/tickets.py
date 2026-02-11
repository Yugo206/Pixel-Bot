import sqlite3
import time
import discord
from discord.ext import commands
import os
import asyncio
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, timedelta, timezone
from cogs.warn import ContestationView
from utils.setupdatabase import DB_PATH
print("DB ABS PATH:", os.path.abspath(DB_PATH))

class AvisModal(discord.ui.Modal, title="Ton avis"):
    avis = discord.ui.TextInput(
        label="Laisse ton avis",
        style=discord.TextStyle.paragraph,
        min_length=5,
        max_length=500
    )

    def __init__(self, bot, view, message):
        super().__init__()
        self.bot = bot
        self.view = view
        self.message = message

    async def on_submit(self, interaction: discord.Interaction):
        channel = self.bot.get_channel(int(os.getenv("CHANNEL_MODO_ID")))
        if channel is None:
            channel = await self.bot.fetch_channel(int(os.getenv("CHANNEL_MODO_ID")))

        await channel.send(
            f"Avis de {interaction.user.mention} :\n{self.avis.value}"
        )

        # Désactiver le bouton
        for child in self.view.children:
            if child.custom_id == "ticket:explique":
                child.disabled = True

        await self.message.edit(view=self.view)
        await interaction.response.send_message("Merci pour ton avis !", ephemeral=True)




class AvisView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

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
        advisor = None
        try:
            advisor = self.bot.get_channel(int(os.getenv("CHANNEL_MODO_ID")))
        except Exception as e:
            print(e)
        if advisor is None:
            await interaction.response.send_message("ERREUR : Ouvre un ticket sur Pixel Party pour resoudre le probleme", ephemeral=True)
            return

        await advisor.send(
            f"Avis de {interaction.user.mention} : {select.values[0]}"
        )

        select.disabled = True
        await interaction.message.edit(view=self)
        await interaction.response.send_message("Merci pour ton avis !", ephemeral=True)

    @discord.ui.button(
        label="Explique-nous !",
        style=discord.ButtonStyle.blurple,
        custom_id="ticket:explique"
    )
    async def explique(self, interaction: discord.Interaction, button: discord.ui.Button):
        modal = AvisModal(
            bot=self.bot,
            view=self,
            message=interaction.message
        )
        await interaction.response.send_modal(modal)




class ModoView(discord.ui.View):
    def __init__(self, thread, membre, raison, message):
        super().__init__(timeout=None)
        self.thread = thread
        self.membre = membre
        self.raison = raison
        self.message = message


    @discord.ui.button(label="Prendre en chage", style=discord.ButtonStyle.blurple, custom_id="ticket:prendre")
    async def prendre(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = self.thread
        membre = self.membre
        await interaction.response.send_message(f"Tu a pris le ticket. Le lien est ici : {thread.mention}.", ephemeral=True)
        await thread.edit(locked=False)
        embed = self.message.embeds[0]
        embed.set_field_at(2, name="Modérateur : ", value=interaction.user.mention)
        embed.set_field_at(4, name="Statue", value="Actif")
        button.disabled = True
        await interaction.message.edit(view=self)
        await self.message.edit(embed=embed)
        messs = await thread.send(f"{interaction.user.mention}")
        await messs.delete()

        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                c = conn.cursor()
                c.execute(
                    "UPDATE ticket SET modo_id = ?, statut = ? WHERE membre_id = ?", (interaction.user.id, 2, membre.id))
                conn.commit()
        except sqlite3.OperationalError as e:
            print(e)

class SatisfactionView(discord.ui.View):
    def __init__(self, membre):
        super().__init__(timeout=None)
        self.membre = membre

    @discord.ui.select(
        options=[
            discord.SelectOption(label="Super bien !", description="Le ticket s'est bien passé", emoji="🙂"),
            discord.SelectOption(label="Mal", description="Le membre a insulté / n'a pas respecté le staff", emoji="😕"),
            discord.SelectOption(label="Pas de reponse",
                                 description="Tu as mentionné plusieurs fois le membre, mais pas de réponses.",
                                 emoji="🚫")
        ],
        custom_id="ticket:satisfaction"
    )
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        selected_value = select.values[0]

        # Cas positif : rien à faire sauf désactiver le select
        if selected_value == "Super bien !":
            await self._disable_and_respond(interaction)
            return

        # Cas négatifs : "Mal" ou "Pas de reponse"
        warn_count = 0
        warn_id = None

        try:
            with sqlite3.connect(DB_PATH, timeout=5.0) as conn:
                c = conn.cursor()

                # Récupérer le nombre de warns actuel
                c.execute("SELECT warn FROM utilisateurs WHERE user_id = ?", (self.membre.id,))
                result = c.fetchone()

                iso_time = datetime.now(timezone.utc).isoformat()

                if result is None:
                    # L'utilisateur n'existe pas dans la table → on l'insère
                    c.execute("INSERT INTO utilisateurs (user_id, warn) VALUES (?, 1)", (self.membre.id,))
                    warn_count = 1
                elif result[0] is None:
                    # L'utilisateur existe mais warn est NULL → on met à 1
                    c.execute("UPDATE utilisateurs SET warn = 1 WHERE user_id = ?", (self.membre.id,))
                    warn_count = 1
                else:
                    # L'utilisateur existe avec un compteur → on incrémente
                    warn_count = result[0] + 1
                    c.execute("UPDATE utilisateurs SET warn = ? WHERE user_id = ?", (warn_count, self.membre.id))

                # Insérer le warn dans la table warns
                c.execute(
                    "INSERT INTO warns (user_id, modo_id, raison, created_at, created_at_iso) VALUES (?, ?, ?, ?, ?)",
                    (self.membre.id, interaction.user.id, "Non respect des conditions d'ouverture de ticket",
                     int(time.time()), iso_time)
                )
                conn.commit()

                # Récupérer l'ID du warn qu'on vient d'insérer
                warn_id = c.lastrowid  # ← Méthode correcte pour récupérer le dernier ID inséré

        except sqlite3.OperationalError as e:
            print(f"Erreur SQLite: {e}")
            await interaction.response.send_message("❌ Une erreur est survenue avec la base de données.",
                                                    ephemeral=True)
            return

        # Appliquer les sanctions selon le nombre de warns
        await self._apply_sanctions(interaction, warn_count)

        # Créer l'embed d'avertissement
        embed = self._create_warn_embed(selected_value, interaction.user)

        # Envoyer le message au membre
        try:
            bot = interaction.client
            view = ContestationView(self.membre, bot, warn_id)
            await self.membre.send(embed=embed, view=view)
        except discord.Forbidden:
            print(f"Impossible d'envoyer un DM à {self.membre}")
        except Exception as e:
            print(f"Erreur envoi DM: {e}")

        # Désactiver le select et répondre
        await self._disable_and_respond(interaction)

    def _create_warn_embed(self, selected_value: str, modo: discord.User) -> discord.Embed:
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

        embed.add_field(name="C'est une erreur ?", value="Va vite ouvrir un ticket et conteste cet avertissement")
        embed.set_footer(text="Pixel Party")
        return embed

    async def _apply_sanctions(self, interaction: discord.Interaction, warn_count: int):
        """Applique les sanctions selon le nombre de warns."""
        channel = interaction.guild.get_channel(int(os.getenv("CHANNEL_MODO_ID")))

        if warn_count == 3:
            await self._apply_timeout(interaction, channel, hours=48, reason="3 avertissements")

        elif warn_count == 5:
            await self._apply_timeout(interaction, channel, days=7, reason="5 avertissements")

        elif warn_count == 10:
            await self._apply_ban(interaction, channel, days=30, reason="10 avertissements")

    async def _apply_timeout(self, interaction: discord.Interaction, channel, hours: int = 0, days: int = 0,
                             reason: str = ""):
        """Applique un timeout au membre."""
        # Utiliser datetime.now(timezone.utc) au lieu de datetime.utcnow() (déprécié)
        until = datetime.now(timezone.utc) + timedelta(hours=hours, days=days)

        try:
            await self.membre.timeout(until, reason=reason)
            print(f"Membre {self.membre} timeout jusqu'à {until}")
        except discord.Forbidden:
            if channel:
                await channel.send(f"❌ Erreur : impossible de mute {self.membre.mention} (permissions insuffisantes)")
        except discord.HTTPException as e:
            if channel:
                await channel.send(f"❌ Impossible de mute {self.membre.mention} : {e}")
            return

        # Envoyer un DM au membre
        duration_str = f"{hours}h" if hours else f"{days} jour(s)"
        embed = discord.Embed(
            title="Tu viens d'être mute",
            description=f"Tu as reçu {reason.split()[0]} avertissements, tu viens donc d'être mute {duration_str} sur **Pixel Party**.\nPrends le temps de réfléchir pendant ton mute, ça évitera le ban 😆",
            color=discord.Color.red()
        )
        try:
            await self.membre.send(embed=embed)
        except discord.Forbidden:
            pass

    async def _apply_ban(self, interaction: discord.Interaction, channel, days: int, reason: str):
        """Applique un ban temporaire au membre."""
        unban_at = int(time.time()) + days * 86400

        try:
            embed = discord.Embed(title="Tu viens d'être ban", description="Tu t'est recemment mal comporté sur Pixel Party", colour=discord.Color.red())
            embed.add_field(name="Raison", value="10 avertissements")
            embed.add_field(name="Temps", value="30 jours")
            await self.membre.send(embed=embed)
            await interaction.guild.ban(self.membre, reason=reason)
        except discord.Forbidden:
            if channel:
                await channel.send(f"❌ Erreur : impossible de bannir {self.membre.mention} (permissions insuffisantes)")
            return
        except discord.HTTPException as e:
            if channel:
                await channel.send(f"❌ Impossible de bannir {self.membre.mention} : {e}")
            return

        # Enregistrer le ban temporaire
        try:
            with sqlite3.connect(DB_PATH) as conn:
                c = conn.cursor()
                c.execute(
                    "INSERT INTO temp_bans (user_id, unban_at) VALUES (?, ?)",
                    (self.membre.id, unban_at)
                )
                conn.commit()
        except sqlite3.OperationalError as e:
            print(f"Erreur SQLite temp_bans: {e}")

        if channel:
            await channel.send(f"🔨 {self.membre} banni pour **{days} jour(s)**.\nRaison : {reason}")

    async def _disable_and_respond(self, interaction: discord.Interaction):
        """Désactive le select et répond à l'interaction."""
        # Désactiver tous les enfants de la vue
        for child in self.children:
            child.disabled = True

        try:
            await interaction.response.edit_message(view=self)
        except discord.InteractionResponded:
            await interaction.edit_original_response(view=self)
        except Exception as e:
            print(f"Erreur lors de la désactivation: {e}")


class FermerView(discord.ui.View):
    def __init__(self, raison, membre):
        super().__init__(timeout=None)
        self.raison = raison
        self.membre = membre

    @discord.ui.button(label="Fermer le ticket", style=discord.ButtonStyle.red, custom_id="ticket:close")
    async def create(self, interaction: discord.Interaction, button: discord.ui.Button):
        role = discord.utils.get(interaction.user.roles, id=int(os.getenv("ROLE_MODO_ID")))
        if role:
            await interaction.response.send_message("Comment s'est passé votre ticket ?", view=SatisfactionView(self.membre), ephemeral=True)
        else:
            await interaction.response.send_message("Ticket fermé avec succès", ephemeral=True)
        embed=discord.Embed(title="Ticket fermé", description="Ce ticket est fermé. Vous ne pouvez plus ecrire.")
        embed.add_field(name="Fermé par :", value=interaction.user.mention)
        embed.add_field(name="Raison ititiale du ticket : ", value=self.raison)
        button.disabled = True
        await interaction.message.edit(embed=embed, view=self)
        embed2 = discord.Embed(title="Donne-nous ton avis sur ton ticket !",
                               description="Afin d'ameliorer le systeme de ticket ou de rendre le staff plus efficace, nous souhaitons receuillir ton avis sur ce ticket.")
        await self.membre.send(embed=embed2, view=AvisView(interaction.client))
        thread = interaction.channel
        ts = int((datetime.utcnow() + timedelta(seconds=86400)).timestamp())
        await thread.send(f"Ce ticket as été fermé par {interaction.user.mention}. Il se supprimera <t:{ts}:R>")
        await thread.edit(locked=True, archived=True)
        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                c = conn.cursor()
                c.execute("UPDATE ticket SET statut = 3 WHERE membre_id = ?", (self.membre.id,))
                conn.commit()
        except sqlite3.OperationalError as e:
            print(e)
        print("line 68")


class TicketCreateView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        print("line 13")
        self.bot = bot

    @discord.ui.select(placeholder="Selectionne une option", custom_id="ticket:create", options=[
        discord.SelectOption(label="Partenariat", description="Pour proposer ou discuter d'un partenariat entre serveur/projet", emoji="🤝"),
        discord.SelectOption(label="Support technique", description="Pour signer un bug ou demander de l'aide concernant le serveur ou un bot", emoji="🛠️"),
        discord.SelectOption(label="Demande de rôle", description="Pour demander un rôle spécial, une vérification ou un grade particulier", emoji="🗒️"),
        discord.SelectOption(label="Signaler un membre", description="pour signaler un comportement inapproprié du spam ou un non-respect des règles", emoji="🚨"),
        discord.SelectOption(label="Contester une sanction", description="pour discuter d'un mute, kick ou ban que vous jugez injustifié", emoji="⚖️"),
        discord.SelectOption(label="Question générale", description="pour poser des questions sur le serveur, les évènements, ou son fonctionnement", emoji="❓"),
        discord.SelectOption(label="Problème lié aux économies du serveur", description="pour toute question concernant un achat ou un don", emoji="💰"),
        discord.SelectOption(label="Suggestions pour le serveur", description="pour proposer des idées ou amélioration pour le serveur", emoji="💡"),
        discord.SelectOption(label="Autre / privé", description="pour toute autre demande nécessitant une discussion en privé avec le staff", emoji="🔒"),
    ])
    async def select_callback(self, interaction: discord.Interaction, select: discord.ui.Select):
        print("line 23")
        thread = await interaction.channel.create_thread(
            name=f"ticket-{interaction.user.name}",
            invitable=True
        )

        messs = await thread.send(interaction.user.mention)
        await messs.delete()
        print("line 30")
        raison = select.values[0]
        print(raison)
        view = FermerView(raison, interaction.user)
        embed = discord.Embed(title="Gestionnaire de ticket", description=f"Bienvenue {interaction.user.name} sur ton ticket !", colour=discord.Colour.blue())
        embed.add_field(name="Fermer le ticket", value="Tu peut fermer ton ticket à tout moment en cliquant sur ce boutton", inline=False)
        embed.add_field(name="Raison du ticket : ", value=raison)
        embed.add_field(name="Modérateur :", value="Personne")
        embed.add_field(name=f"Demandé par :", value=interaction.user.mention)
        embed.add_field(name="Statue : ", value="En attente d'un moderateur")
        message = await thread.send(embed=embed, view=view)
        await interaction.response.send_message(f"Ticket crée avec succès dans {thread.mention}", ephemeral=True)
        try:
            channel = interaction.guild.get_channel(int(os.getenv("CHANNEL_MODO_ID")))
        except discord.Forbidden as ee:
            print(ee)
        if channel is None:
            print("PAS  DE CHANNEL TROUVé !!!!!!!")
        elif channel is not None:
            print(f"Le channel est {channel.name}")
        embed2 = discord.Embed(title="Ticket ouvert !", description="Clique sur le boutton ci-dessous pour acceder au ticket et le prendre en charge.", colour=discord.Colour.blue())
        try:
            view2 = ModoView(thread, interaction.user, raison, message)
        except Exception as eee:
            print(eee)
        await channel.send(embed=embed2, view=view2)
        await interaction.message.edit(content="Ouvrir un ticket", view=TicketCreateView(self.bot))
        print("line 207")
        try:
            with sqlite3.connect(DB_PATH, timeout=10.0) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO ticket (thread_id, membre_id, statut, raison) VALUES (?, ?, ?, ?)",
                    (thread.id, interaction.user.id, 1, raison)
                )
                conn.commit()
        except sqlite3.OperationalError as e:
            print(e)
        print("line 218")
        if raison == "Partenariat":
            print("line 220")
            embed5666666 = discord.Embed(title="Bienvenue sur ton ticket partenariat !",
                                         description="Afin de faciliter le travail du staff et te faire gagner du temps, nous souhaitons récuperer les informations du partenariat.",
                                         colour=discord.Colour.blue())
            embed5666666.add_field(name="Etape 1 : Conditions", value="Ces conditions sont obligatoires et mêmes sans le bot ces condition doivent etre accepté sinon partenariat impossible.", inline=False)
            embed5666666.add_field(name="Etape 2 : Ton sevreur", value="Fait une courte descripions de ce qu'est ton serveur.", inline=False)
            embed5666666.add_field(name="Etape 3 : Mentions", value="Donne quelle mention veux que ton serveur et notre serveur.", inline=False)
            embed5666666.add_field(name="Etape 3 : Ta pub", value="Tu donne la publicité de ton serveur avec le lien. Si tu n'a pas de pub, envoie juste le lien.", inline=False)
            embed5666666.add_field(name="Etape 5 : Notre pub & finalistion", value="Le bot envoie la pu du serveur. Le staff viendra ensuite pour publier les annonces.", inline=False)
            embed5666666.add_field(name="Alors, pret a commencer ?", value=" Clique sur le boutton \"Demarrer\" ci-dessous")
            await thread.send(embed=embed5666666, view=PartenariatCommencerView(self.bot))

class PartenariatCommencerView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Demarrer", style=discord.ButtonStyle.green, custom_id="Partenariat:Commencer")
    async def demarrer(self, interaction: discord.Interaction, button: discord.ui.Button):
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
                "• Ping <@1418958299927412879> seulement (sauf exeption du staff) \n"
                "• Tu doit mettre ta pub en premier.\n"
                "• Invitation expirée ou message supprimé = partenariat annulé"
            ),
            inline=False
        )
        embed.set_footer(text="En faisant un partenariat, tu t'engage a respecter ces règles")
        view = ConditionsPartenariatView(self.bot)
        await interaction.response.send_message(embed=embed, view=view)
        button.disabled = True
        await interaction.message.edit(view=self)

class ConditionsPartenariatView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Accepter", style=discord.ButtonStyle.green, custom_id="partenariat:accepter")
    async def accepter(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel

        await interaction.response.send_message(
            embed=discord.Embed(
                title="Description de ton serveur",
                description="Envoie une description de ton serveur.",
                colour=discord.Colour.blurple()
            )
        )
        button.disabled = True
        await interaction.message.edit(view=self)

        def check(m):
            return m.author == interaction.user and m.channel == thread

        try:
            desc_msg = await self.bot.wait_for("message", timeout=240, check=check)
        except asyncio.TimeoutError:
            await thread.send("⏱️ Temps écoulé, on continue.")
            desc_msg = None

        await thread.send(
            embed=discord.Embed(
                title="Publicité de ton serveur",
                description="Envoie maintenant ta publicité.",
                colour=discord.Colour.blurple()
            )
        )

        try:
            pub_msg = await self.bot.wait_for("message", timeout=240, check=check)
        except asyncio.TimeoutError:
            await thread.send("⏱️ Pas de pub reçue.")

        await thread.send(
            embed=discord.Embed(
                title="Choix de la mention",
                description="Choisis la mention souhaitée",
                colour=discord.Colour.blurple()
            ),
            view=MentionPartenariatView(self.bot)
        )


class MentionPartenariatView(discord.ui.View):
    def __init__(self, bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.select(options=[
        discord.SelectOption(label="Aucune mention", description="Aucune mention sur ton serveur", emoji="🚫"),
        discord.SelectOption(label="Mention \"Here\"", description="Mention here sur ton serveur", emoji="🧑‍🧒"),
        discord.SelectOption(label="Mention \"Partenariat\"", description="Mention partenariat sur ton serveur", emoji="🧑‍🧑‍🧒"),
        discord.SelectOption(label="Mention \"Everyone\"", description="Mention everyone sur ton serveur", emoji="🧑‍🧑‍🧒‍🧒")
    ], custom_id="partenariat:mention")
    async def select_callback(self, interaction: discord.Interaction, select : discord.ui.Select):
        mention = select.values[0]
        channel = interaction.message.channel
        await interaction.response.send_message(f"Mention choisi : {mention}")
        embed = discord.Embed(title="Informations collectés !",
                              description="Toute les informations de ton serveur ont été récupérés. Si il en manque, le staff te les demandera.")
        embed.add_field(name="Tu pourrait de demander : je fait quoi maintenant ?",
                        value="Tu attends que le staff traite ta demande. Reste toujours disponible pour aller le plus vite. En attendant, je t'envoie la pub de Pixel Party", inline=False)
        await channel.send(embed=embed)
        await channel.send("# **🎮 Pixel Party | Serveur Multigaming Fun & Actif !** \n ## **Tu cherches un endroit pour jouer, discuter et rigoler ? Rejoins Pixel Party !** \n 🔥 Jeux populaires : Fortnite • Brawl Stars • Minecraft • Roblox \n 🎉 Événements : cache-cache, défilés de mode, défis d’armes, tournois… \n 🏅 Rôles spéciaux à débloquer : VIP, Nintendo, PS5, etc. \n 🗨️ Une vraie communauté chill pour se faire des potes \n 💬 Que tu sois joueur switch, PC, mobile ou console… t’es le/la bienvenu(e) ! \n 🔗 Rejoins-nous maintenant en cliquant [ici](https://discord.gg/cnWz7fXAex)")





class Tickets(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    await bot.add_cog(Tickets(bot))


print("LIGNE 500 !!!")