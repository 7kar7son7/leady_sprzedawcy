from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from allegro_bot.browser import has_display, open_context, running_in_container
from allegro_bot.scraper import AllegroSellerBot
from allegro_bot.storage import Storage

DEFAULT_QUERIES = [
    "kosmetyki",
    "kosmetyki naturalne",
    "pielęgnacja twarzy",
    "szampon",
    "perfumy",
    "makijaż",
    "krem do twarzy",
    "meble ogrodowe",
    "narzędzia",
    "wiertarka",
    "opony",
    "akcesoria samochodowe",
    "zabawki",
    "karma dla psa",
    "odzież damska",
    "buty sportowe",
    "elektronika",
    "słuchawki",
    "agd",
    "dekoracje domu",
]


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
    p.add_argument("--query", "-q", type=str, help="Fraza (w trybie --once: jedna; w pętli: startowa)")
    p.add_argument("--pages", type=int, help="Max stron listingu na frazę")
    p.add_argument("--limit", type=int, help="Max sprzedawców na frazę")
    p.add_argument(
        "--loop",
        action="store_true",
        help="Jedź w kółko po niszach z przerwami (nie wyłącza się sam)",
    )
    p.add_argument("--once", action="store_true", help="Jeden run i koniec")
    p.add_argument(
        "--headless",
        action="store_true",
        default=None,
        help="Bez okna (domyślne na Docker/VPS bez DISPLAY)",
    )
    p.add_argument(
        "--headed",
        action="store_true",
        help="Wymuś okno (wymaga GUI / Xvfb)",
    )
    p.add_argument("--no-chrome", action="store_true", help="Nie używaj systemowego Chrome")
    return p


def query_pool(cfg: dict, extra: str | None) -> list[str]:
    raw = cfg.get("queries") or []
    pool = [str(q).strip() for q in raw if str(q).strip()]
    if not pool:
        pool = list(DEFAULT_QUERIES)
    if extra and extra.strip() and extra.strip() not in pool:
        pool.insert(0, extra.strip())
    elif extra and extra.strip():
        pool = [extra.strip()] + [q for q in pool if q != extra.strip()]
    return pool


async def human_wait(lo: float, hi: float, why: str) -> None:
    sec = random.uniform(min(lo, hi), max(lo, hi))
    if random.random() < 0.15:
        sec += random.uniform(20, 70)
    print(f"[pauza] {why} – {sec:.0f}s", flush=True)
    await asyncio.sleep(sec)


def resolve_headless(args: argparse.Namespace, cfg: dict) -> tuple[bool, bool]:
    if args.headed:
        headless = False
    elif args.headless is True:
        headless = True
    elif "headless" in cfg and cfg.get("headless") is not None:
        headless = bool(cfg.get("headless"))
    else:
        headless = not has_display() or running_in_container()

    use_chrome = not args.no_chrome and bool(cfg.get("use_system_chrome", True))
    if running_in_container() or not has_display():
        use_chrome = False
    return headless, use_chrome


async def scrape_query(
    *,
    page,
    bot: AllegroSellerBot,
    storage: Storage,
    known: set[str],
    query: str,
    max_pages: int,
    limit: int,
    batch_every: int,
    batch_pause: tuple[float, float],
    headless_mode: bool,
) -> tuple[int, int]:
    bot.max_sellers = limit
    candidates = await bot.collect_from_listing(
        page,
        query,
        max_pages=max_pages,
        known_logins=known,
        headless_mode=headless_mode,
    )
    print(f"[+] Do sprawdzenia: {len(candidates)} sprzedawców ({query!r})", flush=True)
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
            known.add(login.lower())
            saved += 1
            if lead.phone:
                with_phone += 1
                print(f"    OK tel={lead.phone} firma={lead.company_name or '-'}")
            else:
                print("    brak publicznego telefonu")
        except Exception as e:
            print(f"    BŁĄD: {e}")
        await bot.pause()
        if batch_every > 0 and saved > 0 and saved % batch_every == 0 and i < len(candidates):
            await human_wait(*batch_pause, "oddech po paczce kontaktów")
    return saved, with_phone


