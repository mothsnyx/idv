# Identity V Discord Bot

A Discord roleplay bot themed on Identity V, with a web dashboard for managing its content.

## Features

### Discord bot
- **Characters**: Hunter and Survivor profiles with owners, levels and an inventory
- **Two currencies**: names and emojis are set in `config.py` (currently Clues and Echoes)
- **Shop & inventory**: buy, use, sell, value and trade items
- **Matches**: `/match` for random results with rewards, `/matchreward` for rewards by count
- **Minigames**: `/scavenging` and `/foraging` with d20 rolls, XP and levels
- **Utility**: dice rolls, random choice, `/location`, reservations, welcome messages, reaction roles

### Web dashboard
Manage profiles, locations, the shop, match messages, minigame outcomes, reservations,
activity checks, embeds, the welcome message and reaction roles.

## Installation (Raspberry Pi / Linux)

```bash
git clone https://github.com/mothsnyx/idv.git ~/idv
cd ~/idv

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env          # add DISCORD_TOKEN and the other values

python main.py               # bot
python dashboard_secure.py   # dashboard (in a second terminal)
```

On first start the bot creates `game_database.db` with a few `[Placeholder]` entries
(shop, locations, match messages, minigame outcomes). Replace them in the dashboard.

## Configuration

| What | Where |
|------|-------|
| Bot token, secrets, IDs, ports | `.env` (see `.env.example`) |
| Currency names & emojis, roles, rewards, exchange rates | `config.py` |
| Minigames (texts, buttons, XP) | `config.py` → `MINIGAMES` |
| Custom Discord emojis | `config.py` → `EMOJI` (paste e.g. `<:name:123456789>`) |
| Admin Discord IDs | `config.py` → `ADMIN_IDS` |
| Shop items, locations, match messages, loot | Dashboard |
| Dashboard logins | `.env` → `DASHBOARD_USERS=admin:password,mod:password` |

To add another minigame, add an entry to `MINIGAMES` in `config.py` and restart both
services. The slash command, database table and dashboard page are created automatically.

## Discord commands

| Area | Commands |
|------|----------|
| Profiles | `/create`, `/profile`, `/list`, `/editname`, `/editrole`, `/deleteprofile`, `/claim` |
| Currency | `/givecurrency`, `/exchange` (admins: `/addcurrency`, `/removecurrency`) |
| Items | `/buy`, `/additem`, `/removeitem`, `/use`, `/giveitem`, `/value`, `/sell` |
| Gameplay | `/match`, `/matchreward`, `/scavenging`, `/foraging`, `/location` |
| Utility | `/roll`, `/choose`, `/help` |
| Reservations | `/reserve`, `/myreservations` |
| Cryptic Notes | `/cnmatch` (solo, or a group of up to 4 where every player accepts for their own character) |
| Staff | `/approvecharacter`, `/releasecharacter` (and the buttons on requests) |
| Admin | `/addcurrency`, `/removecurrency`, `/assignowner`, `/cancelreservation`, `/restart`, `/testwelcome` |

Items accept quantities: `/buy name:Norton items:Lantern:2, Bandage`.

## Roster & reservations

1. A member uses `/reserve` in the reserve channel (autocomplete shows only free characters).
2. The request appears in the staff channel with **Approve**, **Approve (custom length)** and **Deny**.
3. On approval the member is pinged with the expiry date, and the character shows as *(reserved)* on the roster.
4. 24 hours before the end, the member gets a reminder with an **Extend** button (which asks staff again).
5. When the submission is accepted, staff run `/approvecharacter` (or click **Taken** in the dashboard):
   the character becomes taken and the reservation ends without an expiry ping.
6. If the reservation runs out instead, the member is pinged and the character is free again.

If someone with characters leaves the server, staff get an alert with a **Release all** button.
The roster messages in `ROSTER_CHANNEL_ID` (a forum post works) are kept up to date automatically.
Their look (embed or plain message, titles, colors, line format, ...) is set on the dashboard
under **Roster → Roster Layout in Discord**.

## Running as a service

```bash
sudo cp idvbot.service idvdashboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now idvbot idvdashboard

sudo journalctl -u idvbot -f    # logs
```

The service files assume the bot lives in `/home/jay/idv`. Adjust `WorkingDirectory`
and `ExecStart` if it doesn't.

The dashboard's **Pull Updates & Restart** button runs `git pull` and restarts the
services with `sudo systemctl`, so the user needs passwordless sudo for those two commands.

### Running next to the old bot

This bot uses ports 5010 (dashboard) and 5012 (internal API), so it runs next to the
old bot (5000/5002) without any extra setup. The dashboard is at `http://PI-IP:5010`.

## Backup

```bash
cp game_database.db game_database_backup_$(date +%Y%m%d).db
```

## Security notes

- `.env` and `*.db` are in `.gitignore` and never committed.
- The bot's internal API only listens on `127.0.0.1`, so only the dashboard can reach it.
- Dashboard logins live only in `.env`. Without `DASHBOARD_USERS` nobody can log in.
- When exposing the dashboard (e.g. with ngrok), add ngrok's own login in front:
  `ngrok http 5010 --basic-auth "user:password"`.

---

Identity V is a trademark of NetEase Games. This is an unofficial fan project.
