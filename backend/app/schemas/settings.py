"""
Settings API schemas (Phase 6, api-design.md's `GET/PUT /settings/language`,
`GET/PUT /settings/privacy`).

`DOCUMENTED_LANGUAGE_CODES` is database-schema.md's exact set for
`users.preferred_language` ("ml | en | hi | ta"). `translated_replies_available`
is the honest signal for whether a user's chosen language actually gets
translated WhatsApp replies — computed generically from
`app.i18n.templates.SUPPORTED_LANGUAGES` (all 4 documented codes, since the
Hindi/Tamil localization fix), never hard-coded per language here, so this
stays correct automatically if a future language is documented but not yet
translated. Never silently claim full support that doesn't exist to make an
endpoint look complete.
"""

from pydantic import BaseModel, Field

from app.i18n.templates import SUPPORTED_LANGUAGES

DOCUMENTED_LANGUAGE_CODES = {"ml", "en", "hi", "ta"}


class LanguageSettingsResponse(BaseModel):
    preferred_language: str
    auto_detect_language: bool
    translated_replies_available: bool


class LanguageSettingsUpdateRequest(BaseModel):
    preferred_language: str = Field(min_length=2, max_length=5)
    auto_detect_language: bool | None = None


class PrivacySettingsResponse(BaseModel):
    privacy_mode: bool


class PrivacySettingsUpdateRequest(BaseModel):
    privacy_mode: bool


def translated_replies_available(language_code: str) -> bool:
    return language_code in SUPPORTED_LANGUAGES
