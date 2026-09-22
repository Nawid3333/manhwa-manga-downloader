"""Benchmark: JPEG conversion throughput on this machine.

Generates synthetic manga-like images (tall, noisy, mixed content), then
times CPU conversion at several batch sizes. Built when the GPU backend was
still on the table; after it measured plain CPU encode at 308 img/s (vs 189
with optimize+progressive, identical pixel accuracy), the GPU was rejected
outright -- see the header of src/convert.py. Kept so the numbers stay
reproducible and any future backend change gets benchmarked the same way.

Usage (from the repo root):
  python -m tests.benchmark_convert [num_images] [batch_sizes_csv]
e.g.
  python -m tests.benchmark_convert 400 10,50,100,400
"""

from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

from PIL import Image

sys.path.insert(0, ".")
from src.convert import _jpeg_dest, run_cpu  # noqa: E402


def make_test_images(dir_path: Path, count: int, width: int = 800, height: int = 1200) -> list[Path]:
    """Create noisy PNGs (worst case: slow to decode, big)."""
    import numpy as np

    dir_path.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(42)
    base = rng.integers(0, 255, (height, width, 3), dtype=np.uint8)
    # manga-ish: mostly white with dark line art
    base = np.clip(base // 3 + 170, 0, 255).astype(np.uint8)
    paths = []
    for i in range(count):
        arr = base.copy()
        arr[:, ::17] = (arr[:, ::17] * 0.4).astype(np.uint8)  # vertical lines
        img = Image.fromarray(arr, "RGB")
        p = dir_path / f"img_{i:04d}.png"
        img.save(p, format="PNG", compress_level=1)  # fast write, big file
        paths.append(p)
    return paths


def bench_cpu(files: list[Path]) -> float:
    start = time.perf_counter()
    stats = run_cpu(files)
    dt = time.perf_counter() - start
    rate = len(files) / dt if dt else 0.0
    print(f"    cpu : {stats.converted} ok, {stats.failed} failed, {dt:.2f}s ({rate:.1f} img/s)")
    return dt


def restore_png(files: list[Path]) -> None:
    """After a bench pass the files are .jpg; rename back for the next pass."""
    for jpg in files:
        dest = _jpeg_dest(jpg)
        if dest.exists():
            dest.rename(jpg)


def main() -> None:
    num = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    sizes = [int(s) for s in (sys.argv[2].split(",") if len(sys.argv) > 2 else ["10", "50", "100", "120"])]
    workdir = Path("_bench_imgs")
    if workdir.exists():
        shutil.rmtree(workdir)

    print(f"Generating {num} test images...")
    files = make_test_images(workdir, num)
    total_mb = sum(f.stat().st_size for f in files) / 1e6
    print(f"Source size: {total_mb:.1f} MB")

    print("\nCPU (all cores)")
    for n in sizes:
        batch = files[:n]
        print(f"  batch {n}:")
        bench_cpu(batch)
        restore_png(batch)

    shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
