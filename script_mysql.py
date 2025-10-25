import asyncio
import configparser
import html
import logging
import shlex
import sqlite3
import shutil
from datetime import datetime
from pathlib import Path
from typing import Tuple, Optional

from telethon import TelegramClient, events
from telethon.errors import FloodWaitError
from pymongo import MongoClient
from bson import ObjectId

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
MONGO_COLL = cfg('default', 'collection_name')  # products collection

# During setup you can leave None so everyone can use commands. Then set your ID.
AUTHORIZED_USER_ID = None  # e.g., 123456789

# ───────────────────────────────────────────────────────────────────────────────
# Helpers
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
        import sqlite3 as _sqlite3
        conn = _sqlite3.connect(str(db), timeout=max_wait_s)
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
        # clean WAL/SHM
        for f in (wal, shm):
            try:
                if f.exists(): f.unlink()
            except Exception as rm_err:
                log.warning("Could not remove %s: %s", f, rm_err)

def caption_for(name: str, description: str) -> str:
    """
    Telegram photo caption limit is 1024 chars.
    We bold the name and add description on next line; truncate if needed.
    """
    base = f"<b>{esc(name)}</b>"
    if description:
        desc = esc(description)
        cap = f"{base}\n{desc}"
    else:
        cap = base
    if len(cap) > 1024:
        # keep name, truncate desc gracefully
        room = 1024 - len(base) - 1  # one newline
        truncated = (desc[:max(0, room - 1)] + "…") if room > 0 else ""
        cap = f"{base}\n{truncated}" if truncated else base
    return cap

def parse_insert_args(text: str) -> Optional[Tuple[str, str, str]]:
    """
    /insert <name> <description> <url>
    Supports quotes for name/description with spaces:
      /insert "Cool Name" "Long description here" https://...
    Fallback: treat last token as URL, first as name, middle as description.
    """
    parts = shlex.split(text)
    # parts[0] is '/insert'
    if len(parts) >= 4:
        # happy path with quotes
        return parts[1], " ".join(parts[2:-1]), parts[-1]
    # fallback simple split
    raw = text.strip().split(maxsplit=1)
    if len(raw) < 2:
        return None
    tail = raw[1].rsplit(maxsplit=1)
    if len(tail) < 2:
        return None
    middle, url = tail
    # now split middle into name and desc (first word becomes name)
    mid_parts = middle.split(maxsplit=1)
    if len(mid_parts) == 1:
        name, desc = mid_parts[0], ""
    else:
        name, desc = mid_parts[0], mid_parts[1]
    return name, desc, url

def parse_update_args(text: str) -> Optional[Tuple[str, str, str, str]]:
    """
    /update <object_id> <name> <description> <url>
    Quotes supported for name/description.
    """
    parts = shlex.split(text)
    if len(parts) >= 5:
        oid = parts[1]
        name = parts[2]
        desc = " ".join(parts[3:-1])
        url = parts[-1]
        return oid, name, desc, url
    # fallback
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

