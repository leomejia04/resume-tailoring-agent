"""Download names like ``LeonardoSuzana_Business_Analyst_OFI_CV.pdf``."""

from __future__ import annotations

import re
import unicodedata

_PART_MAX = 40


def _clean(label: str | None) -> str:
    if not label:
        return ""
    ascii_text = unicodedata.normalize("NFKD", label).encode("ascii", "ignore").decode()
    words = re.findall(r"[A-Za-z0-9]+", ascii_text)
    return "_".join(words)[:_PART_MAX].strip("_")


def download_filename(prefix: str | None, position: str | None, company: str | None) -> str:
    """Prefix_(Position)_(Company)_CV.pdf with every part reduced to safe characters."""
    parts = [_clean(prefix) or "Resume", _clean(position) or "Role", _clean(company)]
    return "_".join(p for p in parts if p) + "_CV.pdf"
