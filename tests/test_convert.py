"""Tests for src/convert.py: which files get picked up for conversion, and
that the CPU conversion path produces valid JPEGs and cleans up sources."""

from __future__ import annotations

from pathlib import Path

from PIL import Image

from src.convert import _is_probable_image, _jpeg_dest, collect_non_jpeg, convert_tree, run_cpu

JPEG_MAGIC = b"\xff\xd8\xff\xe0"


def _make_png(path: Path, size: tuple[int, int] = (4, 4)) -> None:
    Image.new("RGB", size, color=(200, 30, 30)).save(path, format="PNG")


# ---- collect_non_jpeg ----------------------------------------------------


def test_collect_non_jpeg_missing_root_returns_empty():
    assert collect_non_jpeg(Path("does/not/exist")) == []


def test_collect_non_jpeg_picks_up_known_non_jpeg_extensions(tmp_path: Path):
    _make_png(tmp_path / "page.png")
    (tmp_path / "page.webp").write_bytes(b"RIFF....WEBP")
    (tmp_path / "already.jpg").write_bytes(JPEG_MAGIC)
    (tmp_path / "in_progress.part").write_bytes(b"partial")

    found = {p.name for p in collect_non_jpeg(tmp_path)}
    assert found == {"page.png", "page.webp"}


def test_collect_non_jpeg_detects_extensionless_images_by_content(tmp_path: Path):
    (tmp_path / "blob").write_bytes(JPEG_MAGIC + b"0" * 20)
    (tmp_path / "readme").write_text("just text")

    found = {p.name for p in collect_non_jpeg(tmp_path)}
    assert found == {"blob"}


# ---- _is_probable_image / _jpeg_dest ---------------------------------------


def test_is_probable_image_true_for_known_magic_bytes(tmp_path: Path):
    path = tmp_path / "blob"
    path.write_bytes(JPEG_MAGIC + b"0" * 10)
    assert _is_probable_image(path) is True


def test_is_probable_image_false_for_plain_text(tmp_path: Path):
    path = tmp_path / "blob"
    path.write_text("hello")
    assert _is_probable_image(path) is False


def test_jpeg_dest_swaps_suffix():
    assert _jpeg_dest(Path("a/b/page.webp")) == Path("a/b/page.jpg")


# ---- run_cpu ---------------------------------------------------------------


def test_run_cpu_empty_list_is_a_noop():
    stats = run_cpu([])
    assert stats.converted == 0
    assert stats.failed == 0


def test_run_cpu_converts_png_to_jpeg_and_removes_source(tmp_path: Path):
    src = tmp_path / "page.png"
    _make_png(src)

    stats = run_cpu([src], workers=1)

    assert stats.converted == 1
    assert stats.failed == 0
    dest = tmp_path / "page.jpg"
    assert dest.exists()
    assert not src.exists()
    with Image.open(dest) as img:
        assert img.format == "JPEG"


def test_run_cpu_reports_failure_for_unreadable_file(tmp_path: Path):
    src = tmp_path / "broken.webp"
    src.write_bytes(b"this is not actually image data")

    stats = run_cpu([src], workers=1)

    assert stats.converted == 0
    assert stats.failed == 1
    assert stats.errors
    assert src.exists()  # left in place since conversion never succeeded


# ---- convert_tree (end-to-end) ----------------------------------------------


def test_convert_tree_leaves_only_jpegs_behind(tmp_path: Path):
    _make_png(tmp_path / "one.png")
    _make_png(tmp_path / "two.png")

    stats = convert_tree(tmp_path, workers=1, quiet=True)

    assert stats.converted == 2
    remaining_exts = {p.suffix for p in tmp_path.iterdir()}
    assert remaining_exts == {".jpg"}
