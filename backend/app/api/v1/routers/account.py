"""
Phase 8 full account deletion (roadmap §8.4, api-design.md's already-
designed `DELETE /account`). See app/agent/account_deletion.py for the
actual deletion logic — this router is a thin auth/confirmation/response
wrapper around it.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.account_deletion import delete_account
from app.api.v1.deps import get_current_user, get_db_session
from app.core.config import Settings, get_settings
from app.models.user import User
from app.schemas.account import AccountDeletionRequest, AccountDeletionResponse

router = APIRouter(prefix="/api/v1/account", tags=["account"])

_REQUIRED_CONFIRMATION_PHRASE = "DELETE"


@router.delete("", response_model=AccountDeletionResponse)
async def delete_my_account(
    payload: AccountDeletionRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
    settings: Settings = Depends(get_settings),
) -> AccountDeletionResponse:
    if payload.confirmation_phrase != _REQUIRED_CONFIRMATION_PHRASE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f'Confirmation required — send confirmation_phrase: "{_REQUIRED_CONFIRMATION_PHRASE}".',
        )

    summary = await delete_account(session, settings, user)

    return AccountDeletionResponse(
        message="Your account and all associated data have been deleted.",
        analyses_deleted=summary.analyses_deleted,
        scheduled_checks_deleted=summary.scheduled_checks_deleted,
        sessions_deleted=summary.sessions_deleted,
        notifications_deleted=summary.notifications_deleted,
        moderation_reports_deleted=summary.moderation_reports_deleted,
    )
