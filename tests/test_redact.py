from brain.redact import redact


def test_redacts_common_secrets():
    cases = [
        "token ghp_abcdefghijklmnopqrstuvwxyz0123",
        "Authorization: Bearer abcdefghijklmnopqrstuvwxyz012345",
        "password=hunter2hunter2",
        "API_KEY: 'sk-abcdefghijklmnopqrstuv'",
        "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ4eHh4eHh4In0.c2lnbmF0dXJlc2lnbmF0dXJl",
        "postgres://user:s3cretpw@db:5432/x",
        "0123456789abcdef0123456789abcdef01234567",
        "-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----",
    ]
    for c in cases:
        out = redact(c)
        assert "REDACTED" in out, c
    assert "hunter2" not in redact("password=hunter2hunter2")
    assert "s3cretpw" not in redact(cases[5])


def test_leaves_normal_text():
    t = "Deployed brain to prod at commit a00a843 and restarted the pod."
    assert redact(t) == t
