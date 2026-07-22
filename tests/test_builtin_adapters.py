"""Tests for the first-party adapters that need an optional driver.

Two layers:

- **always run** — naming, identifier limits, address construction, discovery and
  the config plumbing. These are the parts that silently corrupt state when wrong
  (two slices collapsing onto one identifier), so they get tested without a server.
- **gated on a real server** — one full provision → resolve → clone → teardown
  round-trip per backend, skipped unless the matching ``BINKY_TEST_*`` env var
  points at one. CI sets them from service containers.
"""

from __future__ import annotations

import os

import pytest

from binky import adapters, ops
from binky.builtin_adapters import (
    ClickHouseAdapter,
    ElasticsearchAdapter,
    KafkaAdapter,
    MongoAdapter,
    MySQLAdapter,
    QdrantAdapter,
    RabbitMQAdapter,
    RedisAdapter,
    S3Adapter,
    _fit,
)
from binky.config import load_config_data


# --------------------------------------------------------------------------- #
#  discovery + wiring
# --------------------------------------------------------------------------- #
def test_every_shipped_adapter_is_discoverable() -> None:
    names = adapters.available()
    for expected in (
        "postgres", "redis", "valkey", "mysql", "mariadb", "percona", "mongodb",
        "s3", "minio", "rabbitmq", "kafka", "redpanda", "qdrant",
        "clickhouse", "elasticsearch", "opensearch",
    ):
        assert expected in names


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [("valkey", "redis"), ("percona", "mysql"), ("mariadb", "mysql"),
     ("opensearch", "elasticsearch"), ("minio", "s3"), ("redpanda", "kafka")],
)
def test_protocol_compatible_aliases_resolve_to_one_implementation(
    alias: str, canonical: str
) -> None:
    """A fork that speaks the same protocol gets the same slice, not a copy of the code."""
    url = {"admin_url": "http://user:pw@localhost:1234"}
    assert type(adapters.get(alias, url)) is type(adapters.get(canonical, url))


def test_lazy_load_does_not_need_the_driver_installed() -> None:
    """Constructing an adapter must not import its driver — only using it should.

    This is what lets `binky status` work on a machine with no boto3, and what
    makes the missing-extra error land at the point the user can act on it.
    """
    adapter = adapters.get("s3", {"admin_url": "http://localhost:9000"})
    assert isinstance(adapter, S3Adapter)
    assert adapter.resolve("acme_feat") == "s3://acme-feat"


def test_adapter_versions_are_accepted() -> None:
    assert isinstance(adapters.get("redis@7", {"admin_url": "redis://x:6379"}), RedisAdapter)


