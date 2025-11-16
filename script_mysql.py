# main.py
# User-mode Telethon automation with presets + scheduler (Render-ready)
# ---------------------------------------------------------------
# Env vars required:
#   API_ID, API_HASH, TELETHON_STRING_SESSION
#   MONGO_URI, MONGODB_NAME, COLLECTION_NAME
# Optional:
#   SAVED_COLLECTION (default: saved_campaigns)
#   CONTROL_CHAT_IDS (e.g., "self,-1001234567890")
#   AUTHORIZED_USER_ID (numeric)
#   TZ (e.g., "America/Los_Angeles")
# Start (Render worker): python main.py
# ---------------------------------------------------------------

import asyncio
import html
import logging
import os
import re
import shlex
import shutil
import signal
import sqlite3
import tempfile
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession

from pymongo import MongoClient, ASCENDING
from bson import ObjectId

from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

# ----------------- Logging -----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("user-scheduler")
logging.getLogger("telethon").setLevel(logging.INFO)

# ----------------- Config -----------------
def getenv_str(name: str, default: Optional[str] = None) -> str:
    v = os.getenv(name, default)
    if v is None:
        raise RuntimeError(f"Missing env var: {name}")
    return v

API_ID  = int(getenv_str("API_ID"))
API_HASH = getenv_str("API_HASH")
STRING_SESSION = getenv_str("TELETHON_STRING_SESSION")

MONGO_URI = getenv_str("MONGO_URI")
MONGO_DB  = getenv_str("MONGODB_NAME")
MONGO_COLL = getenv_str("COLLECTION_NAME")
SAVED_COLL = os.getenv("SAVED_COLLECTION", "saved_campaigns")

TZ_NAME = os.getenv("TZ", "America/Los_Angeles")
SERVICE_TZ = ZoneInfo(TZ_NAME)
DEFAULT_SEND_HOUR = 9

# Control chats (where you can type commands)
_raw_ctrl = os.getenv("CONTROL_CHAT_IDS", "self")
CONTROL_CHAT_IDS_RAW = [x.strip() for x in _raw_ctrl.split(",") if x.strip()]

AUTHORIZED_USER_ID = os.getenv("AUTHORIZED_USER_ID")
AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID) if AUTHORIZED_USER_ID and AUTHORIZED_USER_ID.isdigit() else None

# ----------------- Utils -----------------
def esc(s: str) -> str:
    return html.escape(str(s), quote=True)

def today_str() -> str:
    return datetime.now(tz=SERVICE_TZ).strftime("%Y-%m-%d")

def normalize_quotes(s: str) -> str:
    return s.replace('“', '"').replace('”', '"').replace('‘', "'").replace('’', "'")

def slug(s: str) -> str:
    import re as _re
    s = _re.sub(r'[^A-Za-z0-9]+', '-', s.strip()).strip('-')
    return s.lower()[:24] or 'preset'

def caption_for(name: str, description: str) -> str:
    base = f"<b>{esc(name)}</b>"
    if description:
        desc = esc(description)
        out = f"{base}\n{desc}"
    else:
        out = base
    if len(out) > 1024:
        room = 1024 - len(base) - 1
        truncated = (desc[:max(0, room - 1)] + "…") if room > 0 else ""
        out = f"{base}\n{truncated}" if truncated else base
    return out

def campaign_hash(text: str, images: List[str]) -> str:
    payload = {"text": (text or "").strip(), "images": sorted(images or [])}
    h = hashlib.sha256(json.dumps(payload, separators=(',', ':'), ensure_ascii=False).encode("utf-8")).hexdigest()
    return f"sha256:{h}"

def ensure_session_unlocked(session_base: str, max_wait_s: float = 0.5, force: bool = False) -> None:
    # Not used with StringSession, but harmless if you switch to file sessions.
    db  = Path(f"{session_base}.session")
    wal = Path(f"{session_base}.session-wal")
    shm = Path(f"{session_base}.session-shm")
    if not db.exists():
        return
    def reset():
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = Path("sessions_backup"); backup_dir.mkdir(parents=True, exist_ok=True)
        bdb = backup_dir / f"{db.name}.{ts}.bak"
        log.warning("Session DB locked; backup to %s; Telethon will recreate.", bdb)
        try: shutil.move(str(db), str(bdb))
        except Exception as e: log.warning("Move failed: %s", e)
        for f in (wal, shm):
            try:
                if f.exists(): f.unlink()
            except Exception as e:
                log.warning("Remove %s failed: %s", f, e)
    if force:
        reset(); return
    try:
        conn = sqlite3.connect(str(db), timeout=max_wait_s, isolation_level=None)
        try: conn.execute("PRAGMA journal_mode=WAL;")
        except Exception: pass
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("COMMIT;")
            conn.close()
            return
        except sqlite3.OperationalError as e:
            conn.close()
            if "locked" in str(e).lower():
                reset(); return
            raise
    except sqlite3.OperationalError as e:
        if "locked" in str(e).lower():
            reset(); return
        raise

