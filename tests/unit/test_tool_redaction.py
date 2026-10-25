import pytest

from app.tools.redaction import redact

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAKCAQEA\n-----END RSA PRIVATE KEY-----"
JWT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJhbGljZSIsImlhdCI6MX0.c2lnbmF0dXJlLXNpZ25hdHVyZQ"


@pytest.mark.parametrize(
    ("text", "expected", "count"),
    [
        (f"key:\n{PEM}\nend", "key:\n[REDACTED:private_key]\nend", 1),
        (f"Authorization: Bearer {JWT}", "Authorization: Bearer [REDACTED:jwt]", 1),
        (
            "aws_access_key_id = AKIAIOSFODNN7EXAMPLE",
            "aws_access_key_id = [REDACTED:aws_access_key]",
            1,
        ),
        ("ghp_abcdefghijklmnopqrstuvwxyz0123456789", "[REDACTED:api_token]", 1),
        ('{"password": "hunter2hunter2"}', '{"password": "[REDACTED]"}', 1),
        ("API_KEY=sk-ant-abcdefghijklmnopqrstu", "API_KEY=[REDACTED:api_token]", 1),
        ("Authorization: Bearer opaque-token-value", "Authorization: Bearer [REDACTED]", 1),
        ("token_count = 5\npassword: null", "token_count = 5\npassword: null", 0),
        ("def get_token(self): return self._token", "def get_token(self): return self._token", 0),
    ],
)
def test_redaction_patterns(text: str, expected: str, count: int):
    assert redact(text) == (expected, count)
