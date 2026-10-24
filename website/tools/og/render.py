"""Render the social cards (public/og/<card>.jpg, 1200x630) and the icons.

    tools/.venv/bin/python tools/og/render.py

Cards are drawn from template.html at 2x and scaled down so text stays crisp.
Icons (favicon.ico, apple-touch-icon, 192/512 PNGs) come from public/brand/icon.svg.
"""
import io
import pathlib

from PIL import Image
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).resolve().parent
SITE = HERE.parent.parent
PUBLIC = SITE / "public"
CARDS = ["home", "docs"]

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1200, "height": 630}, device_scale_factor=2)
    (PUBLIC / "og").mkdir(exist_ok=True)
    for card in CARDS:
        page.goto((HERE / "template.html").as_uri() + f"?card={card}")
        page.wait_for_timeout(400)
        shot = Image.open(io.BytesIO(page.screenshot())).convert("RGB").resize((1200, 630), Image.LANCZOS)
        shot.save(PUBLIC / "og" / f"{card}.jpg", quality=86, optimize=True, progressive=True)
        print(card, (PUBLIC / "og" / f"{card}.jpg").stat().st_size // 1024, "KB")

    svg = (PUBLIC / "brand" / "icon.svg").read_text()
    icon = browser.new_page(viewport={"width": 512, "height": 512})
    icon.set_content(f'<html><body style="margin:0;background:transparent">{svg.replace("<svg ", "<svg width=512 height=512 ", 1)}</body></html>')
    big = Image.open(io.BytesIO(icon.screenshot(omit_background=True))).convert("RGBA")
    browser.close()

big.save(PUBLIC / "brand" / "icon-512.png")
big.resize((192, 192), Image.LANCZOS).save(PUBLIC / "brand" / "icon-192.png")
# iOS draws its own rounded corners on an opaque square.
touch = Image.new("RGBA", (180, 180), (13, 18, 20, 255))
touch.alpha_composite(big.resize((180, 180), Image.LANCZOS))
touch.convert("RGB").save(PUBLIC / "brand" / "apple-touch-icon.png")
big.save(PUBLIC / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48)])
print("icons ok")
