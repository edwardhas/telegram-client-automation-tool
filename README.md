# Telegram Automation Tool (MongoDB + API + Vue 3 Admin)

This version splits the Internal-Control-v1-0.0.0 Telethon script into three main parts:

- **worker/**: Telethon “sender” service (discovers chats + executes due scheduled messages, works with Telegram directly)
- **backend/**: FastAPI REST API (Create/Read/Update/Delete Operations + deliveries + templates)
- **web/** */: Vue 3 admin UI (task scheduling + management + monitoring)

## Why this is better than Internal-Control-v1-0.0.0

- Stop relying on Telegram command parsing (`/insert`, `/save`, `/done`) and instead schedule everything through a **single source of truth**: `scheduled_messages` in MongoDB.
- The worker becomes small and purpose-built: *“read due jobs from Mongo, send, log deliveries.”*
- The UI gives you a real CRUD workflow (list, create, run, delete) without touching bot code.


## Collections used

- `chats` – discovered group/channel chat IDs (in Telegram it is usually negative numbers `-100...`)
- `scheduled_messages` – scheduled jobs (one-time or cron)
- `deliveries` – per-chat send log (deduped by `(scheduledId, chatId)`)
- `saved_campaigns` – optional templates

## Setup


### 1) Run the Backend API

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env (set ADMIN_TOKEN)

!! after everything is configured - run the following code from the root:
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```


### 2) Run the Vue 3 Control WebSite

```bash
cd web
npm install
cp .env.example .env
npm run dev
```

Open the UI at `http://localhost:5173`, go to **Settings**, and paste the same `ADMIN_TOKEN` from the backend.



### 3) Run the worker

The worker requires a **Telethon user session** (StringSession), and MongoDB connection.

**session_string_generator.py**
Generates a Telethon StringSession for your PERSONAL Telegram account.

Steps:
  1) Install: pip install telethon
  2) Run: python session_string_generator.py
  3) Enter your phone & code that you will receive in your Telegram App (and 2FA if enabled). Copy the printed string and paste into .env variable as SESSION_STRING.

```bash
cd worker
python -m venv .venv 
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python worker.py
```
