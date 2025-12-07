# script_mysql.py
# ---------------------------------------------------------------
# Telethon user-mode automation (personal account) with:
#  - Auto-discovery of groups/channels into Mongo `Chats`
#  - Scheduler that uses `Schedules_messages` + `Chats`
#  - Presets in `Saved_compaigns`
#  - Attach-from-phone flow (/insert ... Now + images + /done)
#  - CONTROL_CHAT_IDS gating and AUTHORIZED_USER_ID
#  - De-dup by normalized title
#  - asyncio.run(main()) to avoid "no current event loop" errors.
# ---------------------------------------------------------------

import asyncio
import configparser
import html
import logging
import os
import re
import shlex
import json
import hashlib
import tempfile
import sqlite3  # may be unused, kept for compatibility
import shutil   # may be unused, kept for compatibility
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dateutil import parser as dtparser
from zoneinfo import ZoneInfo

from pymongo import MongoClient, ASCENDING
from pymongo.errors import OperationFailure
from bson import ObjectId

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import PeerUser, PeerChat, PeerChannel

# ----------------- Logging -----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("tg_automation")
logging.getLogger("telethon").setLevel(logging.INFO)

# ----------------- Config -----------------
CONFIG_PATH = Path("config.ini")


def load_config(path: Path) -> configparser.ConfigParser:
    if not path.exists():
        raise SystemExit(
            f"Missing {path}. Create it with [telegram] API_ID, API_HASH, TELETHON_STRING_SESSION, "
            f"and [mongo] uri, db_name, etc."
        )
    cp = configparser.ConfigParser()
    cp.read(path)
    return cp


CONFIG = load_config(CONFIG_PATH)


def get_cfg(section: str, key: str, default: Optional[str] = None) -> str:
    try:
        return CONFIG[section][key]
    except KeyError:
        if default is not None:
            return default
        raise


API_ID = int(get_cfg("telegram", "API_ID"))
API_HASH = get_cfg("telegram", "API_HASH")
SESSION = os.getenv("TELETHON_STRING_SESSION") or get_cfg(
    "telegram", "TELETHON_STRING_SESSION", ""
)

if not SESSION:
    raise SystemExit(
        "No TELETHON_STRING_SESSION provided in environment or config.ini [telegram]."
    )

MONGO_URI = get_cfg("telegram", "MONGO_URI")
MONGO_DB = get_cfg("telegram", "MONGODB_NAME", "TelegramBot")

CHATS_COLL = get_cfg("telegram", "chats_collection", "chats")
PRODUCTS_COLL = get_cfg("telegram", "products_collection", "Announcements")
SCHEDULED_COLL = get_cfg("telegram", "scheduled_collection", "schedules_messages")
DELIVERIES_COLL = get_cfg("telegram", "deliveries_collection", "deliveries")
SAVED_COLL = get_cfg("telegram", "saved_collection", "saved_compaigns")

TZ_NAME = get_cfg("telegram", "TZ_NAME")
CONTROL_CHAT_IDS_RAW = [
    x.strip()
    for x in get_cfg("control", "CONTROL_CHAT_IDS", "self").split(",")
    if x.strip()
]
AUTHORIZED_USER_ID = get_cfg("control", "AUTHORIZED_USER_ID", "")
AUTHORIZED_USER_ID = int(AUTHORIZED_USER_ID) if AUTHORIZED_USER_ID.isdigit() else None

SERVICE_TZ = ZoneInfo(TZ_NAME)
DEFAULT_SEND_HOUR = 9


def parse_control_chat_ids(me_id: int) -> List[int]:
    out: List[int] = []
    for raw in CONTROL_CHAT_IDS_RAW:
        if raw.lower() in ("self", "me"):
            out.append(me_id)
        else:
            try:
                out.append(int(raw))
            except ValueError:
                log.warning("Invalid CONTROL_CHAT_ID entry: %r", raw)
    return out


# ----------------- Utils -----------------
def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def today_str() -> str:
    return datetime.now(tz=SERVICE_TZ).strftime("%Y-%m-%d")


def normalize_quotes(s: str) -> str:
    s = s.replace("“", '"').replace("”", '"').replace("‘", "'").replace("’", "'")
    return s


def slug(s: str, max_len: int = 40) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > max_len:
        s = s[:max_len].rstrip("-")
    return s or "item"


def caption_for(name: str, description: str) -> str:
    base = f"<b>{esc(name)}</b>"
    if description:
        return f"{base}\n{esc(description)}"
    return base


def summarize_multiline(text: str, max_lines: int = 4, max_chars: int = 180) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + ["…"]
    joined = "\n".join(lines)
    if len(joined) <= max_chars:
        return joined
    truncated = joined[: max_chars - 1] + "…"
    return truncated


def campaign_hash(text: str, images: List[Any]) -> str:
    # Normalize images so we can hash both legacy string paths/URLs and new media refs.
    norm_imgs: List[str] = []
    for img in images or []:
        if isinstance(img, dict):
            cid = img.get("chat_id")
            mid = img.get("message_id")
            norm_imgs.append(f"{cid}:{mid}")
        else:
            norm_imgs.append(str(img))
    payload = {"text": (text or "").strip(), "images": sorted(norm_imgs)}
    h = hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    return f"sha256:{h}"


def normalize_title(title: Optional[str]) -> Optional[str]:
    if not title:
        return None
    t = re.sub(r"\s+", " ", title.strip()).lower()
    return t or None


def canonical_chat_id(entity) -> Optional[int]:
    if entity is None:
        return None
    if isinstance(entity, PeerUser):
        return entity.user_id
    if isinstance(entity, PeerChat):
        return -entity.chat_id
    if isinstance(entity, PeerChannel):
        return -entity.channel_id
    if hasattr(entity, "id") and hasattr(entity, "channel_id"):
        return -int(entity.id)
    if hasattr(entity, "id"):
        return int(entity.id)
    return None


