from __future__ import annotations

import asyncio
import os
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Page, async_playwright


def has_display() -> bool:
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
    launch_timeout_ms: int = 60_000,
) -> tuple[object, BrowserContext, Browser | None]:
    """Otwiera kontekst Chromium.

    W Dockerze: zwykły launch() (persistent_context często wisi na locku profilu).
    Zwraca (playwright, context, browser|None).
    """
    profile_dir.mkdir(parents=True, exist_ok=True)

    if headless is None:
        headless = not has_display()
    elif not headless and not has_display():
        print("[!] Brak $DISPLAY – wymuszam headless.")
        headless = True

    in_container = running_in_container() or not has_display()
    if use_system_chrome is None:
        use_system_chrome = has_display() and not in_container

    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--single-process",
    ]

    pw = await async_playwright().start()
    print(
        f"[browser] headless={headless} system_chrome={use_system_chrome} "
        f"container={in_container} profile={profile_dir}"
    )

    async def _launch_simple() -> tuple[BrowserContext, Browser]:
        launch_kwargs: dict = {
            "headless": headless,
            "args": args,
            "timeout": launch_timeout_ms,
        }
        if use_system_chrome:
            try:
                browser = await pw.chromium.launch(channel="chrome", **launch_kwargs)
            except Exception as e:
                print(f"[!] Chrome channel fail: {e} – Chromium Playwright")
                browser = await pw.chromium.launch(**launch_kwargs)
        else:
            browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            locale="pl-PL",
            viewport={"width": 1400, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        return context, browser

    # Docker / VPS: tylko prosty launch (nie hangs na persistent profile)
    if in_container:
        try:
            context, browser = await asyncio.wait_for(
                _launch_simple(), timeout=launch_timeout_ms / 1000 + 15
            )
            print("[browser] OK (launch)")
            return pw, context, browser
        except Exception as e:
            await pw.stop()
            raise RuntimeError(
                f"Chromium nie startuje w kontenerze: {e}. "
                "Sprawdź: playwright install --with-deps chromium"
            ) from e

    # Lokalnie z GUI: persistent profile (Captcha cookie)
    kwargs = {
        "locale": "pl-PL",
        "viewport": {"width": 1400, "height": 900},
        "headless": headless,
        "args": [a for a in args if a != "--single-process"],
        "timeout": launch_timeout_ms,
    }
    try:
        if use_system_chrome:
            try:
                context = await pw.chromium.launch_persistent_context(
                    str(profile_dir), channel="chrome", **kwargs
                )
                return pw, context, None
            except Exception as e:
                print(f"[!] Chrome persistent fail: {e}")
        context = await pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        return pw, context, None
    except Exception as e:
        print(f"[!] persistent fail ({e}) – fallback launch()")
        try:
            context, browser = await _launch_simple()
            return pw, context, browser
        except Exception as e2:
            await pw.stop()
            raise RuntimeError(f"Nie udało się uruchomić przeglądarki: {e2}") from e2


async def wait_until_listing_ready(page: Page, timeout_ms: int = 90_000) -> bool:
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
