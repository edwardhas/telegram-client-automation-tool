import asyncio
import configparser
import html
import logging
import shlex
import sqlite3
import shutil
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple, Optional, List, Union

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from pymongo import MongoClient
from bson import ObjectId

from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

# ───────────────────────────────────────────────────────────────────────────────
# Logging
# ───────────────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("bot")
logging.getLogger("telethon").setLevel(logging.INFO)  # set to DEBUG for deeper logs

# ───────────────────────────────────────────────────────────────────────────────
# Config
# ───────────────────────────────────────────────────────────────────────────────
config = configparser.ConfigParser(
    inline_comment_prefixes=(';', '#'),
    interpolation=None
)
if not config.read('config.ini'):
    raise RuntimeError("config.ini not found.")

def cfg(section: str, key: str, cast=str):
    val = config.get(section, key)
    return cast(val) if cast is not str else val

API_ID = cfg('default', 'api_id', int)
API_HASH = cfg('default', 'api_hash')
BOT_TOKEN = cfg('default', 'bot_token')

MONGO_URI = cfg('default', 'mongo_uri')
MONGO_DB = cfg('default', 'mongodb_name')
MONGO_COLL = cfg('default', 'collection_name')  # products collection name

# During setup you can leave None so everyone can use commands. Then set your ID.
AUTHORIZED_USER_ID = 983776045  # e.g., 123456789

# Service timezone & defaults for free-form scheduling
SERVICE_TZ = ZoneInfo("America/Los_Angeles")
DEFAULT_SEND_HOUR = 9  # 09:00 local time when user omits time (e.g., "Oct 25")

# ───────────────────────────────────────────────────────────────────────────────
# Helpers (general)
# ───────────────────────────────────────────────────────────────────────────────
def is_authorized(sender_id: int) -> bool:
    return AUTHORIZED_USER_ID is None or sender_id == AUTHORIZED_USER_ID

def esc(s: str) -> str:
    return html.escape(str(s), quote=True)

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def ensure_session_unlocked(session_base: str, max_wait_s: float = 0.5) -> None:
    """If Telethon's SQLite session is stuck locked, back it up and let it recreate."""
    db = Path(f"{session_base}.session")
    wal = Path(f"{session_base}.session-wal")
    shm = Path(f"{session_base}.session-shm")
    if not db.exists():
        return
    try:
        conn = sqlite3.connect(str(db), timeout=max_wait_s)
        with conn:
            conn.execute("PRAGMA user_version;")
        conn.close()
    except sqlite3.OperationalError as e:
        if "database is locked" not in str(e).lower():
            raise
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path("sessions_backup"); backup_dir.mkdir(parents=True, exist_ok=True)
        bdb = backup_dir / f"{db.name}.{ts}.bak"
        log.warning("Session DB locked; backing up to %s and recreating.", bdb)
        try:
            shutil.move(str(db), str(bdb))
        except Exception as move_err:
            log.warning("Could not move session DB: %s", move_err)
        for f in (wal, shm):
            try:
                if f.exists(): f.unlink()
            except Exception as rm_err:
                log.warning("Could not remove %s: %s", f, rm_err)

# ───────────────────────────────────────────────────────────────────────────────
# Product feature helpers (your existing CRUD)
# ───────────────────────────────────────────────────────────────────────────────
def caption_for(name: str, description: str) -> str:
    """
    Telegram photo caption limit is 1024 chars.
    Bold the name and add description on next line; truncate if needed.
    """
    base = f"<b>{esc(name)}</b>"
    if description:
        desc = esc(description)
        cap = f"{base}\n{desc}"
    else:
        cap = base
    if len(cap) > 1024:
        room = 1024 - len(base) - 1  # newline
        truncated = (desc[:max(0, room - 1)] + "…") if room > 0 else ""
        cap = f"{base}\n{truncated}" if truncated else base
    return cap