# ----------------- Mongo setup -----------------
def get_db():
    cli = MongoClient(MONGO_URI)
    db = cli[MONGO_DB]

    chats = db[CHATS_COLL]
    products = db[PRODUCTS_COLL]
    scheduled = db[SCHEDULED_COLL]
    deliveries = db[DELIVERIES_COLL]
    saved = db[SAVED_COLL]

    # --- chatId index: only create if it doesn't already exist ---
    try:
        idx = chats.index_information()
    except Exception as e:
        log.warning("Could not read index_information for %s: %s", CHATS_COLL, e)
        idx = {}

    # If an index named "chatId_1" already exists, don't touch it.
    if "chatId_1" not in idx:
        try:
            chats.create_index(
                [("chatId", ASCENDING)],
                unique=True,
                name="chatId_1",
            )
            log.info("Created index chatId_1 on %s", CHATS_COLL)
        except OperationFailure as e:
            log.warning(
                "Could not create unique index chatId_1 on %s (%s). "
                "Continuing without enforcing uniqueness on chatId.",
                CHATS_COLL,
                getattr(e, "details", {}).get("errmsg", str(e)),
            )

    # --- normalizedTitle index: also only create if missing ---
    if "normalizedTitle_1" not in idx:
        try:
            chats.create_index(
                [("normalizedTitle", ASCENDING)],
                unique=True,
                sparse=True,
                name="normalizedTitle_1",
            )
            log.info("Created index normalizedTitle_1 on %s", CHATS_COLL)
        except OperationFailure as e:
            log.warning(
                "Could not create unique index normalizedTitle_1 on %s (%s). "
                "Falling back to non-unique sparse index.",
                CHATS_COLL,
                getattr(e, "details", {}).get("errmsg", str(e)),
            )
            try:
                chats.create_index(
                    [("normalizedTitle", ASCENDING)],
                    sparse=True,
                    name="normalizedTitle_1",
                )
            except Exception as e2:
                log.warning(
                    "Also failed to create non-unique normalizedTitle_1 on %s: %s",
                    CHATS_COLL,
                    e2,
                )

    # Deliveries index (safe to keep as-is — name will auto-resolve if not present)
    try:
        deliveries.create_index(
            [("scheduledId", ASCENDING), ("chatId", ASCENDING)],
            unique=True,
        )
    except OperationFailure as e:
        log.warning(
            "Could not create unique index on Deliveries (scheduledId, chatId): %s",
            getattr(e, "details", {}).get("errmsg", str(e)),
        )

    return db


# ----------------- Time parsing -----------------
def parse_scheduled_to_utc(when_str: str) -> datetime:
    when_str = (when_str or "").strip()
    if not when_str:
        raise ValueError("Empty time string")
    if when_str.lower() == "now":
        return datetime.now(timezone.utc) + timedelta(minutes=5)
    try:
        local_dt = dtparser.parse(when_str, fuzzy=True)
    except Exception as e:
        raise ValueError(f"Could not parse time: {e}")
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=SERVICE_TZ)
    else:
        local_dt = local_dt.astimezone(SERVICE_TZ)
    return local_dt.astimezone(timezone.utc)


def extract_time_of_day_local(when_str: str) -> Tuple[int, int]:
    when_str = (when_str or "").strip()
    if not when_str:
        raise ValueError("Empty time string")
    local_dt = dtparser.parse(when_str, fuzzy=True)
    if local_dt.tzinfo is None:
        local_dt = local_dt.replace(tzinfo=SERVICE_TZ)
    else:
        local_dt = local_dt.astimezone(SERVICE_TZ)
    return local_dt.hour, local_dt.minute


# ----------------- Simple throttling -----------------
_last_send_time: Optional[datetime] = None
MIN_DELAY_SECONDS = 0.3


async def throttle():
    global _last_send_time
    now = datetime.now(timezone.utc)
    if _last_send_time is not None:
        delta = (now - _last_send_time).total_seconds()
        if delta < MIN_DELAY_SECONDS:
            await asyncio.sleep(MIN_DELAY_SECONDS - delta)
    _last_send_time = datetime.now(timezone.utc)


# ----------------- Telegram send helpers -----------------
async def send_text_safe(
    client: TelegramClient,
    chat_id: int,
    text: str,
    parse_mode: Optional[str] = None,
    link_preview: bool = False,
) -> List[int]:
    if not text:
        return []
    try:
        await throttle()
        msg = await client.send_message(
            chat_id, text, parse_mode=parse_mode, link_preview=link_preview
        )
        return [msg.id]
    except FloodWaitError as e:
        log.warning("FloodWait while sending text to %s: %s", chat_id, e)
        await asyncio.sleep(e.seconds + 1)
        msg = await client.send_message(
            chat_id, text, parse_mode=parse_mode, link_preview=link_preview
        )
        return [msg.id]


async def _upload_all(client: TelegramClient, paths: List[str]):
    up = []
    for p in paths:
        await throttle()
        up.append(p)
    return up


async def _download_one(url_or_path: str, dest_dir: str) -> Optional[str]:
    import aiohttp, urllib.request

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
                if resp.status != 200:
                    return None
                data = await resp.read()
                with open(path, "wb") as f:
                    f.write(data)
        return path
    except Exception:
        try:
            with urllib.request.urlopen(url_or_path, timeout=30) as r:
                data = r.read()
                with open(path, "wb") as f:
                    f.write(data)
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
            im2.save(out_path, "JPEG", quality=90, optimize=True)
            return out_path
    except Exception:
        return src_path


