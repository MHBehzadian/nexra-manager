# Telegram Automation

A production-grade Telegram automation tool. **Step 1** (this milestone) ships a
central, admin-only control bot with a first-run terminal setup wizard, robust
logging, and a clean, extensible project layout.

## Why Telethon (not Pyrogram)?

Both are excellent MTProto libraries. Telethon was chosen because this tool's
core goal is **many user sessions + forwarding**:

- **Multi-session at scale** — first-class `StringSession` support makes it
  trivial to store dozens of user accounts (in `.env`, a DB, or files) and spin
  clients up/down independently.
- **Mature forwarding API** — `client.forward_messages()` and the events system
  are stable and well-documented across versions.
- **Long-term stability** — a very stable public API and large community,
  which matters for a long-running service.

> Note: both libraries need `API_ID` / `API_HASH` (from my.telegram.org) even to
> run a *bot* — that's an MTProto requirement, not a Telethon quirk.

## Project layout

```
telegram-automation/
├── main.py            # entry point
├── config.py          # settings + first-run TUI wizard
├── bot/
│   ├── client.py      # BotApp: Telethon lifecycle + service wiring
│   ├── handlers.py    # admin-gated events, menus, add-account & numbers flows
│   ├── keyboards.py   # inline keyboard layouts
│   └── state.py       # conversation FSM (add-account, set-channel)
├── accounts/
│   ├── store.py       # async JSON persistence of account metadata
│   ├── manager.py     # Telethon session lifecycle (login/verify/remove)
│   └── coordinator.py # cross-account orchestration (join, read numbers)
├── database/
│   ├── models.py      # SQLAlchemy models (numbers, read_cursors)
│   └── db.py          # async repository (SQLAlchemy 2.0 + aiosqlite)
├── sender/
│   ├── content.py     # greetings, delay ranges, Iran-time active window
│   ├── media.py       # download & cache channel voice/images
│   └── engine.py      # per-account worker loops (the campaign)
├── sessions/          # per-account .session files (git-ignored)
├── data/              # accounts.json + app.db + media/ (git-ignored)
├── utils/
│   └── logger.py      # loguru config (console + rotating files)
├── logs/              # created at runtime
├── pyproject.toml
├── requirements.txt
└── .env.example
```

## Setup

```bash
# 1) create a virtual environment
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux/Mac: source .venv/bin/activate

# 2) install
pip install -r requirements.txt      # or:  pip install -e .

# 3) run — the first launch walks you through setup
python main.py
```

On first run you'll be asked for `API_ID`, `API_HASH`, `BOT_TOKEN`, and
`ADMIN_ID`. They are saved to a local `.env` (git-ignored). Alternatively, copy
`.env.example` to `.env` and fill it in yourself.

## Usage

Message your bot as the admin:

- `/start` or `/menu` — open the main menu (inline keyboard)
- `/id` — show your numeric id

Any non-admin user is rejected and the attempt is logged.

### Account management

From **👤 اکانت‌ها** (Accounts) in the main menu:

- **➕ Add account** — a guided conversation: session name → phone → login code
  → 2FA password (only if enabled). Each account is logged in with its own
  `sessions/<name>.session` file.
  - ⚠️ When sending the login **code**, send it with a separator
    (e.g. `1-2-3-4-5`) so Telegram doesn't invalidate a code posted as plain
    text in a chat. The bot strips non-digits automatically.
  - The bot best-effort deletes your code/password messages from the chat.
- **📋 List accounts** — shows every account with a 🟢 active / 🔴 inactive
  status. Opening one runs a live authorization check.
- **🗑 Remove account** — confirmation → remote log-out → deletes the session
  file and the store entry.
- **/cancel** (or the ❌ لغو button) aborts an in-progress add flow and cleans
  up any partial session.

Account metadata (session name, phone, status, timestamps) is stored in
`data/accounts.json`. Conversation state is tracked in memory per user
(`bot/state.py`).

### Numbers & channel

From **📇 شماره‌ها** (Numbers) in the main menu:

- **🔧 Set channel** — set the numbers channel (`CHANNEL_ID` / PNUMBERS). Accepts
  a `@username`, a numeric id (`-100…`), or an invite link. Persisted to `.env`.
  Can also be set from the terminal:
  ```bash
  python main.py set-channel @my_numbers_channel
  ```
