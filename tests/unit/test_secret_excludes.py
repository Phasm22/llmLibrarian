from ingest import ADD_DEFAULT_EXCLUDE


def test_default_exclude_contains_secret_patterns():
    expected = {
        ".env",
        ".env.*",
        ".aws/",
        ".ssh/",
        "*.pem",
        "*.key",
        "secrets.json",
        "credentials.json",
        "credentials*.json",
    }
    assert expected.issubset(set(ADD_DEFAULT_EXCLUDE))