async def send_files_safe(
    client: TelegramClient,
    chat_id: int,
    files,
    *,
    caption: Optional[str] = None,
    parse_mode: str = "html",
) -> List[int]:
    """
    Send a list of images safely.
    - If `files` is a list of media refs: {"chat_id": int, "message_id": int}, reuse Telegram media (Saved Messages vault).
    - Otherwise, treat `files` as legacy paths/URLs and fall back to downloading and uploading.
    """
    if not files:
        return []
    files = list(files)[:10]

    # --- New path: media references (Saved Messages / vault) ---
    if all(isinstance(f, dict) and "chat_id" in f and "message_id" in f for f in files):
        media_refs = files
        msgs = []
        for ref in media_refs:
            try:
                ref_chat = int(ref.get("chat_id"))
                mid = int(ref.get("message_id"))
                m = await client.get_messages(ref_chat, ids=mid)
                if m and m.media:
                    msgs.append(m)
            except Exception as e:
                log.warning("Failed to resolve media ref %s: %s", ref, e)
        if not msgs:
            ids: List[int] = []
            if caption:
                ids.extend(
                    await send_text_safe(
                        client,
                        chat_id,
                        caption,
                        parse_mode=parse_mode,
                        link_preview=True,
                    )
                )
            return ids

        media_objs = [m.media for m in msgs]
        await throttle()
        try:
            if len(media_objs) == 1:
                result = await client.send_file(
                    chat_id,
                    media_objs[0],
                    caption=caption,
                    parse_mode=parse_mode,
                    force_document=False,
                )
            else:
                captions = (
                    [caption] + [""] * (len(media_objs) - 1) if caption else None
                )
                result = await client.send_file(
                    chat_id,
                    media_objs,
                    caption=captions,
                    parse_mode=parse_mode,
                    force_document=False,
                )
            if isinstance(result, list):
                return [m.id for m in result]
            return [result.id]
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds + 1)
            try:
                if len(media_objs) == 1:
                    result = await client.send_file(
                        chat_id,
                        media_objs[0],
                        caption=caption,
                        parse_mode=parse_mode,
                        force_document=False,
                    )
                else:
                    captions = (
                        [caption] + [""] * (len(media_objs) - 1)
                        if caption
                        else None
                    )
                    result = await client.send_file(
                        chat_id,
                        media_objs,
                        caption=captions,
                        parse_mode=parse_mode,
                        force_document=False,
                    )
                if isinstance(result, list):
                    return [m.id for m in result]
                return [result.id]
            except Exception as e2:
                log.warning(
                    "Album send with media refs failed after FloodWait for chat %s: %s",
                    chat_id,
                    e2,
                )
                ids = []
                if caption:
                    ids.extend(
                        await send_text_safe(
                            client,
                            chat_id,
                            caption,
                            parse_mode=parse_mode,
                            link_preview=True,
                        )
                    )
                return ids
        except Exception as e:
            log.warning(
                "Album send with media refs failed for chat %s: %s", chat_id, e
            )
            ids = []
            if caption:
                ids.extend(
                    await send_text_safe(
                        client,
                        chat_id,
                        caption,
                        parse_mode=parse_mode,
                        link_preview=True,
                    )
                )
            return ids

    # --- Legacy path: treat as paths/URLs, download then upload as album ---
    tmpdir = tempfile.mkdtemp(prefix="tg_album_")
    local_paths: List[str] = []
    prepared_paths: List[str] = []
    try:
        for u in files:
            p = await _download_one(str(u), tmpdir)
            if p:
                local_paths.append(p)
        if not local_paths:
            ids = []
            if caption:
                ids.extend(
                    await send_text_safe(
                        client,
                        chat_id,
                        caption,
                        parse_mode=parse_mode,
                        link_preview=True,
                    )
                )
            for f in files:
                ids.extend(
                    await send_text_safe(
                        client,
                        chat_id,
                        str(f),
                        parse_mode=parse_mode,
                        link_preview=True,
                    )
                )
            return ids
        for p in local_paths:
            prepared_paths.append(_ensure_photo_jpeg(p, tmpdir) or p)
        uploaded = await _upload_all(client, prepared_paths)
        captions = [caption] + [""] * (len(uploaded) - 1) if caption else None
        await throttle()
        result = await client.send_file(
            chat_id,
            uploaded,
            caption=captions,
            parse_mode=parse_mode,
            force_document=False,
        )
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        uploaded = await _upload_all(client, prepared_paths or local_paths)
        captions = [caption] + [""] * (len(uploaded) - 1) if caption else None
        result = await client.send_file(
            chat_id,
            uploaded,
            caption=captions,
            parse_mode=parse_mode,
            force_document=False,
        )
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    except Exception as e:
        log.warning(
            "Album send failed for chat %s: %s; fallback to links.", chat_id, e
        )
        ids = []
        if caption:
            ids.extend(
                await send_text_safe(
                    client,
                    chat_id,
                    caption,
                    parse_mode=parse_mode,
                    link_preview=True,
                )
            )
        for f in files:
            ids.extend(
                await send_text_safe(
                    client,
                    chat_id,
                    str(f),
                    parse_mode=parse_mode,
                    link_preview=True,
                )
            )
        return ids
    finally:
        try:
            for p in set(local_paths + prepared_paths):
                try:
                    os.remove(p)
                except Exception:
                    pass
            os.rmdir(tmpdir)
        except Exception:
            pass


def uploads_dir() -> str:
    d = Path("data/uploads")
    d.mkdir(parents=True, exist_ok=True)
    return str(d.resolve())


# ----------------- Pending capture sessions -----------------
PENDING_INSERT: Dict[tuple, Dict[str, Any]] = {}  # key=(chat_id,user_id)
PENDING_SAVE: Dict[tuple, Dict[str, Any]] = {}

