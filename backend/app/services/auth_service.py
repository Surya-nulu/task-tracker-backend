import asyncio

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, UnauthorizedError
from app.core.security import hash_password, verify_password
from app.models import User

# Checked against when the email is unknown, so "no such user" and "wrong
# password" both cost exactly one bcrypt verification.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def register(db: AsyncSession, email: str, password: str) -> User:
    email = normalise_email(email)

    if await db.scalar(select(User.id).where(User.email == email)) is not None:
        raise ConflictError("Email already registered")

    # bcrypt is CPU-bound (~250 ms); keep it off the event loop.
    hashed = await asyncio.to_thread(hash_password, password)
    user = User(email=email, hashed_password=hashed)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent registration of the same email.
        await db.rollback()
        raise ConflictError("Email already registered") from None
    return user


async def authenticate(db: AsyncSession, email: str, password: str) -> User:
    user = await db.scalar(select(User).where(User.email == normalise_email(email)))

    hashed = user.hashed_password if user is not None else _DUMMY_HASH
    password_ok = await asyncio.to_thread(verify_password, password, hashed)

    if user is None or not password_ok:
        raise UnauthorizedError("Invalid email or password")
    return user