"""Phase 8 Notifications API schemas (app/models/notification.py)."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class NotificationResponse(BaseModel):
    id: UUID
    notification_type: str
    channel: str
    title: str
    body: str
    related_entity_type: str | None
    related_entity_id: UUID | None
    delivery_status: str
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    items: list[NotificationResponse]
    page: int
    page_size: int
    total: int
    total_pages: int
    unread_count: int