# NEW: robust parse /insert "Name" "Description" [images...] <time-or-Now>
def parse_insert_args_v2(text: str) -> Optional[Tuple[str, str, List[str], str]]:
    """
    /insert "Name" "Description" [https://a.jpg https://b.jpg] Oct 25 9am
    /insert "Name" "Description" [https://a.jpg,https://b.jpg] Now
    - Name, Description: quoted if they contain spaces
    - Images: tokens wrapped between '[' and ']' (spaces or commas inside)
    - Scheduled time: everything after the closing ']' as one free-form string (e.g., 'Oct 25 9am', 'Now')
    """
    parts = shlex.split(text)
    if len(parts) < 5 or parts[0].lower() != "/insert":
        return None

    name = parts[1]
    description = parts[2]

    i = 3
    if i >= len(parts):
        return None

    # Must start with a token that begins with '['
    if not parts[i].startswith('['):
        return None

    # Collect tokens until a token ending with ']'
    images_tokens = []
    while i < len(parts):
        tok = parts[i]
        images_tokens.append(tok)
        if tok.endswith(']'):
            break
        i += 1
    else:
        # no closing bracket
        return None

    # Join the bracketed segment and strip the brackets
    bracketed = " ".join(images_tokens).strip()
    if not (bracketed.startswith('[') and bracketed.endswith(']')):
        return None
    inner = bracketed[1:-1].strip()

    # Split inner by commas or whitespace
    images = [u.strip() for u in re.split(r"[,\s]+", inner) if u.strip()]

    # Everything after the closing ']' is the scheduled time string
    scheduled_str = " ".join(parts[i+1:]).strip()
    if not scheduled_str:
        return None

    return name, description, images, scheduled_str

def parse_update_args(text: str) -> Optional[Tuple[str, str, str, str]]:
    """
    /update <object_id> <name> <description> <url>
    Quotes supported for name/description. (Left unchanged here.)
    """
    parts = shlex.split(text)
    if len(parts) >= 5:
        oid = parts[1]
        name = parts[2]
        desc = " ".join(parts[3:-1])
        url = parts[-1]
        return oid, name, desc, url
    raw = text.strip().split(maxsplit=2)
    if len(raw) < 3:
        return None
    oid = raw[1]
    rest = raw[2]
    tail = rest.rsplit(maxsplit=1)
    if len(tail) < 2:
        return None
    middle, url = tail
    mid_parts = middle.split(maxsplit=1)
    if len(mid_parts) == 1:
        name, desc = mid_parts[0], ""
    else:
        name, desc = mid_parts[0], mid_parts[1]
    return oid, name, desc, url

# ───────────────────────────────────────────────────────────────────────────────
# Automation helpers (Option A)
# ───────────────────────────────────────────────────────────────────────────────
GLOBAL_GAP = 0.25  # seconds between API calls (gentle global throttle)
_last_sent_global = 0.0

async def throttle():
    """Simple global throttle to be nice to Telegram API."""
    global _last_sent_global
    now = asyncio.get_running_loop().time()
    gap = GLOBAL_GAP - (now - _last_sent_global)
    if gap > 0:
        await asyncio.sleep(gap)
    _last_sent_global = asyncio.get_running_loop().time()

async def send_text_safe(client: TelegramClient, chat_id: int, text: str, *, parse_mode: str = 'html', link_preview: bool = False):
    while True:
        try:
            await throttle()
            msg = await client.send_message(chat_id, text, parse_mode=parse_mode, link_preview=link_preview)
            return [msg.id]
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)

