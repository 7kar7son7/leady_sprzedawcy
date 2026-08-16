"""Statusy CRM przy leadzie (kontakt telefoniczny)."""

from __future__ import annotations

# klucz w bazie → etykieta w panelu
STATUSES: list[tuple[str, str]] = [
    ("nowy", "Nowy"),
    ("zadzwoniono", "Zadzwoniono"),
    ("przemysli", "Przemyśli"),
    ("sprawdzi", "Sprawdzi"),
    ("nie_odbiera", "Nie odbiera"),
    ("nie_chce", "Nie chce / nie skorzysta"),
    ("skorzystal", "Skorzystał"),
]

STATUS_LABELS = dict(STATUSES)
STATUS_KEYS = {k for k, _ in STATUSES}
DEFAULT_STATUS = "nowy"
