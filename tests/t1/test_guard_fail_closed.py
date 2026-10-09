from evaluation.t1.guard_client import GuardClient, may_continue


def test_only_exact_safe_true_can_continue():
    assert may_continue({"label": "Safe", "allowed": True})
    assert not may_continue({"label": "Unsafe", "allowed": False})
    assert not may_continue({"label": "Controversial", "allowed": False})
    assert not may_continue({"label": "Unknown", "allowed": False})
    assert not may_continue({"label": "Safe", "allowed": False})
    assert not may_continue({"label": "Safe", "allowed": 1})
    assert not may_continue({"label": "Safe", "allowed": True, "extra": "unexpected"})
    assert not may_continue(None)


def test_malformed_generation_is_unknown():
    from evaluation.t1.guard_service import parse_label

    assert parse_label("Safety: Safe\nCategories: None") == "Safe"
    assert parse_label("Safety: Safe\nCategories: None\nRefusal: No") == "Safe"
    assert parse_label("Safety: Unsafe\nCategories: PII") == "Unsafe"
    assert parse_label("Safety: Controversial\nCategories: Politically Sensitive Topics") == "Controversial"
    assert parse_label("preface\nSafety: Safe\nCategories: None") == "Unknown"
    assert parse_label("Safety: Safe\nOther: unexpected") == "Unknown"
    assert parse_label("Safety: Maybe\nCategories: None") == "Unknown"
    assert parse_label("") == "Unknown"


def test_non_loopback_guard_url_rejected():
    try:
        GuardClient("http://0.0.0.0:18196")
    except ValueError:
        pass
    else:
        raise AssertionError("public/non-loopback address must be rejected")
