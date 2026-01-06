from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from croniter import croniter
from dotenv import load_dotenv
from pymongo import MongoClient, ASCENDING
from pymongo.errors import DuplicateKeyError
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from telethon.sessions import StringSession
from telethon.tl.types import PeerChat, PeerChannel, PeerUser
from zoneinfo import ZoneInfo

try:
    from .settings import load_settings
except ImportError:
    from settings import load_settings

log = logging.getLogger("tg_worker")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("telethon").setLevel(logging.INFO)


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


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


def build_caption(title: str, description: str) -> str:
    title = (title or "").strip()
    description = (description or "").strip()
    if description:
        return f"<b>{esc(title)}</b>\n{esc(description)}"
    return f"<b>{esc(title)}</b>"


_last_send_time: Optional[datetime] = None


async def throttle(min_delay_seconds: float) -> None:
    global _last_send_time
    now = datetime.now(timezone.utc)
    if _last_send_time is not None:
        delta = (now - _last_send_time).total_seconds()
        if delta < min_delay_seconds:
            await asyncio.sleep(min_delay_seconds - delta)
    _last_send_time = datetime.now(timezone.utc)


async def send_text_safe(
    client: TelegramClient,
    chat_id: int,
    text: str,
    *,
    parse_mode: Optional[str] = None,
    link_preview: bool = False,
    min_delay_seconds: float = 0.35,
) -> List[int]:
    if not text:
        return []
    try:
        await throttle(min_delay_seconds)
        msg = await client.send_message(chat_id, text, parse_mode=parse_mode, link_preview=link_preview)
        return [msg.id]
    except FloodWaitError as e:
        log.warning("FloodWait while sending to %s: %s", chat_id, e)
        await asyncio.sleep(e.seconds + 1)
        msg = await client.send_message(chat_id, text, parse_mode=parse_mode, link_preview=link_preview)
        return [msg.id]


async def _download_one(url: str, dest_dir: str) -> Optional[str]:
    import aiohttp
    import urllib.request

    if os.path.exists(url):
        return url

    # derive a filename
    guess = url.split("?")[0].lower()
    ext = ".jpg"
    for e in (".jpg", ".jpeg", ".png", ".webp", ".bmp"):
        if guess.endswith(e):
            ext = e
            break
    filename = hashlib.sha1(url.encode("utf-8")).hexdigest() + ext
    path = os.path.join(dest_dir, filename)

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                with open(path, "wb") as f:
                    f.write(data)
        return path
    except Exception:
        # fallback
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                data = r.read()
            with open(path, "wb") as f:
                f.write(data)
            return path
        except Exception:
            return None


def _ensure_photo_jpeg(src_path: str, dest_dir: str) -> str:
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


async def send_images_safe(
    client: TelegramClient,
    chat_id: int,
    image_urls: List[str],
    *,
    caption: Optional[str],
    parse_mode: str = "html",
    min_delay_seconds: float = 0.35,
) -> List[int]:
    if not image_urls:
        return []
    image_urls = image_urls[:10]

    tmpdir = tempfile.mkdtemp(prefix="tg_album_")
    local_paths: List[str] = []
    prepared: List[str] = []
    try:
        for u in image_urls:
            p = await _download_one(u, tmpdir)
            if p:
                local_paths.append(p)
        if not local_paths:
            # fall back to sending links + caption
            ids: List[int] = []
            if caption:
                ids.extend(await send_text_safe(client, chat_id, caption, parse_mode=parse_mode, link_preview=True, min_delay_seconds=min_delay_seconds))
            for u in image_urls:
                ids.extend(await send_text_safe(client, chat_id, u, parse_mode=parse_mode, link_preview=True, min_delay_seconds=min_delay_seconds))
            return ids

        for p in local_paths:
            prepared.append(_ensure_photo_jpeg(p, tmpdir))

        captions = [caption] + [""] * (len(prepared) - 1) if caption else None
        await throttle(min_delay_seconds)
        result = await client.send_file(chat_id, prepared, caption=captions, parse_mode=parse_mode, force_document=False)
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        result = await client.send_file(chat_id, prepared or local_paths, caption=caption, parse_mode=parse_mode, force_document=False)
        if isinstance(result, list):
            return [m.id for m in result]
        return [result.id]
    finally:
        try:
            for p in set(local_paths + prepared):
                try:
                    os.remove(p)
                except Exception:
                    pass
            os.rmdir(tmpdir)
        except Exception:
            pass


