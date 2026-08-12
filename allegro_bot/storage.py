from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

from allegro_bot.models import SellerLead

CSV_FIELDS = [
    "login",
    "company_name",
    "phone",
    "email",
    "nip",
    "address",
    "seller_url",
    "offer_url",
    "query",
    "raw_note",
]


class Storage:
    def __init__(self, csv_path: Path, sqlite_path: Path | None = None) -> None:
        self.csv_path = csv_path
        self.sqlite_path = sqlite_path
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        if self.sqlite_path:
            self.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()

    def _init_db(self) -> None:
        assert self.sqlite_path
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sellers (
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
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()

    def known_logins(self) -> set[str]:
        known: set[str] = set()
        if self.csv_path.exists():
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    login = (row.get("login") or "").strip()
                    if login:
                        known.add(login.lower())
        if self.sqlite_path and self.sqlite_path.exists():
            with sqlite3.connect(self.sqlite_path) as conn:
                for (login,) in conn.execute("SELECT login FROM sellers"):
                    if login:
                        known.add(login.lower())
        return known

    def save(self, lead: SellerLead) -> None:
        write_header = not self.csv_path.exists() or self.csv_path.stat().st_size == 0
        with self.csv_path.open("a", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
            if write_header:
                w.writeheader()
            w.writerow({k: getattr(lead, k) for k in CSV_FIELDS})

        if not self.sqlite_path:
            return
        with sqlite3.connect(self.sqlite_path) as conn:
            conn.execute(
                """
                INSERT INTO sellers (
                    login, company_name, phone, email, nip, address,
                    seller_url, offer_url, query, raw_note, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(login) DO UPDATE SET
                    company_name=excluded.company_name,
                    phone=COALESCE(excluded.phone, sellers.phone),
                    email=COALESCE(excluded.email, sellers.email),
                    nip=COALESCE(excluded.nip, sellers.nip),
                    address=COALESCE(excluded.address, sellers.address),
                    seller_url=COALESCE(excluded.seller_url, sellers.seller_url),
                    offer_url=COALESCE(excluded.offer_url, sellers.offer_url),
                    query=excluded.query,
                    raw_note=excluded.raw_note,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    lead.login,
                    lead.company_name,
                    lead.phone,
                    lead.email,
                    lead.nip,
                    lead.address,
                    lead.seller_url,
                    lead.offer_url,
                    lead.query,
                    lead.raw_note,
                ),
            )
            conn.commit()

    def save_state(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
