"""Smoke tests for the Binky CLI entrypoint."""

import io
import sys

from typer.testing import CliRunner

from binky import __version__, cli
from binky.cli import app

runner = CliRunner()


def test_force_utf8_output_survives_a_cp1252_stdout(monkeypatch) -> None:
    """The CLI prints ✓/✗/⚠/— glyphs; on a redirected or non-UTF-8 Windows stdout Python
    falls back to cp1252 and a lone secho would raise UnicodeEncodeError, killing a command
    whose work already succeeded. _force_utf8_output must pin UTF-8 so the glyph writes cleanly.
    """
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    # a cp1252 stream chokes on ✓ before the fix — prove that, then prove the fix clears it
    try:
        print("✓", file=stream)
        stream.flush()
        raise AssertionError("expected cp1252 stdout to reject the glyph before reconfigure")
    except UnicodeEncodeError:
        pass

    cli._force_utf8_output()

    assert stream.encoding.lower() == "utf-8"
    print("✓ up — done …", file=stream)  # no UnicodeEncodeError
    stream.flush()
    assert "✓" in raw.getvalue().decode("utf-8")


def test_version_flag() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_version_command() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert __version__ in result.stdout


def test_no_args_shows_help() -> None:
    result = runner.invoke(app, [])
    # no_args_is_help exits with code 0 and prints usage
    assert "Usage" in result.stdout


def test_logs_command(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("BINKY_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "binky.toml").write_text(
        '[project]\nname = "demo"\n[services.x]\ncmd = "run"\n', encoding="utf-8"
    )
    monkeypatch.chdir(proj)
    log = tmp_path / "home" / "logs" / "demo" / "feat" / "x.log"
    log.parent.mkdir(parents=True)
    log.write_text("hello log line\n", encoding="utf-8")

    result = runner.invoke(app, ["logs", "feat"])
    assert result.exit_code == 0
    assert "hello log line" in result.stdout
