from app.extractor import (
    extract_bare_urls,
    is_known_secondary_url,
    is_solidot_url,
    normalize_url,
)


def test_normalize_url_removes_tracking_parameters() -> None:
    result = normalize_url(
        "https://example.org/paper?id=7&utm_source=telegram&fbclid=abc#results"
    )

    assert result == "https://example.org/paper?id=7"


def test_normalize_url_rejects_non_http_protocols() -> None:
    assert normalize_url("file:///etc/passwd") == ""


def test_extract_bare_article_url() -> None:
    text = (
        "Подробности https://www.zdnet.com/business/story/?utm_source=rss. "
        "Конец."
    )

    assert extract_bare_urls(text) == ["https://www.zdnet.com/business/story/"]


def test_source_classification_blocks_solidot_and_known_media() -> None:
    assert is_solidot_url("https://www.solidot.org/story?sid=1")
    assert is_known_secondary_url("https://www.zdnet.com/business/story/")
    assert not is_known_secondary_url("https://www.adviescollegeicttoetsing.nl/documenten/1")
