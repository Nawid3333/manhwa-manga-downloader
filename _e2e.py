"""End-to-end download test: 1-2 chapters per site (throwaway)."""
import asyncio
import shutil
import sys
from pathlib import Path

sys.path.insert(0, ".")


async def test_nelomanga() -> None:
    from src.sites import nelomanga

    out = Path("series_nelo_test")
    if out.exists():
        shutil.rmtree(out)
    stats = await nelomanga.download_series_url(
        "https://www.nelomanga.net/manga/solo-leveling",
        out,
        chapters=[200],
    )
    print("nelomanga stats:", stats)
    for f in sorted(out.rglob("*"))[:5]:
        print("  ", f)


async def test_mgread() -> None:
    from src.sites import mgread

    out = Path("series_mgread_test")
    if out.exists():
        shutil.rmtree(out)
    stats = await mgread.download_series_url(
        "https://mgread.io/manga/solo-leveling/",
        out,
        chapters=[1],
    )
    print("mgread stats:", stats)
    for f in sorted(out.rglob("*"))[:5]:
        print("  ", f)


async def main() -> None:
    await test_nelomanga()
    await test_mgread()


asyncio.run(main())