# ----------------- Time parsing -----------------
def parse_scheduled_to_utc(s: str) -> datetime:
    s = s.strip()
    if s.lower() == "now":
        return datetime.now(timezone.utc) + timedelta(minutes=5)
    now_local = datetime.now(tz=SERVICE_TZ)
    default_base = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    dt = dtparser.parse(s, dayfirst=False, fuzzy=True, default=default_base)
    user_time = bool(re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm))\b", s, re.I) or re.search(r"\b\d{1,2}:\d{2}\b", s))
    candidate = dt.astimezone(SERVICE_TZ) if dt.tzinfo else dt.replace(tzinfo=SERVICE_TZ)
    if not user_time:
        candidate = candidate.replace(hour=DEFAULT_SEND_HOUR, minute=0, second=0, microsecond=0)
    year_in_input = re.search(r"\b(19|20)\d{2}\b", s) is not None
    if not year_in_input and candidate < now_local:
        candidate = candidate.replace(year=candidate.year + 1)
    return candidate.astimezone(timezone.utc)

def extract_time_of_day_local(when_str: str) -> Tuple[int, int]:
    s = when_str.strip()
    now_local = datetime.now(tz=SERVICE_TZ)
    default_base = now_local.replace(month=1, day=1, hour=DEFAULT_SEND_HOUR, minute=0, second=0, microsecond=0)
    dt = dtparser.parse(s, dayfirst=False, fuzzy=True, default=default_base)
    if dt.tzinfo:
        dt = dt.astimezone(SERVICE_TZ)
    else:
        dt = dt.replace(tzinfo=SERVICE_TZ)
    return dt.hour, dt.minute

def combine_date_and_tod_to_utc(d: date, hour: int, minute: int) -> datetime:
    local_dt = datetime(d.year, d.month, d.day, hour, minute, tzinfo=SERVICE_TZ)
    return local_dt.astimezone(timezone.utc)

# ----------------- Sending helpers -----------------
GLOBAL_GAP = 0.25
_last_sent_global = 0.0

async def throttle():
    global _last_sent_global
    now = asyncio.get_running_loop().time()
    gap = GLOBAL_GAP - (now - _last_sent_global)
    if gap > 0:
        await asyncio.sleep(gap)
    _last_sent_global = asyncio.get_running_loop().time()

async def send_text_safe(client: TelegramClient, chat_id: int, text: str, *, parse_mode: str = 'html', link_preview: bool = False) -> List[int]:
    while True:
        try:
            await throttle()
            m = await client.send_message(chat_id, text, parse_mode=parse_mode, link_preview=link_preview)
            return [m.id]
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)

async def _download_one(url_or_path: str, dest_dir: str) -> Optional[str]:
    import os, aiohttp, urllib.request
    if url_or_path.startswith("file://"):
        p = url_or_path[7:]
        return p if os.path.exists(p) else None
    if os.path.exists(url_or_path):
        return url_or_path
    guess = url_or_path.split("?")[0].lower()
    ext = ".jpg"
    for e in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        if guess.endswith(e):
            ext = e
            break
    filename = hashlib.sha1(url_or_path.encode("utf-8")).hexdigest() + ext
    path = os.path.join(dest_dir, filename)
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url_or_path) as resp:
                if resp.status != 200: return None
                data = await resp.read()
                with open(path, "wb") as f: f.write(data)
        return path
    except Exception:
        try:
            with urllib.request.urlopen(url_or_path, timeout=30) as r:
                data = r.read()
                with open(path, "wb") as f: f.write(data)
            return path
        except Exception:
            return None

def _ensure_photo_jpeg(src_path: str, dest_dir: str) -> Optional[str]:
    try:
        from PIL import Image
    except Exception:
        return src_path
    try:
        with Image.open(src_path) as im:
            im.verify()
        with Image.open(src_path) as im2:
            if im2.mode not in ("RGB", "L"):
                im2 = im2.convert("RGB")
            out_name = hashlib.md5(src_path.encode("utf-8")).hexdigest() + ".jpg"
            out_path = os.path.join(dest_dir, out_name)
            im2.save(out_path, format="JPEG", quality=85, optimize=True)
            return out_path
    except Exception:
        return src_path

async def _upload_all(client: TelegramClient, paths: List[str]):
    up = []
    for p in paths:
        await throttle()
        uf = await client.upload_file(p, file_name=os.path.basename(p))
        up.append(uf)
    return up

