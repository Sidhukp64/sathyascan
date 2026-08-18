"""
Local CLI to create/update an admin_users row (Phase 9, roadmap §9.1).

Deliberately NOT an HTTP endpoint — run this manually by someone with
direct database access (`DATABASE_URL` from the environment/.env, same as
every other management operation in this codebase). A public "create admin"
API would itself be the single largest privilege-escalation attack surface
Phase 9 could introduce.

Usage:
    python -m scripts.create_admin --email ops@example.com --role admin
    (prompts for a password interactively, never accepted as a CLI arg —
    shell history / process-list visibility would leak it)

If the email already exists, updates its password/role/is_active instead of
failing — makes this script idempotent for "reset an admin's password"
too, without a second script.
"""

import argparse
import asyncio
import getpass
import sys

from sqlalchemy import select

from app.core.admin_security import hash_password, normalize_admin_email
from app.core.config import get_settings
from app.db.session import build_engine, build_sessionmaker
from app.models.admin_user import AdminUser


async def _create_or_update_admin(email: str, password: str, role: str) -> None:
    settings = get_settings()
    engine = build_engine(settings.database_url)
    sessionmaker = build_sessionmaker(engine)

    normalized_email = normalize_admin_email(email)
    password_hash = hash_password(password)

    async with sessionmaker() as session:
        result = await session.execute(select(AdminUser).where(AdminUser.email == normalized_email))
        admin = result.scalar_one_or_none()

        if admin is None:
            admin = AdminUser(email=normalized_email, password_hash=password_hash, role=role, is_active=True)
            session.add(admin)
            action = "created"
        else:
            admin.password_hash = password_hash
            admin.role = role
            admin.is_active = True
            action = "updated"

        await session.commit()
        print(f"Admin user {action}: {normalized_email} (role={role})")

    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or update a SathyaScan admin user.")
    parser.add_argument("--email", required=True, help="Admin login email.")
    parser.add_argument(
        "--role", default="admin", choices=["admin", "moderator"], help="Admin role (default: admin)."
    )
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    password_confirm = getpass.getpass("Confirm password: ")
    if password != password_confirm:
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(1)
    if len(password) < 8:
        print("Password must be at least 8 characters.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_create_or_update_admin(args.email, password, args.role))


if __name__ == "__main__":
    main()
