# Telegram Automation Refactor (Mongo-only + API + Vue 3 Admin)

This refactor splits your current all-in-one Telethon script into three clean parts:

- **worker/**: Telethon “sender” service (discovers chats + executes due scheduled messages)
- **backend/**: FastAPI REST API (CRUD + deliveries + templates)
- **web/**: Vue 3 admin UI (form-based scheduling)

## Why this is easier to manage

- You stop relying on complex Telegram command parsing (`/insert`, `/save`, `/done`) and instead schedule everything through a **single source of truth**: `scheduled_messages` in MongoDB.
- The worker becomes small and purpose-built: *“read due jobs from Mongo, send, log deliveries.”*
- The UI gives you a real CRUD workflow (list, create, run-now, delete) without touching bot code.

## Mongo-only?

Yes — this design uses **MongoDB as the only database**.

**Recommendation:** Mongo-only is totally fine here.
- ✅ Great fit for job documents, delivery logs, and campaign templates
- ✅ Easy to index + query for “due jobs”
- ⚠️ Make sure you keep the right indexes (already created in `backend/db.py` and `worker/worker.py`)

## Collections used

- `chats` – discovered group/channel chat IDs (negative `-100...`)
- `scheduled_messages` – scheduled jobs (one-time or cron)
- `deliveries` – per-chat send log (deduped by `(scheduledId, chatId)`)
- `saved_campaigns` – optional templates

(Your existing `Announcements` collection is left alone; you can keep it if it’s useful for a content library.)

## Setup

### 1) Run the worker

The worker requires a **Telethon user session** (StringSession), and MongoDB connection.

```bash
cd worker
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env
python worker.py
```

### 2) Run the API

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env (set ADMIN_TOKEN)
uvicorn main:app --host 0.0.0.0 --port 8000

!! after everything is configured - run the following code from the root:
python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
```

### 3) Run the Vue 3 UI

```bash
cd web
npm install
cp .env.example .env
npm run dev
```

Open the UI at `http://localhost:5173`, go to **Settings**, and paste the same `ADMIN_TOKEN` from the backend.

## Migration notes for your current DB

Your current `config.ini` has non-standard collection names:
- `scheduled_collection = schedules_messages`
- `saved_collection = saved_compaigns`

In this refactor I standardized these to:
- `scheduled_messages`
- `saved_campaigns`

If you want to keep your existing names (no rename), just change the collection env vars in `backend/.env` and `worker/.env`.

## Security notes (important)

- Keep the API behind a firewall / VPN if possible.
- If you expose it publicly, add HTTPS + strong auth (token rotation, rate limits, optional IP allowlist).
- Do **not** commit your `.env` files.

