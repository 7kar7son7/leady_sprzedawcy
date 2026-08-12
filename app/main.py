from __future__ import annotations

import asyncio
import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "sellers.csv"
STATE_PATH = DATA / "last_run.json"
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"

# job dłuższy niż to → uznaj za zawieszony
STUCK_AFTER_SEC = 10 * 60

templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="Allegro Seller Leads")

_job_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "query": None,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "log": [],
    "result": None,
}
_cancel = threading.Event()


def _log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    with _job_lock:
        _job["log"] = (_job.get("log") or [])[-120:] + [line]
    print(line, flush=True)


def load_config() -> dict:
    path = CONFIG_PATH if CONFIG_PATH.exists() else EXAMPLE_CONFIG
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def read_leads(limit: int = 500) -> list[dict]:
    if not CSV_PATH.exists():
        return []
    with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    rows.reverse()
    return rows[:limit]


def lead_stats(rows: list[dict]) -> dict:
    with_phone = sum(1 for r in rows if (r.get("phone") or "").strip())
    return {"total": len(rows), "with_phone": with_phone}


def _job_age_sec() -> float | None:
    started = _job.get("started_at")
    if not started:
        return None
    try:
        t0 = datetime.fromisoformat(started)
        if t0.tzinfo is None:
            t0 = t0.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t0).total_seconds()
    except Exception:
        return None


def clear_stuck_job_if_needed() -> None:
    """Odblokuj panel gdy job wisi w RUN bez końca (np. hang Chromium)."""
    with _job_lock:
        if not _job.get("running"):
            return
        age = _job_age_sec()
        if age is None or age < STUCK_AFTER_SEC:
            return
        _job["running"] = False
        _job["finished_at"] = datetime.now(timezone.utc).isoformat()
        _job["error"] = (
            f"Zawieszony po {int(age)}s (prawdopodobnie Chromium/Captcha). "
            "Kliknij Stop albo spróbuj ponownie po Redeploy z nowym kodem."
        )
        _job["log"] = (_job.get("log") or []) + [
            f"{datetime.now().strftime('%H:%M:%S')} AUTO-STOP: job zawieszony"
        ]
    _cancel.set()


