from app.sanitizer import REDACTED, sanitize_text, sanitize_value


def test_sanitize_nested_sensitive_keys() -> None:
    payload = {
        "command": "login",
        "password": "secret-value",
        "nested": {"api_key": "secret-key", "safe": "visible"},
    }

    result = sanitize_value(payload)

    assert result["password"] == REDACTED
    assert result["nested"]["api_key"] == REDACTED
    assert result["nested"]["safe"] == "visible"


def test_sanitize_secret_patterns_in_text() -> None:
    value = "Authorization: Bearer abcdefghijklmnop and api_key=top-secret"

    result = sanitize_text(value)

    assert "abcdefghijklmnop" not in result
    assert "top-secret" not in result
    assert REDACTED in result