async def send_files_safe(client: TelegramClient, chat_id: int, files: List[str], *, caption: Optional[str] = None, parse_mode: str = 'html') -> List[int]:
    if not files: return []
    files = files[:10]
    tmpdir = tempfile.mkdtemp(prefix="tg_album_")
    local_paths, prepared_paths = [], []
    try:
        for u in files:
            p = await _download_one(u, tmpdir)
            if p: local_paths.append(p)
        if not local_paths:
            ids = []
            if caption:
                ids.extend(await send_text_safe(client, chat_id, caption, parse_mode=parse_mode, link_preview=True))
            for f in files:
                ids.extend(await send_text_safe(client, chat_id, f, parse_mode=parse_mode, link_preview=True))
            return ids
        for p in local_paths:
            prepared_paths.append(_ensure_photo_jpeg(p, tmpdir) or p)
        uploaded = await _upload_all(client, prepared_paths)
        captions = [caption] + [""] * (len(uploaded) - 1) if caption else None
        await throttle()
        result = await client.send_file(chat_id, uploaded, caption=captions, parse_mode=parse_mode, force_document=False)
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        uploaded = await _upload_all(client, prepared_paths or local_paths)
        captions = [caption] + [""] * (len(uploaded) - 1) if caption else None
        result = await client.send_file(chat_id, uploaded, caption=captions, parse_mode=parse_mode, force_document=False)
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    except Exception as e:
        log.warning("Album send failed for chat %s: %s; fallback to links.", chat_id, e)
        ids = []
        if caption:
            ids.extend(await send_text_safe(client, chat_id, caption, parse_mode=parse_mode, link_preview=True))
        for f in files:
            ids.extend(await send_text_safe(client, chat_id, f, parse_mode=parse_mode, link_preview=True))
        return ids
    finally:
        try:
            for p in set(local_paths + prepared_paths):
                try: os.remove(p)
                except Exception: pass
            os.rmdir(tmpdir)
        except Exception:
            pass

def uploads_dir() -> str:
    d = Path("data/uploads"); d.mkdir(parents=True, exist_ok=True)
    return str(d.resolve())

# ----------------- Pending capture sessions -----------------
PENDING_INSERT: Dict[tuple, Dict[str, Any]] = {}  # key=(chat_id,user_id)
PENDING_SAVE: Dict[tuple, Dict[str, Any]] = {}

# ----------------- Mongo & scheduler -----------------
async def post_scheduled_message(client: TelegramClient, chat_id: int, msg_doc: dict) -> List[int]:
    text = msg_doc.get("text", "") or ""
    images = (msg_doc.get("images", []) or [])[:10]
    parse_mode = 'html' if (msg_doc.get("parseMode", "HTML") or "HTML").upper() == "HTML" else None
    disable_preview = bool(msg_doc.get("disablePreview", True))
    if images:
        return await send_files_safe(client, chat_id, images, caption=text if text else None, parse_mode=parse_mode or 'html')
    else:
        return await send_text_safe(client, chat_id, text, parse_mode=parse_mode or 'html', link_preview=not disable_preview)

async def scheduler_loop(client: TelegramClient, db):
    scheduled = db["scheduled_messages"]
    deliveries = db["deliveries"]
    chats_coll = db["chats"]
    log.info("Scheduler loop started.")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            due_cur = scheduled.find({
                "status": {"$in": ["scheduled", None, "processing"]},
                "scheduledAt": {"$lte": now_utc}
            }).limit(50)

            for msg in due_cur:
                msg_id = msg.get("_id")
                try:
                    scheduled.update_one({"_id": msg_id}, {"$set": {"status": "processing"}})

                    # Target chat list (explicit only in this user-mode build)
                    explicit_ids = msg.get("targetChatIds") or []
                    chat_ids = sorted(set(int(x) for x in explicit_ids if int(x) < 0))

                    delivered = []
                    for cid in chat_ids:
                        # idempotent claim
                        try:
                            res = deliveries.update_one(
                                {"schedId": msg_id, "chatId": cid},
                                {"$setOnInsert": {"claimedAt": now_utc}},
                                upsert=True
                            )
                            # if not upserted, already claimed
                            if not res.upserted_id:
                                continue
                        except Exception as e:
                            log.warning("Claim (schedId=%s, chatId=%s) failed: %s", msg_id, cid, e)
                            continue

                        try:
                            ids = await post_scheduled_message(client, cid, msg)
                            delivered.append({"chatId": cid, "messageIds": ids})
                            deliveries.update_one(
                                {"schedId": msg_id, "chatId": cid},
                                {"$set": {"sentAt": datetime.now(timezone.utc), "messageIds": ids, "error": None}}
                            )
                        except Exception as e:
                            log.warning("Sending to chat %s failed: %s", cid, e)
                            deliveries.update_one(
                                {"schedId": msg_id, "chatId": cid},
                                {"$set": {"sentAt": None, "error": str(e)}}
                            )

                    scheduled.update_one({"_id": msg_id}, {"$set": {"status": "sent", "deliveredTo": delivered}})
                    log.info("Message %s processed; deliveries: %d", msg_id, len(delivered))

                except Exception as e:
                    log.exception("Error processing %s: %s", msg_id, e)
                    scheduled.update_one({"_id": msg_id}, {"$set": {"status": "failed", "lastError": str(e)}})
        except Exception as loop_err:
            log.exception("Scheduler loop iteration error: %s", loop_err)
        await asyncio.sleep(8)

