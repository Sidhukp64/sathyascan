"""
Get-or-create user identity (decisions.md §7). First time a phone number is
seen, a `users` row is created holding the encrypted number + its lookup
hash; every subsequent message from the same number resolves to the same row.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import encrypt_phone_number
from app.core.security import hash_phone_number
from app.models.user import User


async def get_or_create_user(
    session: AsyncSession,
    phone_number: str,
    phone_hash_pepper: str,
    phone_encryption_key: str,
    default_language: str = "en",
) -> User:
    phone_hash = hash_phone_number(phone_number, phone_hash_pepper)

    result = await session.execute(select(User).where(User.phone_number_hash == phone_hash))
    user = result.scalar_one_or_none()
    if user is not None:
        user.last_active_at = datetime.now(timezone.utc)
        return user

    user = User(
        phone_number_encrypted=encrypt_phone_number(phone_number, phone_encryption_key),
        phone_number_hash=phone_hash,
        preferred_language=default_language,
    )
    session.add(user)
    await session.flush()  # assigns user.id without ending the transaction
    return user
