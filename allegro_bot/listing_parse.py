from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse


def _extract_balanced_json(html: str, marker: str) -> dict | None:
    start = html.find(marker)
    if start < 0:
        return None
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
    if end is None:
        return None
    try:
        return json.loads(html[start:end])
    except json.JSONDecodeError:
        return None


def parse_listing_store_state(html: str) -> list[dict]:
    """Wyciąga unikalnych sprzedawców z __listing_StoreState w HTML listingu."""
    data = _extract_balanced_json(html, '{"__listing_StoreState"')
    if not data:
        return []
    store = data.get("__listing_StoreState") or {}
    elements = ((store.get("items") or {}).get("elements")) or []
    out: list[dict] = []
    seen: set[str] = set()
    for el in elements:
        seller = el.get("seller") or {}
        login = (seller.get("login") or "").strip()
        seller_url = seller.get("userListingUrl")
        if seller_url and "/uzytkownik/" in seller_url:
            m = re.search(r"/uzytkownik/([^/?#]+)", seller_url)
            if m:
                login = unquote(m.group(1)).strip() or login
        if not login:
            continue
        key = login.lower()
        if key in seen:
            continue
        seen.add(key)

        offer_url = None
        raw_url = el.get("url") or ""
        if "redirect=" in raw_url:
            q = parse_qs(urlparse(raw_url).query)
            redirect = q.get("redirect", [""])[0]
            if redirect:
                offer_url = unquote(redirect).split("&")[0].split("?")[0]
        if not offer_url and el.get("offerId"):
            # fallback – czasem brak pełnego sluga; i tak otworzymy po ID jeśli trzeba
            offer_url = f"https://allegro.pl/oferta/{el['offerId']}"
        if offer_url and "?" in offer_url:
            offer_url = offer_url.split("?", 1)[0]

        out.append(
            {
                "login": login,
                "seller_url": seller_url or f"https://allegro.pl/uzytkownik/{login}",
                "offer_url": offer_url,
                "company": bool(seller.get("company")),
                "seller_title": seller.get("title"),
                "offer_id": el.get("offerId"),
            }
        )
    return out
