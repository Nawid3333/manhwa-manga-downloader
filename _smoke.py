"""Smoke test for the new site drivers (no downloads, just discovery)."""
import asyncio
import sys

sys.path.insert(0, ".")


async def test_nelomanga() -> None:
    from src.sites import nelomanga

    url = "https://www.nelomanga.net/manga/solo-leveling"
    print("== nelomanga ==")
    print("classify: chapter?", nelomanga.is_chapter_url(url), "list?", nelomanga.is_list_url(url))
    async with nelomanga.client() as c:
        links = await nelomanga.fetch_all_chapter_links(c, url)
        print("chapters:", len(links), "first:", links[0], "last:", links[-1])
        # fractional + trailing-zero sanity
        labels = sorted({nelomanga.chapter_label(u) for u, _ in links})
        print("has 194.1:", "194.1" in labels, "has 179.0:", "179.0" in labels, "has 3.1:", "3.1" in labels)
        urls = await nelomanga.fetch_image_urls(c, links[-1][0])  # oldest chapter
        print("oldest chapter", links[-1][0].rsplit("/", 1)[-1], "->", len(urls), "imgs, first:", urls[0] if urls else None, "last:", urls[-1] if urls else None)
        urls200 = await nelomanga.fetch_image_urls(c, links[0][0])
        print("newest chapter", links[0][0].rsplit("/", 1)[-1], "->", len(urls200), "imgs")
        frac = [u for u, n in links if not float(n).is_integer()]
        if frac:
            fu = await nelomanga.fetch_image_urls(c, frac[0])
            print("fractional", frac[0].rsplit("/", 1)[-1], "->", len(fu), "imgs")


async def test_mgread() -> None:
    from src.sites import mgread

    url = "https://mgread.io/manga/solo-leveling/"
    print("== mgread ==")
    print("classify: chapter?", mgread.is_chapter_url(url), "list?", mgread.is_list_url(url))
    async with mgread.client() as c:
        links = await mgread.fetch_all_chapter_urls(c, url)
        print("chapters:", len(links), "first:", links[0], "last:", links[-1])
        urls = await mgread.fetch_image_urls(c, links[0][0])
        print("oldest chapter", links[0][0], "->", len(urls), "imgs, first:", urls[0] if urls else None)
        folder = mgread.chapter_folder_name(links[0][0])
        print("folder:", folder)


async def main() -> None:
    await test_nelomanga()
    await test_mgread()


asyncio.run(main())