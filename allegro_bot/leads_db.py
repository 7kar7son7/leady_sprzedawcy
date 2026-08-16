"""PostgreSQL – wspólna baza leadów (panel + bot lokalny)."""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from allegro_bot.models import SellerLead
from allegro_bot.statuses import DEFAULT_STATUS, STATUS_KEYS

TABLE = "allegro_seller_leads"

_pool: ConnectionPool | None = None
_schema_ready = False


def database_url() -> str:
    return (os.environ.get("DATABASE_URL") or "").strip()


def configured() -> bool:
    return bool(database_url())


def _pool_get() -> ConnectionPool:
    global _pool
    url = database_url()
    if not url:
        raise RuntimeError("Brak DATABASE_URL – ustaw w .env albo w env hostingu.")
    if _pool is None:
        _pool = ConnectionPool(
            conninfo=url,
            min_size=1,
            max_size=6,
            kwargs={"row_factory": dict_row, "autocommit": False},
            open=True,
        )
    return _pool


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    with _pool_get().connection() as conn:
        yield conn


def init_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {TABLE} (
                    login TEXT PRIMARY KEY,
                    company_name TEXT,
                    phone TEXT,
                    email TEXT,
                    nip TEXT,
                    address TEXT,
                    seller_url TEXT,
                    offer_url TEXT,
                    query TEXT,
                    raw_note TEXT,
                    status TEXT NOT NULL DEFAULT '{DEFAULT_STATUS}',
                    status_note TEXT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    status_updated_at TIMESTAMPTZ
                )
                """
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {TABLE}_status_idx ON {TABLE} (status)"
            )
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {TABLE}_created_idx ON {TABLE} (created_at DESC)"
            )
        conn.commit()
    _schema_ready = True


def known_logins() -> set[str]:
    if not configured():
        return set()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT login FROM {TABLE}")
            rows = cur.fetchall()
    return {(r["login"] or "").strip().lower() for r in rows if r.get("login")}


def upsert_lead(lead: SellerLead) -> None:
    """Zapis z bota. Statusu nie nadpisuje – ręczna praca w panelu zostaje."""
    init_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                INSERT INTO {TABLE} (
                    login, company_name, phone, email, nip, address,
                    seller_url, offer_url, query, raw_note, status,
                    created_at, updated_at
                ) VALUES (
                    %(login)s, %(company_name)s, %(phone)s, %(email)s, %(nip)s, %(address)s,
                    %(seller_url)s, %(offer_url)s, %(query)s, %(raw_note)s, %(status)s,
                    NOW(), NOW()
                )
                ON CONFLICT (login) DO UPDATE SET
                    company_name = COALESCE(EXCLUDED.company_name, {TABLE}.company_name),
                    phone = COALESCE(EXCLUDED.phone, {TABLE}.phone),
                    email = COALESCE(EXCLUDED.email, {TABLE}.email),
                    nip = COALESCE(EXCLUDED.nip, {TABLE}.nip),
                    address = COALESCE(EXCLUDED.address, {TABLE}.address),
                    seller_url = COALESCE(EXCLUDED.seller_url, {TABLE}.seller_url),
                    offer_url = COALESCE(EXCLUDED.offer_url, {TABLE}.offer_url),
                    query = COALESCE(EXCLUDED.query, {TABLE}.query),
                    raw_note = COALESCE(EXCLUDED.raw_note, {TABLE}.raw_note),
                    updated_at = NOW()
                """,
                {
                    "login": lead.login,
                    "company_name": lead.company_name,
                    "phone": lead.phone,
                    "email": lead.email,
                    "nip": lead.nip,
                    "address": lead.address,
                    "seller_url": lead.seller_url,
                    "offer_url": lead.offer_url,
                    "query": lead.query,
                    "raw_note": lead.raw_note,
                    "status": DEFAULT_STATUS,
                },
            )
        conn.commit()


def list_leads(status: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
    init_schema()
    sql = f"SELECT * FROM {TABLE}"
    params: dict[str, Any] = {"limit": limit}
    if status and status in STATUS_KEYS:
        sql += " WHERE status = %(status)s"
        params["status"] = status
    sql += " ORDER BY created_at DESC LIMIT %(limit)s"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
    return [_serialize(r) for r in rows]


def stats() -> dict[str, Any]:
    init_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM {TABLE}")
            total = int(cur.fetchone()["n"])
            cur.execute(
                f"""
                SELECT COUNT(*) AS n FROM {TABLE}
                WHERE NULLIF(TRIM(phone), '') IS NOT NULL
                """
            )
            with_phone = int(cur.fetchone()["n"])
            cur.execute(
                f"""
                SELECT COUNT(*) AS n FROM {TABLE}
                WHERE NULLIF(TRIM(email), '') IS NOT NULL
                """
            )
            with_email = int(cur.fetchone()["n"])
            cur.execute(
                f"SELECT status, COUNT(*) AS n FROM {TABLE} GROUP BY status"
            )
            by_status = {r["status"]: int(r["n"]) for r in cur.fetchall()}
    contacted = total - by_status.get(DEFAULT_STATUS, 0)
    won = by_status.get("skorzystal", 0)
    lost = by_status.get("nie_chce", 0)
    conversion = round(100.0 * won / contacted, 1) if contacted else 0.0
    return {
        "total": total,
        "with_phone": with_phone,
        "with_email": with_email,
        "by_status": by_status,
        "contacted": contacted,
        "won": won,
        "lost": lost,
        "conversion_pct": conversion,
    }


def update_status(login: str, status: str, status_note: str | None = None) -> bool:
    if status not in STATUS_KEYS:
        raise ValueError(f"Nieznany status: {status}")
    login = (login or "").strip()
    if not login:
        return False
    init_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET status = %(status)s,
                    status_note = COALESCE(%(status_note)s, status_note),
                    status_updated_at = NOW(),
                    updated_at = NOW()
                WHERE login = %(login)s
                """,
                {
                    "login": login,
                    "status": status,
                    "status_note": (status_note.strip() if status_note else None),
                },
            )
            n = cur.rowcount
        conn.commit()
    return n > 0


def update_note(login: str, status_note: str) -> bool:
    login = (login or "").strip()
    if not login:
        return False
    init_schema()
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {TABLE}
                SET status_note = %(status_note)s, updated_at = NOW()
                WHERE login = %(login)s
                """,
                {"login": login, "status_note": status_note.strip()},
            )
            n = cur.rowcount
        conn.commit()
    return n > 0


def _serialize(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in row.items():
        if isinstance(v, datetime):
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        else:
            out[k] = v
    return out
