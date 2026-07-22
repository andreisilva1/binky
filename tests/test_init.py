"""`binky init` — the generated file must always be valid, and never hold a secret."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from binky.cli import app
from binky.config import load_config, security_warnings
from binky.init_cmd import InitError, adapter_for, env_keys, generate

runner = CliRunner()

FULL_COMPOSE = """
services:
  db:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: hunter2
    ports: ["5432:5432"]
  cache:
    image: redis:7
  search:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.15.0
  web:
    build: .
    command: npm run dev
    ports: ["3000:3000"]
    depends_on: [db, cache]
  worker:
    build: ./worker
    depends_on:
      db:
        condition: service_healthy
"""


def write(root, name, text):
    path = root / name
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def project(tmp_path):
    write(tmp_path, "docker-compose.yml", FULL_COMPOSE)
    return tmp_path


# --------------------------------------------------------------------------- #
#  the generated file is always usable
# --------------------------------------------------------------------------- #
def test_the_generated_toml_is_valid_binky_config(project):
    result = generate(project)
    write(project, "binky.toml", result.toml)
    cfg = load_config(project / "binky.toml")

    assert cfg.services["db"].adapter == "postgres"
    assert cfg.services["cache"].adapter == "redis"
    assert cfg.services["search"].adapter == "elasticsearch"
    assert cfg.services["web"].cmd == "npm run dev"


def test_a_service_without_a_command_is_commented_out_not_guessed(project):
    """A guessed `cmd` would produce a file that looks right and doesn't run."""
    result = generate(project)
    write(project, "binky.toml", result.toml)

    # `worker` builds from source and declares no command — it must not appear as
    # a live service, but the file must still parse.
    cfg = load_config(project / "binky.toml")
    assert "worker" not in cfg.services
    assert "# [services.worker]" in result.toml
    assert any("worker" in note and "TODO" in note for note in result.notes)


def test_ports_are_not_carried_over_because_binky_allocates_them(project):
    result = generate(project)
    assert "3000" not in result.toml
    assert "5432:5432" not in result.toml
    write(project, "binky.toml", result.toml)
    assert load_config(project / "binky.toml").services["web"].url == "web"


def test_process_services_are_wired_to_the_adapters_they_depend_on(project):
    write(project, "binky.toml", generate(project).toml)
    web = load_config(project / "binky.toml").services["web"]
    assert web.env == {"DB_URL": "${db.url}", "CACHE_URL": "${cache.url}"}


# --------------------------------------------------------------------------- #
#  no secret ever reaches the generated file
# --------------------------------------------------------------------------- #
def test_an_inline_compose_password_never_reaches_the_toml(project):
    result = generate(project)
    assert "hunter2" not in result.toml
    assert 'admin_url = "${env.' in result.toml


def test_env_values_are_never_read_only_key_names(tmp_path):
    write(tmp_path, ".env", "DATABASE_URL=postgres://root:s3cret@localhost:5432/app\n# a comment\n")
    assert env_keys(tmp_path) == ["DATABASE_URL"]


def test_an_existing_env_key_is_referenced_instead_of_inventing_one(tmp_path):
    write(tmp_path, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n")
    write(tmp_path, ".env", "DATABASE_URL=postgres://root:s3cret@localhost:5432/app\n")
    result = generate(tmp_path)

    assert 'admin_url = "${env.DATABASE_URL}"' in result.toml
    assert "s3cret" not in result.toml
    # Nothing to export, so nothing to nag about.
    assert not any("export" in note for note in result.notes)


def test_a_missing_env_key_becomes_a_note_not_a_placeholder_value(tmp_path):
    write(tmp_path, "docker-compose.yml", "services:\n  db:\n    image: postgres:16\n")
    result = generate(tmp_path)
    assert any("export PG_ADMIN_URL=" in note for note in result.notes)


def test_clone_from_is_suggested_once_not_on_every_adapter(project):
    """The most useful comment in the file stops being useful if it repeats."""
    toml = generate(project).toml
    assert toml.count("# clone_from") == 1
    # ...and it is commented out, so it cannot take effect without a migrate/seed.
    assert "\nclone_from" not in toml


def test_a_compose_without_a_clone_capable_adapter_suggests_nothing(tmp_path):
    write(tmp_path, "compose.yml", "services:\n  cache:\n    image: redis:7\n")
    assert "clone_from" not in generate(tmp_path).toml


def test_the_report_says_what_it_dropped(tmp_path):
    write(
        tmp_path,
        "compose.yml",
        "services:\n  db:\n    image: postgres:16\n    volumes: [d:/data]\n"
        "    healthcheck:\n      test: [CMD, true]\n",
    )
    notes = generate(tmp_path).notes
    assert any("volumes, healthcheck" in note and "not carried over" in note for note in notes)


def test_the_generated_file_raises_no_security_warnings(project):
    write(project, "binky.toml", generate(project).toml)
    assert security_warnings(load_config(project / "binky.toml")) == []


# --------------------------------------------------------------------------- #
#  compose shapes found in the wild
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("image", "expected"),
    [
        ("postgres:16", "postgres"),
        ("pgvector/pgvector:pg16", "postgres"),
        ("postgis/postgis:16-3.4", "postgres"),
        ("mysql:8", "mysql"),
        ("mariadb:11", "mariadb"),
        ("mongo:7", "mongodb"),
        ("valkey/valkey:8", "valkey"),
        ("redis:7-alpine", "redis"),
        ("minio/minio", "s3"),
        ("docker.redpanda.com/redpandadata/redpanda:v24.2.1", "redpanda"),
        ("apache/kafka:3.9.0", "kafka"),
        ("clickhouse/clickhouse-server:24", "clickhouse"),
        ("opensearchproject/opensearch:2", "opensearch"),
        ("docker.elastic.co/elasticsearch/elasticsearch:8.15.0", "elasticsearch"),
        ("nginx:alpine", None),
        ("node:22", None),
    ],
)
def test_images_map_to_the_right_adapter(image, expected):
    assert adapter_for(image) == expected


