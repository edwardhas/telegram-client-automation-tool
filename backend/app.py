from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List

from zoneinfo import ZoneInfo

from bson import ObjectId
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

from .db import ensure_indexes, get_db
from .models import (
    ChatOut,
    DeliveryOut,
    SavedCampaignCreate,
    SavedCampaignOut,
    ScheduledMessageCreate,
    ScheduledMessageOut,
    ScheduledMessageUpdate,
)
from .scheduling import compute_next_run_at
from .security import require_admin
from .settings import settings


def _local_input_to_utc(dt: datetime | None, tz_name: str) -> datetime | None:
    """Convert a datetime coming from the UI into UTC.

    The UI sends naive datetimes (no offset) interpreted in tz_name.
    We store all datetimes in MongoDB as UTC.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo(tz_name)).astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


app = FastAPI(title="Telegram Automation API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",") if o.strip()],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _id_str(doc: Dict[str, Any]) -> Dict[str, Any]:
    if doc is None:
        return doc
    d = dict(doc)
    if "_id" in d and isinstance(d["_id"], ObjectId):
        d["_id"] = str(d["_id"])
    if "scheduledId" in d and isinstance(d["scheduledId"], ObjectId):
        d["scheduledId"] = str(d["scheduledId"])
    return d


@app.on_event("startup")
async def _startup():
    await ensure_indexes()


# ----------------- Chats -----------------

@app.get("/api/chats", dependencies=[Depends(require_admin)], response_model=List[ChatOut])
async def list_chats(active: bool | None = Query(default=True)):
    db = get_db()
    q = {"chatId": {"$lt": 0}}
    if active is not None:
        q["isActive"] = active
    cur = db[settings.CHATS_COLLECTION].find(q).sort("title", 1)
    out = []
    async for doc in cur:
        out.append(_id_str(doc))
    return out


# ----------------- Scheduled Messages -----------------

@app.get(
    "/api/messages",
    dependencies=[Depends(require_admin)],
    response_model=List[ScheduledMessageOut],
)
async def list_messages(limit: int = 50, skip: int = 0):
    db = get_db()
    # Safety: ignore legacy docs that don't match our API schema (pre-refactor).
    q = {"title": {"$exists": True}}
    cur = (
        db[settings.SCHEDULED_MESSAGES_COLLECTION]
        .find(q)
        .sort("createdAt", -1)
        .skip(skip)
        .limit(limit)
    )
    out = []
    async for doc in cur:
        # Normalize nullable fields that may exist in older docs.
        if doc.get("targetChatIds") is None:
            doc["targetChatIds"] = []
        if doc.get("imageUrls") is None:
            doc["imageUrls"] = []
        out.append(_id_str(doc))
    return out


@app.post(
    "/api/messages",
    dependencies=[Depends(require_admin)],
    response_model=ScheduledMessageOut,
)
async def create_message(payload: ScheduledMessageCreate):
    db = get_db()

    now = datetime.now(timezone.utc)
    tz_name = payload.tz or settings.DEFAULT_TZ
    run_at_utc = _local_input_to_utc(payload.runAt, tz_name) if payload.scheduleType == "once" else None
    end_at_utc = _local_input_to_utc(payload.endAt, tz_name)

    next_run = compute_next_run_at(
        schedule_type=payload.scheduleType,
        run_at=run_at_utc,
        cron=payload.cron,
        end_at=end_at_utc,
        tz_name=tz_name,
    )

    doc = {
        "title": payload.title,
        "description": payload.description or "",
        "imageUrls": [str(u) for u in payload.imageUrls],
        "targetsMode": payload.targetsMode,
        "targetChatIds": [int(x) for x in payload.targetChatIds] if payload.targetsMode == "explicit" else [],
        "parseMode": payload.parseMode,
        "disablePreview": bool(payload.disablePreview),
        "scheduleType": payload.scheduleType,
        "runAt": run_at_utc,
        "cron": payload.cron,
        "endAt": end_at_utc,
        "tz": payload.tz,
        "enabled": bool(payload.enabled) and (next_run is not None),
        "status": "scheduled" if next_run is not None else "ended",
        "nextRunAt": next_run,
        "lastRunAt": None,
        "createdAt": now,
        "updatedAt": now,
    }
    res = await db[settings.SCHEDULED_MESSAGES_COLLECTION].insert_one(doc)
    saved = await db[settings.SCHEDULED_MESSAGES_COLLECTION].find_one({"_id": res.inserted_id})
    return _id_str(saved)


@app.patch(
    "/api/messages/{message_id}",
    dependencies=[Depends(require_admin)],
    response_model=ScheduledMessageOut,
)
async def update_message(message_id: str, payload: ScheduledMessageUpdate):
    db = get_db()
    try:
        oid = ObjectId(message_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid message id")

    existing = await db[settings.SCHEDULED_MESSAGES_COLLECTION].find_one({"_id": oid})
    if not existing:
        raise HTTPException(status_code=404, detail="Not found")

    update: Dict[str, Any] = {}
    for field, value in payload.model_dump(exclude_unset=True).items():
        if field == "imageUrls" and value is not None:
            update["imageUrls"] = [str(u) for u in value]
        else:
            update[field] = value

    merged = {**existing, **update}

    # Normalize UI-provided datetimes to UTC before computing/storing.
    tz_name = merged.get("tz", settings.TZ_NAME)
    if "runAt" in update and update["runAt"] is not None:
        update["runAt"] = _local_input_to_utc(update["runAt"], tz_name)
        merged["runAt"] = update["runAt"]
    if "endAt" in update and update["endAt"] is not None:
        update["endAt"] = _local_input_to_utc(update["endAt"], tz_name)
        merged["endAt"] = update["endAt"]
    # Recompute nextRunAt if schedule fields changed or enabled toggled on
    if any(k in update for k in ("scheduleType", "runAt", "cron", "endAt", "tz", "enabled")):
        next_run = compute_next_run_at(
            schedule_type=merged.get("scheduleType", "once"),
            run_at=merged.get("runAt"),
            cron=merged.get("cron"),
            end_at=merged.get("endAt"),
            tz_name=tz_name,
        )
        update["nextRunAt"] = next_run
        # If we have an endAt and there are no more runs, mark as ended.
        if next_run is None:
            update["enabled"] = False
            update["status"] = "ended"

    update["updatedAt"] = datetime.now(timezone.utc)

    await db[settings.SCHEDULED_MESSAGES_COLLECTION].update_one({"_id": oid}, {"$set": update})
    saved = await db[settings.SCHEDULED_MESSAGES_COLLECTION].find_one({"_id": oid})
    return _id_str(saved)


@app.post(
    "/api/messages/{message_id}/run",
    dependencies=[Depends(require_admin)],
    response_model=ScheduledMessageOut,
)
async def run_message_now(message_id: str):
    db = get_db()
    try:
        oid = ObjectId(message_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid message id")

    now = datetime.now(timezone.utc)
    await db[settings.SCHEDULED_MESSAGES_COLLECTION].update_one(
        {"_id": oid},
        {"$set": {"nextRunAt": now, "status": "scheduled", "enabled": True, "updatedAt": now}},
    )
    saved = await db[settings.SCHEDULED_MESSAGES_COLLECTION].find_one({"_id": oid})
    if not saved:
        raise HTTPException(status_code=404, detail="Not found")
    return _id_str(saved)


@app.delete(
    "/api/messages/{message_id}",
    dependencies=[Depends(require_admin)],
)
async def delete_message(message_id: str):
    db = get_db()
    try:
        oid = ObjectId(message_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid message id")
    await db[settings.SCHEDULED_MESSAGES_COLLECTION].delete_one({"_id": oid})
    return {"ok": True}


# ----------------- Deliveries -----------------

@app.get(
    "/api/messages/{message_id}/deliveries",
    dependencies=[Depends(require_admin)],
    response_model=List[DeliveryOut],
)
async def list_deliveries(message_id: str, limit: int = 100):
    db = get_db()
    try:
        oid = ObjectId(message_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid message id")
    cur = (
        db[settings.DELIVERIES_COLLECTION]
        .find({"scheduledId": oid})
        .sort("sentAt", -1)
        .limit(limit)
    )
    out = []
    async for doc in cur:
        out.append(_id_str(doc))
    return out


# ----------------- Saved Campaigns (Templates) -----------------

@app.get(
    "/api/campaigns",
    dependencies=[Depends(require_admin)],
    response_model=List[SavedCampaignOut],
)
async def list_campaigns(limit: int = 100):
    db = get_db()
    cur = db[settings.SAVED_CAMPAIGNS_COLLECTION].find({}).sort("updatedAt", -1).limit(limit)
    out = []
    async for doc in cur:
        out.append(_id_str(doc))
    return out


@app.post(
    "/api/campaigns",
    dependencies=[Depends(require_admin)],
    response_model=SavedCampaignOut,
)
async def create_campaign(payload: SavedCampaignCreate):
    db = get_db()
    now = datetime.now(timezone.utc)
    doc = {
        "code": payload.code,
        "title": payload.title,
        "description": payload.description or "",
        "imageUrls": [str(u) for u in payload.imageUrls],
        "targetsMode": payload.targetsMode,
        "targetChatIds": [int(x) for x in payload.targetChatIds] if payload.targetsMode == "explicit" else [],
        "parseMode": payload.parseMode,
        "disablePreview": bool(payload.disablePreview),
        "createdAt": now,
        "updatedAt": now,
    }
    await db[settings.SAVED_CAMPAIGNS_COLLECTION].insert_one(doc)
    saved = await db[settings.SAVED_CAMPAIGNS_COLLECTION].find_one({"code": payload.code})
    return _id_str(saved)