def compute_next_run_at_utc(doc: Dict[str, Any], tz: ZoneInfo) -> datetime | None:
    schedule_type = doc.get("scheduleType", "once")
    end_at = doc.get("endAt")

    def _as_datetime(v: Any) -> datetime | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v)
            except Exception:
                return None
        return None

    end_at_dt = _as_datetime(end_at)
    # endAt is stored in Mongo as naive UTC datetime (driver strips tzinfo).
    end_utc = None
    if end_at_dt is not None:
        if end_at_dt.tzinfo is None:
            end_utc = end_at_dt.replace(tzinfo=timezone.utc)
        else:
            end_utc = end_at_dt.astimezone(timezone.utc)
    if schedule_type == "once":
        run_at = doc.get("runAt") or doc.get("nextRunAt")
        if run_at is None:
            raise ValueError("Missing runAt for once schedule")
        run_at_dt = _as_datetime(run_at)
        if run_at_dt is None:
            raise ValueError("Invalid runAt")
        if run_at_dt.tzinfo is None:
            run_at_dt = run_at_dt.replace(tzinfo=timezone.utc)
        next_utc = run_at_dt.astimezone(timezone.utc)
        if end_utc is not None and next_utc > end_utc:
            return None
        return next_utc

    cron = doc.get("cron")
    if not cron:
        raise ValueError("Missing cron for cron schedule")

    now_local = datetime.now(tz)
    it = croniter(cron, now_local)
    next_local = it.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    next_utc = next_local.astimezone(timezone.utc)
    if end_utc is not None and next_utc > end_utc:
        return None
    return next_utc


def get_db(settings):
    cli = MongoClient(settings.MONGO_URI)
    db = cli[settings.MONGODB_NAME]

    chats = db[settings.CHATS_COLLECTION]
    deliveries = db[settings.DELIVERIES_COLLECTION]
    scheduled = db[settings.SCHEDULED_MESSAGES_COLLECTION]

    # Indexes (safe)
    chats.create_index([("chatId", ASCENDING)], unique=True, name="chatId_1")
    chats.create_index([("normalizedTitle", ASCENDING)], unique=True, sparse=True, name="normalizedTitle_1")
    # Deliveries: one delivery per (scheduledId, chatId, runAt) so cron can repeat.
    existing = deliveries.index_information()
    if "scheduledId_chatId_uniq" in existing:
        try:
            deliveries.drop_index("scheduledId_chatId_uniq")
        except Exception:
            pass
    deliveries.create_index(
        [("scheduledId", ASCENDING), ("chatId", ASCENDING), ("runAt", ASCENDING)],
        unique=True,
        name="scheduledId_chatId_runAt_uniq",
        partialFilterExpression={
            "scheduledId": {"$type": "objectId"},
            "runAt": {"$type": "date"},
        },
    )
    deliveries.create_index([("scheduledId", ASCENDING), ("runAt", ASCENDING)], name="scheduledId_runAt_1")
    scheduled.create_index([("status", ASCENDING), ("nextRunAt", ASCENDING)], name="due_1")

    return db


