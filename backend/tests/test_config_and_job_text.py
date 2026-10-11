import pytest
from pydantic import ValidationError

from app.config import Settings
from app.services.job_text import find_headings, normalize_job_text, word_count


@pytest.mark.parametrize(
    "origins",
    ["*", "https://*.example.com", "https://example.com/path", "ftp://example.com", "example.com"],
)
def test_invalid_cors_origins_are_rejected(origins):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_allowed_origins=origins)


def test_cors_origins_parse_from_comma_separated_string():
    settings = Settings(
        _env_file=None, cors_allowed_origins="http://localhost:5173, https://app.example.com/"
    )
    assert settings.cors_allowed_origins == ["http://localhost:5173", "https://app.example.com"]


@pytest.mark.parametrize("origin", ["http://app.example.com", "https://localhost:5173"])
def test_production_requires_https_non_local_origins(origin):
    with pytest.raises(ValidationError, match="production"):
        Settings(_env_file=None, app_env="production", cors_allowed_origins=origin)


def test_normalize_job_text():
    raw = "﻿Title\r\n\r\n\r\n\r\nLine\twith  tabs\x00 ​\rEnd   "
    assert normalize_job_text(raw) == "Title\n\nLine with tabs\nEnd"


def test_headings_need_heading_shape():
    text = "\n".join(
        [
            "WHAT YOU'LL DO",
            "Experience with SQL is helpful for this team.",
            "- 3+ years of experience",
            "Nice to have:",
            "## Skills",
            "Experience with dashboards",
        ]
    )
    found = [(h.kind, h.text, h.line) for h in find_headings(text)]
    assert found == [
        ("responsibilities", "WHAT YOU'LL DO", 1),
        ("preferred", "Nice to have", 4),
        ("required", "Skills", 5),
    ]


def test_curly_apostrophes_match():
    assert find_headings("What You’ll Need")[0].kind == "required"


def test_word_count_handles_contractions_and_hyphens():
    assert word_count("You'll build day-to-day reports, fast.") == 5