@pytest.mark.parametrize(
    ("name", "hint"),
    [("redis", "admin_url"), ("mysql", "admin_url"), ("mongodb", "admin_url"),
     ("rabbitmq", "management API"), ("kafka", "admin_url")],
)
def test_missing_admin_url_is_a_clear_error(name: str, hint: str, monkeypatch) -> None:
    for var in ("BINKY_REDIS_URL", "BINKY_MYSQL_URL", "BINKY_MONGO_URL",
                "BINKY_RABBITMQ_URL", "BINKY_KAFKA_URL"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(adapters.AdapterError, match=hint):
        adapters.get(name, {})


def test_missing_driver_names_the_extra_to_install(monkeypatch) -> None:
    """A missing optional driver must say which extra installs it."""
    import builtins

    adapter = adapters.get("mongodb", {"admin_url": "mongodb://localhost:27017"})
    real = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "pymongo":
            raise ImportError("no pymongo")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(adapters.AdapterError, match=r"binky\[mongodb\]"):
        adapter.provision("acme_feat")


# --------------------------------------------------------------------------- #
#  identifier safety — the class of bug that silently shares state
# --------------------------------------------------------------------------- #
def test_fit_never_collapses_two_names_onto_one() -> None:
    a = "acme_" + "a" * 90
    b = "acme_" + "a" * 89 + "b"
    assert _fit(a, 63) != _fit(b, 63)
    assert len(_fit(a, 63)) <= 63
    assert _fit("short", 63) == "short"  # untouched when it already fits


def test_s3_bucket_names_obey_s3_rules() -> None:
    assert S3Adapter._bucket("acme_feat_login") == "acme-feat-login"
    assert S3Adapter._bucket("ab") == "binky-ab"  # padded past S3's 3-char floor
    assert S3Adapter._bucket("a_b") == "a-b"  # already legal, left alone
    long_name = S3Adapter._bucket("acme_" + "x" * 100)
    assert len(long_name) <= 63
    assert not long_name.startswith("-") and not long_name.endswith("-")


def test_mysql_rejects_an_unsafe_identifier() -> None:
    adapter = MySQLAdapter({"admin_url": "mysql://root@localhost:3306"})
    assert adapter._ident("acme_feat") == "acme_feat"
    with pytest.raises(adapters.AdapterError, match="unsafe"):
        adapter._ident("acme`; DROP DATABASE x; --")


def test_mongo_strips_characters_mongo_forbids() -> None:
    assert "/" not in MongoAdapter._ident("acme/feat")
    assert "." not in MongoAdapter._ident("acme.feat")
    assert len(MongoAdapter._ident("a" * 80)) <= 63


def test_clickhouse_rejects_an_unsafe_identifier() -> None:
    assert ClickHouseAdapter._ident("acme_feat") == "acme_feat"
    with pytest.raises(adapters.AdapterError, match="unsafe"):
        ClickHouseAdapter._ident('acme"; DROP DATABASE x; --')


def test_elasticsearch_prefix_obeys_index_naming_rules() -> None:
    assert ElasticsearchAdapter._prefix("Acme_Feat") == "acme_feat"  # index names are lowercase
    assert ElasticsearchAdapter._prefix("_leading") == "leading"  # cannot start with _
    assert len(ElasticsearchAdapter._prefix("a" * 300)) <= 200


def test_one_slice_is_never_a_prefix_of_another_slices_indices() -> None:
    """`acme_feat` must not own `acme_feat_2`'s indices.

    Prefix isolation lives or dies on this: teardown of one worktree deleting a
    sibling's data is the worst failure this adapter could have.
    """
    adapter = ElasticsearchAdapter({"admin_url": "http://localhost:9200"})
    short, long = adapter._prefix("acme_feat"), adapter._prefix("acme_feat_2")
    assert not (long == short or long.startswith(short + adapter.SEP))
    assert f"{long}-products".startswith(long + adapter.SEP)  # but it does own its own


# --------------------------------------------------------------------------- #
#  address construction
# --------------------------------------------------------------------------- #
def test_rabbitmq_address_uses_the_amqp_port_not_the_management_port() -> None:
    adapter = RabbitMQAdapter({"admin_url": "http://guest:guest@localhost:15672"})
    assert adapter.resolve("acme_feat") == "amqp://guest:guest@localhost:5672/acme_feat"


def test_rabbitmq_amqp_port_is_overridable() -> None:
    adapter = RabbitMQAdapter(
        {"admin_url": "http://guest:guest@rabbit:15672", "amqp_port": "5673"}
    )
    assert adapter.resolve("acme_feat").endswith("rabbit:5673/acme_feat")


def test_kafka_address_carries_the_topic_prefix() -> None:
    adapter = KafkaAdapter({"admin_url": "kafka://localhost:9092"})
    assert adapter.resolve("acme_feat") == "kafka://localhost:9092/acme_feat"


def test_kafka_accepts_a_bare_bootstrap_pair() -> None:
    assert KafkaAdapter({"admin_url": "localhost:9092"}).bootstrap == "localhost:9092"


def test_qdrant_address_carries_the_collection_prefix() -> None:
    adapter = QdrantAdapter({"admin_url": "http://localhost:6333/"})
    assert adapter.resolve("acme_feat") == "http://localhost:6333/acme_feat"


def test_clickhouse_address_is_the_http_endpoint_with_the_database() -> None:
    adapter = ClickHouseAdapter({"admin_url": "http://default:@localhost:8123"})
    assert adapter.resolve("acme_feat") == "http://default:@localhost:8123/acme_feat"


def test_elasticsearch_address_carries_the_index_prefix() -> None:
    adapter = ElasticsearchAdapter({"admin_url": "http://localhost:9200/"})
    assert adapter.resolve("acme_feat") == "http://localhost:9200/acme_feat"


def test_clickhouse_credentials_travel_as_a_header_not_in_the_url() -> None:
    """Userinfo in a request line leaks into logs and proxy access lines."""
    from binky.builtin_adapters import _split_auth

    root, headers = _split_auth("http://default:s3cret@localhost:8123")
    assert root == "http://localhost:8123"
    assert headers["authorization"].startswith("Basic ")
    assert "s3cret" not in root


def test_redis_resolve_before_provision_is_a_clear_error(monkeypatch) -> None:
    adapter = RedisAdapter({"admin_url": "redis://localhost:6379"})
    monkeypatch.setattr(adapter, "_client", lambda db=0: _FakeRedis({}))
    with pytest.raises(adapters.AdapterError, match="provision it first"):
        adapter.resolve("acme_feat")


class _FakeRedis:
    def __init__(self, mapping: dict) -> None:
        self.mapping = mapping

    def hget(self, _key: str, field: str):
        return self.mapping.get(field)

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
#  config plumbing — `options` reaching the adapter
# --------------------------------------------------------------------------- #
def test_options_reach_the_adapter_flattened_and_env_resolved(monkeypatch) -> None:
    monkeypatch.setenv("MINIO_KEY", "s3cret")
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "blobs": {
                    "adapter": "s3",
                    "admin_url": "http://localhost:9000",
                    "buckets": ["uploads"],
                    "options": {"region": "eu-west-1", "secret_key": "${env.MINIO_KEY}"},
                }
            },
        }
    )
    config = ops._adapter_config(cfg.services["blobs"], "blobs", {}, "feat")
    assert config["region"] == "eu-west-1"
    assert config["secret_key"] == "s3cret"  # resolved, so it never sits in the toml
    assert config["buckets"] == ["uploads"]
    assert config["admin_url"] == "http://localhost:9000"


