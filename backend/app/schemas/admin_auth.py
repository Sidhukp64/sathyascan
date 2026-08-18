"""Admin auth schemas (Phase 9, roadmap §9.1)."""

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    email: str


class AdminLogoutResponse(BaseModel):
    message: str