async def sync_dialogs(client: TelegramClient, chats_coll):
    now = datetime.now(timezone.utc)
    async for dlg in client.iter_dialogs():
        ent = dlg.entity
        chat_id = canonical_chat_id(ent)
        if chat_id is None or chat_id >= 0:
            continue
        title = getattr(ent, "title", getattr(ent, "username", None))
        norm = normalize_title(title)
        doc = {
            "chatId": chat_id,
            "title": title,
            "normalizedTitle": norm,
            "type": getattr(ent, "megagroup", None) and "megagroup" or ent.__class__.__name__,
            "isActive": True,
            "firstSeenAt": now,
            "lastSeenAt": now,
        }

        # Prefer de-dupe by title if present
        if norm:
            existing = chats_coll.find_one({"normalizedTitle": norm})
            if existing:
                chats_coll.update_one({"_id": existing["_id"]}, {"$set": {"lastSeenAt": now, "isActive": True, "chatId": chat_id}})
                continue

        chats_coll.update_one(
            {"chatId": chat_id},
            {"$setOnInsert": doc, "$set": {"lastSeenAt": now, "isActive": True}},
            upsert=True,
        )


async def periodic_dialog_sync(client: TelegramClient, chats_coll, minutes: int):
    while True:
        try:
            await sync_dialogs(client, chats_coll)
        except Exception as e:
            log.warning("Dialog sync failed: %s", e)
        await asyncio.sleep(minutes * 60)


async def send_scheduled_to_targets(
    client: TelegramClient,
    db,
    settings,
    doc: Dict[str, Any],
    run_at: datetime,
) -> None:
    scheduled_coll = db[settings.SCHEDULED_MESSAGES_COLLECTION]
    deliveries_coll = db[settings.DELIVERIES_COLLECTION]
    chats_coll = db[settings.CHATS_COLLECTION]

    scheduled_id = doc["_id"]
    title = doc.get("title", "")
    description = doc.get("description", "")
    image_urls = doc.get("imageUrls") or []
    parse_mode = "html" if (doc.get("parseMode", "HTML") or "HTML").upper() == "HTML" else None
    disable_preview = bool(doc.get("disablePreview", True))

    targets_mode = (doc.get("targetsMode") or "all").lower()
    if targets_mode == "explicit":
        chat_ids = sorted({int(x) for x in (doc.get("targetChatIds") or []) if int(x) < 0})
    else:
        chat_ids = [
            int(d["chatId"])
            for d in chats_coll.find({"isActive": True, "chatId": {"$lt": 0}}, {"chatId": 1})
        ]
        chat_ids = sorted(set(chat_ids))

    if not chat_ids:
        scheduled_coll.update_one({"_id": scheduled_id}, {"$set": {"status": "no_targets", "updatedAt": datetime.now(timezone.utc)}})
        return

    caption = build_caption(title, description)

    for cid in chat_ids:
        try:
            # skip if already delivered
            if deliveries_coll.find_one({"scheduledId": scheduled_id, "chatId": cid, "runAt": run_at}):
                continue

            if image_urls:
                msg_ids = await send_images_safe(client, cid, [str(u) for u in image_urls], caption=caption, parse_mode=parse_mode or "html", min_delay_seconds=settings.MIN_DELAY_SECONDS)
            else:
                msg_ids = await send_text_safe(client, cid, caption, parse_mode=parse_mode or "html", link_preview=not disable_preview, min_delay_seconds=settings.MIN_DELAY_SECONDS)

            deliveries_coll.insert_one(
                {
                    "scheduledId": scheduled_id,
                    "chatId": cid,
                    "runAt": run_at,
                    "messageIds": msg_ids,
                    "status": "sent",
                    "sentAt": datetime.now(timezone.utc),
                }
            )
        except DuplicateKeyError:
            continue
        except Exception as e:
            log.warning("Delivery failed scheduled=%s chat=%s: %s", scheduled_id, cid, e)
            try:
                deliveries_coll.insert_one(
                    {
                        "scheduledId": scheduled_id,
                        "chatId": cid,
                        "runAt": run_at,
                        "messageIds": [],
                        "status": "error",
                        "error": str(e),
                        "sentAt": datetime.now(timezone.utc),
                    }
                )
            except DuplicateKeyError:
                pass


