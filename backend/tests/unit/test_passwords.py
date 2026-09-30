from app.auth.passwords import hash_password, password_policy_violations, verify_password


def test_auth_14_short_password_is_rejected_as_too_short() -> None:
    assert "too_short" in password_policy_violations("abc")


def test_auth_14_seven_characters_is_too_short_and_eight_is_not() -> None:
    assert "too_short" in password_policy_violations("x9!kQ2#")
    assert "too_short" not in password_policy_violations("x9!kQ2#v")


def test_auth_14_common_password_is_rejected() -> None:
    assert "common" in password_policy_violations("password123")


def test_auth_14_common_check_is_case_insensitive() -> None:
    assert "common" in password_policy_violations("PassWord123")


def test_auth_14_short_and_common_password_reports_both_rules() -> None:
    assert password_policy_violations("123456") == ["too_short", "common"]


def test_auth_14_strong_password_has_no_violations() -> None:
    assert password_policy_violations("x9!kQ2#vLm") == []


def test_auth_14_no_composition_rules_are_enforced() -> None:
    assert password_policy_violations("zqxwvutsrponm") == []


def test_verify_password_accepts_correct_password() -> None:
    stored = hash_password("s3cret-Pass")

    assert verify_password(stored, "s3cret-Pass") is True


def test_verify_password_rejects_wrong_password() -> None:
    stored = hash_password("s3cret-Pass")

    assert verify_password(stored, "s3cret-pass") is False


def test_verify_password_rejects_malformed_hash() -> None:
    assert verify_password("not-a-hash", "s3cret-Pass") is False


def test_hash_password_uses_argon2id() -> None:
    assert hash_password("s3cret-Pass").startswith("$argon2id$")


def test_hash_password_is_salted() -> None:
    assert hash_password("s3cret-Pass") != hash_password("s3cret-Pass")
