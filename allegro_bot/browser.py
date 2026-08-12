from __future__ import annotations

from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright


async def open_context(
    *,
    profile_dir: Path,
    headless: bool = False,
    use_system_chrome: bool = True,
) -> tuple[object, BrowserContext]:
    """Otwiera trwały kontekst przeglądarki (cookie / Captcha zostają w profilu)."""
    profile_dir.mkdir(parents=True, exist_ok=True)
    pw = await async_playwright().start()
    kwargs = {
        "locale": "pl-PL",
        "viewport": {"width": 1400, "height": 900},
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if use_system_chrome:
        try:
            context = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                channel="chrome",
                **kwargs,
            )
            return pw, context
        except Exception:
            pass
    context = await pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    return pw, context


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
