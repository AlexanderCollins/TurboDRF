"""Screenshot pages of the built site (bun run preview) for review.

    tools/.venv/bin/python tools/shot.py http://localhost:4329 out_dir / /docs/ ...
"""
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

base, out, *paths = sys.argv[1:]
Path(out).mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    browser = p.chromium.launch()
    for width, height, tag in [(1440, 900, "desktop"), (390, 844, "mobile")]:
        page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
        for path in paths:
            page.goto(base + path, wait_until="networkidle")
            # Reveal everything that animates in on scroll.
            page.evaluate("""() => { document.querySelectorAll('[data-reveal],[data-draw]').forEach(e => e.classList.add('is-in')); }""")
            page.wait_for_timeout(1600)
            name = (path.strip("/").replace("/", "_") or "home") + f"_{tag}.png"
            page.screenshot(path=f"{out}/{name}", full_page=True)
            print(name)
        page.close()
    browser.close()