# ----------------- Dialog sync (auto-discovery) with title de-dup -----------------
async def sync_all_chats_from_dialogs(client: TelegramClient, chats_coll):
    """Scan dialogs and upsert groups/channels; avoid duplicates by normalized title."""
    now = datetime.now(timezone.utc)
    async for dlg in client.iter_dialogs():
        ent = dlg.entity
        chat_id = canonical_chat_id(ent)
        if chat_id is None or chat_id >= 0:
            continue  # only groups/channels
        title = getattr(ent, "title", getattr(ent, "username", None))
        norm = normalize_title(title)

        base_doc = {
            "chatId": chat_id,
            "title": title,
            "normalizedTitle": norm,
            "type": getattr(ent, "megagroup", None)
            and "megagroup"
            or getattr(ent, "__class__", type("X", (), {})).__name__,
            "isActive": True,
            "firstSeenAt": now,
            "lastSeenAt": now,
        }

        existing_by_title = None
        if norm:
            existing_by_title = chats_coll.find_one({"normalizedTitle": norm})
        if existing_by_title:
            update = {"$set": {"lastSeenAt": now, "isActive": True}}
            if "chatId" not in existing_by_title:
                update["$set"]["chatId"] = chat_id
            chats_coll.update_one({"_id": existing_by_title["_id"]}, update)
        else:
            chats_coll.update_one(
                {"chatId": chat_id},
                {
                    "$setOnInsert": base_doc,
                    "$set": {"lastSeenAt": now, "isActive": True},
                },
                upsert=True,
            )


async def periodic_dialog_sync(client: TelegramClient, db):
    chats_coll = db[CHATS_COLL]
    while True:
        try:
            await sync_all_chats_from_dialogs(client, chats_coll)
        except Exception as e:
            log.warning("Dialog sync failed: %s", e)
        await asyncio.sleep(30 * 60)


# ----------------- Scheduler core -----------------
async def post_scheduled_message(
    client: TelegramClient, chat_id: int, msg_doc: dict
) -> List[int]:
    text = msg_doc.get("text", "") or ""
    images = (msg_doc.get("images", []) or [])[:10]
    parse_mode = (
        "html" if (msg_doc.get("parseMode", "HTML") or "HTML").upper() == "HTML" else None
    )
    disable_preview = bool(msg_doc.get("disablePreview", True))

    if images:
        return await send_files_safe(
            client,
            chat_id,
            images,
            caption=text if text else None,
            parse_mode=parse_mode or "html",
        )
    else:
        return await send_text_safe(
            client,
            chat_id,
            text,
            parse_mode=parse_mode or "html",
            link_preview=not disable_preview,
        )


async def scheduler_loop(client: TelegramClient, db):
    scheduled = db[SCHEDULED_COLL]
    deliveries = db[DELIVERIES_COLL]
    chats_coll = db[CHATS_COLL]
    log.info("Scheduler loop started.")
    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            cur = (
                scheduled.find(
                    {
                        "status": {"$in": ["scheduled", None, "processing"]},
                        "scheduledAt": {"$lte": now_utc},
                    }
                )
                .sort("scheduledAt", 1)
                .limit(50)
            )
            for msg in cur:
                msg_id = msg["_id"]
                scheduled.update_one(
                    {"_id": msg_id}, {"$set": {"status": "processing"}}
                )

                explicit_ids = msg.get("targetChatIds") or []
                targets_mode = (msg.get("targets") or "all").lower()
                if explicit_ids:
                    chat_ids = sorted(
                        {
                            int(x)
                            for x in explicit_ids
                            if isinstance(x, (int, str)) and int(x) < 0
                        }
                    )
                elif targets_mode == "all":
                    chat_ids = [
                        int(d["chatId"])
                        for d in chats_coll.find(
                            {"isActive": True, "chatId": {"$lt": 0}}, {"chatId": 1}
                        )
                    ]
                    chat_ids = sorted(set(chat_ids))
                else:
                    chat_ids = []

                if not chat_ids:
                    log.info("No targets for scheduled message %s", msg_id)
                    scheduled.update_one(
                        {"_id": msg_id}, {"$set": {"status": "no_targets"}}
                    )
                    continue

                for cid in chat_ids:
                    if deliveries.find_one(
                        {"scheduledId": msg_id, "chatId": cid}
                    ):
                        continue
                    try:
                        ids = await post_scheduled_message(client, cid, msg)
                        deliveries.insert_one(
                            {
                                "scheduledId": msg_id,
                                "chatId": cid,
                                "messageIds": ids,
                                "status": "sent",
                                "sentAt": datetime.now(timezone.utc),
                            }
                        )
                    except Exception as e:
                        log.warning(
                            "Failed to send scheduled %s to %s: %s", msg_id, cid, e
                        )
                        deliveries.insert_one(
                            {
                                "scheduledId": msg_id,
                                "chatId": cid,
                                "messageIds": [],
                                "status": "error",
                                "error": str(e),
                                "sentAt": datetime.now(timezone.utc),
                            }
                        )
                scheduled.update_one(
                    {"_id": msg_id},
                    {"$set": {"status": "done", "completedAt": datetime.now(timezone.utc)}},
                )
        except Exception as e:
            log.error("Scheduler loop error: %s", e)
        await asyncio.sleep(5)


