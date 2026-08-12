from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from allegro_bot.browser import open_context
from allegro_bot.scraper import AllegroSellerBot
from allegro_bot.storage import Storage


def load_config(path: Path | None) -> dict:
    cfg_path = path or (ROOT / "config.yaml")
    if not cfg_path.exists():
        example = ROOT / "config.example.yaml"
        if example.exists():
            cfg_path = example
        else:
            return {}
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Bot Allegro – zbiera publiczne dane sprzedawców (O sprzedającym)."
    )
    p.add_argument("--config", type=Path, default=None, help="Ścieżka do config.yaml")
    p.add_argument("--query", "-q", type=str, help="Fraza wyszukiwania, np. wiertarka")
    p.add_argument("--pages", type=int, help="Ile stron listingu")
    p.add_argument("--limit", type=int, help="Max unikalnych sprzedawców")
    p.add_argument("--headless", action="store_true", help="Bez okna (często blokowane Captchą)")
    p.add_argument("--no-chrome", action="store_true", help="Nie używaj systemowego Chrome")
    return p


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    query = (args.query or cfg.get("query") or "").strip()
    if not query:
        print("Podaj frazę: --query \"...\" albo ustaw query w config.yaml")
        return 2

    max_pages = int(args.pages or cfg.get("max_pages") or 3)
    max_sellers = int(args.limit or cfg.get("max_sellers") or 100)
    delay_min = float(cfg.get("delay_min_sec") or 1.5)
    delay_max = float(cfg.get("delay_max_sec") or 3.5)
    headless = bool(args.headless or cfg.get("headless") or False)
    use_chrome = not args.no_chrome and bool(cfg.get("use_system_chrome", True))

    profile_dir = ROOT / str(cfg.get("browser_profile_dir") or "browser_profile")
    csv_path = ROOT / str(cfg.get("output_csv") or "data/sellers.csv")
    sqlite_path = ROOT / str(cfg.get("output_sqlite") or "data/sellers.sqlite")

    storage = Storage(csv_path, sqlite_path)
    known = storage.known_logins()
    bot = AllegroSellerBot(
        delay_min=delay_min,
        delay_max=delay_max,
        max_sellers=max_sellers,
    )

    print(f"=== Allegro seller bot ===")
    print(f"query={query!r} pages={max_pages} limit={max_sellers}")
    print(f"profil={profile_dir}")
    print(f"wynik CSV={csv_path}")
    if headless:
        print("[!] headless – Allegro często pokazuje Captchę. Lepiej bez --headless.")

    pw, context = await open_context(
        profile_dir=profile_dir,
        headless=headless,
        use_system_chrome=use_chrome,
    )
    try:
        page = context.pages[0] if context.pages else await context.new_page()
        candidates = await bot.collect_from_listing(
            page, query, max_pages=max_pages, known_logins=known
        )
        print(f"[+] Do sprawdzenia: {len(candidates)} sprzedawców")

        saved = 0
        with_phone = 0
        for i, row in enumerate(candidates, 1):
            login = row["login"]
            print(f"[{i}/{len(candidates)}] {login} …", flush=True)
            try:
                lead = await bot.scrape_seller_from_offer(
                    page,
                    login=login,
                    seller_url=row.get("seller_url"),
                    offer_url=row.get("offer_url"),
                    query=query,
                )
                storage.save(lead)
                saved += 1
                if lead.phone:
                    with_phone += 1
                    print(f"    OK tel={lead.phone} firma={lead.company_name or '-'}")
                else:
                    print("    brak publicznego telefonu")
            except Exception as e:
                print(f"    BŁĄD: {e}")
            await bot.pause()

        storage.save_state(
            ROOT / "data" / "last_run.json",
            {
                "query": query,
                "candidates": len(candidates),
                "saved": saved,
                "with_phone": with_phone,
                "csv": str(csv_path),
            },
        )
        print(f"=== Koniec: zapisano {saved}, z telefonem {with_phone} ===")
        return 0
    finally:
        await context.close()
        await pw.stop()


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
