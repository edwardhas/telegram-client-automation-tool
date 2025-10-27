import asyncio
import configparser
import html
import logging
import shlex
import sqlite3
import shutil
import re
import json
import hashlib
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Tuple, Optional, List, Union

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.tl.types import ChatBannedRights  # optional type hint only
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
logging.getLogger("telethon").setLevel(logging.INFO)  # set to DEBUG for deep logs

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

# NEW: robust parse /insert "Name" "Description" [images...] <time-or-Now> [groups=[-1001,-1002]]
def parse_insert_args_v2(text: str) -> Optional[Tuple[str, str, List[str], str, List[int]]]:
    """
    Accepts:
      /insert "Name" "Description" [https://a.jpg https://b.jpg] Oct 25 9am
      /insert "Name" "Description" [https://a.jpg,https://b.jpg] Now
      /insert "Name" "Description" [img] Oct 30 4pm groups=[-1001,-1002]
    - Skips duplicate '/insert' tokens at the start
    - Images: collect tokens between '[' and ']' (spaces or commas inside)
    - Scheduled time: everything after the closing ']' up to optional 'groups=[...]'
    - Optional target groups as groups=[id1,id2,...]
    """
    parts = shlex.split(text)
    if not parts:
        return None

    # Skip any leading '/insert' tokens
    i = 0
    while i < len(parts) and parts[i].lower() == "/insert":
        i += 1

    # Need at least: name, description, [images], schedule/Now
    if i + 4 > len(parts):
        return None

    name = parts[i]; i += 1
    description = parts[i]; i += 1

    # images must start with '['
    if i >= len(parts) or not parts[i].startswith('['):
        return None

    images_tokens = []
    while i < len(parts):
        tok = parts[i]
        images_tokens.append(tok)
        if tok.endswith(']'):
            i += 1
            break
        i += 1
    else:
        # never found closing bracket
        return None

    bracketed = " ".join(images_tokens).strip()
    if not (bracketed.startswith('[') and bracketed.endswith(']')):
        return None
    inner = bracketed[1:-1].strip()
    images = [u.strip() for u in re.split(r"[,\s]+", inner) if u.strip()]

    # Everything after images could be "<time>" [groups=[...]]
    trailing = " ".join(parts[i:]).strip()
    if not trailing:
        return None

    # Optional groups=[...]
    target_ids: List[int] = []
    m = re.search(r"\bgroups=\[(.*?)\]\s*$", trailing, flags=re.I)
    if m:
        groups_blob = m.group(1)
        trailing = trailing[:m.start()].strip()  # remove groups=[...] from time part
        for g in groups_blob.split(","):
            g = g.strip()
            if not g:
                continue
            try:
                target_ids.append(int(g))
            except ValueError:
                pass

    scheduled_str = trailing.strip()
    if not scheduled_str:
        return None

    # unique target ids (if any)
    target_ids = sorted(set(target_ids))

    return name, description, images, scheduled_str, target_ids

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

# Campaign hash (optional dedupe across time/content)
def campaign_hash(text: str, images: List[str]) -> str:
    payload = {
        "text": (text or "").strip(),
        "images": sorted(images or [])
    }
    h = hashlib.sha256(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode('utf-8')).hexdigest()
    return f"sha256:{h}"

# ───────────────────────────────────────────────────────────────────────────────
# Automation helpers
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

# ---------- NEW: robust album sender with local downloads + upload ----------
async def _download_one(url: str, dest_dir: str) -> Optional[str]:
    """Download a single image to dest_dir. Returns path or None on failure."""
    guess = url.split("?")[0].lower()
    ext = ".jpg"
    for e in (".jpg", ".jpeg", ".png", ".webp"):
        if guess.endswith(e):
            ext = e
            break
    filename = hashlib.sha1(url.encode("utf-8")).hexdigest() + ext
    path = os.path.join(dest_dir, filename)

    try:
        import aiohttp  # type: ignore
        timeout = aiohttp.ClientTimeout(total=25)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                with open(path, "wb") as f:
                    f.write(data)
        return path
    except Exception as e:
        try:
            import urllib.request
            with urllib.request.urlopen(url, timeout=25) as r:
                data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
            return path
        except Exception as e2:
            log.warning("Download failed for %s: %s / %s", url, e, e2)
            return None

