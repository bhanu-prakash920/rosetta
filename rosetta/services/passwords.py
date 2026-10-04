"""Password hashing with Argon2id (memory-hard, the OWASP first choice)."""
from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

_ph = PasswordHasher(time_cost=2, memory_cost=19_456, parallelism=1)
# Verified against when the account does not exist, so a missing user costs the
# same time as a wrong password and the login endpoint does not leak which it was.
_DUMMY = _ph.hash("rosetta-dummy-password")


def hash_password(password: str) -> str:
    return _ph.hash(password)


def verify_password(hashed: str | None, password: str) -> bool:
    try:
        return _ph.verify(hashed or _DUMMY, password) and hashed is not None
    except (VerificationError, InvalidHashError):
        return False