- **🔗 Join all accounts** — makes every stored account join the channel.
  New accounts also **join automatically** right after being added.
- **📥 Read numbers** — each account reads the channel **ascending**, remembering
  its own last processed `message_id` (in `read_cursors`), parses `+98…` numbers,
  and upserts them into the `numbers` table (duplicates ignored). The channel's
  first message (a voice message, used later) has no text and is skipped.
- **📊 Number stats** — counts by status (pending / used / unknown / completed).

Numbers live in a SQLite database (`data/app.db`) via SQLAlchemy async:

| table          | columns                                                                              |
|----------------|--------------------------------------------------------------------------------------|
| `numbers`      | phone, status, assigned_to, text_sent_at, voice_sent_at, source_message_id, source_text |
| `read_cursors` | account_phone, channel_id, last_message_id                                           |

The **`AccountCoordinator`** (`accounts/coordinator.py`) ties accounts, the
channel, and the database together — it owns joining and ascending reads.

### Campaign (sending engine)

From **🚀 کمپین** (Campaign) in the main menu:

- **🔄 Refresh media** — downloads the channel's voice message(s) and image(s)
  once into `data/media/`. Messages are re-sent from this cache (not forwarded).
- **▶️ Start** — asks for confirmation ("با تایید من"), then starts one async
  worker per active account. **⏸ Stop** cancels them.
- **⏱ Voice-delay** — set the greeting→voice gap (default **15 min – 2 h**) live
  from the bot; stored in `data/campaign.json`.
- **💾 Backup now** / **📈 Report now** — send a backup / daily report to the admin
  on demand.

A single **dispatcher** hands out numbers round-robin, one per minute, each to a
*different* account (so a late channel edit can never cause a double message):

```
[only 06:00–24:00 Asia/Tehran]
  dispatcher: next account (round-robin) ← claim next pending number (atomic)
              → spawn a background task for it
              → wait 1 minute, then the next number goes to the next account

  per-number task:
    resolve phone to a Telegram user (ImportContacts); not on Telegram → unknown
    → random greeting  ..........  text_sent_at; channel edit "Task ✅"
    → wait 15 min – 2 h  (bot-configurable)
    → voice(s) + image(s) from cache ...  voice_sent_at; channel edit "Task ✅✅"
```

**Channel "Task" marker.** After each stage the source channel message is edited
(by the **bot**, which must be a channel admin with *Edit messages*) to append
`Task` + progress ticks — one tick = greeting sent, two ticks = voice sent, or
`Task ❌ <reason>` on failure (e.g. the number has no Telegram account). The
reader **skips any message that already contains `Task`**, so numbers are never
re-processed. If the bot can't edit (not admin), sending still works — the DB is
the source of truth and the atomic claim still prevents double-messaging.

- **No double-messaging:** atomic DB claim + round-robin + the 1-minute gap.
- **Account trouble** (spam-limited / banned / deauthorized) is detected: the
  reason is written on the channel message, the account is pulled from rotation
  (admin notified), and the number is handed to another account — re-greeted if
  it wasn't greeted yet, or resumed at the voice step if it was.
- **Resumable:** on restart, greeted-but-not-voiced numbers are finished (voice
  only, tracked in memory so in-flight numbers are never double-sent); numbers
  claimed but not yet greeted are requeued.
- Only runs inside the Iran-time window; `FloodWait` is retried automatically.

### Reports & backups

- **Daily** at 23:00 Tehran the bot DMs the admin a summary (people messaged in
  the last 24 h, queue counts) and sends a backup of `app.db` + `accounts.json`.
- The admin is notified immediately when the **pending queue is empty**
  («شماره‌ها تمام شدند»).

## Deployment (Linux server)

```bash
# on the server
git clone <repo> /opt/telegram-automation && cd /opt/telegram-automation
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# configure ONCE interactively (writes .env), then Ctrl-C after it connects:
python main.py            # or: cp .env.example .env && edit it

# install the service
sudo cp deploy/telegram-automation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-automation
journalctl -u telegram-automation -f      # follow logs
```

The service runs non-interactively, so `.env` must be configured first (the app
detects "no terminal" and exits with a clear message otherwise). Edit
`User`/`WorkingDirectory`/`ExecStart` in the unit file to match your paths.

## What's next

- Forwarding engine with rules, filters, and scheduling
- Per-account daily caps and richer scheduling controls
