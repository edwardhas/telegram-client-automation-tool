from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Literal

from pydantic import BaseModel, Field, HttpUrl, model_validator


ScheduleType = Literal["once", "cron"]
TargetsMode = Literal["all", "explicit"]


class ChatOut(BaseModel):
    id: str = Field(alias="_id")
    chatId: int
    title: Optional[str] = None
    normalizedTitle: Optional[str] = None
    type: Optional[str] = None
    isActive: bool = True
    firstSeenAt: Optional[datetime] = None
    lastSeenAt: Optional[datetime] = None


class ScheduledMessageBase(BaseModel):
    title: str
    description: Optional[str] = ""
    imageUrls: List[HttpUrl] = Field(default_factory=list)

    targetsMode: TargetsMode = "all"
    targetChatIds: List[int] = Field(default_factory=list)

    parseMode: Literal["HTML", "Markdown", "None"] = "HTML"
    disablePreview: bool = True

    scheduleType: ScheduleType = "once"
    runAt: Optional[datetime] = None  # required if scheduleType == "once"
    cron: Optional[str] = None        # required if scheduleType == "cron"
    endAt: Optional[datetime] = None  # optional end time for cron schedules (interpreted in tz)
    tz: str = "America/Los_Angeles"

    enabled: bool = True

    @model_validator(mode="after")
    def _validate_schedule(self):
        if self.scheduleType == "once":
            if self.runAt is None:
                raise ValueError("runAt is required when scheduleType='once'")
        else:
            if not self.cron:
                raise ValueError("cron is required when scheduleType='cron'")
        if self.targetsMode == "explicit":
            if not self.targetChatIds:
                raise ValueError("targetChatIds is required when targetsMode='explicit'")
        return self


class ScheduledMessageCreate(ScheduledMessageBase):
    pass


class ScheduledMessageUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    imageUrls: Optional[List[HttpUrl]] = None

    targetsMode: Optional[TargetsMode] = None
    targetChatIds: Optional[List[int]] = None

    parseMode: Optional[Literal["HTML", "Markdown", "None"]] = None
    disablePreview: Optional[bool] = None

    scheduleType: Optional[ScheduleType] = None
    runAt: Optional[datetime] = None
    cron: Optional[str] = None
    endAt: Optional[datetime] = None
    tz: Optional[str] = None

    enabled: Optional[bool] = None


class ScheduledMessageOut(BaseModel):
    id: str = Field(alias="_id")
    title: str
    description: Optional[str] = ""
    imageUrls: List[str] = Field(default_factory=list)

    targetsMode: TargetsMode = "all"
    targetChatIds: List[int] = Field(default_factory=list)

    parseMode: str = "HTML"
    disablePreview: bool = True

    scheduleType: ScheduleType = "once"
    runAt: Optional[datetime] = None
    cron: Optional[str] = None
    endAt: Optional[datetime] = None
    tz: str = "America/Los_Angeles"

    enabled: bool = True
    status: str = "scheduled"
    nextRunAt: Optional[datetime] = None
    lastRunAt: Optional[datetime] = None
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None


class DeliveryOut(BaseModel):
    id: str = Field(alias="_id")
    scheduledId: str
    chatId: int
    runAt: Optional[datetime] = None
    status: str
    sentAt: Optional[datetime] = None
    error: Optional[str] = None
    messageIds: List[int] = Field(default_factory=list)


class SavedCampaignCreate(BaseModel):
    code: str
    title: str
    description: Optional[str] = ""
    imageUrls: List[HttpUrl] = Field(default_factory=list)
    targetsMode: TargetsMode = "all"
    targetChatIds: List[int] = Field(default_factory=list)
    parseMode: Literal["HTML", "Markdown", "None"] = "HTML"
    disablePreview: bool = True


class SavedCampaignOut(BaseModel):
    id: str = Field(alias="_id")
    code: str
    title: str
    description: Optional[str] = ""
    imageUrls: List[str] = Field(default_factory=list)
    targetsMode: TargetsMode = "all"
    targetChatIds: List[int] = Field(default_factory=list)
    parseMode: str = "HTML"
    disablePreview: bool = True
    createdAt: Optional[datetime] = None
    updatedAt: Optional[datetime] = None
