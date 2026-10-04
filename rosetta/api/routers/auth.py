from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select

from ...db.models import AppUser
from ...services import audit
from ...services.passwords import verify_password
from ..security import Principal, current_user, issue_token
from ..state import write_db
from ..support import TokenBucket

router = APIRouter(prefix="/auth", tags=["auth"])
# Password guessing is slowed per account and address: five tries, then one every six seconds.
# Keyed on both, so an attacker cannot lock a user out from another address.
attempts = TokenBucket(per_minute=10, burst=5)


@router.post("/token", summary="OAuth2 password grant: exchange credentials for a bearer token")
def token(request: Request, form: OAuth2PasswordRequestForm = Depends()):
    email = form.username.strip().lower()[:200]
    ip = request.client.host if request.client else ""
    ok_to_try, wait = attempts.take(f"{ip}|{email}")
    if not ok_to_try:
        raise HTTPException(429, "too many sign-in attempts", {"Retry-After": str(max(1, int(wait + 0.999)))})
    with write_db() as s:
        user = s.execute(select(AppUser).where(AppUser.email == email)).scalar_one_or_none()
        ok = verify_password(user.password_hash if user else None, form.password) and bool(user and user.is_active)
        audit.record(s, actor_kind="user", actor=email, action="auth.login", resource="auth/token",
                     outcome="ok" if ok else "denied", ip=ip,
                     tenant_id=user.tenant_id if (ok and user) else None)
        if not ok or user is None:
            s.commit()
            # One message for every failure: no hint about which part was wrong.
            raise HTTPException(401, "invalid credentials", {"WWW-Authenticate": "Bearer"})
        roles = [r.name for r in user.roles]
        tok, ttl = issue_token(user.id, user.email, roles, user.tenant_id, user.display_name)
    return {"access_token": tok, "token_type": "bearer", "expires_in": ttl,  # nosec B105 - OAuth2 token type
            "user": {"email": email, "name": user.display_name, "roles": roles, "tenant_id": user.tenant_id}}


@router.get("/me", summary="Who the token belongs to")
def me(p: Principal = Depends(current_user)):
    return {"email": p.email, "name": p.name, "roles": list(p.roles), "tenant_id": p.tenant_id,
            "sees_precise_location": p.sees_precise_location, "sees_personal_data": p.sees_personal_data}
