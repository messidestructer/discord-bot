# Roblox Group Discord Bot

A fully-featured Discord bot for Roblox groups. EP tracking, event logging with OCR, auto-promotions, moderation, Google Sheets sync, and more. **100% free — no paid AI or services required.**

---

## Features

| Feature | Details |
|---|---|
| **Roblox Verification** | Members verify via profile code check, or instantly via Bloxlink |
| **EP Tracking** | Add/subtract EP per player, full audit log, leaderboard |
| **Event Logging** | Attach a screenshot → Tesseract OCR reads names automatically. Edit before confirming. |
| **Auto-Promotions** | Bot checks EP every 6 hours and promotes group members automatically |
| **Group Management** | Set ranks, kick from group, accept/deny join requests |
| **Moderation** | Ban, kick, mute/timeout, warn, purge messages |
| **Weekly Reports** | Auto-posts every Sunday: most active player, top gainers, leaderboard |
| **Google Sheets** | Syncs EP leaderboard and event log on a schedule |
| **Profile Command** | Shows Roblox rank, EP, and leaderboard position |

---

## Requirements

- Python 3.10+
- Tesseract-OCR (free — install script handles it)
- A free Oracle Cloud server (recommended) or any Linux VPS

---

## Quick Start

```bash
# 1. Clone or download the bot files
cd discord-bot

# 2. Run the install script
bash install.sh

# 3. Configure
cp .env.example .env
nano .env

# 4. Edit event types (optional)
nano events_config.json

# 5. Start
python3 main.py
```

---

## Full Setup Guide

### Step 1 — Create the Discord Bot Application

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications)
2. Click **New Application**, give it a name, click **Create**
3. Go to the **Bot** tab → click **Add Bot**
4. Under **Privileged Gateway Intents**, turn ON:
   - ✅ Server Members Intent
   - ✅ Message Content Intent
5. Click **Reset Token** → copy your token (save it — you only see it once)
6. Go to **OAuth2 → URL Generator**
   - Scopes: `bot`, `applications.commands`
   - Permissions: Manage Roles, Kick Members, Ban Members, Moderate Members, Manage Nicknames, Manage Messages, Send Messages, Read Message History, View Channels
7. Copy the generated URL → open it → invite the bot to your server

> ⚠️ **Important:** In your server's role list (Server Settings → Roles), drag the bot's role **above** all roles it needs to manage.

---

### Step 2 — Get Your IDs

Enable Discord Developer Mode: **User Settings → Advanced → Developer Mode**

Then right-click to copy IDs:
- Right-click your **server icon** → Copy Server ID → this is your `GUILD_ID`
- Right-click each **channel** → Copy ID
- Right-click each **role** → Copy ID