def _run_bot_sync(query: str, pages: int, limit: int) -> None:
    from allegro_bot.browser import open_context
    from allegro_bot.scraper import AllegroSellerBot
    from allegro_bot.storage import Storage

    cfg = load_config()
    delay_min = float(cfg.get("delay_min_sec") or 1.5)
    delay_max = float(cfg.get("delay_max_sec") or 3.5)
    profile_dir = ROOT / str(cfg.get("browser_profile_dir") or "browser_profile")
    csv_path = ROOT / str(cfg.get("output_csv") or "data/sellers.csv")
    sqlite_path = ROOT / str(cfg.get("output_sqlite") or "data/sellers.sqlite")
    _cancel.clear()

    async def _go() -> None:
        storage = Storage(csv_path, sqlite_path)
        known = storage.known_logins()
        bot = AllegroSellerBot(
            delay_min=delay_min,
            delay_max=delay_max,
            max_sellers=limit,
        )
        pw = context = browser = None
        try:
            _log(f"start: query={query!r} pages={pages} limit={limit}")
            _log("uruchamiam Chromium (max 75s)…")
            try:
                pw, context, browser = await asyncio.wait_for(
                    open_context(
                        profile_dir=profile_dir,
                        headless=True,
                        use_system_chrome=False,
                        launch_timeout_ms=60_000,
                    ),
                    timeout=75,
                )
            except Exception as e:
                raise RuntimeError(
                    f"Przeglądarka nie wystartowała: {e}. "
                    "Na hostingu potrzeba: playwright install --with-deps chromium"
                ) from e
            _log("Chromium OK – idę na listing Allegro…")
            if _cancel.is_set():
                _log("anulowano")
                return

            page = context.pages[0] if context.pages else await context.new_page()
            candidates = await bot.collect_from_listing(
                page,
                query,
                max_pages=pages,
                known_logins=known,
                headless_mode=True,
                log_fn=_log,
            )
            if _cancel.is_set():
                _log("anulowano po listingu")
                return

            _log(f"znaleziono sprzedawców: {len(candidates)}")
            if not candidates:
                _log(
                    "0 sprzedawców – często Captcha DataDome na hostingu (headless). "
                    "Spróbuj lokalnie z oknem albo inną frazę."
                )

            saved = 0
            with_phone = 0
            for i, row in enumerate(candidates, 1):
                if _cancel.is_set():
                    _log("anulowano w trakcie ofert")
                    break
                login = row["login"]
                _log(f"[{i}/{len(candidates)}] {login}")
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
                        _log(f"  OK tel={lead.phone}")
                    else:
                        _log("  brak publicznego telefonu")
                except Exception as e:
                    _log(f"  err: {e}")
                await bot.pause()

            storage.save_state(
                STATE_PATH,
                {
                    "query": query,
                    "candidates": len(candidates),
                    "saved": saved,
                    "with_phone": with_phone,
                    "csv": str(csv_path),
                },
            )
            _job["result"] = {
                "saved": saved,
                "with_phone": with_phone,
                "candidates": len(candidates),
            }
            _log(f"KONIEC: kandydaci={len(candidates)} zapisano={saved} z_tel={with_phone}")
        finally:
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

    try:
        asyncio.run(_go())
    except Exception as e:
        _job["error"] = f"{type(e).__name__}: {e}"
        _log(f"FATAL: {_job['error']}")
    finally:
        with _job_lock:
            _job["running"] = False
            _job["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_job(query: str, pages: int, limit: int) -> bool:
    clear_stuck_job_if_needed()
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(
            {
                "running": True,
                "query": query,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "finished_at": None,
                "error": None,
                "log": [],
                "result": None,
            }
        )
    t = threading.Thread(
        target=_run_bot_sync,
        args=(query, pages, limit),
        daemon=True,
        name="allegro-bot",
    )
    t.start()
    return True


def stop_job() -> None:
    _cancel.set()
    with _job_lock:
        if _job.get("running"):
            _job["running"] = False
            _job["finished_at"] = datetime.now(timezone.utc).isoformat()
            _job["error"] = _job.get("error") or "Zatrzymane ręcznie (Stop)."
            _job["log"] = (_job.get("log") or []) + [
                f"{datetime.now().strftime('%H:%M:%S')} STOP kliknięty"
            ]


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "leady-sprzedawcy"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    clear_stuck_job_if_needed()
    all_rows: list[dict] = []
    if CSV_PATH.exists():
        with CSV_PATH.open("r", encoding="utf-8-sig", newline="") as f:
            all_rows = list(csv.DictReader(f))
    rows = list(reversed(all_rows))[:300]
    stats = {
        "total": len(all_rows),
        "with_phone": sum(1 for r in all_rows if (r.get("phone") or "").strip()),
    }
    cfg = load_config()
    age = _job_age_sec()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "rows": rows,
            "stats": stats,
            "job": _job,
            "job_age_sec": int(age) if age is not None else None,
            "default_query": (cfg.get("query") or "").strip(),
            "csv_exists": CSV_PATH.exists(),
        },
    )


@app.get("/api/status")
async def api_status() -> JSONResponse:
    clear_stuck_job_if_needed()
    return JSONResponse(
        {
            "job": _job,
            "job_age_sec": _job_age_sec(),
            "stats": lead_stats(read_leads(10_000)),
        }
    )


@app.get("/download.csv")
async def download_csv():
    if not CSV_PATH.exists():
        return HTMLResponse("Brak pliku sellers.csv – najpierw odpal scrapowanie.", status_code=404)
    return FileResponse(
        CSV_PATH,
        media_type="text/csv",
        filename="sellers.csv",
    )


@app.post("/run")
async def run_bot(
    query: str = Form(...),
    pages: int = Form(2),
    limit: int = Form(30),
):
    q = (query or "").strip()
    if not q:
        return RedirectResponse("/?err=query", status_code=303)
    pages = max(1, min(int(pages), 20))
    limit = max(1, min(int(limit), 500))
    ok = start_job(q, pages, limit)
    if not ok:
        return RedirectResponse("/?err=busy", status_code=303)
    return RedirectResponse("/?started=1", status_code=303)


@app.post("/stop")
async def stop_bot():
    stop_job()
    return RedirectResponse("/?stopped=1", status_code=303)


def create_app() -> FastAPI:
    DATA.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and EXAMPLE_CONFIG.exists():
        CONFIG_PATH.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return app


create_app()
