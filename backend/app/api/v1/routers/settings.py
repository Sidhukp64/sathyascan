"""
Phase 6 Settings API (docs/api-design.md's `GET/PUT /settings/language`,
`GET/PUT /settings/privacy`).

**Privacy Mode toggle — side-effect scope, disclosed design decision**:
`PUT /settings/privacy` updates `users.privacy_mode` for real (it is read by
every future analysis's `privacy_mode_snapshot` — decisions.md §8) and the
change takes effect immediately for anything created from that point on.
It does NOT retroactively touch analyses that already exist: each
`analyses.privacy_mode_snapshot` is documented as "captured at analysis
time, immutable even if the user later flips the setting"
(app/models/analysis.py) — retroactively re-purging old data on a toggle
would violate that already-locked invariant. The periodic purge task
(app/agent/retention.py, wired in main.py's lifespan) runs continuously
regardless of when a setting last changed, so anything that becomes overdue
under whichever policy applied AT THE TIME it was created is swept on its
own schedule — this endpoint's job is to change the setting honestly, not to
force an out-of-band purge.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.audit import write_audit_log
from app.api.v1.deps import get_current_user, get_db_session
from app.models.user import User
from app.schemas.settings import (
    DOCUMENTED_LANGUAGE_CODES,
    LanguageSettingsResponse,
    LanguageSettingsUpdateRequest,
    PrivacySettingsResponse,
    PrivacySettingsUpdateRequest,
    translated_replies_available,
)

router = APIRouter(prefix="/api/v1/settings", tags=["dashboard-settings"])


@router.get("/language", response_model=LanguageSettingsResponse)
async def get_language_settings(user: User = Depends(get_current_user)) -> LanguageSettingsResponse:
    return LanguageSettingsResponse(
        preferred_language=user.preferred_language,
        auto_detect_language=user.auto_detect_language,
        translated_replies_available=translated_replies_available(user.preferred_language),
    )


@router.put("/language", response_model=LanguageSettingsResponse)
async def update_language_settings(
    payload: LanguageSettingsUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> LanguageSettingsResponse:
    if payload.preferred_language not in DOCUMENTED_LANGUAGE_CODES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported language code. Must be one of: {sorted(DOCUMENTED_LANGUAGE_CODES)}.",
        )

    previous_language = user.preferred_language
    user.preferred_language = payload.preferred_language
    if payload.auto_detect_language is not None:
        user.auto_detect_language = payload.auto_detect_language

    await write_audit_log(
        session,
        actor_type="user",
        action="language_changed",
        entity_type="user",
        entity_id=user.id,
        metadata={"from": previous_language, "to": payload.preferred_language},
    )
    await session.commit()

    return LanguageSettingsResponse(
        preferred_language=user.preferred_language,
        auto_detect_language=user.auto_detect_language,
        translated_replies_available=translated_replies_available(user.preferred_language),
    )


@router.get("/privacy", response_model=PrivacySettingsResponse)
async def get_privacy_settings(user: User = Depends(get_current_user)) -> PrivacySettingsResponse:
    return PrivacySettingsResponse(privacy_mode=user.privacy_mode)


@router.put("/privacy", response_model=PrivacySettingsResponse)
async def update_privacy_settings(
    payload: PrivacySettingsUpdateRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_db_session),
) -> PrivacySettingsResponse:
    previous_value = user.privacy_mode
    user.privacy_mode = payload.privacy_mode

    await write_audit_log(
        session,
        actor_type="user",
        action="privacy_mode_changed",
        entity_type="user",
        entity_id=user.id,
        metadata={"from": previous_value, "to": payload.privacy_mode},
    )
    await session.commit()

    return PrivacySettingsResponse(privacy_mode=user.privacy_mode)
