"""Benchmark: how much concurrency a site's image CDN actually tolerates.

Talks to a real site (unlike the hermetic pytest suite, which never touches
the network -- see tests/conftest.py). Ramps concurrency across a real
chapter's image URLs and reports latency + error/throttle rate at each
level, so IMAGE_CONCURRENCY (src/common.py) can be tuned against measured
server behavior instead of a guess. Built in response to
"downloaded file failed its integrity check" warnings showing up during real
runs -- the question this answers is: at what concurrency does that start
happening, and is it a hard block or a soft slowdown?

This is a diagnostic tool, not part of the app or the test suite (it isn't
named test_*.py, so pytest skips it, and it's excluded from pyright the same
way tests/ is -- see pyproject.toml).

Usage (from the repo root):
  python -m tests.benchmark_server <chapter-or-series-url> [levels_csv]
e.g.
  python -m tests.benchmark_server https://mgread.io/manga/foo/chapter-1/ 2,4,8,16,32,48

If a series (list) URL is given, the most recent chapter is benchmarked.
Requests fetch full image bodies (so truncation shows up the same way it
does in a real run) but only check status code + byte count against
Content-Length -- not a full Pillow decode -- to keep the tool itself fast.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

sys.path.insert(0, ".")
from config import site_for_url  # noqa: E402

DEFAULT_LEVELS = [2, 4, 8, 16, 32, 48]
REQUESTS_PER_LEVEL = 24  # sample size at each concurrency level
COOLDOWN_BETWEEN_LEVELS = 3.0
ABORT_ERROR_RATE = 0.5  # stop ramping once a level looks outright blocked
REQUEST_TIMEOUT = 30.0


@dataclass
class LevelResult:
    level: int
    latencies_ms: list[float] = field(default_factory=list)
    errors: int = 0
    truncated: int = 0
    total: int = 0

    @property
    def failures(self) -> int:
        return self.errors + self.truncated

    @property
    def error_rate(self) -> float:
        return self.failures / self.total if self.total else 0.0


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


async def _fetch_one(client: httpx.AsyncClient, url: str, sem: asyncio.Semaphore, result: LevelResult) -> None:
    async with sem:
        start = time.perf_counter()
        try:
            resp = await client.get(url, timeout=REQUEST_TIMEOUT)
            body = resp.content
            elapsed_ms = (time.perf_counter() - start) * 1000
            result.total += 1
            result.latencies_ms.append(elapsed_ms)
            if resp.status_code != 200:
                result.errors += 1
                return
            content_length = resp.headers.get("Content-Length")
            if (content_length is not None and len(body) < int(content_length)) or len(body) < 512:
                result.truncated += 1
        except httpx.HTTPError:
            result.total += 1
            result.latencies_ms.append((time.perf_counter() - start) * 1000)
            result.errors += 1


async def _bench_level(client: httpx.AsyncClient, urls: list[str], level: int) -> LevelResult:
    result = LevelResult(level=level)
    sem = asyncio.Semaphore(level)
    sample = [urls[i % len(urls)] for i in range(REQUESTS_PER_LEVEL)]
    start = time.perf_counter()
    await asyncio.gather(*(_fetch_one(client, url, sem, result) for url in sample))
    wall = time.perf_counter() - start
    ok = result.total - result.failures
    throughput = ok / wall if wall else 0.0
    mean = statistics.mean(result.latencies_ms) if result.latencies_ms else 0.0
    p50 = _percentile(result.latencies_ms, 0.50)
    p90 = _percentile(result.latencies_ms, 0.90)
    worst = max(result.latencies_ms) if result.latencies_ms else 0.0
    print(
        f"  concurrency {level:>3}: {ok:>2}/{result.total} ok  "
        f"error_rate={result.error_rate:5.1%}  "
        f"mean={mean:6.0f}ms  p50={p50:6.0f}ms  p90={p90:6.0f}ms  max={worst:6.0f}ms  "
        f"throughput={throughput:5.1f} img/s"
    )
    return result


async def _pick_chapter_url(url: str) -> str:
    site = site_for_url(url)
    if site is None:
        raise SystemExit(f"Unsupported site for {url!r}. Is this URL from a driver in src/sites/?")
    driver = site.driver
    kind = driver.classify(url)
    if kind == "chapter":
        return url
    if kind != "list":
        raise SystemExit(f"Could not classify {url!r} as a chapter or series URL.")
    chapters = await driver.count_chapters(url)
    if not chapters:
        raise SystemExit("No chapters found on that series page.")
    chapter_url, num = chapters[-1]
    print(f"Series URL given -- benchmarking the most recent chapter ({num:g}): {chapter_url}")
    return chapter_url


async def run(url: str, levels: list[int]) -> None:
    site = site_for_url(url)
    if site is None:
        raise SystemExit(f"Unsupported site for {url!r}. Is this URL from a driver in src/sites/?")
    driver = site.driver

    chapter_url = await _pick_chapter_url(url)
    async with driver.client() as client:
        image_urls = await driver.image_urls(client, chapter_url)
        if not image_urls:
            raise SystemExit("No image URLs found for that chapter.")
        print(f"{len(image_urls)} image URL(s) in this chapter; sampling {REQUESTS_PER_LEVEL} requests per level.\n")

        results: list[LevelResult] = []
        for level in levels:
            result = await _bench_level(client, image_urls, level)
            results.append(result)
            if result.error_rate >= ABORT_ERROR_RATE:
                print(
                    f"\n  error rate {result.error_rate:.0%} at concurrency {level} looks like a hard block "
                    "-- stopping before it escalates further."
                )
                break
            await asyncio.sleep(COOLDOWN_BETWEEN_LEVELS)

    print()
    healthy = [r for r in results if r.error_rate <= 0.05]
    if healthy:
        best = max(healthy, key=lambda r: r.level)
        print(f"Recommended IMAGE_CONCURRENCY for {site.key}: ~{best.level} (highest level tested with <=5% errors)")
    else:
        print(f"Every tested level already showed errors for {site.key} -- try lower levels, e.g. 1,2,4.")


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)
    url = sys.argv[1]
    levels = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else DEFAULT_LEVELS
    asyncio.run(run(url, levels))


if __name__ == "__main__":
    main()