# ----------------- Command parsing -----------------
def parse_name_desc_images_and_time(text: str) -> Optional[Tuple[str, str, List[str], str, List[int], bool]]:
    """
    Supports both:
      /insert "Name" "Desc" [https://a.jpg https://b.jpg] Nov 28 3pm groups=[-100…]
      /insert "Name" "Desc" Now [groups=[-100…]]      (attach-from-phone)
      /preset save "Name" "Desc" 4pm [groups=[-100…]] (attach-from-phone)
    """
    t = normalize_quotes(text).strip()
    parts = shlex.split(t)
    if not parts: return None
    # strip leading commands
    i = 0
    while i < len(parts) and parts[i].lower() in {"/insert", "/preset", "save"}:
        i += 1
    if i + 3 > len(parts):  # name, desc, time
        return None

    name = parts[i]; i += 1
    description = parts[i]; i += 1
    images: List[str] = []
    attach_mode = False

    if i < len(parts) and parts[i].startswith('['):
        toks = []
        while i < len(parts):
            tok = parts[i]; toks.append(tok)
            if tok.endswith(']'):
                i += 1; break
            i += 1
        else:
            return None
        inner = " ".join(toks)[1:-1].strip()
        images = [u.strip() for u in re.split(r"[, \n\t]+", inner) if u.strip()]
    else:
        attach_mode = True

    trailing = " ".join(parts[i:]).strip()
    if not trailing: return None

    target_ids: List[int] = []
    m = re.search(r"\bgroups=\[(.*?)\]\s*$", trailing, flags=re.I)
    if m:
        blob = m.group(1)
        trailing = trailing[:m.start()].strip()
        for g in re.split(r"[, \n\t]+", blob.strip()):
            if not g: continue
            try: target_ids.append(int(g))
            except ValueError: pass

    when_str = trailing.strip()
    if not when_str: return None
    return name, description, images, when_str, sorted(set(target_ids)), attach_mode