**Channels to create** (if they don't exist):
| Channel | Purpose |
|---|---|
| `#mod-log` | Moderation actions (bans, kicks, mutes) |
| `#ep-log` | EP changes and event logs |
| `#promotions` | Auto-promotion announcements |
| `#weekly-report` | Sunday auto-report |

**Roles to create:**
| Role | Purpose |
|---|---|
| `EP Manager` | Can use `/ep` and `/log` commands |
| `Verified` | Assigned after Roblox verification |

---

### Step 3 — Set Up a Roblox Bot Account

1. Create a **new Roblox account** (NOT your main — this account will be in your group managing ranks)
2. Add it to your group and give it the highest rank you want it to be able to set
3. Log into Roblox with this account in your browser
4. Open browser DevTools (F12) → Application → Cookies → copy the `.ROBLOSECURITY` value
5. Paste this as `ROBLOX_COOKIE` in your `.env`

> ⚠️ The cookie expires periodically. When the bot logs Roblox errors, you need to refresh it.

---

### Step 4 — Set Up Bloxlink (Optional but Recommended)

Bloxlink lets members verify instantly if they've already verified through Bloxlink.

1. Add the Bloxlink bot to your server at [blox.link](https://blox.link)
2. Go to [blox.link/dashboard](https://blox.link/dashboard) → your server → **API Keys**
3. Generate a free API key and add it as `BLOXLINK_API_KEY` in `.env`

Without Bloxlink, the manual `/verify` flow still works fine.

---

### Step 5 — Fill in `.env`

```env
# Required
DISCORD_TOKEN=your_bot_token
GUILD_ID=your_server_id
ROBLOX_GROUP_ID=12345678
ROBLOX_COOKIE=your_roblosecurity_cookie

# Channels
LOG_CHANNEL_ID=111111111111111111
MOD_LOG_CHANNEL_ID=222222222222222222
EVENT_LOG_CHANNEL_ID=333333333333333333
REPORT_CHANNEL_ID=444444444444444444
PROMOTION_CHANNEL_ID=555555555555555555

# Roles
VERIFIED_ROLE_ID=666666666666666666
EP_MANAGER_ROLE_ID=777777777777777777

# Optional
BLOXLINK_API_KEY=
REPORT_UNIT_NAME=My Roblox Group
```

---

### Step 6 — Configure Event Types

Edit `events_config.json`:

```json
[
  { "name": "Training", "ep": 2 },
  { "name": "Patrol",   "ep": 1 },
  { "name": "Tryout",   "ep": 3 },
  { "name": "Joint Op", "ep": 4 }
]
```

These appear as choices in the `/log` command automatically.

---

### Step 7 — Configure Promotion Rules

Edit your `.env` to set promotion thresholds. The default rules are:

| EP Required | Roblox Rank # | Name |
|---|---|---|
| 0 | 1 | Recruit |
| 10 | 5 | Private |
| 25 | 10 | Corporal |
| 50 | 15 | Sergeant |
| 100 | 20 | Lieutenant |
| 200 | 25 | Captain |
| 350 | 30 | Major |
| 500 | 35 | Colonel |

To override, set `PROMOTION_RULES` in `.env` as JSON:
```
PROMOTION_RULES=[{"min_ep":0,"rank":1,"name":"Recruit"},{"min_ep":15,"rank":10,"name":"Private"}]
```

The rank numbers must match your actual Roblox group role rank numbers (found in group Configure → Roles).

---

### Step 8 — Google Sheets Setup (Optional — Free)

1. Go to [Google Cloud Console](https://console.cloud.google.com) → create a project
2. Enable the **Google Sheets API**
3. Go to **IAM & Admin → Service Accounts** → create a service account
4. Download the JSON credentials file → save it as `credentials.json` next to `main.py`
5. Create a Google Sheets spreadsheet → share it with the service account email (Editor)
6. Copy the spreadsheet ID from its URL
7. In `.env`:
   ```
   ENABLE_SHEETS=true
   GOOGLE_SHEETS_CREDS_FILE=credentials.json
   GOOGLE_SHEET_ID=your_spreadsheet_id_here
   ```

---

### Step 9 — Free Hosting (Oracle Cloud)

Oracle Cloud has a **permanently free** tier that's perfect for this bot.

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (requires a credit card for verification, but free tier won't charge you)
2. Go to **Compute → Instances → Create Instance**
3. Settings:
   - Image: Ubuntu 22.04
   - Shape: **VM.Standard.A1.Flex** (Ampere ARM) — 1 OCPU, 6 GB RAM
   - SSH Keys: Generate and download the key pair
4. Wait for **Running** status, note the Public IP
5. Connect via SSH:
   ```bash
   # Mac/Linux
   chmod 400 ~/Downloads/ssh-key.key
   ssh -i ~/Downloads/ssh-key.key ubuntu@YOUR_IP
   
   # Windows PowerShell
   ssh -i C:\Users\You\Downloads\ssh-key.key ubuntu@YOUR_IP
   ```
6. On the server, install and run:
   ```bash
   sudo apt update && sudo apt install -y python3 python3-pip git
   git clone https://github.com/YOURUSERNAME/YOUR_REPO.git
   cd YOUR_REPO/discord-bot
   bash install.sh
   cp .env.example .env
   nano .env    # fill in your values
   python3 main.py
   ```
7. Install PM2 to keep it running forever:
   ```bash
   sudo npm install -g pm2
   pm2 start "python3 main.py" --name discord-bot
   pm2 save
   pm2 startup    # copy and run the command it prints
   ```

---

## All Commands

### Member Commands
| Command | Description |
|---|---|
| `/verify <username>` | Link your Roblox account |
| `/bloxlink-sync` | Auto-verify via Bloxlink |
| `/profile [member]` | View group profile (EP, rank, stats) |
| `/ep check [username]` | Check EP total |
| `/setnick` | Set your nickname to your Roblox username |
| `/whois [member]` | Look up a member's Roblox account |
| `/ping` | Bot latency |
| `/serverinfo` | Server stats |
| `/avatar [member]` | Get member avatar |
| `/poll <question>` | Create a vote |
| `/promotionrules` | View EP promotion thresholds |

### Staff Commands (EP Manager Role)
| Command | Description |
|---|---|
| `/ep add <username> <amount>` | Add EP |
| `/ep subtract <username> <amount>` | Remove EP |
| `/ep audit <username>` | View EP change history |
| `/log <event_type> [screenshot]` | Log event, award EP |
| `/weeklystats` | This week's summary |
| `/leaderboard [limit]` | EP leaderboard |

### Admin Commands (Manage Roles / Manage Guild)
| Command | Description |
|---|---|
| `/rank <username> <rank#>` | Set Roblox group rank |
| `/grouproles` | List all group roles |
| `/kickroblox <username>` | Remove from Roblox group |
| `/joinrequests` | View pending join requests |
| `/acceptjoin <username/all>` | Accept join request(s) |
| `/denyjoin <username>` | Deny join request |
| `/checkpromotions` | Manually run promotion check |
| `/genreport` | Post weekly report now |
| `/syncsheets` | Manually sync to Google Sheets |
| `/kick <member>` | Kick from Discord |
| `/ban <member>` | Ban from Discord |
| `/unban <user_id>` | Unban by ID |
| `/mute <member> [minutes]` | Timeout a member |
| `/unmute <member>` | Remove timeout |
| `/warn <member> <reason>` | Warn and log |
| `/warnings <member>` | View warning history |
| `/clearwarnings <member>` | Clear all warnings |
| `/purge <amount> [member]` | Delete messages |
| `/slowmode <seconds>` | Set channel slowmode |
| `/userinfo [member]` | Full member info |
| `/rolemembers <role>` | List role members |

---

## Keeping the Bot Running (systemd)

Alternative to PM2 — create `/etc/systemd/system/discord-bot.service`:

```ini
[Unit]
Description=Discord Group Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/discord-bot
ExecStart=/usr/bin/python3 main.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable discord-bot
sudo systemctl start discord-bot
journalctl -u discord-bot -f   # live logs
```

---

## Updating the Bot

```bash
# On your server
cd discord-bot
git pull
pm2 restart discord-bot    # or: sudo systemctl restart discord-bot
```

---

## File Structure

```
discord-bot/
├── main.py                   Bot entry point
├── events_config.json        Event types and EP values
├── requirements.txt
├── .env.example              Config template
├── install.sh                One-command installer
│
├── cogs/
│   ├── verify.py             /verify, /bloxlink-sync, /whois
│   ├── roblox_group.py       /rank, /kickroblox, /joinrequests, etc.
│   ├── ep.py                 /ep add/subtract/check/audit, /leaderboard
│   ├── log_event.py          /log (OCR + manual)
│   ├── promotions.py         Auto-promotion task + /checkpromotions
│   ├── moderation.py         /ban, /kick, /mute, /warn, /purge, etc.
│   ├── report.py             /genreport, /weeklystats, sheets sync
│   ├── profile.py            /profile
│   └── fun.py                /ping, /poll, /avatar, etc.
│
├── storage/
│   └── database.py           JSON database, all read/write
│
└── utils/
    ├── roblox_api.py          Roblox API calls
    ├── ocr.py                 Tesseract OCR helpers
    └── sheets.py              Google Sheets + weekly stats
```

---

## Troubleshooting

**Commands not showing up** — Wait up to 1 hour for global sync. Re-invite the bot to force a refresh. Check `GUILD_ID` is set in `.env` for instant server-only sync.

**OCR not reading screenshot** — Make sure Tesseract is installed: `tesseract --version`. Reinstall with `sudo apt install -y tesseract-ocr`. Use manual entry as fallback.

**Roblox rank changes failing** — Check your `ROBLOX_COOKIE` hasn't expired. Get a new one from the bot account's browser. Make sure the bot account's rank is higher than any rank it needs to set.

**Bot offline after server reboot** — Run `pm2 startup` and follow its instructions to enable auto-start.
