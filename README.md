# Pixel Bot

Pixel bot est un bot discord multigaming développé en **python** (discord.py) pour gerer : 
- Les systèmes de partenariat
- Les systeme de moderation
- Les systeme d'argent & d'XP
- Les tickets, recrutement et bien plus

## 1) Installation

Pour cloner le repo :

```bash
git clone https://github.com/Yugo206/Pixel-Bot.git
```
*Astuce : Pensez a cloner la branche `main`car elle est la version stable. Pour 
les version beta, clonez la branche `beta`*

## 2) Installer les dépendances

Pour installer les dépendances :

```bash
pip install -r requirements.txt
```
## 3) Base de données MariaDB

Le bot utilise **MariaDB** (compatible MySQL) au lieu de SQLite. Il faut un serveur MariaDB
déjà installé et démarré (en local pour le dev, sur votre serveur pour la prod), puis créer
une base et un utilisateur dédiés :

```sql
CREATE DATABASE IF NOT EXISTS pixelbot_n CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'pixelbot'@'localhost' IDENTIFIED BY 'un_mot_de_passe_solide';
GRANT ALL PRIVILEGES ON pixelbot_n.* TO 'pixelbot'@'localhost';
FLUSH PRIVILEGES;
```

*Astuce : en prod, remplacez `'localhost'` par l'hôte depuis lequel le bot se connectera
(ou `'%'` si la connexion se fait depuis une autre machine).*

Le bot crée lui-même toutes les tables au démarrage (`init_db`) : il n'y a rien d'autre à
préparer une fois la base et l'utilisateur créés.

## 4) Ajout du .env

Dans le meme dossier que `start.py`, créer un nouveau fichier `.env` et y écrire ceci :
(remplace les crochet par les ids de votre serveur)

```.env
DISCORD_TOKEN=[votre token discord]
DB_HOST=[l'adresse de votre serveur MariaDB, ex: 127.0.0.1]
DB_PORT=[le port de votre serveur MariaDB, généralement 3306]
DB_USER=[l'utilisateur MariaDB créé à l'étape précédente]
DB_PASSWORD=[son mot de passe]
DB_NAME=[le nom de la base, ex: pixelbot_n]
DB_SSL=[true si l'hébergeur de la base l'exige (ex: alwaysdata), false pour une base locale/sur le même serveur (optionnel, défaut false)]
DB_SSL_CA=[chemin vers un certificat CA custom, rarement nécessaire (optionnel)]
DB_POOL_RECYCLE=[durée en secondes avant recyclage des connexions inactives du pool (optionnel, défaut 1800)]
DB_POOL_MINSIZE=[nombre minimum de connexions gardées ouvertes dans le pool (optionnel, défaut 1)]
DB_POOL_MAXSIZE=[nombre maximum de connexions simultanées dans le pool (optionnel, défaut 5)]
LOG_LEVEL=[niveau de verbosité des logs : DEBUG/INFO/WARNING/ERROR (optionnel, défaut INFO)]
```
*Astuce : `DB_SSL`, `DB_SSL_CA`, `DB_POOL_RECYCLE`, `DB_POOL_MINSIZE` et `DB_POOL_MAXSIZE` sont indépendants de
l'hébergeur — que la base et le bot tournent sur la même machine (VPS) ou séparément (ex: base chez alwaysdata,
bot ailleurs), il suffit d'ajuster ces variables sans toucher au code.*

Ce `.env` ne contient plus que ce qui doit exister avant même la connexion à la base
(le token et l'accès MariaDB) : tout le reste (IDs de rôles/salons, alertes d'erreur,
récompenses...) est décrit dans la section suivante.

### Réglages en base (table `config`)

Les IDs de rôles/salons, `GUILD_ID`, `OWNER_ID`, `DM_ERROR_COOLDOWN` et les montants
de `/daily` (`DAILY_REWARD`/`DAILY_COOLDOWN`) — tout ce qui n'a pas besoin d'exister
avant la connexion à la base — sont stockés dans une table `config` (clé/valeur),
créée automatiquement comme les autres tables. Comme `shop`, elle s'édite directement
en base, sans commande dédiée ni redémarrage du bot pour la plupart des changements :

