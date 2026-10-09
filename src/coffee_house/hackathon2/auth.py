"""WhatsApp staff authentication and server-owned authorization policy."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from .storage import H2StorageError, H2Store

_PHONE = re.compile(r"^\+[1-9][0-9]{7,14}$")
_CODE = re.compile(r"^[0-9]{8}$")
_ROLES = {"admin", "manager", "cashier"}
_ROLE_ACTIONS = {
    "staff.manage": {"admin"},
    "catalog.manage": {"admin"},
    "inventory.adjust": {"admin", "manager"},
    "reports.read": {"admin", "manager"},
    "audit.read": {"admin"},
    "order.read": {"admin", "manager", "cashier"},
    "order.accept": {"admin", "cashier"},
    "order.pass": {"admin", "cashier"},
    "order.reject": {"admin"},
    "order.advance": {"admin", "cashier"},
    "ticket.review": {"admin", "cashier"},
    "ticket.commit": {"admin", "cashier"},
    "sale.register": {"admin", "cashier"},
}
_ASSIGNED_ACTIONS = {"order.read", "order.accept", "order.pass", "order.advance"}


class AuthorizationError(PermissionError):
    """A staff operation failed its server-side authorization check."""


@dataclass(frozen=True, slots=True)
class Principal:
    phone: str
    role: str
    expires_at: str


class PersonnelAuth:
    """OTP and per-operation role checks. OTPs and session tokens never leave memory."""

    def __init__(self, store: H2Store, *, otp_pepper: str):
        if len(otp_pepper) < 32:
            raise ValueError("H2_OTP_PEPPER debe tener al menos 32 caracteres aleatorios.")
        self._store = store
        self._pepper = otp_pepper.encode("utf-8")

    def bootstrap_first_admin(self, phone: str, *, now: str | None = None) -> bool:
        """Create the first admin only on an empty H2 database; never reset staff."""
        if not _PHONE.fullmatch(phone):
            raise ValueError("H2_INITIAL_ADMIN_PHONE no tiene formato E.164.")
        if self._store.staff_count():
            return False
        self._store.create_staff(phone, "admin", now=now)
        return True

    async def request_code(
        self,
        phone: str,
        send_message: Callable[[str, str], Awaitable[object]],
        *,
        now: str | None = None,
    ) -> bool:
        """Send a single 8-digit, 5-minute OTP to its registered source number."""
        if not _PHONE.fullmatch(phone):
            return False
        moment = _as_utc(now)
        code = f"{secrets.randbelow(100_000_000):08d}"
        digest = self._otp_digest(phone, code)
        expires = (moment + timedelta(minutes=5)).isoformat(timespec="milliseconds")
        try:
            self._store.store_auth_code(phone, digest, expires, now=moment.isoformat(timespec="milliseconds"))
        except H2StorageError:
            # Same outcome for unknown numbers and resend throttling; no account enumeration.
            return False
        try:
            await send_message(phone, f"Tu código de acceso es {code}. Vence en 5 minutos.")
        except (httpx.HTTPError, RuntimeError, ValueError):
            # Do not log the exception: HTTP client error details can contain request data.
            return False
        return True

    def verify_code(self, phone: str, code: str, *, now: str | None = None) -> bool:
        if not _PHONE.fullmatch(phone):
            return False
        candidate = code if isinstance(code, str) and _CODE.fullmatch(code) else "invalid-code"
        moment = _as_utc(now)
        session_hash = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        expires = (moment + timedelta(minutes=30)).isoformat(timespec="milliseconds")
        return self._store.verify_auth_code_and_create_session(
            phone,
            self._otp_digest(phone, candidate),
            session_hash,
            now=moment.isoformat(timespec="milliseconds"),
            expires_at=expires,
        )

    def has_pending_code(self, phone: str, *, now: str | None = None) -> bool:
        if not _PHONE.fullmatch(phone):
            return False
        return self._store.auth_code_pending(phone, now=now)

    def principal(self, phone: str, *, now: str | None = None) -> Principal | None:
        if not _PHONE.fullmatch(phone):
            return None
        session = self._store.staff_session(phone, now=now)
        if session is None:
            return None
        return Principal(phone=session["phone"], role=session["role"], expires_at=session["session_expires_at"])

    def authorize(self, phone: str, action: str, *, folio: str | None = None, now: str | None = None) -> Principal:
        """Re-read active role/session/assignment for every privileged operation."""
        principal = self.principal(phone, now=now)
        role_set = _ROLE_ACTIONS.get(action)
        allowed = principal is not None and role_set is not None and principal.role in role_set
        if allowed and principal.role == "cashier" and action in _ASSIGNED_ACTIONS:
            allowed = folio is not None and self._store.order_assignment_is_current(folio, phone)
        if not allowed:
            actor = phone if _PHONE.fullmatch(phone) else "anonymous:invalid"
            self._store.record_denial(actor, f"authorize:{action[:112]}", folio or "-")
            raise AuthorizationError("Acción no autorizada.")
        return principal

    def invite_staff(self, inviter: str, phone: str, role: str, *, now: str | None = None) -> None:
        self.authorize(inviter, "staff.manage", now=now)
        if role not in _ROLES:
            raise ValueError("Rol no reconocido.")
        self._store.create_staff(phone, role, invited_by=inviter, now=now)

    def change_staff_role(
        self, actor: str, phone: str, role: str, *, active: bool = True, now: str | None = None,
    ) -> None:
        self.authorize(actor, "staff.manage", now=now)
        self._store.set_staff_role(phone, role, actor=actor, active=active, now=now)

    def _otp_digest(self, phone: str, code: str) -> str:
        message = f"coffee-house-h2-otp\0{phone}\0{code}".encode()
        return hmac.new(self._pepper, message, hashlib.sha256).hexdigest()


def _as_utc(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("La fecha debe incluir zona horaria.")
    return parsed.astimezone(timezone.utc)