def _ensure_photo_jpeg(src_path: str, dest_dir: str) -> Optional[str]:
    """
    Try to make sure Telegram treats it as a photo:
    - If Pillow available: open, convert to RGB, save as JPEG (quality=85)
    - Else: return original path
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return src_path
    try:
        with Image.open(src_path) as im:
            # small safety: reject obviously non-image
            im.verify()
        with Image.open(src_path) as im2:
            if im2.mode not in ("RGB", "L"):
                im2 = im2.convert("RGB")
            out_name = hashlib.md5(src_path.encode("utf-8")).hexdigest() + ".jpg"
            out_path = os.path.join(dest_dir, out_name)
            im2.save(out_path, format="JPEG", quality=85, optimize=True)
            return out_path
    except Exception as e:
        log.warning("Pillow re-encode failed for %s: %s", src_path, e)
        return src_path

async def _upload_all(client: TelegramClient, paths: List[str]):
    """
    Upload files to Telegram to ensure they're photos, not documents.
    Returns a list of UploadFile objects.
    """
    uploaded = []
    for p in paths:
        await throttle()
        uf = await client.upload_file(p, file_name=os.path.basename(p))
        uploaded.append(uf)
    return uploaded

async def send_files_safe(
    client: TelegramClient,
    chat_id: int,
    files: List[str],
    *,
    caption: Optional[str] = None,
    parse_mode: str = 'html'
):
    """
    Force a single grouped post (album) whenever there are multiple images:
    1) Download up to 10 images to local temp files.
    2) Re-encode to JPEG (if Pillow available) to guarantee 'photo' type.
    3) Upload all first; then send as one album with caption on the first media.
    4) If that fails, retry with raw URLs list.
    5) Final fallback: text + links.
    """
    if not files:
        return []

    files = files[:10]  # Telegram album limit

    tmpdir = tempfile.mkdtemp(prefix="tg_album_")
    local_paths: List[str] = []
    prepared_paths: List[str] = []
    try:
        # 1) Download
        for u in files:
            p = await _download_one(u, tmpdir)
            if p:
                local_paths.append(p)

        # We want all of them; otherwise albums can fail. If any missing, still try with what we have.
        if len(local_paths) >= 2:
            # 2) Re-encode to JPEG for consistency
            for p in local_paths:
                prepared_paths.append(_ensure_photo_jpeg(p, tmpdir) or p)

            try:
                # 3) Upload first to ensure Telegram sees them as photos
                uploaded = await _upload_all(client, prepared_paths)
                await throttle()
                result = await client.send_file(
                    chat_id,
                    uploaded,
                    caption=caption,
                    parse_mode=parse_mode,
                    force_document=False
                )
                if isinstance(result, list):
                    return [m.id for m in result]
                return [result.id]
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds + 1)
                await throttle()
                uploaded = await _upload_all(client, prepared_paths)
                result = await client.send_file(
                    chat_id,
                    uploaded,
                    caption=caption,
                    parse_mode=parse_mode,
                    force_document=False
                )
                if isinstance(result, list):
                    return [m.id for m in result]
                return [result.id]
            except Exception as e:
                log.warning("Uploaded album send failed for chat %s: %s. Will retry with URLs.", chat_id, e)

        # 4) Retry with URL list (may still group if Telegram accepts them)
        try:
            await throttle()
            result = await client.send_file(
                chat_id,
                files,
                caption=caption,
                parse_mode=parse_mode,
                force_document=False
            )
            if isinstance(result, list):
                return [m.id for m in result]
            return [result.id]
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            await throttle()
            result = await client.send_file(
                chat_id,
                files,
                caption=caption,
                parse_mode=parse_mode,
                force_document=False
            )
            if isinstance(result, list):
                return [m.id for m in result]
            return [result.id]
        except Exception as e:
            # 5) Final fallback: text + links (last resort)
            log.warning("URL album send failed for chat %s: %s. Falling back to text+links.", chat_id, e)
            ids = []
            if caption:
                ids.extend(await send_text_safe(client, chat_id, caption, parse_mode=parse_mode, link_preview=True))
            for f in files:
                ids.extend(await send_text_safe(client, chat_id, f, parse_mode=parse_mode, link_preview=True))
            return ids
    finally:
        # cleanup temp files
        try:
            # remove all files we created
            for p in set(local_paths + prepared_paths):
                try: os.remove(p)
                except Exception: pass
            os.rmdir(tmpdir)
        except Exception:
            pass

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
      - 'Now'  (case-insensitive) → send in 5 minutes
    Returns timezone-aware UTC datetime.
    Rules:
      - 'Now' → current UTC + 5 minutes  (changed per request)
      - date-only → default 09:00 local
      - no year → use this year; if already past, bump to next year
      - no tz → assume SERVICE_TZ
    """
    s = s.strip()
    if s.lower() == "now":
        return datetime.now(timezone.utc) + timedelta(minutes=5)

    now_local = datetime.now(tz=SERVICE_TZ)
    default_base = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    dt = dtparser.parse(s, dayfirst=False, fuzzy=True, default=default_base)

    user_provided_time = bool(re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm))\b", s, re.I) or re.search(r"\b\d{1,2}:\d{2}\b", s))
    candidate = dt
    if candidate.tzinfo is None:
        candidate = candidate.replace(tzinfo=SERVICE_TZ)
    if not user_provided_time:
        candidate = candidate.replace(hour=DEFAULT_SEND_HOUR, minute=0, second=0, microsecond=0)

    year_in_input = re.search(r"\b(19|20)\d{2}\b", s) is not None
    if not year_in_input and candidate.astimezone(SERVICE_TZ) < now_local:
        candidate = candidate.replace(year=candidate.year + 1)

    return candidate.astimezone(timezone.utc)

