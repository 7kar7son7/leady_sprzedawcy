from __future__ import annotations

import os
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright


def has_display() -> bool:
    """True jeśli jest GUI (X11/Wayland/Windows desktop)."""
    if os.name == "nt":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def running_in_container() -> bool:
    return Path("/.dockerenv").exists() or os.environ.get("KUBERNETES_SERVICE_HOST") is not None


async def open_context(
    *,
    profile_dir: Path,
    headless: bool | None = None,
    use_system_chrome: bool | None = None,
) -> tuple[object, BrowserContext]:
    """Otwiera trwały kontekst przeglądarki (cookie zostają w profilu).

    Na VPS/Docker bez $DISPLAY wymusza headless + Chromium Playwright
    (bez systemowego Chrome / bez X11).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)

    if headless is None:
        headless = not has_display()
    elif not headless and not has_display():
        print("[!] Brak $DISPLAY – wymuszam headless (kontener/VPS bez GUI).")
        headless = True

    if use_system_chrome is None:
        # W Dockerze zwykle nie ma Google Chrome + DISPLAY
        use_system_chrome = has_display() and not running_in_container()

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
    ]
    if headless:
        args.append("--headless=new")

    pw = await async_playwright().start()
    kwargs: dict = {
        "locale": "pl-PL",
        "viewport": {"width": 1400, "height": 900},
        "headless": headless,
        "args": args,
        "ignore_default_args": ["--enable-automation"],
    }

    print(
        f"[browser] headless={headless} system_chrome={use_system_chrome} "
        f"display={has_display()} container={running_in_container()}"
    )

    last_err: Exception | None = None
    if use_system_chrome:
        try:
            context = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chrome",
                **kwargs,
            )
            return pw, context
        except Exception as e:
            last_err = e
            print(f"[!] System Chrome niedostępny ({e}) – biorę Chromium Playwright.")

    try:
        context = await pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        return pw, context
    except Exception as e:
        await pw.stop()
        raise RuntimeError(
            f"Nie udało się uruchomić przeglądarki (headless={headless}). "
            f"Ostatni błąd: {e}. "
            f"Poprzedni (Chrome): {last_err}"
        ) from e


async def wait_until_listing_ready(page: Page, timeout_ms: int = 180_000) -> bool:
    """Czeka aż zniknie Captcha i pojawi się store state z ofertami."""
    try:
        await page.wait_for_function(
            """
            () => {
              const blocked = !!document.querySelector(
                'iframe[title*="DataDome"], iframe[src*="captcha-delivery"]'
              );
              if (blocked) return false;
              const html = document.documentElement.innerHTML;
              return html.includes('"__listing_StoreState"') && html.includes('"seller"');
            }
            """,
            timeout=timeout_ms,
        )
        return True
    except Exception:
        return False


async def dismiss_cookie_banner(page: Page) -> None:
    import re

    for text in ("zgadzam się", "akceptuję", "accept"):
        loc = page.get_by_role("button", name=re.compile(text, re.I))
        try:
            if await loc.count():
                await loc.first.click(timeout=1500)
                return
        except Exception:
            continue
    for sel in (
        'button[data-role="accept-consent"]',
        "#onetrust-accept-btn-handler",
    ):
        try:
            btn = page.locator(sel).first
            if await btn.count():
                await btn.click(timeout=1500)
                return
        except Exception:
            continue