def test_options_cannot_shadow_admin_url() -> None:
    cfg = load_config_data(
        {
            "project": {"name": "acme"},
            "services": {
                "db": {
                    "adapter": "postgres",
                    "admin_url": "postgres://real",
                    "options": {"admin_url": "postgres://spoofed"},
                }
            },
        }
    )
    config = ops._adapter_config(cfg.services["db"], "db", {}, "feat")
    assert config["admin_url"] == "postgres://real"


def test_options_are_rejected_on_a_process_service() -> None:
    from binky.config import ConfigError

    with pytest.raises(ConfigError, match="only valid on an adapter"):
        load_config_data(
            {
                "project": {"name": "acme"},
                "services": {"web": {"cmd": "run", "options": {"region": "x"}}},
            }
        )


# --------------------------------------------------------------------------- #
#  real-server round-trips (skipped unless a server is configured)
# --------------------------------------------------------------------------- #
def _roundtrip(adapter, slice_name: str, *, clone: bool) -> None:
    """provision → resolve → (clone) → teardown, asserting idempotence throughout."""
    clone_name = slice_name + "_clone"
    adapter.teardown(slice_name)
    adapter.teardown(clone_name)

    adapter.provision(slice_name)
    adapter.provision(slice_name)  # idempotent
    assert adapter.resolve(slice_name)

    if clone:
        adapter.clone(slice_name, clone_name)
        assert adapter.resolve(clone_name)
        adapter.teardown(clone_name)

    adapter.teardown(slice_name)
    adapter.teardown(slice_name)  # idempotent


SLICE = "binky_test_slice_zzz"


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_REDIS"), reason="set BINKY_TEST_REDIS")
def test_redis_roundtrip() -> None:
    adapter = adapters.get("redis", {"admin_url": os.environ["BINKY_TEST_REDIS"]})
    _roundtrip(adapter, SLICE, clone=True)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_REDIS"), reason="set BINKY_TEST_REDIS")
def test_redis_reports_the_logical_database_cap() -> None:
    """The 16-DB ceiling must surface as a clear error, not a silent collision."""
    url = os.environ["BINKY_TEST_REDIS"]
    adapter = adapters.get("redis", {"admin_url": url, "databases": "3"})
    claimed = [f"{SLICE}_cap_{i}" for i in range(2)]
    try:
        for name in claimed:
            adapter.provision(name)
        with pytest.raises(adapters.AdapterError, match="out of logical databases"):
            adapter.provision(f"{SLICE}_cap_overflow")
    finally:
        for name in claimed:
            adapter.teardown(name)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_MYSQL"), reason="set BINKY_TEST_MYSQL")
def test_mysql_roundtrip() -> None:
    adapter = adapters.get("mysql", {"admin_url": os.environ["BINKY_TEST_MYSQL"]})
    _roundtrip(adapter, SLICE, clone=adapter.capabilities()["clone"])


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_MONGO"), reason="set BINKY_TEST_MONGO")
def test_mongodb_roundtrip() -> None:
    adapter = adapters.get("mongodb", {"admin_url": os.environ["BINKY_TEST_MONGO"]})
    _roundtrip(adapter, SLICE, clone=True)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_S3"), reason="set BINKY_TEST_S3")