async def run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    looping = args.loop or (not args.once and not (args.query or "").strip())
    if args.once:
        looping = False

    query_one = (args.query or cfg.get("query") or "").strip()
    if not looping and not query_one:
        print("Podaj frazę: --query \"...\" albo odpal --loop (nisze z config.yaml)")
        return 2

    max_pages = int(args.pages or cfg.get("max_pages") or 3)
    max_sellers = int(args.limit or cfg.get("max_sellers") or 12)
    delay_min = float(cfg.get("delay_min_sec") or 4.0)
    delay_max = float(cfg.get("delay_max_sec") or 12.0)
    batch_every = int(cfg.get("batch_every") or 7)
    q_pause = (
        float(cfg.get("pause_between_queries_min") or 90),
        float(cfg.get("pause_between_queries_max") or 240),
    )
    batch_pause = (
        float(cfg.get("pause_after_batch_min") or 40),
        float(cfg.get("pause_after_batch_max") or 120),
    )
    round_pause = (
        float(cfg.get("pause_between_rounds_min") or 420),
        float(cfg.get("pause_between_rounds_max") or 900),
    )

    headless, use_chrome = resolve_headless(args, cfg)
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

    from allegro_bot import leads_db

    print("=== Allegro seller bot ===")
    print(f"tryb={'pętla (nie wyłącza się)' if looping else 'jeden run'}")
    print(f"profil={profile_dir}")
    print(f"wynik CSV={csv_path}")
    if leads_db.configured():
        print("zapis do panelu WWW: PostgreSQL TAK")
    else:
        print("[!] brak DATABASE_URL – panel nic nie zobaczy")
    if headless:
        print("[!] headless – Captcha Allegro może zablokować listing.")

    pw = None
    context = None
    browser = None

    async def boot():
        nonlocal pw, context, browser
        pw, context, browser = await open_context(
            profile_dir=profile_dir,
            headless=headless,
            use_system_chrome=use_chrome,
        )
        return context.pages[0] if context.pages else await context.new_page()

    async def shutdown():
        nonlocal pw, context, browser
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        pw = context = browser = None

    try:
        page = await boot()
        if not looping:
            saved, with_phone = await scrape_query(
                page=page,
                bot=bot,
                storage=storage,
                known=known,
                query=query_one,
                max_pages=max_pages,
                limit=max_sellers,
                batch_every=0,
                batch_pause=batch_pause,
                headless_mode=headless,
            )
            storage.save_state(
                ROOT / "data" / "last_run.json",
                {
                    "query": query_one,
                    "saved": saved,
                    "with_phone": with_phone,
                    "csv": str(csv_path),
                },
            )
            print(f"=== Koniec: zapisano {saved}, z telefonem {with_phone} ===")
            return 0

        pool = query_pool(cfg, query_one or None)
        print(f"nisze w puli: {len(pool)} – Ctrl+C żeby zatrzymać")
        round_no = 0
        queries_done = 0
        while True:
            round_no += 1
            order = pool[:]
            random.shuffle(order)
            print(f"\n=== runda {round_no} ({len(order)} fraz) ===", flush=True)
            for qi, query in enumerate(order, 1):
                pages_now = random.randint(1, max(1, max_pages))
                limit_now = random.randint(max(4, max_sellers // 2), max_sellers)
                print(
                    f"\n--- {query!r}  strony={pages_now}  limit={limit_now} "
                    f"({qi}/{len(order)}) ---",
                    flush=True,
                )
                try:
                    saved, with_phone = await scrape_query(
                        page=page,
                        bot=bot,
                        storage=storage,
                        known=known,
                        query=query,
                        max_pages=pages_now,
                        limit=limit_now,
                        batch_every=batch_every,
                        batch_pause=batch_pause,
                        headless_mode=headless,
                    )
                    print(f"=== {query!r}: +{saved} (tel {with_phone}) ===", flush=True)
                    storage.save_state(
                        ROOT / "data" / "last_run.json",
                        {
                            "query": query,
                            "saved": saved,
                            "with_phone": with_phone,
                            "round": round_no,
                            "loop": True,
                        },
                    )
                except Exception as e:
                    print(f"[!] błąd frazy {query!r}: {type(e).__name__}: {e}", flush=True)
                    print("[!] restartuję przeglądarkę…", flush=True)
                    await shutdown()
                    await human_wait(45, 120, "po błędzie")
                    page = await boot()
                queries_done += 1
                if queries_done % 8 == 0:
                    print("[!] odświeżam sesję Chrome (pamięć / cookies)", flush=True)
                    await shutdown()
                    await human_wait(25, 70, "przed nową sesją")
                    page = await boot()
                if qi < len(order):
                    await human_wait(*q_pause, f"zmiana niszy po {query!r}")
            await human_wait(*round_pause, f"dłuższa przerwa po rundzie {round_no}")
    except KeyboardInterrupt:
        print("\n[stop] przerwane (Ctrl+C) – bot zatrzymany ręcznie", flush=True)
        return 0
    except Exception as e:
        print(f"BŁĄD FATALNY: {type(e).__name__}: {e}", flush=True)
        return 1
    finally:
        await shutdown()


def main() -> None:
    args = build_parser().parse_args()
    try:
        raise SystemExit(asyncio.run(run(args)))
    except RuntimeError as e:
        if "Event loop is closed" in str(e):
            print("BŁĄD: Event loop is closed (zwykle po padzie Chrome bez DISPLAY).")
            raise SystemExit(1) from e
        raise


if __name__ == "__main__":
    main()
