"""Kompatybilność wsteczna: uvicorn webapp:app."""
from app.main import app

__all__ = ["app"]
