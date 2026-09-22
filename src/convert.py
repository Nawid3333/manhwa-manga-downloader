"""Post-download image conversion to JPEG.

Target format is always JPEG. After a run, any non-JPEG image (webp/png/...)
under the output tree is re-encoded in place and the source deleted.

CPU-only by decision (2026-09-22): the workload is codec-bound (decode +
libjpeg encode), GPUs have no JPEG encoder to offer, and the AMD RX 9070 XT
has no usable compute runtime on this Python (no torch-directml wheels for
3.14, ROCm judged overkill for a codec job). Measured on 400 real webp
pages, all cores: plain encode 308 img/s vs 189 img/s with
optimize+progressive, with identical pixel accuracy (mean abs err
0.591/255 for both). So we encode plain: speed matters more than ~10%
extra file size for downloaded manga.
"""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageOps

from term import cerror, cinfo, csuccess, cwarning

# --- settings (see config.py for the user-facing knobs) ---------------------
JPEG_QUALITY = 90

NON_JPEG_EXTS = {".webp", ".png", ".gif", ".bmp", ".tif", ".tiff", ".avif"}
JPEG_EXTS = {".jpg", ".jpeg", ".jfif"}


@dataclass
class ConvertStats:
    converted: int = 0
    failed: int = 0
    bytes_before: int = 0
    bytes_after: int = 0
    errors: list[str] = field(default_factory=list)


def collect_non_jpeg(root: Path) -> list[Path]:
    """All images under root whose extension (or content) is not JPEG."""
    if not root.exists():
        return []
    candidates: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        ext = p.suffix.lower()
        if ext in {".part", ".tmp"}:
            continue
        if ext in NON_JPEG_EXTS:
            candidates.append(p)
        elif ext in JPEG_EXTS:
            continue  # already jpeg by name
        elif not ext and _is_probable_image(p):
            # extensionless blobs: decide by content
            candidates.append(p)
    return candidates


def _is_probable_image(path: Path) -> bool:
    try:
        with path.open("rb") as f:
            head = f.read(16)
    except OSError:
        return False
    return head.startswith((b"\xff\xd8", b"\x89PNG", b"RIFF", b"GIF8", b"II*\x00", b"MM\x00*"))


def _jpeg_dest(src: Path) -> Path:
    return src.with_suffix(".jpg")


# ---------------------------------------------------------------------------
# CPU backend (worker must be module-level for Windows spawn pickling)
# ---------------------------------------------------------------------------


def _convert_one(args: tuple[str, int]) -> tuple[bool, str, int, int, str]:
    """Convert one image to JPEG. Returns (ok, path, before, after, message)."""
    src_path, quality = args
    src = Path(src_path)
    dest = _jpeg_dest(src)
    try:
        Image.MAX_IMAGE_PIXELS = None  # manga strips exceed the default cap
        before = src.stat().st_size
        with src.open("rb") as f:
            img = Image.open(f)
            img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        tmp = dest.with_suffix(".jpg.part")
        img.save(tmp, format="JPEG", quality=quality)
        tmp.replace(dest)
        after = dest.stat().st_size
        if src.exists() and src != dest:
            src.unlink()
        return (True, str(src), before, after, "")
    except Exception as exc:  # noqa: BLE001 - report per-file, keep going
        return (False, str(src), 0, 0, f"{type(exc).__name__}: {exc}")


def run_cpu(files: list[Path], *, quality: int = JPEG_QUALITY, workers: int | None = None) -> ConvertStats:
    """Convert files to JPEG across a process pool (all cores by default)."""
    stats = ConvertStats()
    if not files:
        return stats
    workers = workers or min(32, (os.cpu_count() or 4))
    with ProcessPoolExecutor(max_workers=workers) as pool:
        for ok, path_s, before, after, msg in pool.map(_convert_one, ((str(p), quality) for p in files), chunksize=8):
            if ok:
                stats.converted += 1
                stats.bytes_before += before
                stats.bytes_after += after
            else:
                stats.failed += 1
                stats.errors.append(f"{path_s}: {msg}")
    return stats


def convert_tree(
    root: Path,
    *,
    quality: int = JPEG_QUALITY,
    workers: int | None = None,
    quiet: bool = False,
) -> ConvertStats:
    """Convert every non-JPEG image under root to JPEG (in place).

    Deletes sources after successful conversion; the final state of the tree
    is JPEG-only. Files already named .jpg/.jpeg are left untouched.
    """
    files = collect_non_jpeg(root)
    if not files:
        return ConvertStats()

    if not quiet:
        cinfo(f"Converting {len(files)} non-JPEG image(s) -> JPEG (cpu, {os.cpu_count()} cores)")

    stats = run_cpu(files, quality=quality, workers=workers)
    if not quiet:
        msg = (
            f"Converted {stats.converted} image(s) to JPEG"
            + (f", {stats.failed} failed" if stats.failed else "")
            + f" (size {stats.bytes_before / 1e6:.1f} MB -> {stats.bytes_after / 1e6:.1f} MB)"
        )
        if stats.failed:
            cerror(msg)
        else:
            csuccess(msg)
        for err in stats.errors[:5]:
            cwarning(f"  {err}")
    return stats


if __name__ == "__main__":
    import sys

    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    convert_tree(target)
