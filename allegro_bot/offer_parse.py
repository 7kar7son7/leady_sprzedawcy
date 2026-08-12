from __future__ import annotations

import json
import re

from allegro_bot.extract import normalize_phone


def _find_json_objects_with_key(html: str, key: str) -> list[dict]:
    """Znajdź obiekty JSON zawierające dany klucz (płytkie / średnie zagnieżdżenie)."""
    out: list[dict] = []
    needle = f'"{key}"'
    pos = 0
    while True:
        idx = html.find(needle, pos)
        if idx < 0:
            break
        # cofnij do początku obiektu
        start = html.rfind("{", 0, idx)
        if start < 0:
            pos = idx + len(needle)
            continue
        depth = 0
        in_str = False
        esc = False
        end = None
        for i, ch in enumerate(html[start:], start):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
        if end:
            raw = html[start:end]
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and key in obj:
                    out.append(obj)
            except json.JSONDecodeError:
                pass
        pos = idx + len(needle)
        if len(out) >= 30:
            break
    return out


def parse_offer_seller_contacts(html: str) -> dict:
    """Publiczne telefony/emaile/firma z HTML oferty Allegro."""
    result: dict = {
        "phone": None,
        "email": None,
        "company_name": None,
        "address": None,
        "nip": None,
    }

    for contact in _find_json_objects_with_key(html, "phones"):
        phones = contact.get("phones") or []
        emails = contact.get("emails") or []
        if phones and not result["phone"]:
            num = phones[0].get("number") if isinstance(phones[0], dict) else None
            result["phone"] = normalize_phone(num)
        if emails and not result["email"]:
            addr = emails[0].get("address") if isinstance(emails[0], dict) else None
            if addr and "allegro.pl" not in addr.lower():
                result["email"] = addr.strip().lower()
        if result["phone"] and result["email"]:
            break

    for company in _find_json_objects_with_key(html, "representativeLabel"):
        name = company.get("name") or company.get("representativeLabel")
        if name and not result["company_name"]:
            result["company_name"] = str(name).strip()
        addr = company.get("address") or {}
        if isinstance(addr, dict) and not result["address"]:
            parts = [
                addr.get("street"),
                " ".join(x for x in [addr.get("zipCode"), addr.get("city")] if x),
                addr.get("country"),
            ]
            result["address"] = ", ".join(p for p in parts if p) or None
        tin = company.get("taxIdentificationNumber")
        if tin and not result["nip"]:
            digits = re.sub(r"\D", "", str(tin))
            if len(digits) == 10:
                result["nip"] = digits

    if not result["nip"]:
        m = re.search(r'"taxIdentificationNumber"\s*:\s*"(\d{10})"', html)
        if m:
            result["nip"] = m.group(1)

    return result
