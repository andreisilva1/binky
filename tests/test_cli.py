"""Smoke tests for the Warren CLI entrypoint."""

from typer.testing import CliRunner

from warren import __version__
from warren.cli import app

runner = CliRunner()


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
    monkeypatch.setenv("WARREN_HOME", str(tmp_path / "home"))
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / "warren.toml").write_text(
        '[project]\nname = "demo"\n[services.x]\ncmd = "run"\n', encoding="utf-8"
    )
    monkeypatch.chdir(proj)
    log = tmp_path / "home" / "logs" / "demo" / "feat" / "x.log"
    log.parent.mkdir(parents=True)
    log.write_text("hello log line\n", encoding="utf-8")

    result = runner.invoke(app, ["logs", "feat"])
    assert result.exit_code == 0
    assert "hello log line" in result.stdout