async def send_files_safe(client: TelegramClient, chat_id: int, files: List[str], *, caption: Optional[str] = None, parse_mode: str = 'html'):
    """
    Send one or multiple image URLs.
    - If one file: caption applied.
    - If multiple: send each individually (caption on first only) to keep it simple.
    """
    ids = []
    if not files:
        return ids

    for idx, f in enumerate(files):
        try:
            await throttle()
            cap = caption if (idx == 0 and caption) else None
            msg = await client.send_file(chat_id, f, caption=cap, parse_mode=parse_mode)
            ids.append(msg.id)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            await throttle()
            cap = caption if (idx == 0 and caption) else None
            msg = await client.send_file(chat_id, f, caption=cap, parse_mode=parse_mode)
            ids.append(msg.id)
        except Exception as e:
            log.warning("send_file failed for chat %s, url=%s: %s. Falling back to text link.", chat_id, f, e)
            fallback = f if idx > 0 else ((caption + "\n" if caption else "") + f)
            ids.extend(await send_text_safe(client, chat_id, fallback, parse_mode='html', link_preview=True))
    return ids

async def send_product_photo(client: TelegramClient, chat_id: int, name: str, description: str, url: str):
    """Legacy helper for single-image replies."""
    cap = caption_for(name, description)
    ids = await send_files_safe(client, chat_id, [url], caption=cap, parse_mode='html')
    return ids

def parse_iso_or_datetime(value: Union[str, datetime]) -> datetime:
    """
    Accepts:
      - Python datetime (assumed UTC if tz-aware, else treated as UTC)
      - ISO-8601 string with or without 'Z'
    Returns an aware datetime in UTC.
    """
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    s = str(value).strip()
    if s.endswith('Z'):
        s = s[:-1] + '+00:00'
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(f"scheduledAt is not ISO-8601: {value}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def parse_scheduled_to_utc(s: str) -> datetime:
    """
    Parse free-form time like:
      - 'October 25', 'Oct 25 9am PT', '2025-10-25T09:00'
      - 'Now'  (case-insensitive) → immediate send
    Returns timezone-aware UTC datetime.
    Rules:
      - 'Now' → current UTC + 1 second
      - date-only → default 09:00 local
      - no year → use this year; if already past, bump to next year
      - no tz → assume SERVICE_TZ
    """
    s = s.strip()
    if s.lower() == "now":
        return datetime.utcnow().replace(tzinfo=timezone.utc) + timedelta(seconds=1)

    now_local = datetime.now(tz=SERVICE_TZ)

    # dateutil parse with default baseline in local tz (so missing fields are sane)
    default_base = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    dt = dtparser.parse(s, dayfirst=False, fuzzy=True, default=default_base)

    # If no explicit time in the input, set default hour
    user_provided_time = bool(re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm))\b", s, re.I) or re.search(r"\b\d{1,2}:\d{2}\b", s))
    candidate = dt
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=SERVICE_TZ)
    if not user_provided_time:
        candidate = candidate.replace(hour=DEFAULT_SEND_HOUR, minute=0, second=0, microsecond=0)

    # If no year present in input, roll to next year if already past
    year_in_input = re.search(r"\b(19|20)\d{2}\b", s) is not None
    if not year_in_input and candidate.astimezone(SERVICE_TZ) < now_local:
        candidate = candidate.replace(year=candidate.year + 1)

    return candidate.astimezone(timezone.utc)

async def post_scheduled_message(client: TelegramClient, chat_id: int, msg_doc: dict):
    """
    Sends one scheduled message to a chat: text + optional images.
    msg_doc fields:
      - text (HTML ok)
      - images: list of URLs (optional)
      - parseMode: 'HTML'|'MarkdownV2'|'None' (default HTML)
      - disablePreview: bool
    """
    text = msg_doc.get("text", "")
    images = msg_doc.get("images", []) or []
    parse_mode = 'html' if msg_doc.get("parseMode", "HTML").upper() == "HTML" else None
    disable_preview = bool(msg_doc.get("disablePreview", True))

    message_ids = []
    if text:
        message_ids.extend(await send_text_safe(client, chat_id, text, parse_mode='html' if parse_mode else None, link_preview=not disable_preview))
    if images:
        caption = None
        message_ids.extend(await send_files_safe(client, chat_id, images, caption=caption, parse_mode='html' if parse_mode else None))
    return message_ids