async def post_scheduled_message(client: TelegramClient, chat_id: int, msg_doc: dict):
    """
    Send one scheduled message as a single post:
    - If images present: send as album with caption = text
    - If no images: send text-only message
    """
    text = msg_doc.get("text", "") or ""
    images = (msg_doc.get("images", []) or [])[:10]  # enforce album limit
    parse_mode = 'html' if msg_doc.get("parseMode", "HTML").upper() == "HTML" else None
    disable_preview = bool(msg_doc.get("disablePreview", True))

    if images:
        return await send_files_safe(client, chat_id, images, caption=text if text else None, parse_mode=parse_mode or 'html')
    else:
        return await send_text_safe(client, chat_id, text, parse_mode=parse_mode or 'html', link_preview=not disable_preview)

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
    chats_coll = db["chats"]                    # chat registry
    scheduled_coll = db["scheduled_messages"]   # campaigns / date table
    deliveries_coll = db["deliveries"]          # per-chat idempotency

    # Guard: don't accidentally use a sessions-like collection for products
    if MONGO_COLL.strip().lower() in {"session", "sessions"}:
        raise RuntimeError(
            f"MONGO_COLL='{MONGO_COLL}' looks like a sessions collection. "
            "Change 'collection_name' in config.ini to something like 'products'."
        )

    # ── Create helpful indexes (idempotent) ────────────────────────────────────
    try:
        chats_coll.create_index("chatId", unique=True)
        scheduled_coll.create_index([("status", 1), ("scheduledAt", 1)])
        deliveries_coll.create_index([("schedId", 1), ("chatId", 1)], unique=True)
    except Exception as e:
        log.warning("Index creation warning: %s", e)

    # ── EVENT-DRIVEN CHAT DISCOVERY (no iter_dialogs for bots) ─────────────────
    async def upsert_chat(event_chat, *, active: bool = True):
        try:
            chat_id = getattr(event_chat, "id", None)
            if chat_id is None:
                return
            doc = {
                "chatId": chat_id,
                "title": getattr(event_chat, "title", getattr(event_chat, "username", None)),
                "type": event_chat.__class__.__name__.lower(),  # 'channel','chat','user',...
                "isActive": active,
                "lastSeenAt": datetime.now(timezone.utc)
            }
            chats_coll.update_one(
                {"chatId": chat_id},
                {"$set": doc, "$setOnInsert": {"firstSeenAt": doc["lastSeenAt"]}},
                upsert=True
            )
        except Exception as e:
            log.warning("upsert_chat failed: %s", e)

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

    # QUICK REGISTER CURRENT GROUP/CHANNEL
    @client.on(events.NewMessage(pattern=r"(?i)^/here(?:\s|$)"))
    async def on_here(event):
        """Run /here inside a group/channel to register it immediately."""
        if not event.chat:
            return await event.reply("This command must be used in a group/channel.")
        await upsert_chat(event.chat, active=True)
        await event.reply(f"Registered this chat: <code>{getattr(event.chat, 'id', None)}</code>", parse_mode='html')

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
            " • /insert \"Name\" \"Description\" [https://a.jpg https://b.jpg] Oct 25 9am | Now [groups=[-1001,-1002]]\n"
            " • /update <object_id> <name> <description> <url>\n"
            " • /delete <object_id>\n\n"
            "Admin:\n"
            " • /here   (run this inside a group to register it)\n"
            " • /groups_add [ -1001 -1002 ]\n"
            " • /groups_list\n\n"
            "Automation: campaigns in 'scheduled_messages' are sent to explicit targetChatIds or to all active chats.",
            parse_mode='html'
        )

    # Add groups manually (private, admin only)
    @client.on(events.NewMessage(pattern=r"(?i)^/groups_add(?:\s|$)"))
    async def on_groups_add(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
        parts = shlex.split(event.raw_text)
        if len(parts) < 2:
            return await event.reply('Usage: <code>/groups_add [ -1001 -1002 ]</code>', parse_mode='html')

        blob = " ".join(parts[1:])
        m = re.search(r"\[(.*)\]", blob)
        if not m:
            return await event.reply('Usage: <code>/groups_add [ -1001 -1002 ]</code>', parse_mode='html')

        ids = []
        for token in re.split(r"[,\s]+", m.group(1).strip()):
            if not token:
                continue
            try:
                ids.append(int(token))
            except ValueError:
                pass
        ids = sorted(set(ids))
        if not ids:
            return await event.reply("No valid IDs found.", parse_mode='html')

        added = 0
        for cid in ids:
            try:
                chats_coll.update_one(
                    {"chatId": cid},
                    {"$set": {"chatId": cid, "title": None, "type": "channel", "isActive": True, "lastSeenAt": datetime.now(timezone.utc)},
                     "$setOnInsert": {"firstSeenAt": datetime.now(timezone.utc)}},
                    upsert=True
                )
                added += 1
            except Exception as e:
                log.warning("Failed to upsert chatId %s: %s", cid, e)
        await event.reply(f"✅ Registered/updated {added} chat IDs.", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/groups_list(?:\s|$)"))
    async def on_groups_list(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
        docs = list(chats_coll.find({}, {"chatId": 1, "title": 1, "type": 1, "isActive": 1}).limit(200))
        if not docs:
            return await event.reply("No known chats.", parse_mode='html')
        lines = []
        for d in docs:
            lines.append(f"{d.get('chatId')} | {esc(d.get('title') or '')} | {d.get('type')} | {'active' if d.get('isActive') else 'inactive'}")
        await event.reply("<b>Known chats</b>:\n" + "\n".join(lines), parse_mode='html')

    # ── PRODUCT CRUD (unchanged, lightweight) ──────────────────────────────────
    @client.on(events.NewMessage(pattern=r"(?i)^/select(?:\s|$)"))
    async def on_select(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return
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

    # NEW /insert format with images[] + scheduled time (or Now) + optional groups=[...]
    @client.on(events.NewMessage(pattern=r"(?i)^/insert(?:\s|$)"))
    async def on_insert(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return

        raw = event.raw_text.strip()
        if raw.lower().startswith("/insert /insert"):
            raw = raw[8:].lstrip()

        parsed = parse_insert_args_v2(raw)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                "<code>/insert \"Name\" \"Description\" [https://a.jpg https://b.jpg] Oct 25 9am</code>\n"
                "or\n"
                "<code>/insert \"Name\" \"Description\" [https://a.jpg] Now</code>\n"
                "Optional explicit targets:\n"
                "<code>/insert \"Name\" \"Description\" [img] Oct 30 4pm groups=[-1001,-1002]</code>\n"
                "Notes:\n"
                " • Images must be inside [ ] separated by spaces or commas\n"
                " • Time can be date-only (defaults 09:00 local) or full time; use 'Now' to send in 5 minutes\n",
                parse_mode='html'
            )

        name, description, images, when_str, target_ids = parsed

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

        # 2) create scheduled message
        text = f"<b>{esc(name)}</b>\n{esc(description)}"
        sched_doc = {
            "_id": f"auto_{product_id}",
            "text": text,
            "images": images,
            "parseMode": "HTML",
            "disablePreview": True,
            "scheduledAt": scheduled_utc,
            "targets": "all" if not target_ids else "explicit",
            "targetChatIds": target_ids if target_ids else None,
            "status": "scheduled",
            "productId": product_id,
            "createdAt": datetime.now(timezone.utc),
            "contentHash": campaign_hash(text, images)
        }
        # Remove None fields
        sched_doc = {k: v for k, v in sched_doc.items() if v is not None}
        try:
            db["scheduled_messages"].insert_one(sched_doc)
        except Exception as e:
            log.warning("Failed to insert scheduled message: %s", e)

        # 2b) Resolve and SHOW targets right now in the reply
        if target_ids:
            resolved_chat_ids = sorted(set(target_ids))
        else:
            resolved_chat_ids = [
                d["chatId"]
                for d in chats_coll.find(
                    {"isActive": True, "type": {"$ne": "user"}},
                    {"chatId": 1}
                )
            ]
            resolved_chat_ids = sorted(set(int(x) for x in resolved_chat_ids))

        # 3) echo preview now (first image + caption) and confirm schedule + targets
        if images:
            await send_files_safe(client, event.chat_id, [images[0]], caption=caption_for(name, description))
        else:
            await client.send_message(event.chat_id, text, parse_mode='html')

        local_time = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
        utc_time = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
        if resolved_chat_ids:
            preview = ", ".join(str(c) for c in resolved_chat_ids[:10])
            suffix = "" if len(resolved_chat_ids) <= 10 else f" …(+{len(resolved_chat_ids)-10} more)"
            targets_line = f"targets: {len(resolved_chat_ids)} chats [{preview}{suffix}]"
        else:
            targets_line = ("targets: none yet — add with /here in a group or "
                            "/groups_add [ -1001234567890 ]")

        await event.reply(
            f"📅 Scheduled for: <b>{esc(local_time)}</b> (<code>{esc(utc_time)}</code>)\n"
            f"{esc(targets_line)}",
            parse_mode='html'
        )

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

    # ── SCHEDULER LOOP with per-chat idempotency ───────────────────────────────
    async def scheduler_loop():
        log.info("Scheduler loop started.")
        while True:
            try:
                now_utc = datetime.now(timezone.utc)
                # Fetch all due, scheduled messages
                due_cur = scheduled_coll.find({
                    "status": {"$in": ["scheduled", None, "processing"]},
                    "scheduledAt": {"$lte": now_utc}
                }).limit(50)

                for msg in due_cur:
                    msg_id = msg.get("_id")
                    try:
                        # Mark processing (idempotent)
                        scheduled_coll.update_one(
                            {"_id": msg_id},
                            {"$set": {"status": "processing"}}
                        )

                        # Normalize scheduledAt type if it's string
                        try:
                            _ = parse_iso_or_datetime(msg.get("scheduledAt", now_utc))
                        except Exception:
                            scheduled_coll.update_one({"_id": msg_id}, {"$set": {"status": "failed", "lastError": "Invalid scheduledAt"}})
                            continue

                        # Determine targets: explicit > all-active-non-user
                        explicit_ids = msg.get("targetChatIds") or []
                        if explicit_ids:
                            chat_ids = sorted(set(int(x) for x in explicit_ids))
                        elif msg.get("targets", "all") == "all":
                            chat_ids = [
                                d["chatId"]
                                for d in chats_coll.find(
                                    {"isActive": True, "type": {"$ne": "user"}},
                                    {"chatId": 1, "type": 1}
                                )
                            ]
                        else:
                            chat_ids = []

                        delivered = []
                        for cid in chat_ids:
                            # Per-chat claim-before-send (unique (schedId, chatId))
                            try:
                                res = deliveries_coll.update_one(
                                    {"schedId": msg_id, "chatId": cid},
                                    {"$setOnInsert": {"claimedAt": now_utc}},
                                    upsert=True
                                )
                                if not res.upserted_id:
                                    continue
                            except Exception as e:
                                log.warning("Claim (schedId=%s, chatId=%s) failed: %s", msg_id, cid, e)
                                continue

                            # Perform the send (album enforced inside)
                            try:
                                ids = await post_scheduled_message(client, cid, msg)
                                delivered.append({"chatId": cid, "messageIds": ids})
                                deliveries_coll.update_one(
                                    {"schedId": msg_id, "chatId": cid},
                                    {"$set": {"sentAt": datetime.now(timezone.utc), "messageIds": ids, "error": None}}
                                )
                            except Exception as e:
                                log.warning("Sending to chat %s failed: %s", cid, e)
                                deliveries_coll.update_one(
                                    {"schedId": msg_id, "chatId": cid},
                                    {"$set": {"sentAt": None, "error": str(e)}}
                                )

                        scheduled_coll.update_one(
                            {"_id": msg_id},
                            {"$set": {"status": "sent", "deliveredTo": delivered}}
                        )
                        log.info("Message %s processed; deliveries: %d", msg_id, len(delivered))

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