```sql
INSERT INTO config (cle, valeur) VALUES ('ROLE_PC', '123456789012345678')
    ON DUPLICATE KEY UPDATE valeur = VALUES(valeur);
```

Toutes ces clés sont optionnelles : une clé absente désactive juste la fonctionnalité
associée (ex: pas de `ROLE_ROBLOX` = le jeu Roblox n'apparaît pas dans "Personnaliser
mon profil"). Les clés disponibles :

| Clé | Rôle |
|---|---|
| `GUILD_ID` | ID de votre serveur (période de test staff, déban automatique) |
| `OWNER_ID` | votre id discord, prévenu en MP en cas d'erreur (voir "Alertes d'erreur" ci-dessous) |
| `CHANNEL_MODO_ID` | salon de modération |
| `CHANNEL_TRADE_ID` | salon de trade brainrot |
| `CHANNEL_COMMANDE_ID` | salon de commande (annonces de niveau, erreurs) |
| `ROLE_MODO_ID` | rôle modérateur |
| `ROLE_RECRUTEMENT` | rôle donné aux modérateurs test acceptés |
| `ROLE_PC`, `ROLE_XBOX`, `ROLE_PLAYSTATION`, `ROLE_NINTENDO`, `ROLE_FORTNITE`, `ROLE_MINECRAFT`, `ROLE_BRAWLSTARS`, `ROLE_GTA`, `ROLE_ROBLOX` | rôles jeu/plateforme détectés par le bouton "Personnaliser mon profil" (/profil) |
| `DM_ERROR_COOLDOWN` | secondes minimum entre deux MP d'alerte pour la même erreur (défaut 300) |
| `DAILY_REWARD` | argent gagné avec `/daily` (défaut 50) |
| `DAILY_COOLDOWN` | délai en secondes entre deux `/daily` (défaut 86400, soit 24h) |

*Astuce : Pensez a avoir activé le mode developpeur dans les parametres de discord pour obtenir les identifiants.*

**Première installation :** pour éviter du SQL manuel dès le départ, vous pouvez
renseigner ces mêmes clés dans `.env` (voir `.env.example`) avant le tout premier
lancement du bot : une migration automatique les copie une seule fois vers `config`
au démarrage (voir `_migrate_env_to_config` dans `utils/setupdatabase.py`). Une fois
cette copie faite, elles peuvent être retirées de `.env` sans effet — le bot ne les
relit plus jamais depuis l'environnement, et les éditer ensuite ne se fait qu'en base.

⚠️ La plupart de ces clés (rôles/salons, `DM_ERROR_COOLDOWN`) sont relues à chaque
utilisation : une modification en base est prise en compte immédiatement, sans
redémarrer le bot. Deux exceptions, lues une seule fois au démarrage : `DAILY_REWARD`/
`DAILY_COOLDOWN`, et le destinataire des MP d'alerte (fixé au lancement à partir
d'`OWNER_ID` — le changer en base met à jour son affichage ailleurs dans le bot, mais
pas la destination des alertes déjà en cours). Un redémarrage est nécessaire pour ces
trois clés.

### Alertes d'erreur

Si `OWNER_ID` est défini (en base, voir ci-dessus), le bot envoie un MP au propriétaire
dès qu'une erreur est loggée (`logger.error`/`logger.critical` — voir
`utils/error_handler.py`), avec un anti-spam (`DM_ERROR_COOLDOWN`, un MP max par type
d'erreur sur cette durée). Les erreurs `critical` (problèmes de base de données) sont
en plus enregistrées dans la table `error` (créée automatiquement) pour investigation
ultérieure, avec la trace complète si disponible.

Ce même système sert aussi de garde-fou pour la configuration : à chaque démarrage,
le bot vérifie que chaque clé du tableau ci-dessus est bien présente dans `config`
(voir `missing_keys()` dans `utils/config.py`). Une clé jamais configurée (absente de
`config` **et** de `.env` — sinon la migration l'aurait copiée) déclenche un MP listant
tout ce qui manque, pour éviter qu'une fonctionnalité reste silencieusement désactivée
sans que personne ne le remarque.

## 5) Terminé !

*Astuce : Pensez a faire git pull sur la branche `main` pour obtenir les nouvelles fonctionnalités 
et les corrections de bug. Pour les version bêta, il faut pull la branche `beta`*