# ----------------- Parsing helpers for /insert and /preset -----------------
def parse_name_desc_images_and_time(
    raw: str,
) -> Optional[Tuple[str, str, List[str], str, List[int], bool]]:
    """
    Parse formats like:
    /insert "Name" "Description" [https://a.jpg https://b.jpg] Nov 28 3pm groups=[-100…]
    /insert "Name" "Description" Now
    /preset save "Name" "Description" [https://…] 4pm groups=[-100…]
    /preset save "Name" "Description" 4pm
    Returns (name, desc, images, when_str, target_ids, attach_mode)
    where attach_mode=True means "time is Now/no URLs: expect images via /done flow".
    """
    raw = normalize_quotes(raw or "")
    if not raw:
        return None
    try:
        parts = shlex.split(raw)
    except ValueError:
        return None
    if not parts:
        return None
    if parts[0].lower() in ("/insert", "insert", "/preset", "preset", "save"):
        parts = parts[1:]
    if len(parts) < 2:
        return None
    name = parts[0].strip('"')
    description = parts[1].strip('"')
    rest = parts[2:]
    images: List[str] = []
    when_str = ""
    target_ids: List[int] = []
    attach_mode = False

    txt = " ".join(rest)
    m = re.search(r"\[([^\]]*)\]", txt)
    if m:
        block = m.group(1).strip()
        txt_before = txt[: m.start()].strip()
        txt_after = txt[m.end() :].strip()
        txt = " ".join([t for t in [txt_before, txt_after] if t])
        if block:
            images = [x for x in block.split() if x]

    m2 = re.search(r"groups=\[([^\]]*)\]", txt)
    if m2:
        block = m2.group(1).strip()
        txt_before = txt[: m2.start()].strip()
        txt_after = txt[m2.end() :].strip()
        txt = " ".join([t for t in [txt_before, txt_after] if t])
        if block:
            ids_raw = [x for x in block.split() if x]
            for s in ids_raw:
                try:
                    cid = int(s)
                    target_ids.append(cid)
                except ValueError:
                    pass

    when_str = (txt or "").strip() or "Now"
    if not images and when_str.lower() == "now":
        attach_mode = True
    return name, description, images, when_str, target_ids, attach_mode


