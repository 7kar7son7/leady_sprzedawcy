from __future__ import annotations

import re

PHONE_CANDIDATE_RE = re.compile(
    r"(?:\+48[\s\-]?)?(?:\(?\d{2,3}\)?[\s\-]?)?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}"
)
NIP_RE = re.compile(r"\b(?:NIP[:\s]*)?(\d{3}[-\s]?\d{3}[-\s]?\d{2}[-\s]?\d{2})\b", re.I)
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
LOGIN_FROM_URL_RE = re.compile(r"/uzytkownik/([^/?#]+)", re.I)


def normalize_phone(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("48") and len(digits) == 11:
        digits = digits[2:]
    if len(digits) != 9:
        return None
    # odrzuć oczywiste śmieci / daty
    if digits[0] == "0":
        return None
    return f"+48{digits}"


def extract_phones(text: str) -> list[str]:
    found: list[str] = []
    for m in PHONE_CANDIDATE_RE.findall(text or ""):
        n = normalize_phone(m)
        if n and n not in found:
            found.append(n)
    return found


def extract_emails(text: str) -> list[str]:
    blocked = {"allegro.pl", "example.com", "sentry.io"}
    out: list[str] = []
    for e in EMAIL_RE.findall(text or ""):
        el = e.lower().strip()
        if any(b in el for b in blocked):
            continue
        if el not in out:
            out.append(el)
    return out


def extract_nip(text: str) -> str | None:
    m = NIP_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return digits if len(digits) == 10 else None


def login_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = LOGIN_FROM_URL_RE.search(url)
    if not m:
        return None
    try:
        from urllib.parse import unquote

        return unquote(m.group(1)).strip()
    except Exception:
        return m.group(1).strip()