# ----------------- Main app -----------------
async def main():
    # --- Telegram user login (StringSession) ---
    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError("Personal session not authorized. Regenerate TELETHON_STRING_SESSION.")

    me = await client.get_me()
    MY_ID = me.id
    log.info("Connected as personal account: @%s (id=%s)", getattr(me, "username", None), MY_ID)

    # --- Mongo ---
    mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    mongo.admin.command("ping")
    log.info("MongoDB connection OK")
    db = mongo[MONGO_DB]
    products = db[MONGO_COLL]
    chats_coll = db["chats"]
    scheduled_coll = db["scheduled_messages"]
    deliveries_coll = db["deliveries"]
    saved_coll = db[SAVED_COLL]
    # Indexes
    try:
        chats_coll.create_index("chatId", unique=True)
        scheduled_coll.create_index([("status", 1), ("scheduledAt", 1)])
        deliveries_coll.create_index([("schedId", 1), ("chatId", 1)], unique=True)
        saved_coll.create_index([("code", ASCENDING)], unique=True)
        saved_coll.create_index([("name", ASCENDING)])
    except Exception as e:
        log.warning("Index creation warning: %s", e)

    # --- Control chat(s) gating ---
    CONTROL_CHAT_IDS: set[int] = set()
    for token in CONTROL_CHAT_IDS_RAW:
        if token.lower() == "self":
            CONTROL_CHAT_IDS.add(MY_ID)
        else:
            try: CONTROL_CHAT_IDS.add(int(token))
            except ValueError:
                log.warning("Invalid CONTROL_CHAT_IDS token ignored: %s", token)

    def is_control_message(event) -> bool:
        # Only you (and optionally a second admin) and (optionally) inside allowed chats
        sender_ok = (event.sender_id == MY_ID) or (AUTHORIZED_USER_ID and event.sender_id == AUTHORIZED_USER_ID)
        chat_ok = (not CONTROL_CHAT_IDS) or (event.chat_id in CONTROL_CHAT_IDS)
        return sender_ok and chat_ok

    # --- Chat discovery ---
    async def upsert_chat(event_chat, *, active: bool = True):
        try:
            chat_id = getattr(event_chat, "id", None)
            if chat_id is None: return
            doc = {
                "chatId": chat_id,
                "title": getattr(event_chat, "title", getattr(event_chat, "username", None)),
                "type": event_chat.__class__.__name__.lower(),
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

    @client.on(events.ChatAction)
    async def on_chat_action(event):
        try:
            if (event.user_added or event.user_joined) and event.user_id == MY_ID:
                await upsert_chat(event.chat, active=True)
            if (event.user_kicked or event.user_left) and event.user_id == MY_ID:
                await upsert_chat(event.chat, active=False)
        except Exception as e:
            log.warning("ChatAction handling failed: %s", e)

    # --- Admin helpers ---
    @client.on(events.NewMessage(pattern=r"(?i)^/id(?:\s|$)", func=is_control_message))
    async def cmd_id(event):
        await event.reply(f"Your Telegram numeric ID:\n<code>{event.sender_id}</code>", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/start(?:\s|$)", func=is_control_message))
    async def cmd_start(event):
        await event.reply(
            "👋 User-mode scheduler ready.\n"
            "Commands:\n"
            " • /here (run in a group to register it)\n"
            " • /groups_add [ -100… -100… ] • /groups_list\n"
            " • /insert \"Name\" \"Desc\" [img…] Nov 28 3pm groups=[-100…]\n"
            "   ↳ or attach-mode: /insert \"Name\" \"Desc\" Now  (send photos, then /done)\n"
            " • /preset save \"Name\" \"Desc\" [img…] 4pm groups=[-100…]  (or attach-mode + /done)\n"
            " • /preset list • /preset show CODE • /preset delete CODE\n"
            " • /preset send CODE [Now|Today|Nov 28 3pm] [groups=[-100…]]",
            parse_mode='html'
        )

    def _is_valid_group(cid: int) -> bool:
        return isinstance(cid, int) and cid < 0

    @client.on(events.NewMessage(pattern=r"(?i)^/here(?:\s|$)"))
    async def cmd_here(event):
        # allow from any group you are in (not only control chats)
        if not (event.is_group or event.is_channel):  # needs a chat
            return await event.reply("Use this inside a group/channel.")
        await upsert_chat(event.chat, active=True)
        await event.reply(f"Registered this chat: <code>{getattr(event.chat, 'id', None)}</code>", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/groups_add(?:\s|$)", func=is_control_message))
    async def cmd_groups_add(event):
        parts = shlex.split(event.raw_text)
        if len(parts) < 2:
            return await event.reply('Usage: <code>/groups_add [ -1001 -1002 ]</code>', parse_mode='html')
        m = re.search(r"\[(.*)\]", " ".join(parts[1:]))
        if not m:
            return await event.reply('Usage: <code>/groups_add [ -1001 -1002 ]</code>', parse_mode='html')
        ids, bad = [], []
        for tok in re.split(r"[, \n\t]+", m.group(1).strip()):
            if not tok: continue
            try:
                v = int(tok)
                if _is_valid_group(v): ids.append(v)
                else: bad.append(tok)
            except ValueError:
                bad.append(tok)
        ids = sorted(set(ids))
        now = datetime.now(timezone.utc)
        added = 0
        for cid in ids:
            try:
                chats_coll.update_one(
                    {"chatId": cid},
                    {"$set": {"chatId": cid, "title": None, "type": "channel", "isActive": True, "lastSeenAt": now},
                     "$setOnInsert": {"firstSeenAt": now}},
                    upsert=True
                )
                added += 1
            except Exception as e:
                log.warning("Failed upsert chatId %s: %s", cid, e)
        msg = f"✅ Registered/updated {added} chat IDs."
        if bad:
            msg += f"\n❗️ Skipped non-group IDs: <code>{', '.join(bad)}</code> (must be negative -100…)"
        await event.reply(msg, parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/groups_list(?:\s|$)", func=is_control_message))
    async def cmd_groups_list(event):
        docs = list(chats_coll.find({"chatId": {"$lt": 0}}, {"chatId": 1, "title": 1, "type": 1, "isActive": 1}).limit(200))
        if not docs:
            return await event.reply("No known groups/channels. Use /here in a group.", parse_mode='html')
        lines = [f"{d.get('chatId')} | {esc(d.get('title') or '')} | {d.get('type')} | {'active' if d.get('isActive') else 'inactive'}"
                 for d in docs]
        await event.reply("<b>Known groups/channels</b>:\n" + "\n".join(lines), parse_mode='html')

    # --- INSERT (one-off schedule, supports attach mode) ---
    @client.on(events.NewMessage(pattern=r"(?i)^/insert(?:\s|$)", func=is_control_message))
    async def cmd_insert(event):
        raw = normalize_quotes(event.raw_text.strip())
        if raw.lower().startswith("/insert /insert"):
            raw = raw[8:].lstrip()
        parsed = parse_name_desc_images_and_time(raw)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                "<code>/insert \"Name\" \"Description\" [https://a.jpg https://b.jpg] Nov 28 3pm groups=[-100…]</code>\n"
                "or attach-mode:\n"
                "<code>/insert \"Name\" \"Description\" Now</code> (send photos, then /done)",
                parse_mode='html'
            )
        name, description, images, when_str, target_ids, attach_mode = parsed
        if target_ids and any(cid >= 0 for cid in target_ids):
            return await event.reply("❌ Targets must be negative IDs (-100…).", parse_mode='html')

        if not attach_mode:
            try:
                scheduled_utc = parse_scheduled_to_utc(when_str)
            except Exception as e:
                return await event.reply(f"❌ Could not parse time: <code>{esc(e)}</code>", parse_mode='html')
            product_doc = {"name": name, "description": description, "images": images[:10], "last_edit": today_str()}
            res = products.insert_one(product_doc)
            product_id = res.inserted_id
            text = f"<b>{esc(name)}</b>\n{esc(description)}"
            sched_doc = {
                "_id": f"auto_{product_id}",
                "text": text,
                "images": images[:10],
                "parseMode": "HTML",
                "disablePreview": True,
                "scheduledAt": scheduled_utc,
                "targets": "explicit" if target_ids else "all",
                "targetChatIds": target_ids if target_ids else None,
                "status": "scheduled",
                "productId": product_id,
                "createdAt": datetime.now(timezone.utc),
                "contentHash": campaign_hash(text, images[:10]),
            }
            scheduled_coll.insert_one(sched_doc)
            if images:
                await send_files_safe(client, event.chat_id, [images[0]], caption=caption_for(name, description))
            else:
                await event.reply(caption_for(name, description), parse_mode='html')
            lt = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
            ut = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
            return await event.reply(f"📅 Scheduled for: <b>{esc(lt)}</b> (<code>{esc(ut)}</code>)", parse_mode='html')

        # attach-from-phone
        key = (event.chat_id, event.sender_id)
        old = PENDING_INSERT.pop(key, None)
        if old:
            await event.reply("ℹ️ Previous pending insert was discarded.", parse_mode='html')
        PENDING_INSERT[key] = {
            "name": name, "description": description, "when_str": when_str,
            "targets": target_ids, "paths": [],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
            "task": None
        }
        async def timeout_finalize():
            await asyncio.sleep(60)
            if key in PENDING_INSERT:
                await event.reply("⏰ Time window ended. Finalizing with images received so far…", parse_mode='html')
                await finalize_insert_schedule(client, key, db, products, chats_coll, scheduled_coll)
        PENDING_INSERT[key]["task"] = asyncio.create_task(timeout_finalize())
        await event.reply("📎 Attach up to 10 photos now (as images, not files). When done, send <code>/done</code>.", parse_mode='html')

    async def finalize_insert_schedule(client: TelegramClient, key: tuple, db, products, chats_coll, scheduled_coll):
        sess = PENDING_INSERT.pop(key, None)
        if not sess:
            return
        name = sess["name"]; description = sess["description"]
        when_str = sess["when_str"]; target_ids = sess["targets"]
        paths: List[str] = sess["paths"]
        try:
            scheduled_utc = parse_scheduled_to_utc(when_str)
        except Exception as e:
            return await client.send_message(key[0], f"❌ Could not parse time: <code>{esc(e)}</code>", parse_mode='html')
        product_doc = {"name": name, "description": description, "images": paths, "last_edit": today_str()}
        res = products.insert_one(product_doc)
        product_id = res.inserted_id
        text = f"<b>{esc(name)}</b>\n{esc(description)}"
        sched_doc = {
            "_id": f"auto_{product_id}",
            "text": text,
            "images": paths[:10],
            "parseMode": "HTML",
            "disablePreview": True,
            "scheduledAt": scheduled_utc,
            "targets": "explicit" if target_ids else "all",
            "targetChatIds": target_ids if target_ids else None,
            "status": "scheduled",
            "productId": product_id,
            "createdAt": datetime.now(timezone.utc),
            "contentHash": campaign_hash(text, paths[:10]),
        }
        db["scheduled_messages"].insert_one(sched_doc)

        if paths:
            await send_files_safe(client, key[0], [paths[0]], caption=caption_for(name, description))
        else:
            await client.send_message(key[0], caption_for(name, description), parse_mode='html')

        lt = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
        ut = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
        await client.send_message(key[0], f"📅 Scheduled for: <b>{esc(lt)}</b> (<code>{esc(ut)}</code>)", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/done(?:\s|$)", func=is_control_message))
    async def cmd_done(event):
        key = (event.chat_id, event.sender_id)
        if key in PENDING_INSERT:
            task = PENDING_INSERT[key].get("task")
            if task and not task.done(): task.cancel()
            return await finalize_insert_schedule(client, key, db, products, chats_coll, scheduled_coll)
        if key in PENDING_SAVE:
            task = PENDING_SAVE[key].get("task")
            if task and not task.done(): task.cancel()
            sess = PENDING_SAVE[key]
            return await finalize_save_preset(client, key, db, saved_coll,
                                             name=sess["name"], description=sess["description"],
                                             when_str=sess["when_str"], targets=sess["targets"])
        return await event.reply("There is no pending action to finalize.", parse_mode='html')

    @client.on(events.NewMessage(func=is_control_message))
    async def media_collector(event):
        if not event.photo and not (event.document and getattr(event.document, "mime_type", "").startswith("image/")):
            return
        key = (event.chat_id, event.sender_id)
        active = PENDING_INSERT if key in PENDING_INSERT else (PENDING_SAVE if key in PENDING_SAVE else None)
        if not active: return
        sess = active.get(key)
        if datetime.now(timezone.utc) > sess["expires_at"]:
            task = sess.get("task")
            if task and not task.done(): task.cancel()
            if active is PENDING_INSERT:
                await finalize_insert_schedule(client, key, db, products, chats_coll, scheduled_coll)
            else:
                await finalize_save_preset(client, key, db, saved_coll,
                                           name=sess["name"], description=sess["description"],
                                           when_str=sess["when_str"], targets=sess["targets"])
            return
        try:
            out_dir = uploads_dir()
            fn = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{event.id}.jpg"
            out_path = os.path.join(out_dir, fn)
            await event.download_media(file=out_path)
            sess.setdefault("paths", []).append(out_path)
            if len(sess["paths"]) > 10:
                sess["paths"] = sess["paths"][:10]
            await event.reply(f"✅ Added image ({len(sess['paths'])}/10).", parse_mode='html')
        except Exception as e:
            log.warning("Media download failed: %s", e)
            await event.reply("❌ Failed to save that image. Try again.", parse_mode='html')

    # --- PRESETS ---
    async def finalize_save_preset(client: TelegramClient, key: tuple, db, saved_coll, *, name: str, description: str, when_str: str, targets: List[int]):
        sess = PENDING_SAVE.pop(key, None)
        paths: List[str] = sess["paths"] if sess else []
        hour, minute = extract_time_of_day_local(when_str)
        text = f"<b>{esc(name)}</b>\n{esc(description)}"
        import secrets
        base = slug(name)
        for _ in range(8):
            code = f"{base}-{secrets.token_hex(2)}"
            if not saved_coll.find_one({"code": code}):
                break
        else:
            code = f"{base}-{int(datetime.now().timestamp())}"
        doc = {
            "code": code,
            "name": name,
            "description": description,
            "text": text,
            "images": paths[:10],
            "parseMode": "HTML",
            "disablePreview": True,
            "defaultTargets": "explicit",
            "targetChatIds": [int(x) for x in targets if isinstance(x, int) and x < 0],
            "timeOfDayLocal": {"hour": hour, "minute": minute, "tz": str(SERVICE_TZ)},
            "whenStrOriginal": when_str,
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        }
        saved_coll.insert_one(doc)
        if paths:
            await send_files_safe(client, key[0], [paths[0]], caption=f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>", parse_mode='html')
        else:
            await client.send_message(key[0], f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+save(?:\s|$)", func=is_control_message))
    async def cmd_preset_save(event):
        raw = normalize_quotes(event.raw_text.strip())
        raw = re.sub(r"(?i)^/preset\s+save\s*", "", raw, count=1).strip()
        parsed = parse_name_desc_images_and_time(raw)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                "<code>/preset save \"Name\" \"Description\" [https://a.jpg …] 4pm groups=[-100…]</code>\n"
                "or attach-mode:\n"
                "<code>/preset save \"Name\" \"Description\" 4pm</code> (send photos, then /done)\n"
                "• Saves text, images, targets, and the time-of-day for reuse.",
                parse_mode='html'
            )
        name, description, images, when_str, target_ids, attach_mode = parsed
        if target_ids and any(cid >= 0 for cid in target_ids):
            return await event.reply("❌ Targets must be negative IDs (-100…).", parse_mode='html')

        if not attach_mode:
            hour, minute = extract_time_of_day_local(when_str)
            text = f"<b>{esc(name)}</b>\n{esc(description)}"
            import secrets
            base = slug(name)
            for _ in range(8):
                code = f"{base}-{secrets.token_hex(2)}"
                if not saved_coll.find_one({"code": code}):
                    break
            else:
                code = f"{base}-{int(datetime.now().timestamp())}"
            doc = {
                "code": code,
                "name": name,
                "description": description,
                "text": text,
                "images": images[:10],
                "parseMode": "HTML",
                "disablePreview": True,
                "defaultTargets": "explicit",
                "targetChatIds": [int(x) for x in target_ids if int(x) < 0],
                "timeOfDayLocal": {"hour": hour, "minute": minute, "tz": str(SERVICE_TZ)},
                "whenStrOriginal": when_str,
                "createdAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc),
            }
            saved_coll.insert_one(doc)
            if images:
                await send_files_safe(client, event.chat_id, [images[0]], caption=f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>", parse_mode='html')
            else:
                await event.reply(f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>", parse_mode='html')
            return

        key = (event.chat_id, event.sender_id)
        old = PENDING_SAVE.pop(key, None)
        if old:
            await event.reply("ℹ️ Previous pending preset-save was discarded.", parse_mode='html')
        PENDING_SAVE[key] = {
            "name": name, "description": description, "when_str": when_str,
            "targets": target_ids, "paths": [],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
            "task": None
        }
        async def timeout_finalize():
            await asyncio.sleep(60)
            if key in PENDING_SAVE:
                await event.reply("⏰ Time window ended. Finalizing preset with images received so far…", parse_mode='html')
                sess = PENDING_SAVE[key]
                await finalize_save_preset(client, key, db, saved_coll,
                                           name=sess["name"], description=sess["description"],
                                           when_str=sess["when_str"], targets=sess["targets"])
        PENDING_SAVE[key]["task"] = asyncio.create_task(timeout_finalize())
        await event.reply("📎 Attach up to 10 photos now (as images, not files). When done, send <code>/done</code>.", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+list(?:\s|$)", func=is_control_message))
    async def cmd_preset_list(event):
        docs = list(saved_coll.find({}, {"code": 1, "name": 1, "timeOfDayLocal": 1, "targetChatIds": 1}).limit(200))
        if not docs:
            return await event.reply("No saved presets.", parse_mode='html')
        rows = []
        for d in docs:
            tod = d.get("timeOfDayLocal", {})
            rows.append(f"{esc(d.get('name',''))} — <code>{esc(d.get('code',''))}</code> • {int(tod.get('hour',DEFAULT_SEND_HOUR)):02d}:{int(tod.get('minute',0)):02d} • targets={len(d.get('targetChatIds') or [])}")
        await event.reply("<b>Saved presets</b>:\n" + "\n".join(rows), parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+show\s+(\S+)", func=is_control_message))
    async def cmd_preset_show(event):
        m = re.search(r"(?i)^/preset\s+show\s+(\S+)", event.raw_text.strip())
        code = m.group(1)
        doc = saved_coll.find_one({"code": code})
        if not doc:
            return await event.reply("❌ Not found.", parse_mode='html')
        name = doc.get("name",""); description = doc.get("description","")
        images = doc.get("images") or []
        if images:
            await send_files_safe(client, event.chat_id, [images[0]], caption=caption_for(name, description))
        else:
            await event.reply(caption_for(name, description), parse_mode='html')
        tod = doc.get("timeOfDayLocal", {})
        await event.reply(f"Code: <code>{code}</code>\nTime-of-day: {int(tod.get('hour',DEFAULT_SEND_HOUR)):02d}:{int(tod.get('minute',0)):02d}", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+delete\s+(\S+)", func=is_control_message))
    async def cmd_preset_delete(event):
        m = re.search(r"(?i)^/preset\s+delete\s+(\S+)", event.raw_text.strip())
        code = m.group(1)
        res = saved_coll.delete_one({"code": code})
        if res.deleted_count:
            await event.reply(f"🗑️ Deleted preset <code>{code}</code>.", parse_mode='html')
        else:
            await event.reply("Not found.", parse_mode='html')

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+send\s+(\S+)(.*)$", func=is_control_message))
    async def cmd_preset_send(event):
        m = re.search(r"(?i)^/preset\s+send\s+(\S+)(.*)$", event.raw_text.strip())
        code = m.group(1)
        trailing = (m.group(2) or "").strip()

        groups_override: Optional[List[int]] = None
        mg = re.search(r"\bgroups=\[(.*?)\]\s*$", trailing, flags=re.I)
        if mg:
            blob = mg.group(1); trailing = trailing[:mg.start()].strip()
            gids = []
            for g in re.split(r"[, \n\t]+", blob.strip()):
                if not g: continue
                try:
                    val = int(g)
                    if val < 0: gids.append(val)
                except ValueError:
                    pass
            groups_override = sorted(set(gids))

        when_str = trailing.strip() or "Now"
        doc = saved_coll.find_one({"code": code})
        if not doc:
            return await event.reply("❌ Preset not found.", parse_mode='html')

        # compute scheduled time
        if when_str.lower() == "now":
            scheduled_utc = datetime.now(timezone.utc) + timedelta(minutes=5)
        else:
            try:
                has_time = bool(re.search(r"\b(\d{1,2}(:\d{2})?\s*(am|pm))\b", when_str, re.I) or re.search(r"\b\d{1,2}:\d{2}\b", when_str))
                if has_time:
                    scheduled_utc = parse_scheduled_to_utc(when_str)
                else:
                    dt_local = dtparser.parse(when_str, dayfirst=False, fuzzy=True, default=datetime.now(tz=SERVICE_TZ).replace(hour=0, minute=0, second=0, microsecond=0))
                    if dt_local.tzinfo is None:
                        dt_local = dt_local.replace(tzinfo=SERVICE_TZ)
                    tod = doc.get("timeOfDayLocal", {}) or {}
                    hour = int(tod.get("hour", DEFAULT_SEND_HOUR))
                    minute = int(tod.get("minute", 0))
                    scheduled_utc = combine_date_and_tod_to_utc(dt_local.date(), hour, minute)
            except Exception as e:
                return await event.reply(f"❌ Could not parse time: <code>{esc(e)}</code>", parse_mode='html')

        targets = groups_override if groups_override is not None else (doc.get("targetChatIds") or [])
        targets = [int(x) for x in targets if isinstance(x, int) and x < 0]

        text = doc.get("text") or (f"<b>{esc(doc.get('name',''))}</b>\n{esc(doc.get('description',''))}")
        images = (doc.get("images") or [])[:10]
        sched_doc = {
            "text": text,
            "images": images,
            "parseMode": "HTML",
            "disablePreview": True,
            "scheduledAt": scheduled_utc,
            "targets": "explicit",
            "targetChatIds": targets,
            "status": "scheduled",
            "createdAt": datetime.now(timezone.utc),
            "contentHash": campaign_hash(text, images),
            "presetCode": code,
        }
        scheduled_coll.insert_one(sched_doc)
        lt = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
        ut = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
        await event.reply(f"🚀 Queued preset <code>{code}</code> for <b>{esc(lt)}</b> (<code>{esc(ut)}</code>) to {len(targets)} chats.", parse_mode='html')

    # --- Kick off scheduler and run ---
    asyncio.create_task(scheduler_loop(client, db))
    log.info("User-mode automation started.")
    await client.run_until_disconnected()

# ----------------- Entrypoint (Render-safe) -----------------
if __name__ == "__main__":
    stop_event = asyncio.Event()

    def _handle_stop(*_):
        try:
            stop_event.set()
        except Exception:
            pass

    try:
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_stop)
            except NotImplementedError:
                # Windows or restricted env: ignore
                pass

        loop.run_until_complete(main())
        loop.run_until_complete(stop_event.wait())
    finally:
        try:
            loop.close()
        except Exception:
            pass