# ───────────────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────────────
async def main():
    log.info("Starting bot…")

    # Sessions dir + session unlock
    Path("sessions").mkdir(parents=True, exist_ok=True)
    session_base = "sessions/Bot"
    ensure_session_unlocked(session_base)

    # Telethon client
    client = TelegramClient(session_base, API_ID, API_HASH)
    await client.start(bot_token=BOT_TOKEN)
    me = await client.get_me()
    my_id = me.id
    log.info("Connected as @%s (id=%s)", getattr(me, "username", None), my_id)

    # Mongo
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command("ping")
        log.info("MongoDB connection OK")
    except Exception as e:
        log.error("Mongo connection failed: %s", e)
        raise

    db = mongo[MONGO_DB]
    products = db[MONGO_COLL]                   # your product CRUD collection
    chats_coll = db["chats"]                    # auto-discovered chats
    scheduled_coll = db["scheduled_messages"]   # "Date table"

    # ── AUTO-DISCOVERY OF CHATS (no /bind needed) ──────────────────────────────
    async def upsert_chat(event_chat, *, active: bool = True):
        try:
            chat_id = getattr(event_chat, "id", None)
            if chat_id is None:
                return
            doc = {
                "chatId": chat_id,
                "title": getattr(event_chat, "title", None),
                "type": event_chat.__class__.__name__.lower(),
                "isActive": active,
                "lastSeenAt": datetime.utcnow().replace(tzinfo=timezone.utc)
            }
            chats_coll.update_one({"chatId": chat_id}, {"$set": doc, "$setOnInsert": {"firstSeenAt": doc["lastSeenAt"]}}, upsert=True)
        except Exception as e:
            log.warning("upsert_chat failed: %s", e)

    # Catch-all logger + auto-discover on any message
    @client.on(events.NewMessage)
    async def _log_and_discover(event):
        try:
            sender = await event.get_sender()
            log.info("Update: chat_id=%s sender_id=%s text=%r",
                     getattr(event.chat, "id", None), sender.id, event.raw_text)
            if event.chat:
                await upsert_chat(event.chat, active=True)
        except Exception as e:
            log.warning("Failed to log/discover update: %s", e)

    # Track when the bot is added/removed from groups/channels
    @client.on(events.ChatAction)
    async def on_chat_action(event):
        try:
            if (event.user_added or event.user_joined) and event.user_id == my_id:
                await upsert_chat(event.chat, active=True)
                log.info("Bot added to chat %s", getattr(event.chat, "id", None))
            if (event.user_kicked or event.user_left) and event.user_id == my_id:
                await upsert_chat(event.chat, active=False)
                log.info("Bot removed from chat %s", getattr(event.chat, "id", None))
        except Exception as e:
            log.warning("ChatAction handling failed: %s", e)

    # ── ADMIN/UTILITY COMMANDS ─────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"(?i)^/id(?:\s|$)"))
    async def on_id(event):
        sender = await event.get_sender()
        await event.reply(f"Your Telegram numeric ID:\n<code>{sender.id}</code>", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/start(?:\s|$)"))
    async def on_start(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            log.info("Unauthorized /start from %s", sender.id)
            return
        await event.reply(
            "👋 Bot is alive.\n"
            "Product commands:\n"
            " • /select\n"
            " • /insert \"Name\" \"Description\" [https://a.jpg https://b.jpg] Oct 25 9am | Now\n"
            " • /update <object_id> <name> <description> <url>\n"
            " • /delete <object_id>\n\n"
            "Automation: the bot auto-sends docs from 'scheduled_messages' to all active chats.",
            parse_mode='html'
        )

    # ── PRODUCT CRUD ───────────────────────────────────────────────────────────
    @client.on(events.NewMessage(pattern=r"(?i)^/select(?:\s|$)"))
    async def on_select(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
        # Read images[] (fallback to legacy url)
        cursor = products.find({}, projection={"name": 1, "description": 1, "images": 1, "url": 1}).limit(50)
        found = False
        try:
            for doc in cursor:
                found = True
                name = doc.get("name", "")
                description = doc.get("description", "")
                images = doc.get("images") or []
                if not images and doc.get("url"):
                    images = [doc["url"]]
                if images:
                    await send_files_safe(client, event.chat_id, [images[0]], caption=caption_for(name, description))
                else:
                    await client.send_message(event.chat_id, caption_for(name, description), parse_mode='html')
            if not found:
                await event.reply("No products found.")
        except Exception as e:
            await event.reply(f"❌ Error listing products: <code>{esc(e)}</code>", parse_mode='html')

    # NEW /insert format with images[] + scheduled time (or Now)
    @client.on(events.NewMessage(pattern=r"(?i)^/insert(?:\s|$)"))
    async def on_insert(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return

        parsed = parse_insert_args_v2(event.raw_text)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                "<code>/insert \"Name\" \"Description\" [https://a.jpg https://b.jpg] Oct 25 9am</code>\n"
                "or\n"
                "<code>/insert \"Name\" \"Description\" [https://a.jpg] Now</code>\n"
                "Notes:\n"
                " • Images must be inside [ ] separated by spaces or commas\n"
                " • Time can be date-only (defaults 09:00 local) or full time; use 'Now' to send immediately\n",
                parse_mode='html'
            )

        name, description, images, when_str = parsed

        try:
            scheduled_utc = parse_scheduled_to_utc(when_str)
        except Exception as e:
            return await event.reply(f"❌ Could not parse time: <code>{esc(e)}</code>", parse_mode='html')

        # 1) insert product (images always an array)
        product_doc = {
            "name": name,
            "description": description,
            "images": images,
            "last_edit": today_str()
        }
        try:
            res = products.insert_one(product_doc)
            product_id = res.inserted_id
        except Exception as e:
            return await event.reply(f"❌ Insert error: <code>{esc(e)}</code>", parse_mode='html')

        # 2) create scheduled message for ALL chats
        text = f"<b>{esc(name)}</b>\n{esc(description)}"
        sched_doc = {
            "_id": f"auto_{product_id}",
            "text": text,
            "images": images,
            "parseMode": "HTML",
            "disablePreview": True,
            "scheduledAt": scheduled_utc,
            "targets": "all",
            "status": "scheduled",
            "productId": product_id
        }
        try:
            db["scheduled_messages"].insert_one(sched_doc)
        except Exception as e:
            log.warning("Failed to insert scheduled message: %s", e)

        # 3) echo the product back now (first image + caption) and confirm schedule
        if images:
            await send_files_safe(client, event.chat_id, [images[0]], caption=caption_for(name, description))
        else:
            await client.send_message(event.chat_id, text, parse_mode='html')

        local_time = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
        utc_time = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
        await event.reply(f"📅 Scheduled for: <b>{esc(local_time)}</b> (<code>{esc(utc_time)}</code>)", parse_mode='html')

    # Keep /update and /delete as-is (legacy single url for now)
    @client.on(events.NewMessage(pattern=r"(?i)^/update(?:\s|$)"))
    async def on_update(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
        parsed = parse_update_args(event.raw_text)
        if not parsed:
            return await event.reply(
                "Usage:\n<code>/update OBJECT_ID \"New Name\" \"New description\" https://example.com/image.jpg</code>",
                parse_mode='html'
            )
        oid_str, name, description, url = parsed
        try:
            oid = ObjectId(oid_str)
        except Exception:
            return await event.reply("❌ Invalid ObjectId.", parse_mode='html')

        try:
            res = products.update_one(
                {"_id": oid},
                {"$set": {"name": name, "description": description, "url": url, "last_edit": today_str()}}
            )
            if not res.matched_count:
                return await event.reply(f"Not found: <code>{esc(oid_str)}</code>", parse_mode='html')
            await send_product_photo(client, event.chat_id, name, description, url)
        except Exception as e:
            await event.reply(f"❌ Update error: <code>{esc(e)}</code>", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/delete(?:\s|$)"))
    async def on_delete(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
        parts = shlex.split(event.raw_text)
        if len(parts) < 2:
            return await event.reply("Usage: <code>/delete OBJECT_ID</code>", parse_mode='html')
        oid_str = parts[1]
        try:
            oid = ObjectId(oid_str)
        except Exception:
            return await event.reply("❌ Invalid ObjectId.", parse_mode='html')

        try:
            res = products.delete_one({"_id": oid})
            if res.deleted_count:
                await event.reply(f"🗑️ Deleted <code>{esc(oid_str)}</code>.", parse_mode='html')
            else:
                await event.reply(f"Not found: <code>{esc(oid_str)}</code>.", parse_mode='html')
        except Exception as e:
            await event.reply(f"❌ Delete error: <code>{esc(e)}</code>", parse_mode='html')

    # ── SCHEDULER LOOP (Option A) ───────────────────────────────────────────────
    async def scheduler_loop():
        log.info("Scheduler loop started.")
        while True:
            try:
                now_utc = datetime.utcnow().replace(tzinfo=timezone.utc)
                # Fetch all due, scheduled messages
                due_cur = scheduled_coll.find({
                    "status": {"$in": ["scheduled", None]},
                    "scheduledAt": {"$lte": now_utc}
                })

                for msg in due_cur:
                    msg_id = msg.get("_id")
                    try:
                        # Mark "processing" to avoid double send if loop iteration overlaps
                        scheduled_coll.update_one({"_id": msg_id, "status": {"$in": ["scheduled", None]}},
                                                  {"$set": {"status": "processing"}})

                        # Normalize scheduledAt type if it's string
                        try:
                            _ = parse_iso_or_datetime(msg.get("scheduledAt", now_utc))
                        except Exception:
                            scheduled_coll.update_one({"_id": msg_id}, {"$set": {"status": "failed", "lastError": "Invalid scheduledAt"}})
                            continue

                        # Determine targets
                        targets = msg.get("targets", "all")
                        if targets == "all":
                            chat_ids = [d["chatId"] for d in chats_coll.find({"isActive": True}, {"chatId": 1})]
                        else:
                            chat_ids = list(targets or [])

                        if not chat_ids:
                            log.info("No active chats found for message %s; marking sent.", msg_id)
                            scheduled_coll.update_one({"_id": msg_id}, {"$set": {"status": "sent", "deliveredTo": []}})
                            continue

                        delivered = []
                        for cid in chat_ids:
                            try:
                                ids = await post_scheduled_message(client, cid, msg)
                                delivered.append({"chatId": cid, "messageIds": ids})
                            except Exception as e:
                                log.warning("Sending to chat %s failed: %s", cid, e)
                                # Optionally deactivate chat on permanent errors
                                # chats_coll.update_one({"chatId": cid}, {"$set": {"isActive": False, "lastError": str(e)}})

                        scheduled_coll.update_one({"_id": msg_id}, {"$set": {"status": "sent", "deliveredTo": delivered}})
                        log.info("Message %s sent to %d chats", msg_id, len(delivered))

                    except Exception as e:
                        log.exception("Error processing scheduled message %s: %s", msg_id, e)
                        scheduled_coll.update_one({"_id": msg_id}, {"$set": {"status": "failed", "lastError": str(e)}})

            except Exception as loop_err:
                log.exception("Scheduler loop iteration error: %s", loop_err)

            await asyncio.sleep(10)  # wake every 10 seconds

    # ---- Start scheduler in the background
    asyncio.create_task(scheduler_loop())

    log.info("Bot Started…")
    await client.run_until_disconnected()

# ───────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
