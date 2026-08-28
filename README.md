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
CHANNEL_MODO_ID=[l'id de votre salon moderateur]
ROLE_MODO_ID=[l'id de votre role moderateur]
GUILD_ID=[l'id de votre serveur]
CHANNEL_TRADE_ID=[l'id de votre salon de trade brainrot]
CHANNEL_COMMANDE_ID=[l'id de votre salon de commande]
CHANNEL_RECRUTEMENT=[l'identifiant du salon de recrutement]
ROLE_RECRUTEMENT=[l'id du rôle donné aux modérateurs test acceptés]
ROLE_VISITE=[l'id du rôle donné à la fin de la visite guidée (optionnel)]
OWNER_ID=[votre id discord, prévenu en MP en cas d'erreur (voir "Alertes d'erreur" ci-dessous)]
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
DM_ERROR_COOLDOWN=[secondes minimum entre deux MP d'alerte pour la même erreur (optionnel, défaut 300)]
```
*Astuce : Pensez a avoir activé le mode developpeur dans les parametres de discord pour obtenir les identifiants*

*Astuce : `DB_SSL`, `DB_SSL_CA`, `DB_POOL_RECYCLE`, `DB_POOL_MINSIZE` et `DB_POOL_MAXSIZE` sont indépendants de
l'hébergeur — que la base et le bot tournent sur la même machine (VPS) ou séparément (ex: base chez alwaysdata,
bot ailleurs), il suffit d'ajuster ces variables sans toucher au code.*

### Alertes d'erreur

Si `OWNER_ID` est défini, le bot envoie un MP au propriétaire dès qu'une erreur est loggée
(`logger.error`/`logger.critical` — voir `utils/error_handler.py`), avec un anti-spam
(`DM_ERROR_COOLDOWN`, un MP max par type d'erreur sur cette durée). Les erreurs `critical`
(problèmes de base de données) sont en plus enregistrées dans la table `error` (créée
automatiquement) pour investigation ultérieure, avec la trace complète si disponible.

## 5) Terminé !

*Astuce : Pensez a faire git pull sur la branche `main` pour obtenir les nouvelles fonctionnalités 
et les corrections de bug. Pour les version bêta, il faut pull la branche `beta`*
