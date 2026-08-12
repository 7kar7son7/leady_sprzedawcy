from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class SellerLead:
    login: str
    company_name: str | None = None
    phone: str | None = None
    email: str | None = None
    nip: str | None = None
    address: str | None = None
    seller_url: str | None = None
    offer_url: str | None = None
    query: str | None = None
    raw_note: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        extra = row.pop("extra", {}) or {}
        for k, v in extra.items():
            row[f"extra_{k}"] = v
        return row