async def send_product_photo(client: TelegramClient, chat_id: int, name: str, description: str, url: str):
    """
    Try to send as a photo with caption; on failure (bad URL, etc.), fall back to a text message with the link.
    """
    try:
        cap = caption_for(name, description)
        await client.send_file(chat_id, url, caption=cap, parse_mode='html')
    except FloodWaitError as e:
        await asyncio.sleep(e.seconds + 1)
        cap = caption_for(name, description)
        await client.send_file(chat_id, url, caption=cap, parse_mode='html')
    except Exception as e:
        log.warning("send_file failed, falling back to text: %s", e)
        msg = f"<b>{esc(name)}</b>\n{esc(description)}\n{esc(url)}"
        await client.send_message(chat_id, msg, parse_mode='html', link_preview=True)

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
    log.info("Connected as @%s (id=%s)", getattr(me, "username", None), me.id)

    # Mongo
    try:
        mongo = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        mongo.admin.command("ping")
        log.info("MongoDB connection OK")
    except Exception as e:
        log.error("Mongo connection failed: %s", e)
        raise

    db = mongo[MONGO_DB]
    products = db[MONGO_COLL]  # expected fields: name, description, url, last_edit

    # Catch-all logger (helps debug)
    @client.on(events.NewMessage)
    async def _log_all(event):
        try:
            sender = await event.get_sender()
            log.info("Update: chat_id=%s sender_id=%s text=%r",
                     getattr(event.chat, "id", None), sender.id, event.raw_text)
        except Exception as e:
            log.warning("Failed to log update: %s", e)

    # /id (handy to capture your numeric ID)
    @client.on(events.NewMessage(pattern=r"(?i)^/id(?:\s|$)"))
    async def on_id(event):
        sender = await event.get_sender()
        await event.reply(f"Your Telegram numeric ID:\n<code>{sender.id}</code>", parse_mode='html')

    # /start
    @client.on(events.NewMessage(pattern=r"(?i)^/start(?:\s|$)"))
    async def on_start(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            log.info("Unauthorized /start from %s", sender.id)
            return
        await event.reply(
            "👋 Bot is alive.\n"
            "Commands:\n"
            " - /select\n"
            " - /insert <name> <description> <url>\n"
            " - /update <object_id> <name> <description> <url>\n"
            " - /delete <object_id>\n\n",
            parse_mode='html'
        )

    # /select — list all products as image cards (photo + caption)
    @client.on(events.NewMessage(pattern=r"(?i)^/select(?:\s|$)"))
    async def on_select(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return

        # Fetch and stream out products (limit to avoid spam)
        cursor = products.find({}, projection={"name": 1, "description": 1, "url": 1}).limit(50)
        found = False
        async def _send(doc):
            await send_product_photo(
                client,
                event.chat_id,
                doc.get("name", ""),
                doc.get("description", ""),
                doc.get("url", ""),
            )

        try:
            for doc in cursor:
                found = True
                await _send(doc)
            if not found:
                await event.reply("No products found.")
        except Exception as e:
            await event.reply(f"❌ Error listing products: <code>{esc(e)}</code>", parse_mode='html')

    # /insert <name> <description> <url>
    @client.on(events.NewMessage(pattern=r"(?i)^/insert(?:\s|$)"))
    async def on_insert(event):
        sender = await event.get_sender()
        if not is_authorized(sender.id):
            return

        args = parse_insert_args(event.raw_text)
        if not args:
            return await event.reply(
                "Usage:\n<code>/insert \"Name with spaces\" \"Description with spaces\" https://example.com/image.jpg</code>",
                parse_mode='html'
            )
        name, description, url = args
        doc = {
            "name": name,
            "description": description,
            "url": url,
            "last_edit": today_str()
        }
        try:
            res = products.insert_one(doc)
            # load it back (has _id)
            doc["_id"] = res.inserted_id
            await send_product_photo(client, event.chat_id, name, description, url)
        except Exception as e:
            await event.reply(f"❌ Insert error: <code>{esc(e)}</code>", parse_mode='html')

    # /update <object_id> <name> <description> <url>
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
                {"$set": {
                    "name": name,
                    "description": description,
                    "url": url,
                    "last_edit": today_str()
                }}
            )
            if not res.matched_count:
                return await event.reply(f"Not found: <code>{esc(oid_str)}</code>", parse_mode='html')

            # Return the updated product as an image card
            await send_product_photo(client, event.chat_id, name, description, url)
        except Exception as e:
            await event.reply(f"❌ Update error: <code>{esc(e)}</code>", parse_mode='html')

    # /delete <object_id>
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

    log.info("Bot Started…")
    await client.run_until_disconnected()

# ───────────────────────────────────────────────────────────────────────────────
# Entrypoint
# ───────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    asyncio.run(main())