async def scheduler_loop(client: TelegramClient, db, settings):
    scheduled_coll = db[settings.SCHEDULED_MESSAGES_COLLECTION]

    log.info("Scheduler loop started.")
    while True:
        try:
            now = datetime.now(timezone.utc)
            cur = (
                scheduled_coll.find(
                    {
                        "enabled": True,
                        "status": {"$in": ["scheduled", "processing", None]},
                        "nextRunAt": {"$lte": now},
                    }
                )
                .sort("nextRunAt", 1)
                .limit(25)
            )

            for doc in cur:
                scheduled_id = doc["_id"]

                tz_name = doc.get("tz") or settings.TZ_NAME
                try:
                    tz = ZoneInfo(tz_name)
                except Exception:
                    tz = settings.tz

                # Use the scheduled due time as the runAt key (idempotency for cron repeats)
                run_at = doc.get("nextRunAt") or now
                if isinstance(run_at, str):
                    try:
                        run_at = datetime.fromisoformat(run_at)
                    except Exception:
                        run_at = now
                # mark processing
                scheduled_coll.update_one(
                    {"_id": scheduled_id},
                    {"$set": {"status": "processing", "lastRunAt": now, "updatedAt": now}},
                )

                await send_scheduled_to_targets(client, db, settings, doc, run_at)

                schedule_type = doc.get("scheduleType", "once")
                if schedule_type == "once":
                    scheduled_coll.update_one(
                        {"_id": scheduled_id},
                        {"$set": {"status": "done", "enabled": False, "updatedAt": datetime.now(timezone.utc)}},
                    )
                else:
                    try:
                        next_run = compute_next_run_at_utc(doc, tz)
                        if next_run is None:
                            scheduled_coll.update_one(
                                {"_id": scheduled_id},
                                {"$set": {"status": "ended", "enabled": False, "nextRunAt": None, "updatedAt": datetime.now(timezone.utc)}},
                            )
                        else:
                            scheduled_coll.update_one(
                                {"_id": scheduled_id},
                                {"$set": {"status": "scheduled", "nextRunAt": next_run, "updatedAt": datetime.now(timezone.utc)}},
                            )
                    except Exception as e:
                        scheduled_coll.update_one(
                            {"_id": scheduled_id},
                            {"$set": {"status": "error", "enabled": False, "error": str(e), "updatedAt": datetime.now(timezone.utc)}},
                        )
        except Exception as e:
            log.error("Scheduler loop error: %s", e)

        await asyncio.sleep(settings.SCHEDULER_POLL_SECONDS)


async def main():
    load_dotenv()
    settings = load_settings()
    db = get_db(settings)
    chats_coll = db[settings.CHATS_COLLECTION]

    client = TelegramClient(StringSession(settings.STRING_SESSION), settings.API_ID, settings.API_HASH)
    await client.start()
    me = await client.get_me()
    log.info("Logged in as %s (%s)", me.username or me.first_name, me.id)

    # Background tasks
    asyncio.create_task(periodic_dialog_sync(client, chats_coll, settings.DIALOG_SYNC_EVERY_MINUTES))
    asyncio.create_task(scheduler_loop(client, db, settings))

    # Lightweight discovery on incoming group messages
    @client.on(events.NewMessage)
    async def on_any_group_message(event):
        if not (event.is_group or event.is_channel):
            return
        ent = await event.get_chat()
        chat_id = canonical_chat_id(ent)
        if chat_id is None or chat_id >= 0:
            return
        title = getattr(ent, "title", getattr(ent, "username", None))
        norm = normalize_title(title)
        now = datetime.now(timezone.utc)

        base = {
            "chatId": chat_id,
            "title": title,
            "normalizedTitle": norm,
            "type": getattr(ent, "megagroup", None) and "megagroup" or ent.__class__.__name__,
            "isActive": True,
            "firstSeenAt": now,
            "lastSeenAt": now,
        }

        if norm:
            existing = chats_coll.find_one({"normalizedTitle": norm})
            if existing:
                chats_coll.update_one({"_id": existing["_id"]}, {"$set": {"lastSeenAt": now, "isActive": True, "chatId": chat_id}})
                return

        chats_coll.update_one(
            {"chatId": chat_id},
            {"$setOnInsert": base, "$set": {"lastSeenAt": now, "isActive": True}},
            upsert=True,
        )

    log.info("Worker running.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
