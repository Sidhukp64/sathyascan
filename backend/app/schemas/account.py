"""Phase 8 account deletion API schema (roadmap §8.4)."""

from pydantic import BaseModel


class AccountDeletionRequest(BaseModel):
    """`confirmation_phrase` must be the literal string "DELETE" — a
    real, cheap confirmation gate against an accidental/stray click on an
    irreversible action, without a second OTP round trip (a bigger scope
    addition than this phase's "smallest correct solution" instruction
    calls for)."""

    confirmation_phrase: str


class AccountDeletionResponse(BaseModel):
    message: str
    analyses_deleted: int
    scheduled_checks_deleted: int
    sessions_deleted: int
    notifications_deleted: int
    moderation_reports_deleted: int
