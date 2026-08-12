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

# katalog repo (rodzic pakietu app/)
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV_PATH = DATA / "sellers.csv"
STATE_PATH = DATA / "last_run.json"
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"

templates = Jinja2Templates(directory=str(ROOT / "templates"))

app = FastAPI(title="Allegro Seller Leads")

_job_lock = threading.Lock()
_job: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "error": None,
    "log": [],
    "result": None,
}


def _log(msg: str) -> None:
    line = f"{datetime.now().strftime('%H:%M:%S')} {msg}"
    _job["log"] = (_job.get("log") or [])[-80:] + [line]
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

    async def _go() -> None:
        storage = Storage(csv_path, sqlite_path)
        known = storage.known_logins()
        bot = AllegroSellerBot(
            delay_min=delay_min,
            delay_max=delay_max,
            max_sellers=limit,
        )
        pw = context = None
        try:
            _log("start przeglądarki (headless)…")
            pw, context = await open_context(
                profile_dir=profile_dir,
                headless=True,
                use_system_chrome=False,
            )
            page = context.pages[0] if context.pages else await context.new_page()
            candidates = await bot.collect_from_listing(
                page, query, max_pages=pages, known_logins=known
            )
            _log(f"kandydaci: {len(candidates)}")
            saved = 0
            with_phone = 0
            for i, row in enumerate(candidates, 1):
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
                        _log(f"  OK {lead.phone}")
                    else:
                        _log("  brak telefonu")
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
            _job["result"] = {"saved": saved, "with_phone": with_phone}
            _log(f"koniec: zapisano {saved}, z tel. {with_phone}")
        finally:
            if context is not None:
                try:
                    await context.close()
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
        _job["running"] = False
        _job["finished_at"] = datetime.now(timezone.utc).isoformat()


def start_job(query: str, pages: int, limit: int) -> bool:
    with _job_lock:
        if _job["running"]:
            return False
        _job.update(
            {
                "running": True,
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


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "leady-sprzedawcy"}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
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
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "rows": rows,
            "stats": stats,
            "job": _job,
            "default_query": cfg.get("query") or "wiertarka",
            "csv_exists": CSV_PATH.exists(),
        },
    )


@app.get("/api/status")
async def api_status() -> JSONResponse:
    return JSONResponse({"job": _job, "stats": lead_stats(read_leads(10_000))})


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


def create_app() -> FastAPI:
    DATA.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists() and EXAMPLE_CONFIG.exists():
        CONFIG_PATH.write_text(EXAMPLE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return app


create_app()
