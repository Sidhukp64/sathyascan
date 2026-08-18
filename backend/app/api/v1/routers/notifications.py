"""
Phase 8 Notifications API (roadmap §8.8). Reads/updates only
`notifications` rows already written by app/agent/notifications.py — this
router never generates notification content itself, it only lists/marks-read
what the scheduled-check runner (or a future caller) already created.

Every query scoped by `Notification.user_id == current_user.id`, same
IDOR-safe pattern as every other Phase 6-8 router.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.deps import get_current_user, get_db_session
from app.db.base import utcnow
from app.models.notification import Notification
from app.models.user import User
from app.schemas.notification import NotificationListResponse, NotificationResponse

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])

_MAX_PAGE_SIZE = 100


def _to_response(notification: Notification) -> NotificationResponse:
    return NotificationResponse(
        id=notification.id,
        notification_type=notification.notification_type,
        channel=notification.channel,
        title=notification.title,
        body=notification.body,
        related_entity_type=notification.related_entity_type,
        related_entity_id=notification.related_entity_id,
        delivery_status=notification.delivery_status,
        read_at=notification.read_at,
        created_at=notification.created_at,
    )


@router.get("", response_model=NotificationListResponse)
async def list_notifications(
    unread_only: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=_MAX_PAGE_SIZE),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> NotificationListResponse:
    filters = [Notification.user_id == user.id]
    if unread_only:
        filters.append(Notification.read_at.is_(None))

    count_result = await session.execute(select(func.count()).select_from(Notification).where(*filters))
    total = count_result.scalar_one()

    unread_result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    unread_count = unread_result.scalar_one()

    rows_result = await session.execute(
        select(Notification)
        .where(*filters)
        .order_by(Notification.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = rows_result.scalars().all()
    total_pages = (total + page_size - 1) // page_size if total > 0 else 0

    return NotificationListResponse(
        items=[_to_response(row) for row in rows],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=total_pages,
        unread_count=unread_count,
    )


@router.put("/{notification_id}/read", response_model=NotificationResponse)
async def mark_notification_read(
    notification_id: UUID,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> NotificationResponse:
    result = await session.execute(
        select(Notification).where(Notification.id == notification_id, Notification.user_id == user.id)
    )
    notification = result.scalar_one_or_none()
    if notification is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Notification not found.")

    if notification.read_at is None:
        notification.read_at = utcnow()
        await session.commit()
    return _to_response(notification)


@router.put("/read-all", response_model=NotificationListResponse)
async def mark_all_notifications_read(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> NotificationListResponse:
    result = await session.execute(
        select(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
    )
    rows = result.scalars().all()
    now = utcnow()
    for row in rows:
        row.read_at = now
    await session.commit()

    return await list_notifications(unread_only=False, page=1, page_size=_MAX_PAGE_SIZE, user=user, session=session)
