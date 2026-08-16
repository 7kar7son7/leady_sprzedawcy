from __future__ import annotations

import asyncio
import csv
import io
import os
import secrets
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from allegro_bot import leads_db
from allegro_bot.statuses import DEFAULT_STATUS, STATUS_LABELS, STATUSES

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

DATA = ROOT / "data"
CSV_PATH = DATA / "sellers.csv"
STATE_PATH = DATA / "last_run.json"
CONFIG_PATH = ROOT / "config.yaml"
EXAMPLE_CONFIG = ROOT / "config.example.yaml"

STUCK_AFTER_SEC = 10 * 60
OPEN_PATHS = {"/login", "/health"}

PANEL_USER = (os.environ.get("PANEL_USER") or "admin").strip()
PANEL_PASSWORD = os.environ.get("PANEL_PASSWORD") or ""
SESSION_SECRET = (os.environ.get("SESSION_SECRET") or "").strip() or secrets.token_urlsafe(32)

templates = Jinja2Templates(directory=str(ROOT / "templates"))
templates.env.globals["status_labels"] = STATUS_LABELS
templates.env.globals["statuses"] = STATUSES

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
            if leads_db.configured():
                _log("zapis: PostgreSQL (panel WWW) + CSV lokalnie")
            else:
                _log("UWAGA: brak DATABASE_URL – tylko CSV, panel WWW nic nie zobaczy")
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
                    "Odpal bota lokalnie: python main.py --query \"…\""
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


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path in OPEN_PATHS:
        return await call_next(request)
    if request.session.get("user"):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return RedirectResponse("/login", status_code=303)


# SessionMiddleware na zewnątrz, żeby request.session było już w require_login
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=False,
    max_age=60 * 60 * 24 * 14,
)


@app.get("/health")
async def health() -> dict:
    db_ok = False
    db_err = None
    if leads_db.configured():
        try:
            leads_db.init_schema()
            db_ok = True
        except Exception as e:
            db_err = str(e)
    return {"ok": True, "service": "leady-sprzedawcy", "db": db_ok, "db_error": db_err}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> HTMLResponse:
    if request.session.get("user"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
) -> HTMLResponse:
    if not PANEL_PASSWORD:
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Brak PANEL_PASSWORD w środowisku serwera."},
            status_code=500,
        )
    def _eq(a: str, b: str) -> bool:
        if len(a) != len(b):
            return False
        return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))

    ok_user = _eq(username, PANEL_USER)
    ok_pass = _eq(password, PANEL_PASSWORD)
    if not (ok_user and ok_pass):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Zły login lub hasło."},
            status_code=401,
        )
    request.session["user"] = PANEL_USER
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, status: str = "") -> HTMLResponse:
    clear_stuck_job_if_needed()
    db_error = None
    rows: list[dict] = []
    stats = {
        "total": 0,
        "with_phone": 0,
        "with_email": 0,
        "by_status": {},
        "contacted": 0,
        "won": 0,
        "lost": 0,
        "conversion_pct": 0,
    }
    if leads_db.configured():
        try:
            stats = leads_db.stats()
            rows = leads_db.list_leads(status=status or None, limit=500)
        except Exception as e:
            db_error = str(e)
    else:
        db_error = "Brak DATABASE_URL – panel nie ma bazy."
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
            "filter_status": status,
            "db_error": db_error,
            "user": request.session.get("user"),
        },
    )


@app.get("/api/status")
async def api_status() -> JSONResponse:
    clear_stuck_job_if_needed()
    lead_stats = {"total": 0, "with_phone": 0}
    if leads_db.configured():
        try:
            lead_stats = leads_db.stats()
        except Exception:
            pass
    return JSONResponse(
        {
            "job": _job,
            "job_age_sec": _job_age_sec(),
            "stats": lead_stats,
        }
    )


@app.post("/api/lead/status")
async def api_lead_status(request: Request) -> JSONResponse:
    body = await request.json()
    login = str(body.get("login") or "").strip()
    status = str(body.get("status") or "").strip()
    note = body.get("note")
    try:
        ok = leads_db.update_status(
            login,
            status,
            str(note) if note is not None else None,
        )
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    if not ok:
        return JSONResponse({"ok": False, "error": "nie znaleziono leada"}, status_code=404)
    return JSONResponse({"ok": True, "login": login, "status": status})


@app.post("/api/lead/note")
async def api_lead_note(request: Request) -> JSONResponse:
    body = await request.json()
    login = str(body.get("login") or "").strip()
    note = str(body.get("note") or "")
    ok = leads_db.update_note(login, note)
    if not ok:
        return JSONResponse({"ok": False, "error": "nie znaleziono leada"}, status_code=404)
    return JSONResponse({"ok": True})


@app.get("/download.csv")
async def download_csv():
    if not leads_db.configured():
        return HTMLResponse("Brak bazy.", status_code=404)
    rows = leads_db.list_leads(limit=50_000)
    fields = [
        "login",
        "company_name",
        "phone",
        "email",
        "nip",
        "address",
        "status",
        "status_note",
        "seller_url",
        "offer_url",
        "query",
        "created_at",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for row in rows:
        w.writerow(row)
    return Response(
        content=buf.getvalue().encode("utf-8-sig"),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="sellers.csv"'},
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
    if leads_db.configured():
        try:
            leads_db.init_schema()
        except Exception as e:
            print(f"[!] PostgreSQL init: {e}", flush=True)
    return app


create_app()
