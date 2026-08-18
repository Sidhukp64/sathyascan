"""
Phase 8 Conversation/Analysis Session API schemas
(app/models/analysis_session.py). Named `AnalysisSession*` throughout,
mirroring the model's deliberate naming choice — see that model's docstring
for why this is NOT the same thing as the already-documented, still-
unmigrated `conversation_sessions` table (WhatsApp flow-state mirror).
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class AnalysisSessionCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AnalysisSessionRenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class AnalysisSessionMemberSummary(BaseModel):
    analysis_id: UUID
    input_type: str
    overall_result: str | None
    created_at: datetime


class AnalysisSessionResponse(BaseModel):
    id: UUID
    name: str
    analysis_count: int
    created_at: datetime
    updated_at: datetime


class AnalysisSessionDetailResponse(AnalysisSessionResponse):
    analyses: list[AnalysisSessionMemberSummary] = Field(default_factory=list)


class AnalysisSessionListResponse(BaseModel):
    items: list[AnalysisSessionResponse]
    page: int
    page_size: int
    total: int
    total_pages: int


class AddAnalysisToSessionRequest(BaseModel):
    analysis_id: UUID