# ----------------- Main bot logic -----------------
async def main():
    db = get_db()
    chats_coll = db[CHATS_COLL]
    products = db[PRODUCTS_COLL]
    scheduled_coll = db[SCHEDULED_COLL]
    saved_coll = db[SAVED_COLL]

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()
    me = await client.get_me()
    my_id = me.id
    log.info("Logged in as %s (%s)", me.username or me.first_name, my_id)

    control_ids = parse_control_chat_ids(my_id)
    CONTROL_CHAT_IDS: List[int] = control_ids

    async def is_control_message(event) -> bool:
        if (event.sender_id == my_id) or (
            AUTHORIZED_USER_ID and event.sender_id == AUTHORIZED_USER_ID
        ):
            if not CONTROL_CHAT_IDS:
                return True
            return event.chat_id in CONTROL_CHAT_IDS or event.sender_id == my_id
        return False

    # Background tasks
    asyncio.create_task(periodic_dialog_sync(client, db))
    asyncio.create_task(scheduler_loop(client, db))

    # ------------- Chat join/leave tracking -------------
    @client.on(events.ChatAction)
    async def on_chat_action(event):
        if event.user_added or event.user_joined:
            if event.user_id == my_id:
                ent = await event.get_chat()
                chat_id = canonical_chat_id(ent)
                if chat_id is None or chat_id >= 0:
                    return
                title = getattr(ent, "title", getattr(ent, "username", None))
                norm = normalize_title(title)
                now = datetime.now(timezone.utc)
                base_doc = {
                    "chatId": chat_id,
                    "title": title,
                    "normalizedTitle": norm,
                    "type": getattr(ent, "megagroup", None)
                    and "megagroup"
                    or getattr(ent, "__class__", type("X", (), {})).__name__,
                    "isActive": True,
                    "firstSeenAt": now,
                    "lastSeenAt": now,
                }
                existing_by_title = None
                if norm:
                    existing_by_title = chats_coll.find_one({"normalizedTitle": norm})
                if existing_by_title:
                    update = {"$set": {"lastSeenAt": now, "isActive": True}}
                    if "chatId" not in existing_by_title:
                        update["$set"]["chatId"] = chat_id
                    chats_coll.update_one({"_id": existing_by_title["_id"]}, update)
                else:
                    chats_coll.update_one(
                        {"chatId": chat_id},
                        {
                            "$setOnInsert": base_doc,
                            "$set": {"lastSeenAt": now, "isActive": True},
                        },
                        upsert=True,
                    )
        elif event.user_left or event.user_kicked:
            if event.user_id == my_id:
                ent = await event.get_chat()
                chat_id = canonical_chat_id(ent)
                if chat_id is None or chat_id >= 0:
                    return
                now = datetime.now(timezone.utc)
                chats_coll.update_one(
                    {"chatId": chat_id},
                    {"$set": {"isActive": False, "lastSeenAt": now}},
                    upsert=True,
                )

    # ------------- Auto-discover chats on any message -------------
    @client.on(events.NewMessage)
    async def discover_on_message(event):
        if not event.is_group and not event.is_channel:
            return
        ent = await event.get_chat()
        chat_id = canonical_chat_id(ent)
        if chat_id is None or chat_id >= 0:
            return
        title = getattr(ent, "title", getattr(ent, "username", None))
        norm = normalize_title(title)
        now = datetime.now(timezone.utc)
        base_doc = {
            "chatId": chat_id,
            "title": title,
            "normalizedTitle": norm,
            "type": getattr(ent, "megagroup", None)
            and "megagroup"
            or getattr(ent, "__class__", type("X", (), {})).__name__,
            "isActive": True,
            "firstSeenAt": now,
            "lastSeenAt": now,
        }
        existing_by_title = None
        if norm:
            existing_by_title = chats_coll.find_one({"normalizedTitle": norm})
        if existing_by_title:
            update = {"$set": {"lastSeenAt": now, "isActive": True}}
            if "chatId" not in existing_by_title:
                update["$set"]["chatId"] = chat_id
            chats_coll.update_one({"_id": existing_by_title["_id"]}, update)
        else:
            chats_coll.update_one(
                {"chatId": chat_id},
                {
                    "$setOnInsert": base_doc,
                    "$set": {"lastSeenAt": now, "isActive": True},
                },
                upsert=True,
            )

    # ------------- Commands -------------

    @client.on(events.NewMessage(pattern=r"(?i)^/here(?:\s|$)"))
    async def cmd_here(event):
        if not await is_control_message(event):
            return
        ent = await event.get_chat()
        chat_id = canonical_chat_id(ent)
        if chat_id is None or chat_id >= 0:
            return await event.reply("Use /here in a group/channel.")
        title = getattr(ent, "title", getattr(ent, "username", None))
        norm = normalize_title(title)
        now = datetime.now(timezone.utc)
        base_doc = {
            "chatId": chat_id,
            "title": title,
            "normalizedTitle": norm,
            "type": getattr(ent, "megagroup", None)
            and "megagroup"
            or getattr(ent, "__class__", type("X", (), {})).__name__,
            "isActive": True,
            "firstSeenAt": now,
            "lastSeenAt": now,
        }
        existing_by_title = None
        if norm:
            existing_by_title = chats_coll.find_one({"normalizedTitle": norm})
        if existing_by_title:
            update = {"$set": {"lastSeenAt": now, "isActive": True}}
            if "chatId" not in existing_by_title:
                update["$set"]["chatId"] = chat_id
            chats_coll.update_one({"_id": existing_by_title["_id"]}, update)
            return await event.reply(
                f"Updated existing chat by title: <code>{esc(title)}</code> (ID <code>{chat_id}</code>)",
                parse_mode="html",
            )
        else:
            chats_coll.update_one(
                {"chatId": chat_id},
                {
                    "$setOnInsert": base_doc,
                    "$set": {"lastSeenAt": now, "isActive": True},
                },
                upsert=True,
            )
            return await event.reply(
                f"Registered chat: <code>{chat_id}</code> — {esc(title)}",
                parse_mode="html",
            )

    @client.on(events.NewMessage(pattern=r"(?i)^/groups_add\s+(.+)$"))
    async def cmd_groups_add(event):
        if not await is_control_message(event):
            return
        m = re.search(r"\[([^\]]+)\]", event.raw_text)
        if not m:
            return await event.reply(
                "Usage: /groups_add [ -1001234 -1005678 ]", parse_mode="html"
            )
        ids_s = m.group(1).strip().split()
        added, updated = 0, 0
        for s in ids_s:
            try:
                cid = int(s)
            except ValueError:
                continue
            if cid >= 0:
                continue
            try:
                ent = await client.get_entity(cid)
            except Exception as e:
                await event.reply(
                    f"⚠️ Could not fetch {cid}: <code>{esc(e)}</code>",
                    parse_mode="html",
                )
                continue
            title = getattr(ent, "title", getattr(ent, "username", None))
            norm = normalize_title(title)
            now = datetime.now(timezone.utc)
            base_doc = {
                "chatId": cid,
                "title": title,
                "normalizedTitle": norm,
                "type": getattr(ent, "megagroup", None)
                and "megagroup"
                or getattr(ent, "__class__", type("X", (), {})).__name__,
                "isActive": True,
                "firstSeenAt": now,
                "lastSeenAt": now,
            }
            existing_by_title = None
            if norm:
                existing_by_title = chats_coll.find_one({"normalizedTitle": norm})
            if existing_by_title:
                update = {"$set": {"lastSeenAt": now, "isActive": True}}
                if "chatId" not in existing_by_title:
                    update["$set"]["chatId"] = cid
                chats_coll.update_one({"_id": existing_by_title["_id"]}, update)
                updated += 1
            else:
                chats_coll.update_one(
                    {"chatId": cid},
                    {
                        "$setOnInsert": base_doc,
                        "$set": {"lastSeenAt": now, "isActive": True},
                    },
                    upsert=True,
                )
                added += 1
        await event.reply(
            f"Done. Added {added}, updated {updated} chats.", parse_mode="html"
        )

    @client.on(events.NewMessage(pattern=r"(?i)^/chats(?:\s|$)"))
    async def cmd_chats(event):
        if not await is_control_message(event):
            return
        docs = list(
            chats_coll.find(
                {"chatId": {"$lt": 0}}, {"chatId": 1, "title": 1, "isActive": 1}
            ).sort("title", 1)
        )
        if not docs:
            return await event.reply("No chats in DB yet.")
        lines = []
        for d in docs:
            flag = "✅" if d.get("isActive") else "❌"
            lines.append(
                f"{flag} <code>{d['chatId']}</code> — {esc(d.get('title',''))}"
            )
        await event.reply("\n".join(lines), parse_mode="html")

    @client.on(events.NewMessage(pattern=r"(?i)^/id(?:\s|$)"))
    async def cmd_id(event):
        ent = await event.get_chat()
        chat_id = canonical_chat_id(ent)
        await event.reply(
            f"Chat ID: <code>{chat_id}</code>\nYour ID: <code>{event.sender_id}</code>",
            parse_mode="html",
        )

    # -------- /insert --------
    @client.on(events.NewMessage(pattern=r"(?i)^/insert(?:\s|$)"))
    async def cmd_insert(event):
        if not await is_control_message(event):
            return
        raw = normalize_quotes(event.raw_text.strip())
        if raw.lower().startswith("/insert /insert"):
            raw = raw[8:].lstrip()
        parsed = parse_name_desc_images_and_time(raw)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                '<code>/insert "Name" "Description" [https://a.jpg https://b.jpg] Nov 28 3pm groups=[-100…]</code>\n'
                "or attach-mode:\n"
                '<code>/insert "Name" "Description" Now</code> (send photos, then /done)',
                parse_mode="html",
            )
        name, description, images, when_str, target_ids, attach_mode = parsed
        if target_ids and any(cid >= 0 for cid in target_ids):
            return await event.reply(
                "❌ Targets must be negative IDs (-100…).", parse_mode="html"
            )

        # Normal mode: immediate URLs/paths
        if not attach_mode:
            try:
                scheduled_utc = parse_scheduled_to_utc(when_str)
            except Exception as e:
                return await event.reply(
                    f"❌ Could not parse time: <code>{esc(e)}</code>",
                    parse_mode="html",
                )
            product_doc = {
                "name": name,
                "description": description,
                "images": images[:10],
                "last_edit": today_str(),
            }
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
                await send_files_safe(
                    client,
                    event.chat_id,
                    [images[0]],
                    caption=caption_for(name, description),
                )
            else:
                await event.reply(
                    caption_for(name, description), parse_mode="html"
                )
            lt = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
            ut = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
            return await event.reply(
                f"📅 Scheduled for: <b>{esc(lt)}</b> (<code>{esc(ut)}</code>)",
                parse_mode="html",
            )

        # Attach-mode: /insert "Name" "Desc" Now → expect images + /done
        key = (event.chat_id, event.sender_id)
        old = PENDING_INSERT.pop(key, None)
        if old:
            await event.reply(
                "ℹ️ Previous pending insert was discarded.", parse_mode="html"
            )
        PENDING_INSERT[key] = {
            "name": name,
            "description": description,
            "when_str": when_str,
            "targets": target_ids,
            "media": [],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
            "task": None,
        }

        async def timeout_finalize():
            await asyncio.sleep(60)
            if key in PENDING_INSERT:
                await event.reply(
                    "⏰ Time window ended. Finalizing with images received so far…",
                    parse_mode="html",
                )
                await finalize_insert_schedule(
                    client, key, db, products, chats_coll, scheduled_coll
                )

        PENDING_INSERT[key]["task"] = asyncio.create_task(timeout_finalize())
        await event.reply(
            "📎 Attach up to 10 photos now (as images or image-doc files). When done, send <code>/done</code>.",
            parse_mode="html",
        )

    async def finalize_insert_schedule(
        client: TelegramClient, key: tuple, db, products, chats_coll, scheduled_coll
    ):
        sess = PENDING_INSERT.pop(key, None)
        if not sess:
            return
        name = sess["name"]
        description = sess["description"]
        when_str = sess["when_str"]
        target_ids = sess["targets"]
        media_refs: List[Dict[str, Any]] = sess.get("media", [])
        try:
            scheduled_utc = parse_scheduled_to_utc(when_str)
        except Exception as e:
            return await client.send_message(
                key[0],
                f"❌ Could not parse time: <code>{esc(e)}</code>",
                parse_mode="html",
            )
        product_doc = {
            "name": name,
            "description": description,
            "images": media_refs,
            "last_edit": today_str(),
        }
        res = products.insert_one(product_doc)
        product_id = res.inserted_id
        text = f"<b>{esc(name)}</b>\n{esc(description)}"
        sched_doc = {
            "_id": f"auto_{product_id}",
            "text": text,
            "images": media_refs[:10],
            "parseMode": "HTML",
            "disablePreview": True,
            "scheduledAt": scheduled_utc,
            "targets": "explicit" if target_ids else "all",
            "targetChatIds": target_ids if target_ids else None,
            "status": "scheduled",
            "productId": product_id,
            "createdAt": datetime.now(timezone.utc),
            "contentHash": campaign_hash(text, media_refs[:10]),
        }
        db[SCHEDULED_COLL].insert_one(sched_doc)

        if media_refs:
            await send_files_safe(
                client,
                key[0],
                media_refs[:1],
                caption=caption_for(name, description),
            )
        else:
            await client.send_message(
                key[0], caption_for(name, description), parse_mode="html"
            )

        lt = scheduled_utc.astimezone(SERVICE_TZ).strftime("%Y-%m-%d %H:%M %Z")
        ut = scheduled_utc.strftime("%Y-%m-%d %H:%M UTC")
        await client.send_message(
            key[0],
            f"📅 Scheduled for: <b>{esc(lt)}</b> (<code>{esc(ut)}</code>)",
            parse_mode="html",
        )

    # -------- /done --------
    @client.on(events.NewMessage(pattern=r"(?i)^/done(?:\s|$)"))
    async def cmd_done(event):
        if not (
            (event.sender_id == my_id)
            or (AUTHORIZED_USER_ID and event.sender_id == AUTHORIZED_USER_ID)
        ):
            return
        if CONTROL_CHAT_IDS and (
            event.chat_id not in CONTROL_CHAT_IDS and event.sender_id != my_id
        ):
            return

        key = (event.chat_id, event.sender_id)
        if key in PENDING_INSERT:
            task = PENDING_INSERT[key].get("task")
            if task and not task.done():
                task.cancel()
            return await finalize_insert_schedule(
                client, key, db, products, chats_coll, scheduled_coll
            )
        if key in PENDING_SAVE:
            task = PENDING_SAVE[key].get("task")
            if task and not task.done():
                task.cancel()
            sess = PENDING_SAVE[key]
            return await finalize_save_preset(
                client,
                key,
                db,
                saved_coll,
                sess["name"],
                sess["description"],
                sess["when_str"],
                sess["targets"],
            )
        return await event.reply(
            "There is no pending action to finalize.", parse_mode="html"
        )

    # -------- Media collector: collects images for pending insert/preset --------
    @client.on(events.NewMessage)
    async def media_collector(event):
        if not event.photo and not (
            event.document
            and getattr(event.document, "mime_type", "").startswith("image/")
        ):
            return

        sender_ok = (event.sender_id == my_id) or (
            AUTHORIZED_USER_ID and event.sender_id == AUTHORIZED_USER_ID
        )
        chat_ok = (not CONTROL_CHAT_IDS) or (event.chat_id in CONTROL_CHAT_IDS)
        if not (sender_ok and chat_ok):
            return

        key = (event.chat_id, event.sender_id)
        active = (
            PENDING_INSERT
            if key in PENDING_INSERT
            else (PENDING_SAVE if key in PENDING_SAVE else None)
        )
        if not active:
            return
        sess = active.get(key)
        if not sess:
            return

        if datetime.now(timezone.utc) > sess["expires_at"]:
            task = sess.get("task")
            if task and not task.done():
                task.cancel()
            if active is PENDING_INSERT:
                await finalize_insert_schedule(
                    client, key, db, products, chats_coll, scheduled_coll
                )
            else:
                await finalize_save_preset(
                    client,
                    key,
                    db,
                    saved_coll,
                    sess["name"],
                    sess["description"],
                    sess["when_str"],
                    sess["targets"],
                )
            return

        # Saved Messages already holds this image; just reference it
        try:
            ref = {"chat_id": int(event.chat_id), "message_id": int(event.id)}
            media_list = sess.setdefault("media", [])
            media_list.append(ref)
            if len(media_list) > 10:
                del media_list[10:]
            await event.reply(
                f"✅ Added image ({len(media_list)}/10).", parse_mode="html"
            )
        except Exception as e:
            log.warning("Media reference capture failed: %s", e)
            await event.reply(
                "❌ Failed to register that image. Try again.", parse_mode="html"
            )

    # -------- PRESETS --------
    async def finalize_save_preset(
        client: TelegramClient,
        key: tuple,
        db,
        saved_coll,
        name: str,
        description: str,
        when_str: str,
        targets: List[int],
    ):
        sess = PENDING_SAVE.pop(key, None)
        media_refs: List[Dict[str, Any]] = (sess.get("media", []) if sess else [])
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
            "images": media_refs[:10],
            "parseMode": "HTML",
            "disablePreview": True,
            "defaultTargets": "explicit",
            "targetChatIds": [
                int(x) for x in targets if isinstance(x, int) and x < 0
            ],
            "timeOfDayLocal": {
                "hour": hour,
                "minute": minute,
                "tz": str(SERVICE_TZ),
            },
            "whenStrOriginal": when_str,
            "createdAt": datetime.now(timezone.utc),
            "updatedAt": datetime.now(timezone.utc),
        }
        saved_coll.insert_one(doc)
        if media_refs:
            await send_files_safe(
                client,
                key[0],
                media_refs[:1],
                caption=f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>",
                parse_mode="html",
            )
        else:
            await client.send_message(
                key[0],
                f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>",
                parse_mode="html",
            )

    @client.on(events.NewMessage(pattern=r"(?i)^/preset\s+save(?:\s|$)"))
    async def cmd_preset_save(event):
        if not await is_control_message(event):
            return

        raw = normalize_quotes(event.raw_text.strip())
        raw = re.sub(r"(?i)^/preset\s+save\s*", "", raw, count=1).strip()
        parsed = parse_name_desc_images_and_time(raw)
        if not parsed:
            return await event.reply(
                "Usage:\n"
                '<code>/preset save "Name" "Description" [https://a.jpg …] 4pm groups=[-100…]</code>\n'
                "or attach-mode:\n"
                '<code>/preset save "Name" "Description" 4pm</code> (send photos, then /done)\n'
                "• Saves text, images, targets, and the time-of-day for reuse.",
                parse_mode="html",
            )
        name, description, images, when_str, target_ids, attach_mode = parsed
        if target_ids and any(cid >= 0 for cid in target_ids):
            return await event.reply(
                "❌ Targets must be negative IDs (-100…).", parse_mode="html"
            )

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
                "timeOfDayLocal": {
                    "hour": hour,
                    "minute": minute,
                    "tz": str(SERVICE_TZ),
                },
                "whenStrOriginal": when_str,
                "createdAt": datetime.now(timezone.utc),
                "updatedAt": datetime.now(timezone.utc),
            }
            saved_coll.insert_one(doc)
            if images:
                await send_files_safe(
                    client,
                    event.chat_id,
                    [images[0]],
                    caption=f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>",
                    parse_mode="html",
                )
            else:
                await event.reply(
                    f"💾 Saved preset <b>{esc(name)}</b>\nCode: <code>{code}</code>",
                    parse_mode="html",
                )
            return

        key = (event.chat_id, event.sender_id)
        old = PENDING_SAVE.pop(key, None)
        if old:
            await event.reply(
                "ℹ️ Previous pending preset-save was discarded.", parse_mode="html"
            )
        PENDING_SAVE[key] = {
            "name": name,
            "description": description,
            "when_str": when_str,
            "targets": target_ids,
            "media": [],
            "expires_at": datetime.now(timezone.utc) + timedelta(seconds=60),
            "task": None,
        }

        async def timeout_finalize():
            await asyncio.sleep(60)
            if key in PENDING_SAVE:
                await event.reply(
                    "⏰ Time window ended. Finalizing preset with images received so far…",
                    parse_mode="html",
                )
                sess = PENDING_SAVE[key]
                await finalize_save_preset(
                    client,
                    key,
                    db,
                    saved_coll,
                    sess["name"],
                    sess["description"],
                    sess["when_str"],
                    sess["targets"],
                )

        PENDING_SAVE[key]["task"] = asyncio.create_task(timeout_finalize())
        await event.reply(
            "📎 Attach up to 10 photos now (as images or image-doc files). When done, send <code>/done</code>.",
            parse_mode="html",
        )

    log.info("Bot is up and running.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
