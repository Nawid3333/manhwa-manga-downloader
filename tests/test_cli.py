"""Tests for the non-interactive CLI (main.py) and parse_range: argument
parsing, the JSON result on stdout with messages kept on stderr, exit codes
(0 / 2 incomplete / 1 error) and --chapters/--out handling -- driven by an
in-test fake driver over httpx.MockTransport, never the network."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

import config
import main
import term
from src.base import SiteDriver
from term import parse_range

# ---- parse_range ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "total", "expected"),
    [
        ("all", 3, [1, 2, 3]),
        ("*", 2, [1, 2]),
        ("", 2, [1, 2]),
        ("5", 10, [5]),
        ("1-3", 10, [1, 2, 3]),
        ("1,3,5-7", 10, [1, 3, 5, 6, 7]),
        (" 2 , 2 , 1 ", 10, [1, 2]),
        ("8-20", 10, [8, 9, 10]),
        ("0-2", 10, [1, 2]),
        ("11", 10, []),
        ("3-3", 10, [3]),
    ],
)
def test_parse_range(text: str, total: int, expected: list[int]):
    assert parse_range(text, total) == expected


@pytest.mark.parametrize(
    ("text", "first", "last", "expected"),
    [
        ("0", 0, 3, [0]),
        ("all", 0, 2, [0, 1, 2]),
        ("150-152", 100, 151, [150, 151]),
        ("1-5", 100, 151, []),
    ],
)
def test_parse_range_bounds_are_chapter_numbers(text: str, first: int, last: int, expected: list[int]):
    assert parse_range(text, last, first) == expected


@pytest.mark.parametrize("text", ["abc", "1-", "-3", "3-1", "1,x", "1..3"])
def test_parse_range_rejects_garbage(text: str):
    with pytest.raises(ValueError):
        parse_range(text, 10)


def test_prompt_range_falls_back_to_all_on_garbage(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(term, "cinput", lambda *a, **k: "nope")
    assert term.prompt_range(3) == [1, 2, 3]
    monkeypatch.setattr(term, "cinput", lambda *a, **k: "2,3")
    assert term.prompt_range(3) == [2, 3]


# ---- argument parsing -------------------------------------------------------


def test_build_parser_defaults_and_flags():
    args = main.build_parser().parse_args([])
    assert args.url is None and args.chapters is None and args.out is None
    assert not args.yes and not args.json and not args.no_convert
    args = main.build_parser().parse_args(
        ["https://x/y", "--chapters", "1-3", "--out", "o", "-y", "--json", "--no-convert"]
    )
    assert args.url == "https://x/y" and args.chapters == "1-3" and args.out == Path("o")
    assert args.yes and args.json and args.no_convert


# ---- end-to-end runs with a fake driver -------------------------------------


class FakeDriver(SiteDriver):
    key = "fake"
    name = "Fake Site"
    domains = ("fake.test",)

    def __init__(self, handler, numbers=(1, 2, 3)):
        self._handler = handler
        self._numbers = numbers

    def client(self, **kwargs) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler))

    def classify(self, url: str) -> str:
        return "chapter" if "/chapter/" in url else "list"

    def series_slug(self, url: str) -> str:
        return "series"

    async def list_chapters(self, client, url):
        return [(f"https://fake.test/chapter/{n}", float(n)) for n in self._numbers]

    async def image_urls(self, client, chapter_url):
        return [f"{chapter_url}/0.jpg"]

    def folder_name(self, chapter_url: str) -> str:
        return "ch" + chapter_url.rsplit("/", 1)[-1]


@pytest.fixture
def console_reset(monkeypatch: pytest.MonkeyPatch):
    """Undo set_console_stream after a --json run so later tests print normally."""
    monkeypatch.setattr(term, "_console", term._console)
    monkeypatch.setattr(term, "_stream", term._stream)


@pytest.fixture
def cli(tmp_path: Path, jpeg_bytes: bytes, monkeypatch: pytest.MonkeyPatch, console_reset):
    """Run main.main(argv) against a fake site; returns (exit code, parsed stdout JSON)."""
    monkeypatch.setattr(main, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(main, "DOWNLOADS_DIR", tmp_path / "downloads")

    def _run(argv: list[str], handler=None, site_key: str = "fake", capsys=None, numbers=(1, 2, 3)):
        handler = handler or (lambda r: httpx.Response(200, content=jpeg_bytes))
        driver = FakeDriver(handler, numbers)
        site = config.Site(key=site_key, name="Fake Site", domains=("fake.test",), module="fake", driver=driver)

        async def fake_resolve(url: str, **kwargs):
            return site if "fake.test" in url else None

        monkeypatch.setattr(main, "resolve_site", fake_resolve)
        with pytest.raises(SystemExit) as exc:
            main.main(argv)
        code = exc.value.code
        out = capsys.readouterr() if capsys else None
        return code, out

    return _run


def test_json_run_prints_one_object_on_stdout_and_messages_on_stderr(cli, tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    code, out = cli(
        ["https://fake.test/series", "--chapters", "all", "--out", str(out_dir), "--yes", "--json", "--no-convert"],
        capsys=capsys,
    )
    assert code == 0
    assert out.out.count("\n") == 1
    result = json.loads(out.out)
    assert result == {
        "schema": 1,
        "site": "fake",
        "series": "series",
        "out_dir": str(out_dir),
        "chapters": 3,
        "images": 3,
        "failed_chapters": 0,
        "complete_chapters": ["ch1", "ch2", "ch3"],
        "incomplete_chapters": [],
    }
    assert "Done!" in out.err
    assert (out_dir / "ch2" / "0001.jpg").exists()


def test_conversion_only_touches_the_chapters_this_run_wrote(cli, tmp_path: Path, capsys):
    """`--out` may already hold other images (the user's own, a chapter kept with --no-convert): they stay as-is."""
    out_dir = tmp_path / "library"
    own = out_dir / "my_art" / "cover.png"
    earlier = out_dir / "ch1" / "0001.png"
    for path in (own, earlier):
        path.parent.mkdir(parents=True)
        Image.new("RGB", (8, 8), (10, 20, 30)).save(path, format="PNG")
    before = {path: path.read_bytes() for path in (own, earlier)}
    buf = io.BytesIO()
    Image.effect_noise((48, 48), 40).convert("RGB").save(buf, format="PNG")
    png = buf.getvalue()

    code, out = cli(
        ["https://fake.test/series", "--chapters", "2", "--out", str(out_dir), "-y", "--json"],
        handler=lambda r: httpx.Response(200, content=png),
        capsys=capsys,
    )

    assert code == 0 and json.loads(out.out)["complete_chapters"] == ["ch2"]
    assert [p.name for p in (out_dir / "ch2").iterdir()] == ["0001.jpg"]
    assert {path: path.read_bytes() for path in before} == before


def test_chapters_range_filters_the_listing(cli, tmp_path: Path, capsys):
    out_dir = tmp_path / "out"
    code, out = cli(
        ["https://fake.test/series", "--chapters", "2", "--out", str(out_dir), "-y", "--json"], capsys=capsys
    )
    assert code == 0
    assert json.loads(out.out)["chapters"] == 1
    assert (out_dir / "ch2").exists() and not (out_dir / "ch1").exists()


def test_chapters_selects_by_number_not_by_position(cli, tmp_path: Path, capsys):
    """A listing of 4 chapters numbered 0, 1, 150, 151: '150-151' and '0' must reach them."""
    out_dir = tmp_path / "out"
    argv = ["https://fake.test/series", "--out", str(out_dir), "-y", "--json", "--no-convert"]
    code, out = cli([*argv, "--chapters", "150-151"], capsys=capsys, numbers=(0, 1, 150, 151))
    assert code == 0 and json.loads(out.out)["chapters"] == 2
    assert (out_dir / "ch150").exists() and (out_dir / "ch151").exists() and not (out_dir / "ch1").exists()
    code, out = cli([*argv, "--chapters", "0"], capsys=capsys, numbers=(0, 1, 150, 151))
    assert code == 0 and json.loads(out.out)["chapters"] == 1 and (out_dir / "ch0").exists()


def test_yes_without_chapters_means_all_and_default_out_dir(cli, tmp_path: Path, capsys):
    code, out = cli(["https://fake.test/series", "-y", "--json", "--no-convert"], capsys=capsys)
    assert code == 0
    result = json.loads(out.out)
    assert result["chapters"] == 3
    assert Path(result["out_dir"]) == tmp_path / "downloads" / "fake" / "series"


def test_chapter_url_downloads_into_single_chapters(cli, tmp_path: Path, capsys):
    code, out = cli(["https://fake.test/chapter/7", "-y", "--json"], capsys=capsys)
    assert code == 0
    result = json.loads(out.out)
    assert result["chapters"] == 1
    assert Path(result["out_dir"]) == tmp_path / "downloads" / "fake" / "single_chapters"
    assert (tmp_path / "downloads" / "fake" / "single_chapters" / "ch7" / "0001.jpg").exists()


def test_incomplete_chapters_exit_with_2(cli, tmp_path: Path, capsys):
    code, out = cli(
        ["https://fake.test/chapter/4", "--out", str(tmp_path / "o"), "-y", "--json"],
        handler=lambda r: httpx.Response(404),
        capsys=capsys,
    )
    assert code == 2
    result = json.loads(out.out)
    assert result["incomplete_chapters"] == ["ch4"]
    assert result["complete_chapters"] == []
    assert result["images"] == 0


def test_series_is_null_when_the_driver_cannot_name_it(cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    def no_series(self, url: str) -> str:
        raise ValueError(f"Could not extract series slug from {url}")

    monkeypatch.setattr(FakeDriver, "series_slug", no_series)
    code, out = cli(["https://fake.test/chapter/1", "--out", str(tmp_path / "o"), "-y", "--json"], capsys=capsys)
    assert code == 0 and json.loads(out.out)["series"] is None


def test_usage_error_exits_with_1_not_the_incomplete_code(capsys):
    """argparse's own exit code for a bad flag is 2, which a caller would read as "some chapters incomplete"."""
    with pytest.raises(SystemExit) as exc:
        main.main(["https://fake.test/series", "--chapterz", "1"])
    assert exc.value.code == main.EXIT_ERROR
    out = capsys.readouterr()
    assert out.out == "" and "unrecognized arguments" in out.err


def test_keyboard_interrupt_still_prints_the_json_result(cli, monkeypatch: pytest.MonkeyPatch, capsys):
    async def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(FakeDriver, "download_series_url", interrupted)
    code, out = cli(["https://fake.test/chapter/1", "-y", "--json"], capsys=capsys)
    assert code == main.EXIT_ABORTED
    result = json.loads(out.out)
    assert result["site"] == "fake" and result["error"] == "Aborted by user."


def test_unwritable_log_folder_does_not_stop_the_run(cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys):
    def refuse(_logs_dir: Path) -> Path:
        raise PermissionError("logs/ is read-only")

    monkeypatch.setattr(main, "init_file_logging", refuse)
    code, out = cli(["https://fake.test/chapter/1", "--out", str(tmp_path / "o"), "-y", "--json"], capsys=capsys)
    assert code == 0 and json.loads(out.out)["chapters"] == 1
    assert "No log file for this run" in out.err


def test_unresolvable_site_exits_with_1(cli, capsys):
    code, out = cli(["https://nobody.example/x", "-y", "--json"], capsys=capsys)
    assert code == 1
    result = json.loads(out.out)
    assert result["site"] is None and "error" in result
    assert "Unsupported site" in out.err


def test_bad_range_and_empty_selection_exit_with_1(cli, tmp_path: Path, capsys):
    code, out = cli(["https://fake.test/series", "--chapters", "x-y", "-y", "--json"], capsys=capsys)
    assert code == 1 and "Could not parse" in json.loads(out.out)["error"]
    code, out = cli(["https://fake.test/series", "--chapters", "99", "-y", "--json"], capsys=capsys)
    assert code == 1 and "No chapters" in json.loads(out.out)["error"]


def test_invalid_url_exits_with_1(cli, capsys):
    code, out = cli(["ftp://example/x", "-y", "--json"], capsys=capsys)
    assert code == 1
    assert "does not look like a valid URL" in json.loads(out.out)["error"]


def test_non_json_run_prints_messages_to_stdout_and_no_json(cli, tmp_path: Path, capsys):
    code, out = cli(["https://fake.test/chapter/1", "--out", str(tmp_path / "o"), "-y"], capsys=capsys)
    assert code == 0
    assert "Done!" in out.out
    assert "{" not in out.out.splitlines()[-1]


def test_cinput_reads_end_of_input_as_empty_but_lets_ctrl_c_through(monkeypatch: pytest.MonkeyPatch):
    def raise_(exc: BaseException):
        def _input(*args):
            raise exc

        return _input

    monkeypatch.setattr("builtins.input", raise_(EOFError()))
    assert term.cinput("? ") == ""
    monkeypatch.setattr("builtins.input", raise_(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        term.cinput("? ")


def test_ctrl_c_at_the_chapter_prompt_aborts_instead_of_selecting_everything(
    cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
):
    answers = iter(["https://fake.test/series", KeyboardInterrupt()])

    def fake_input(*args) -> str:
        answer = next(answers, "")  # "" afterwards: Enter at any later prompt
        if isinstance(answer, BaseException):
            raise answer
        return answer

    monkeypatch.setattr("builtins.input", fake_input)
    code, _ = cli([], capsys=capsys)
    assert code == main.EXIT_ABORTED
    assert not (tmp_path / "downloads" / "fake").exists()


def test_set_console_stream_routes_rich_and_plain_output(console_reset):
    import io

    buf = io.StringIO()
    term.set_console_stream(buf)
    term.cinfo("hello")
    term.cerror("bad")
    assert "hello" in buf.getvalue() and "bad" in buf.getvalue()
    assert term._stream is buf
    term.set_console_stream(sys.stdout)
