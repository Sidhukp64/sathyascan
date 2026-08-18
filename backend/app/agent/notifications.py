"""
Phase 8 notification infrastructure (app/models/notification.py).

Channel-abstract by design: `channel='in_app'` rows are always fully
"delivered" the instant they're created — there's no separate delivery step
for an API-only, no-frontend backend; a caller just lists them via
`GET /api/v1/notifications`. `channel='whatsapp'` rows are ALWAYS created
with `delivery_status='blocked_by_policy'`, and the real `WhatsAppSender` is
NEVER invoked for them anywhere in this module. decisions.md §4 remains
locked: scheduled/proactive WhatsApp messaging requires (a) confirmed Meta
requirements, (b) an approved message template, (c) budgeted costs — none
of which exist in this environment. This is an explicit, confirmed user
instruction for Phase 8 (not an engineering judgment call): "Do NOT send
proactive WhatsApp messages until the three locked prerequisites are
satisfied."

**The abstraction is real, not a stub needing a future redesign.**
title/body are correctly rendered in the recipient's language
(app/i18n/templates.py's NOTIFICATION_STRINGS — the same deterministic-
template precedent every other user-facing string in this codebase already
follows, never an LLM call), the correct recipient/channel is resolved and
recorded, and the row is persisted with its full real content. Enabling
real WhatsApp delivery later is a matter of adding one
`WhatsAppSender.send_text_message(...)` call inside this module once
decisions.md §4's preconditions are met — nothing upstream (scheduled-check
execution, the notification content, the API surface) needs to change.
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import log_event
from app.i18n.templates import NOTIFICATION_STRINGS
from app.models.notification import Notification

logger = logging.getLogger(__name__)


async def create_notification(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    notification_type: str,
    title: str,
    body: str,
    channel: str = "in_app",
    related_entity_type: str | None = None,
    related_entity_id: uuid.UUID | None = None,
) -> Notification:
    delivery_status = "delivered" if channel == "in_app" else "blocked_by_policy"
    notification = Notification(
        user_id=user_id,
        notification_type=notification_type,
        channel=channel,
        title=title,
        body=body,
        related_entity_type=related_entity_type,
        related_entity_id=related_entity_id,
        delivery_status=delivery_status,
    )
    session.add(notification)
    await session.flush()

    if channel == "whatsapp":
        # See module docstring — decisions.md §4 is still locked. NOT a
        # network call, NOT a WhatsAppSender invocation of any kind — only
        # a log line recording that a real send was deliberately skipped,
        # for operational visibility.
        log_event(
            logger,
            logging.INFO,
            "whatsapp notification blocked by policy (decisions.md §4 preconditions not met)",
            user_id=str(user_id),
            notification_type=notification_type,
        )

    return notification


def _strings(language: str) -> dict[str, str]:
    return NOTIFICATION_STRINGS.get(language, NOTIFICATION_STRINGS["en"])


def render_scheduled_check_completed(claim_text: str, new_result: str, language: str) -> tuple[str, str]:
    strings = _strings(language)
    title = strings["scheduled_check_completed_title"]
    body = strings["scheduled_check_completed_body_unchanged"].format(claim=claim_text, new_result=new_result)
    return title, body


def render_credibility_changed(claim_text: str, old_result: str, new_result: str, language: str) -> tuple[str, str]:
    strings = _strings(language)
    title = strings["credibility_changed_title"]
    body = strings["credibility_changed_body"].format(claim=claim_text, old_result=old_result, new_result=new_result)
    return title, body


def render_scheduled_check_failed(claim_text: str, language: str) -> tuple[str, str]:
    strings = _strings(language)
    title = strings["scheduled_check_failed_title"]
    body = strings["scheduled_check_failed_body"].format(claim=claim_text)
    return title, body


def render_account_deleted(language: str) -> tuple[str, str]:
    strings = _strings(language)
    return strings["account_deleted_title"], strings["account_deleted_body"]


async def notify_scheduled_check_result(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scheduled_check_id: uuid.UUID,
    claim_text: str,
    language: str,
    credibility_changed: bool,
    old_result: str | None,
    new_result: str,
) -> list[Notification]:
    """Creates BOTH channel rows for one scheduled-check completion — an
    always-delivered `in_app` row (the only channel that actually functions
    in this build) and an always-`blocked_by_policy` `whatsapp` row (proves
    the abstraction is real and exercised, per the user's explicit test
    requirement, without ever sending anything)."""
    if credibility_changed and old_result is not None:
        title, body = render_credibility_changed(claim_text, old_result, new_result, language)
        notification_type = "credibility_changed"
    else:
        title, body = render_scheduled_check_completed(claim_text, new_result, language)
        notification_type = "scheduled_check_completed"

    notifications = []
    for channel in ("in_app", "whatsapp"):
        notifications.append(
            await create_notification(
                session,
                user_id=user_id,
                notification_type=notification_type,
                title=title,
                body=body,
                channel=channel,
                related_entity_type="scheduled_check",
                related_entity_id=scheduled_check_id,
            )
        )
    return notifications


async def notify_scheduled_check_failed(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    scheduled_check_id: uuid.UUID,
    claim_text: str,
    language: str,
) -> Notification:
    title, body = render_scheduled_check_failed(claim_text, language)
    return await create_notification(
        session,
        user_id=user_id,
        notification_type="scheduled_check_failed",
        title=title,
        body=body,
        channel="in_app",
        related_entity_type="scheduled_check",
        related_entity_id=scheduled_check_id,
    )


async def notify_account_deleted(session: AsyncSession, *, user_id: uuid.UUID, language: str) -> Notification:
    """Called from app/agent/account_deletion.py BEFORE the user row is
    anonymized — the notification is written to the same DB the deleted
    user's other rows are being purged from, so a client polling
    notifications mid-deletion sees a final, honest record. Not delivered
    via WhatsApp (the account, and its phone number, are being erased in
    the same operation) — in_app only."""
    title, body = render_account_deleted(language)
    return await create_notification(
        session,
        user_id=user_id,
        notification_type="security_event",
        title=title,
        body=body,
        channel="in_app",
    )
