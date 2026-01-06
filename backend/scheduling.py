from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from croniter import croniter


def compute_next_run_at(
    *,
    schedule_type: str,
    run_at: datetime | None,
    cron: str | None,
    end_at: Optional[datetime],
    tz_name: str,
) -> Optional[datetime]:
    """Return the next execution time in UTC.

    Convention:
    - Datetimes stored in MongoDB are UTC (naive or aware). We treat *naive* datetimes as UTC.
    - The only time we interpret a value in tz_name is when we compute the *next* cron occurrence.
    """
    tz = ZoneInfo(tz_name)

    def to_utc(dt: datetime) -> datetime:
        """Treat naive datetimes as UTC. Return aware UTC."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    end_utc: Optional[datetime] = to_utc(end_at) if end_at is not None else None

    if schedule_type == "once":
        if run_at is None:
            raise ValueError("runAt is required for scheduleType='once'")
        return to_utc(run_at)

    if not cron:
        raise ValueError("cron is required for scheduleType='cron'")

    now_local = datetime.now(tz)
    it = croniter(cron, now_local)
    next_local = it.get_next(datetime)
    if next_local.tzinfo is None:
        next_local = next_local.replace(tzinfo=tz)
    next_utc = to_utc(next_local)
    if end_utc is not None and next_utc > end_utc:
        return None
    return next_utc
