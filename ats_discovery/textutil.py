"""Text helpers: HTML to plain text, timestamp normalisation, best-effort compensation parsing.

Pure standard library (html.parser, re, datetime); no third-party HTML dependency.
"""
from __future__ import annotations

import html as _html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "tr", "table", "section", "article", "blockquote",
}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(s: str | None) -> str:
    """HTML (possibly entity-escaped twice, as Greenhouse does) -> plain text with line breaks."""
    if not s:
        return ""
    if "&lt;" in s and "<" not in s:
        s = _html.unescape(s)
    parser = _TextExtractor()
    parser.feed(s)
    parser.close()
    text = "".join(parser.parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def iso(ts) -> str | None:
    """Epoch seconds/ms, ISO string, 'YYYY-MM-DD HH:MM:SS UTC' or RFC-822 -> UTC ISO-8601 (seconds), else None."""
    if ts in (None, "", 0):
        return None
    try:
        if isinstance(ts, (int, float)):
            if ts > 1e11:
                ts = ts / 1000.0
            return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")
        s = str(ts).strip()
        if re.fullmatch(r"\d{10,13}", s):
            return iso(int(s))
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            s += "T00:00:00+00:00"
        s = re.sub(r"\s+UTC$", "+00:00", s).replace("Z", "+00:00")
        try:
            d = datetime.fromisoformat(s)
        except ValueError:
            d = parsedate_to_datetime(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat(timespec="seconds")
    except Exception:
        return None


# ---------------------------------------------------------------- compensation

_NUM = r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_RANGE = re.compile(
    r"\$\s?" + _NUM + r"\s?([kK])?(?:\s*(?:/|per)\s*(?:hr|hour|year|yr))?\s*(?:-|–|—|to|and)\s*\$?\s?" + _NUM + r"\s?([kK])?",
    re.I,
)
_SINGLE = re.compile(r"\$\s?" + _NUM + r"\s?([kK])?\b(?:\s*(?:/|per|an|a)\s*(hr|hour|year|yr))?", re.I)
_HOUR = re.compile(r"(/\s?h(ou)?r|per\s+hour|an\s+hour|hourly|/\s?hour)", re.I)
_YEAR = re.compile(r"(/\s?y(ea)?r|per\s+(year|annum)|annual|a\s+year|yearly|salary|base)", re.I)


def _val(num: str, k: str | None) -> float:
    v = float(num.replace(",", ""))
    return v * 1000 if k else v


def parse_comp(text: str | None):
    """Best-effort USD pay from free text -> (min, max, unit) or (None, None, None).

    Only `$` figures are considered; values below 400 need an hourly cue. unit is 'year' or 'hour'."""
    if not text:
        return None, None, None
    for m in _RANGE.finditer(text):
        k_hi = m.group(4)
        k_lo = m.group(2) or (k_hi if float(m.group(1).replace(",", "")) < 1000 and k_hi else None)
        lo, hi = _val(m.group(1), k_lo), _val(m.group(3), k_hi)
        ctx = text[m.end(): m.end() + 40]
        pre = text[max(0, m.start() - 40): m.start()]
        if lo > hi:
            lo, hi = hi, lo
        if _HOUR.search(ctx) or (hi < 400 and re.search(r"hour|hr", pre + ctx, re.I)):
            if 8 <= lo and hi <= 600:
                return lo, hi, "hour"
            continue
        if 20000 <= lo and hi <= 2_000_000:
            return lo, hi, "year"
    for m in _SINGLE.finditer(text):
        v = _val(m.group(1), m.group(2))
        unit = (m.group(3) or "").lower()
        ctx = text[m.end(): m.end() + 25]
        if unit.startswith("h") or (v < 400 and _HOUR.search(ctx)):
            if 8 <= v <= 600:
                return v, v, "hour"
        elif unit in ("year", "yr") or (
            20000 <= v <= 2_000_000 and _YEAR.search(text[max(0, m.start() - 40): m.end() + 25])
        ):
            return v, v, "year"
    return None, None, None


def norm_employment(s: str | None) -> str:
    """Free-text employment type -> contract | parttime | fulltime | unknown."""
    s = (s or "").lower()
    if any(w in s for w in ("contract", "freelance", "temporary", "consult", "fixed-term", "fixed term")):
        return "contract"
    if "part" in s:
        return "parttime"
    if any(w in s for w in ("full", "permanent", "regular")):
        return "fulltime"
    return "unknown"
