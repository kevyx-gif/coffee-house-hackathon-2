import asyncio
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from coffee_house.hackathon2.auth import AuthorizationError, PersonnelAuth
from coffee_house.hackathon2.storage import H2StorageError, H2Store

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_DIR = ROOT / "data" / "demo-hackathon2"
PEPPER = "local-test-pepper-that-is-never-a-real-secret-123456"
ADMIN = "+5215550000001"
MANAGER = "+5215550000002"
CASHIER = "+5215550000003"
CUSTOMER = "+5215550000004"
NOW = "2026-10-09T12:00:00+00:00"


def make_auth(tmp_path):
    store = H2Store(tmp_path / "h2.sqlite3")
    store.seed_demo_directory(FIXTURE_DIR)
    return store, PersonnelAuth(store, otp_pepper=PEPPER)


def issue_and_verify(auth, phone, *, now=NOW):
    sent = []

    async def sender(recipient, body):
        sent.append((recipient, body))

    assert asyncio.run(auth.request_code(phone, sender, now=now))
    match = re.search(r"\b([0-9]{8})\b", sent[0][1])
    assert match
    assert auth.verify_code(phone, match.group(1), now=now)
    return match.group(1)


def test_bootstrap_is_idempotent_and_only_creates_first_admin(tmp_path):
    store, auth = make_auth(tmp_path)
    assert auth.bootstrap_first_admin(ADMIN, now=NOW)
    assert not auth.bootstrap_first_admin(ADMIN, now=NOW)
    assert store.staff_count() == 1
    with pytest.raises(H2StorageError):
        store.create_staff(MANAGER, "manager", now=NOW)


def test_otp_is_hmac_only_expires_and_creates_single_30_minute_session(tmp_path):
    store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)
    sent = []

    async def sender(recipient, body):
        sent.append((recipient, body))

    assert asyncio.run(auth.request_code(ADMIN, sender, now=NOW))
    assert sent[0][0] == ADMIN
    code = re.search(r"\b([0-9]{8})\b", sent[0][1]).group(1)
    with sqlite3.connect(store.path) as connection:
        saved = connection.execute("SELECT code_hash,attempts,used_at FROM auth_codes WHERE phone=?", (ADMIN,)).fetchone()
    assert len(saved[0]) == 64 and code not in saved[0]
    assert saved[1:] == (0, None)
    assert auth.verify_code(ADMIN, code, now=NOW)
    assert auth.principal(ADMIN, now=NOW).role == "admin"
    assert auth.principal(ADMIN, now="2026-10-09T12:29:00+00:00").role == "admin"
    assert auth.principal(ADMIN, now="2026-10-09T13:00:00+00:00") is None
    assert not auth.verify_code(ADMIN, code, now=NOW)


def test_unknown_staff_is_not_enumerated_and_resend_waits_60_seconds(tmp_path):
    store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)
    store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=NOW)
    calls = []

    async def sender(recipient, body):
        calls.append(recipient)

    assert not asyncio.run(auth.request_code(CUSTOMER, sender, now=NOW))
    assert not calls
    assert asyncio.run(auth.request_code(CASHIER, sender, now=NOW))
    assert calls == [CASHIER]
    assert not asyncio.run(auth.request_code(CASHIER, sender, now="2026-10-09T12:00:59+00:00"))
    assert len(calls) == 1
    assert asyncio.run(auth.request_code(CASHIER, sender, now="2026-10-09T12:01:00+00:00"))
    assert len(calls) == 2


def test_five_wrong_codes_consume_otp_and_expired_code_cannot_login(tmp_path):
    _store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)
    sent = []

    async def sender(recipient, body):
        sent.append(body)

    assert asyncio.run(auth.request_code(ADMIN, sender, now=NOW))
    right = re.search(r"\b([0-9]{8})\b", sent[0]).group(1)
    for _ in range(5):
        assert not auth.verify_code(ADMIN, "00000000" if right != "00000000" else "99999999", now=NOW)
    assert not auth.verify_code(ADMIN, right, now=NOW)
    assert auth.principal(ADMIN, now=NOW) is None

    later = "2026-10-09T12:02:00+00:00"
    assert asyncio.run(auth.request_code(ADMIN, sender, now=later))
    fresh = re.search(r"\b([0-9]{8})\b", sent[-1]).group(1)
    assert not auth.verify_code(ADMIN, fresh, now="2026-10-09T12:07:00+00:00")


def test_current_role_and_action_matrix_are_checked_each_time(tmp_path):
    store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)
    store.create_staff(MANAGER, "manager", invited_by=ADMIN, now=NOW)
    store.create_staff(CASHIER, "cashier", invited_by=ADMIN, now=NOW)
    issue_and_verify(auth, ADMIN)
    issue_and_verify(auth, MANAGER)
    issue_and_verify(auth, CASHIER)

    assert auth.authorize(ADMIN, "staff.manage", now=NOW).role == "admin"
    assert auth.authorize(MANAGER, "reports.read", now=NOW).role == "manager"
    assert auth.authorize(MANAGER, "inventory.adjust", now=NOW).role == "manager"
    with pytest.raises(AuthorizationError):
        auth.authorize(MANAGER, "staff.manage", now=NOW)
    with pytest.raises(AuthorizationError):
        auth.authorize(CASHIER, "inventory.adjust", now=NOW)

    order_id = store.create_order(
        folio="H2-AUTH-1", idempotency_key="auth-order", customer_phone=CUSTOMER,
        items=[{"product_id": "hot_latte", "variant": "mediano", "quantity": 1}],
        total_cents=7000, expires_at="2026-10-09T12:10:00+00:00", now=NOW,
    )
    store.assign_order(order_id, CASHIER, 1, deadline="2026-10-09T12:05:00+00:00", now=NOW)
    with pytest.raises(AuthorizationError):
        auth.authorize(CASHIER, "order.accept", folio="H2-OTHER-1", now=NOW)
    assert auth.authorize(CASHIER, "order.accept", folio="H2-AUTH-1", now=NOW).phone == CASHIER
    auth.change_staff_role(ADMIN, CASHIER, "manager", now=NOW)
    assert auth.principal(CASHIER, now=NOW) is None
    with pytest.raises(AuthorizationError):
        auth.authorize(CASHIER, "reports.read", now=NOW)


def test_failed_delivery_never_returns_or_logs_otp(tmp_path):
    store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)

    async def broken_sender(recipient, body):
        raise RuntimeError(body)

    # The service returns a generic failure and never propagates the sender error/OTP.
    assert not asyncio.run(auth.request_code(ADMIN, broken_sender, now=NOW))
    with sqlite3.connect(store.path) as connection:
        saved = connection.execute("SELECT code_hash FROM auth_codes WHERE phone=?", (ADMIN,)).fetchone()[0]
    assert len(saved) == 64


def test_two_simultaneous_validations_can_only_consume_otp_once(tmp_path):
    _store, auth = make_auth(tmp_path)
    auth.bootstrap_first_admin(ADMIN, now=NOW)
    sent = []

    async def sender(recipient, body):
        sent.append(body)

    assert asyncio.run(auth.request_code(ADMIN, sender, now=NOW))
    code = re.search(r"\b([0-9]{8})\b", sent[0]).group(1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: auth.verify_code(ADMIN, code, now=NOW), range(2)))
    assert sorted(results) == [False, True]