def test_yaml_anchors_are_resolved_not_mis_parsed(tmp_path):
    """The reason this uses a real YAML parser instead of a hand-rolled one."""
    write(
        tmp_path,
        "docker-compose.yml",
        """
x-common: &common
  restart: unless-stopped
  ports: ["5432:5432"]

services:
  db:
    <<: *common
    image: postgres:16
""",
    )
    result = generate(tmp_path)
    write(tmp_path, "binky.toml", result.toml)
    assert load_config(tmp_path / "binky.toml").services["db"].adapter == "postgres"


def test_a_command_given_as_a_list_is_joined(tmp_path):
    write(
        tmp_path,
        "compose.yaml",
        'services:\n  web:\n    build: .\n    command: ["python", "-m", "app"]\n',
    )
    write(tmp_path, "binky.toml", generate(tmp_path).toml)
    assert load_config(tmp_path / "binky.toml").services["web"].cmd == "python -m app"


def test_service_names_with_dashes_become_valid_identifiers(tmp_path):
    write(tmp_path, "compose.yml", "services:\n  my-api-db:\n    image: postgres:16\n")
    write(tmp_path, "binky.toml", generate(tmp_path).toml)
    assert "my_api_db" in load_config(tmp_path / "binky.toml").services


# --------------------------------------------------------------------------- #
#  failure modes
# --------------------------------------------------------------------------- #
def test_no_compose_file_is_a_clear_error(tmp_path):
    with pytest.raises(InitError, match="no compose file found"):
        generate(tmp_path)


def test_malformed_yaml_is_reported_as_such(tmp_path):
    write(tmp_path, "compose.yml", "services:\n  db:\n   - image: [unclosed\n")
    with pytest.raises(InitError, match="not valid YAML"):
        generate(tmp_path)


def test_a_compose_file_without_services_is_rejected(tmp_path):
    write(tmp_path, "compose.yml", "volumes:\n  data: {}\n")
    with pytest.raises(InitError, match="no `services:` block"):
        generate(tmp_path)


# --------------------------------------------------------------------------- #
#  the command itself
# --------------------------------------------------------------------------- #
def test_init_writes_the_file_and_reports(project):
    result = runner.invoke(app, ["init", str(project)])
    assert result.exit_code == 0, result.output
    assert (project / "binky.toml").exists()
    assert "wrote" in result.output


def test_init_refuses_to_clobber_an_existing_config(project):
    write(project, "binky.toml", "# mine\n")
    result = runner.invoke(app, ["init", str(project)])

    assert result.exit_code == 1
    assert "already exists" in result.output
    assert (project / "binky.toml").read_text(encoding="utf-8") == "# mine\n"


def test_force_overwrites(project):
    write(project, "binky.toml", "# mine\n")
    result = runner.invoke(app, ["init", str(project), "--force"])

    assert result.exit_code == 0, result.output
    assert "# mine" not in (project / "binky.toml").read_text(encoding="utf-8")


def test_init_exits_nonzero_when_there_is_nothing_to_read(tmp_path):
    result = runner.invoke(app, ["init", str(tmp_path)])
    assert result.exit_code == 1
    assert "no compose file found" in result.output
