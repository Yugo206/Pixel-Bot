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
DB_HOST=[l'adresse de votre serveur MariaDB, ex: 127.0.0.1]
DB_PORT=[le port de votre serveur MariaDB, généralement 3306]
DB_USER=[l'utilisateur MariaDB créé à l'étape précédente]
DB_PASSWORD=[son mot de passe]
DB_NAME=[le nom de la base, ex: pixelbot_n]
```
*Astuce : Pensez a avoir activé le mode developpeur dans les parametres de discord pour obtenir les identifiants*

## 5) Terminé !

*Astuce : Pensez a faire git pull sur la branche `main` pour obtenir les nouvelles fonctionnalités 
et les corrections de bug. Pour les version bêta, il faut pull la branche `beta`*