def test_s3_roundtrip() -> None:
    adapter = adapters.get(
        "s3",
        {
            "admin_url": os.environ["BINKY_TEST_S3"],
            "access_key": os.environ.get("BINKY_TEST_S3_KEY"),
            "secret_key": os.environ.get("BINKY_TEST_S3_SECRET"),
            "buckets": ["uploads"],
        },
    )
    _roundtrip(adapter, SLICE, clone=True)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_RABBITMQ"), reason="set BINKY_TEST_RABBITMQ")
def test_rabbitmq_roundtrip() -> None:
    adapter = adapters.get("rabbitmq", {"admin_url": os.environ["BINKY_TEST_RABBITMQ"]})
    _roundtrip(adapter, SLICE, clone=False)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_KAFKA"), reason="set BINKY_TEST_KAFKA")
def test_kafka_roundtrip() -> None:
    adapter = adapters.get(
        "kafka", {"admin_url": os.environ["BINKY_TEST_KAFKA"], "topics": "orders,events"}
    )
    _roundtrip(adapter, SLICE, clone=False)


@pytest.mark.skipif(not os.environ.get("BINKY_TEST_QDRANT"), reason="set BINKY_TEST_QDRANT")
def test_qdrant_roundtrip() -> None:
    adapter = adapters.get("qdrant", {"admin_url": os.environ["BINKY_TEST_QDRANT"]})
    _roundtrip(adapter, SLICE, clone=False)


@pytest.mark.skipif(
    not os.environ.get("BINKY_TEST_CLICKHOUSE"), reason="set BINKY_TEST_CLICKHOUSE"
)
def test_clickhouse_roundtrip() -> None:
    adapter = adapters.get("clickhouse", {"admin_url": os.environ["BINKY_TEST_CLICKHOUSE"]})
    _roundtrip(adapter, SLICE, clone=False)


@pytest.mark.skipif(
    not os.environ.get("BINKY_TEST_CLICKHOUSE"), reason="set BINKY_TEST_CLICKHOUSE"
)
def test_clickhouse_database_really_appears_and_disappears() -> None:
    """The round-trip proves the calls succeed; this proves they did something."""
    adapter = adapters.get("clickhouse", {"admin_url": os.environ["BINKY_TEST_CLICKHOUSE"]})
    exists = f"SELECT count() FROM system.databases WHERE name = '{SLICE}'"
    try:
        adapter.provision(SLICE)
        assert adapter._query(exists).strip() == "1"
        # A table written into the slice must survive inside it.
        adapter._query(f"CREATE TABLE {SLICE}.t (x UInt8) ENGINE = MergeTree ORDER BY x")
        adapter._query(f"INSERT INTO {SLICE}.t VALUES (7)")
        assert adapter._query(f"SELECT x FROM {SLICE}.t").strip() == "7"
    finally:
        adapter.teardown(SLICE)
    assert adapter._query(exists).strip() == "0"


@pytest.mark.skipif(
    not os.environ.get("BINKY_TEST_ELASTICSEARCH"), reason="set BINKY_TEST_ELASTICSEARCH"
)
def test_elasticsearch_roundtrip() -> None:
    adapter = adapters.get(
        "elasticsearch",
        {"admin_url": os.environ["BINKY_TEST_ELASTICSEARCH"], "indices": "products,orders"},
    )
    _roundtrip(adapter, SLICE, clone=False)


@pytest.mark.skipif(
    not os.environ.get("BINKY_TEST_ELASTICSEARCH"), reason="set BINKY_TEST_ELASTICSEARCH"
)
def test_elasticsearch_teardown_spares_a_sibling_slice() -> None:
    """Live proof of the prefix-overlap guarantee the unit test asserts on names.

    `binky_test_slice_zzz` tearing down must not touch `binky_test_slice_zzz_2`.
    """
    url = os.environ["BINKY_TEST_ELASTICSEARCH"]
    config = {"admin_url": url, "indices": "products"}
    adapter = adapters.get("elasticsearch", config)
    sibling = SLICE + "_2"
    try:
        adapter.provision(SLICE)
        adapter.provision(sibling)
        assert adapter._owned(SLICE) == [f"{SLICE}-products"]
        adapter.teardown(SLICE)
        assert adapter._owned(SLICE) == []
        assert adapter._owned(sibling) == [f"{sibling}-products"]  # untouched
    finally:
        adapter.teardown(SLICE)
        adapter.teardown(sibling)
