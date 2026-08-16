from __future__ import annotations

import asyncio
import random
from urllib.parse import quote_plus, urljoin

from playwright.async_api import Page

from allegro_bot.browser import dismiss_cookie_banner, wait_until_listing_ready
from allegro_bot.extract import extract_phones
from allegro_bot.listing_parse import parse_listing_store_state
from allegro_bot.models import SellerLead

BASE = "https://allegro.pl"


class AllegroSellerBot:
    def __init__(
        self,
        *,
        delay_min: float = 1.5,
        delay_max: float = 3.5,
        max_sellers: int = 100,
    ) -> None:
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.max_sellers = max_sellers

    async def pause(self, lo: float | None = None, hi: float | None = None) -> None:
        a = self.delay_min if lo is None else lo
        b = self.delay_max if hi is None else hi
        sec = random.uniform(min(a, b), max(a, b))
        if random.random() < 0.12:
            sec += random.uniform(8, 22)
        await asyncio.sleep(sec)

    def listing_url(self, query: str, page_no: int = 1) -> str:
        q = quote_plus(query.strip())
        if page_no <= 1:
            return f"{BASE}/listing?string={q}"
        return f"{BASE}/listing?string={q}&p={page_no}"

    async def collect_from_listing(
        self,
        page: Page,
        query: str,
        max_pages: int,
        known_logins: set[str],
        *,
        headless_mode: bool = False,
        log_fn=None,
    ) -> list[dict]:
        """Zwraca listę {login, seller_url, offer_url} z listingu."""
        found: list[dict] = []
        seen: set[str] = set()
        wait1 = 60_000 if headless_mode else 180_000
        wait2 = 30_000 if headless_mode else 300_000

        def log(msg: str) -> None:
            if log_fn:
                log_fn(msg)
            else:
                print(msg)

        for page_no in range(1, max_pages + 1):
            url = self.listing_url(query, page_no)
            log(f"listing strona {page_no}: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            await dismiss_cookie_banner(page)
            ok = await wait_until_listing_ready(page, timeout_ms=wait1)
            if not ok:
                log(
                    f"[!] Strona {page_no}: Captcha / brak ofert "
                    f"(czekam jeszcze {wait2 // 1000}s)…"
                )
                ok = await wait_until_listing_ready(page, timeout_ms=wait2)
                if not ok:
                    # diagnostyka
                    title = await page.title()
                    html_len = len(await page.content())
                    log(
                        f"[!] Pomijam stronę {page_no} – title={title!r} html={html_len}. "
                        "Na hostingu często DataDome blokuje headless."
                    )
                    break

            html = await page.content()
            rows = parse_listing_store_state(html)

            added = 0
            for row in rows:
                login = (row.get("login") or "").strip()
                key = login.lower()
                if not key or key in seen or key in known_logins:
                    continue
                seen.add(key)
                found.append(
                    {
                        "login": login,
                        "seller_url": row.get("seller_url")
                        or f"{BASE}/uzytkownik/{quote_plus(login)}",
                        "offer_url": row.get("offer_url"),
                        "company": row.get("company"),
                    }
                )
                added += 1
                if len(found) >= self.max_sellers:
                    log(
                        f"[+] Listing strona {page_no}: +{added} "
                        f"sprzedawców (łącznie {len(found)})"
                    )
                    return found

            log(f"[+] Listing strona {page_no}: +{added} sprzedawców (łącznie {len(found)})")
            if added == 0:
                log("[!] Brak sprzedawców w StoreState – Captcha albo zmiana HTML.")
            await self.pause(self.delay_min * 2, self.delay_max * 3)

        return found

    async def scrape_seller_from_offer(
        self,
        page: Page,
        *,
        login: str,
        seller_url: str | None,
        offer_url: str | None,
        query: str,
    ) -> SellerLead:
        from allegro_bot.offer_parse import parse_offer_seller_contacts

        lead = SellerLead(
            login=login,
            seller_url=seller_url,
            offer_url=offer_url,
            query=query,
        )
        target = offer_url or seller_url
        if not target:
            return lead

        await page.goto(target, wait_until="domcontentloaded", timeout=60_000)
        await dismiss_cookie_banner(page)
        await page.wait_for_timeout(int(random.uniform(1400, 3800)))

        # przewiń / kliknij kotwicę o sprzedającym (dane często już są w HTML)
        try:
            link = page.locator('a[href="#about-seller"]').first
            if await link.count():
                await link.click(timeout=2500, force=True)
                await page.wait_for_timeout(800)
        except Exception:
            pass

        html = await page.content()
        parsed = parse_offer_seller_contacts(html)
        lead.phone = parsed.get("phone")
        lead.email = parsed.get("email")
        lead.company_name = parsed.get("company_name")
        lead.address = parsed.get("address")
        lead.nip = parsed.get("nip")

        # fallback: tel: linki
        if not lead.phone:
            tel_hrefs = await page.eval_on_selector_all(
                "a[href^='tel:']",
                "els => els.map(e => e.getAttribute('href') || '')",
            )
            for href in tel_hrefs:
                phones = extract_phones(href.replace("tel:", ""))
                if phones:
                    lead.phone = phones[0]
                    break

        if not lead.seller_url:
            lead.seller_url = urljoin(BASE, f"/uzytkownik/{quote_plus(login)}")

        lead.raw_note = "ok" if lead.phone else "brak_telefonu_w_publicznym_widoku"
        return lead
