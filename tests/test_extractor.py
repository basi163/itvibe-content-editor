from app.extractor import normalize_url


def test_normalize_url_removes_tracking_parameters() -> None:
    result = normalize_url(
        "https://example.org/paper?id=7&utm_source=telegram&fbclid=abc#results"
    )

    assert result == "https://example.org/paper?id=7"


def test_normalize_url_rejects_non_http_protocols() -> None:
    assert normalize_url("file:///etc/passwd") == ""
