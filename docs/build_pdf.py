"""Render docs/architecture.html -> architecture.pdf (repo root) with headless Chromium.

    python docs/build_pdf.py
"""
from __future__ import annotations

import asyncio
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "docs" / "architecture.html"
OUT = ROOT / "architecture.pdf"


async def main() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        await page.goto(SRC.as_uri(), wait_until="load")
        await page.pdf(path=str(OUT), format="A4", print_background=True, prefer_css_page_size=True)
        await browser.close()
    pages = len(re.findall(rb"/Type\s*/Page[^s]", OUT.read_bytes()))
    print(f"wrote {OUT} ({pages} pages)")


if __name__ == "__main__":
    asyncio.run(main())
