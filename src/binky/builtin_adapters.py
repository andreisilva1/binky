"""First-party adapters that need an optional driver.

They live here rather than in :mod:`binky.adapters` so importing the contract
never drags in boto3/pymongo/redis — :func:`binky.adapters.get` imports this
module the first time one of these names is actually used.

Every adapter carves a *slice* of a server you already run. The isolation
primitive differs per backend, and the docstring on each class says exactly what
it is, because that is the thing you need to trust:

=============  ==============================================  =======
adapter        slice                                           clone
=============  ==============================================  =======
redis          one logical DB (capped by the server's count)    yes
mysql          one database                                     if mysqldump is on PATH
mongodb        one database                                     yes
s3/minio       one bucket                                       yes
rabbitmq       one vhost                                        n/a — a fresh vhost is already clean
kafka          one topic prefix                                 no
qdrant         one collection prefix                            no
clickhouse     one database                                     no — see the class docstring
elasticsearch  one index prefix                                 no
=============  ==============================================  =======

``redis`` also answers to ``valkey``, ``mysql`` to ``percona``, and
``elasticsearch`` to ``opensearch`` — same wire protocol, same slice.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import ClassVar

from binky.adapters import Adapter, AdapterError, _with_dbname


# --------------------------------------------------------------------------- #
#  shared helpers
# --------------------------------------------------------------------------- #
def _driver(module: str, extra: str):
    """Import an optional driver, or explain precisely how to get it."""
    try:
        return __import__(module, fromlist=["_"])
    except ImportError as exc:  # pragma: no cover — only without the extra installed
        raise AdapterError(f"{module} not installed — `pip install 'binky[{extra}]'`") from exc


def _fit(name: str, limit: int) -> str:
    """Squeeze a slice name into a backend's identifier limit.

    Plain truncation would let two long worktree names collapse onto one
    identifier — i.e. two agents silently sharing a database. The suffix hash
    makes that impossible.
    """
    if len(name) <= limit:
        return name
    digest = hashlib.sha1(name.encode()).hexdigest()[:8]
    return f"{name[: limit - 9].rstrip('-_')}-{digest}"


def _split_auth(base: str) -> tuple[str, dict[str, str]]:
    """Split a URL into its bare root and the basic-auth header its userinfo implies.

    Credentials belong in the header, not in the request line — a URL with
    userinfo tends to end up in logs and proxy access lines verbatim.
    """
    parts = urllib.parse.urlsplit(base)
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], "", "", ""))
    headers: dict[str, str] = {}
    if parts.username:
        raw = f"{urllib.parse.unquote(parts.username)}:{urllib.parse.unquote(parts.password or '')}"
        headers["authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
    return root, headers


def _http(
    base: str,
    path: str,
    method: str = "GET",
    body: dict | None = None,
    headers: dict | None = None,
) -> dict | list | None:
    """Minimal JSON-over-HTTP call with basic auth taken from the URL's userinfo.

    RabbitMQ, Qdrant and Elasticsearch all expose plain REST APIs, so using urllib
    here keeps three whole adapters dependency-free.
    """
    root, auth = _split_auth(base)
    request = urllib.request.Request(
        root + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"content-type": "application/json", **auth, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404 and method == "DELETE":
            return None  # already gone — teardown stays idempotent
        raise AdapterError(f"{method} {path} failed: {exc.code} {exc.read()[:200]!r}") from exc
    except OSError as exc:
        raise AdapterError(f"{method} {path} failed: {exc}") from exc
    return json.loads(payload) if payload else None


# --------------------------------------------------------------------------- #
#  Redis — one logical DB per worktree
# --------------------------------------------------------------------------- #
# Claim a logical DB for a slice, atomically. Returns the existing index if the
# slice already has one, the lowest free index otherwise, and -1 when the server
# is full. Lua because the read-then-write has to be one step: two `binky up`
# racing would otherwise both see the same free index.
_CLAIM_DB = """
local existing = redis.call('HGET', KEYS[1], ARGV[1])
if existing then return tonumber(existing) end
local used = {}
local all = redis.call('HGETALL', KEYS[1])
for i = 2, #all, 2 do used[tonumber(all[i])] = true end
for idx = 1, tonumber(ARGV[2]) - 1 do
  if not used[idx] then
    redis.call('HSET', KEYS[1], ARGV[1], idx)
    return idx
  end
end
return -1
"""


class RedisAdapter(Adapter):
    """One logical Redis DB per worktree.

    Redis ships with 16 logical DBs and no way to add more at runtime. DB 0 holds
    Binky's slice→index map, so a stock server supports 15 concurrent worktrees
    and the 16th fails loudly. That cap is deliberate: the alternative — namespacing
    by key prefix — cannot implement teardown (no `FLUSHDB`) and leaks across
    `SCAN`/`KEYS`/`DBSIZE`, which is worse than a clear error.
    """

    CAPABILITIES: ClassVar[dict] = {"clone": True}
    MAP_KEY: ClassVar[str] = "binky:slices"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_REDIS_URL")
        if not self.admin_url:
            raise AdapterError("redis adapter needs an admin_url (or $BINKY_REDIS_URL)")
        self._databases = config.get("databases")

    def _client(self, db: int = 0):
        redis = _driver("redis", "redis")
        return redis.Redis.from_url(_with_dbname(self.admin_url, str(db)), decode_responses=True)

    def _count(self, client) -> int:
        if self._databases:
            return int(self._databases)
        # Managed Redis usually disables CONFIG; 16 is the compiled-in default.
        with contextlib.suppress(Exception):
            return int(client.config_get("databases")["databases"])
        return 16

    def _claim(self, slice_name: str) -> int:
        with contextlib.closing(self._client()) as client:
            total = self._count(client)
            index = int(client.eval(_CLAIM_DB, 1, self.MAP_KEY, slice_name, total))
            if index < 0:
                raise AdapterError(
                    f"redis is out of logical databases: {total - 1} slices are in use and "
                    f"this server has {total}. Tear down a worktree, or raise `databases` in "
                    f"redis.conf and set options.databases to match."
                )
            return index

    def provision(self, slice_name: str) -> None:
        self._claim(slice_name)

    def _index(self, slice_name: str) -> int:
        with contextlib.closing(self._client()) as client:
            index = client.hget(self.MAP_KEY, slice_name)
        if index is None:
            raise AdapterError(f"redis slice '{slice_name}' has no database — provision it first")
        return int(index)

    def resolve(self, slice_name: str) -> str:
        return _with_dbname(self.admin_url, str(self._index(slice_name)))

    def teardown(self, slice_name: str) -> None:
        with contextlib.closing(self._client()) as client:
            index = client.hget(self.MAP_KEY, slice_name)
            if index is None:
                return
            with contextlib.closing(self._client(int(index))) as slice_client:
                slice_client.flushdb()
            client.hdel(self.MAP_KEY, slice_name)

    def clone(self, source_slice: str, dest_slice: str) -> None:
        source = self._index(source_slice)
        dest = self._claim(dest_slice)
        with contextlib.closing(self._client(source)) as client:
            for key in client.scan_iter(count=500):
                client.copy(key, key, destination_db=dest, replace=True)


# --------------------------------------------------------------------------- #
#  MySQL / MariaDB — one database per worktree
# --------------------------------------------------------------------------- #
class MySQLAdapter(Adapter):
    """One database per worktree, carved out of the user's own MySQL/MariaDB server.

    MySQL has no `CREATE DATABASE ... TEMPLATE`, so cloning shells out to
    `mysqldump | mysql`. The capability is therefore *dynamic*: if those binaries
    aren't on PATH — or are too old to authenticate against this server — the adapter
    reports `clone: false` and `clone_from` degrades to per-worktree migrate+seed
    instead of silently producing a partial copy.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_MYSQL_URL")
        if not self.admin_url:
            raise AdapterError("mysql adapter needs an admin_url (or $BINKY_MYSQL_URL)")
        self._parts = urllib.parse.urlsplit(self.admin_url)
        self._clone_ok: bool | None = None

    def capabilities(self) -> dict:
        if self._clone_ok is None:
            self._clone_ok = (
                bool(shutil.which("mysqldump") and shutil.which("mysql")) and self._client_works()
            )
        return {"clone": self._clone_ok}

    def _client_works(self) -> bool:
        """Can the client on PATH actually talk to *this* server?

        Presence on PATH is not enough: a client older than the server cannot load
        the server's default auth plugin (`caching_sha2_password` on MySQL 8), and
        fails only once the dump is already under way. Probing once here turns that
        into an accurate `clone: false` up front — `ops` only asks for capabilities
        when `clone_from` is set, so this costs one round trip per cloning service.
        """
        try:
            probe = subprocess.run(
                ["mysql", *self._cli_args(), "-e", "SELECT 1"],
                capture_output=True, env=self._cli_env(), check=False, timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return probe.returncode == 0

    def _cli_env(self) -> dict:
        # The password goes through MYSQL_PWD, not argv — argv is world-readable in `ps`.
        env = {**os.environ}
        if self._parts.password:
            env["MYSQL_PWD"] = urllib.parse.unquote(self._parts.password)
        return env

    def _connect(self):
        pymysql = _driver("pymysql", "mysql")
        parts = self._parts
        try:
            return pymysql.connect(
                host=parts.hostname or "localhost",
                port=parts.port or 3306,
                user=urllib.parse.unquote(parts.username or "root"),
                password=urllib.parse.unquote(parts.password or ""),
                autocommit=True,
            )
        except Exception as exc:
            raise AdapterError(f"could not connect to mysql: {exc}") from exc

    @staticmethod
    def _ident(slice_name: str) -> str:
        # MySQL identifiers cap at 64 chars; backticks are the only quoting, so a
        # backtick in the name would be an injection — slice_name() already strips
        # everything but [a-z0-9_], and this asserts it rather than trusting it.
        name = _fit(slice_name, 64)
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise AdapterError(f"unsafe mysql database name: {slice_name!r}")
        return name

    def provision(self, slice_name: str) -> None:
        name = self._ident(slice_name)
        with contextlib.closing(self._connect()) as conn, conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}`")

    def resolve(self, slice_name: str) -> str:
        return _with_dbname(self.admin_url, self._ident(slice_name))

    def teardown(self, slice_name: str) -> None:
        name = self._ident(slice_name)
        with contextlib.closing(self._connect()) as conn, conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{name}`")

    def _cli_args(self) -> list[str]:
        parts = self._parts
        # `--protocol=TCP` is not optional: the MySQL client reads `-h localhost` as
        # "use the Unix socket" and silently ignores `-P`, so an admin_url pointing at
        # a server on localhost:3306 — a container, most of the time — probes as if the
        # client were broken and downgrades `clone` to false. The driver in `_connect`
        # already speaks TCP for that same URL; this makes the two agree.
        args = [
            "--protocol=TCP",
            "-h", parts.hostname or "localhost",
            "-P", str(parts.port or 3306),
        ]
        if parts.username:
            args += ["-u", urllib.parse.unquote(parts.username)]
        return args

    def clone(self, source_slice: str, dest_slice: str) -> None:
        if not self.capabilities()["clone"]:
            raise AdapterError(
                "mysql clone needs a working `mysqldump` and `mysql` on PATH "
                "(present, and new enough to authenticate against this server)"
            )
        source, dest = self._ident(source_slice), self._ident(dest_slice)
        self.provision(dest_slice)
        env = self._cli_env()
        dump = subprocess.run(
            ["mysqldump", *self._cli_args(), "--routines", "--triggers", "--events",
             "--single-transaction", "--no-tablespaces", source],
            capture_output=True, env=env, check=False,
        )
        if dump.returncode != 0:
            raise AdapterError(f"mysqldump failed: {self._client_error(dump.stderr)}")
        load = subprocess.run(
            ["mysql", *self._cli_args(), dest],
            input=dump.stdout, capture_output=True, env=env, check=False,
        )
        if load.returncode != 0:
            raise AdapterError(f"mysql restore failed: {self._client_error(load.stderr)}")

    @staticmethod
    def _client_error(stderr: bytes) -> str:
        """Translate the client's failure into something the user can act on.

        A `mysqldump` older than the server it is dumping fails to load the server's
        default auth plugin, and says so in a way that reads like a broken install
        rather than a version mismatch. Binky has already fallen back to per-worktree
        migrate+seed by the time this is read, so the message says what to fix, not
        what broke.
        """
        detail = stderr.decode(errors="replace").strip()[:300]
        if "caching_sha2_password" in detail or "1045" in detail:
            detail += (
                " — the `mysqldump` on PATH is most likely older than the server it is "
                "dumping. Slices still work (Binky falls back to per-worktree "
                "migrate+seed); upgrade the MySQL client to get instant clones back."
            )
        return detail


# --------------------------------------------------------------------------- #
#  MongoDB — one database per worktree
# --------------------------------------------------------------------------- #
class MongoAdapter(Adapter):
    """One database per worktree.

    Mongo creates databases lazily, so `provision` writes a marker collection —
    otherwise an empty slice would be indistinguishable from a missing one, and
    `resolve` would hand out an address to nothing.
    """

    CAPABILITIES: ClassVar[dict] = {"clone": True}
    MARKER: ClassVar[str] = "__binky"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_MONGO_URL")
        if not self.admin_url:
            raise AdapterError("mongodb adapter needs an admin_url (or $BINKY_MONGO_URL)")

    def _client(self):
        pymongo = _driver("pymongo", "mongodb")
        try:
            return pymongo.MongoClient(self.admin_url, serverSelectionTimeoutMS=8000)
        except Exception as exc:
            raise AdapterError(f"could not connect to mongodb: {exc}") from exc

    @staticmethod
    def _ident(slice_name: str) -> str:
        # Mongo database names cap at 63 bytes and cannot contain /\. "$*<>:|?
        return _fit(re.sub(r'[/\\. "$*<>:|?]', "_", slice_name), 63)

    def provision(self, slice_name: str) -> None:
        with contextlib.closing(self._client()) as client:
            db = client[self._ident(slice_name)]
            db[self.MARKER].update_one({"_id": "binky"}, {"$set": {"managed": True}}, upsert=True)

    def resolve(self, slice_name: str) -> str:
        return _with_dbname(self.admin_url, self._ident(slice_name))

    def teardown(self, slice_name: str) -> None:
        with contextlib.closing(self._client()) as client:
            client.drop_database(self._ident(slice_name))

    def clone(self, source_slice: str, dest_slice: str) -> None:
        source, dest = self._ident(source_slice), self._ident(dest_slice)
        with contextlib.closing(self._client()) as client:
            src = client[source]
            for name in src.list_collection_names():
                # $out into another database is server-side — no round-tripping
                # documents through this process.
                src[name].aggregate([{"$out": {"db": dest, "coll": name}}])
                for index in src[name].list_indexes():
                    keys = list(index["key"].items())
                    if index["name"] == "_id_":
                        continue  # created implicitly with the collection
                    carried = {"unique", "sparse", "expireAfterSeconds", "partialFilterExpression"}
                    options = {k: v for k, v in index.items() if k in carried}
                    client[dest][name].create_index(keys, name=index["name"], **options)


# --------------------------------------------------------------------------- #
#  S3 / MinIO — one bucket per worktree
# --------------------------------------------------------------------------- #
class S3Adapter(Adapter):
    """One bucket per worktree; the service's declared ``buckets`` become prefixes in it.

    Bucket-per-worktree (rather than bucket-per-declared-bucket) keeps the count
    flat: real S3 has a 100-bucket-per-account soft limit, and a swarm of agents
    would blow through it. MinIO has no such limit, but the shape stays the same.
    """

    CAPABILITIES: ClassVar[dict] = {"clone": True}

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.endpoint = config.get("admin_url") or config.get("endpoint_url")
        self.region = config.get("region") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
        self.access_key = config.get("access_key")
        self.secret_key = config.get("secret_key")
        self.prefixes = list(config.get("buckets") or [])

    def _client(self):
        boto3 = _driver("boto3", "s3")
        kwargs: dict = {"region_name": self.region}
        if self.endpoint:
            kwargs["endpoint_url"] = self.endpoint
        if self.access_key and self.secret_key:
            kwargs["aws_access_key_id"] = self.access_key
            kwargs["aws_secret_access_key"] = self.secret_key
        return boto3.client("s3", **kwargs)

    @staticmethod
    def _bucket(slice_name: str) -> str:
        # S3 bucket names: lowercase, 3-63 chars, [a-z0-9-], no leading/trailing dash.
        name = re.sub(r"-{2,}", "-", re.sub(r"[^a-z0-9-]", "-", slice_name.lower())).strip("-")
        if len(name) < 3:
            name = f"binky-{name}".strip("-")
        return _fit(name, 63)

    @staticmethod
    def _is(exc: Exception, *codes: str) -> bool:
        """botocore raises dynamically-built classes, so match on the error code."""
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        return code in codes or type(exc).__name__ in codes

    def provision(self, slice_name: str) -> None:
        bucket = self._bucket(slice_name)
        client = self._client()
        try:
            # us-east-1 is the one region that rejects an explicit LocationConstraint.
            if self.endpoint or self.region == "us-east-1":
                client.create_bucket(Bucket=bucket)
            else:
                client.create_bucket(
                    Bucket=bucket,
                    CreateBucketConfiguration={"LocationConstraint": self.region},
                )
        except Exception as exc:
            if not self._is(exc, "BucketAlreadyOwnedByYou", "BucketAlreadyExists"):
                raise AdapterError(f"could not create bucket {bucket}: {exc}") from exc
        for prefix in self.prefixes:
            client.put_object(Bucket=bucket, Key=f"{prefix.strip('/')}/", Body=b"")

    def resolve(self, slice_name: str) -> str:
        return f"s3://{self._bucket(slice_name)}"

    def _keys(self, client, bucket: str):
        paginator = client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket):
            for obj in page.get("Contents", []):
                yield obj["Key"]

    def teardown(self, slice_name: str) -> None:
        bucket = self._bucket(slice_name)
        client = self._client()
        try:
            batch: list[dict] = []
            for key in self._keys(client, bucket):
                batch.append({"Key": key})
                if len(batch) == 1000:  # the delete_objects hard limit
                    client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
                    batch = []
            if batch:
                client.delete_objects(Bucket=bucket, Delete={"Objects": batch})
            client.delete_bucket(Bucket=bucket)
        except Exception as exc:
            if not self._is(exc, "NoSuchBucket", "404"):
                raise AdapterError(f"could not delete bucket {bucket}: {exc}") from exc

    def clone(self, source_slice: str, dest_slice: str) -> None:
        source, dest = self._bucket(source_slice), self._bucket(dest_slice)
        self.provision(dest_slice)
        client = self._client()
        for key in self._keys(client, source):
            client.copy_object(Bucket=dest, Key=key, CopySource={"Bucket": source, "Key": key})


# --------------------------------------------------------------------------- #
#  RabbitMQ — one vhost per worktree
# --------------------------------------------------------------------------- #
class RabbitMQAdapter(Adapter):
    """One vhost per worktree, via the management HTTP API.

    No ``clone``: a vhost is empty by construction, so a golden template would copy
    nothing. ``clone_from`` on this adapter correctly falls back to provisioning the
    vhost per worktree, which is already instant.

    ``admin_url`` points at the *management* port (15672); ``options.amqp_port``
    (default 5672) is what ends up in the address handed to your app.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_RABBITMQ_URL")
        if not self.admin_url:
            raise AdapterError(
                "rabbitmq adapter needs an admin_url pointing at the management API "
                "(e.g. http://guest:guest@localhost:15672), or $BINKY_RABBITMQ_URL"
            )
        self._parts = urllib.parse.urlsplit(self.admin_url)
        self.amqp_port = int(config.get("amqp_port") or 5672)
        self.amqp_scheme = config.get("amqp_scheme") or "amqp"

    @staticmethod
    def _vhost(slice_name: str) -> str:
        return slice_name

    def provision(self, slice_name: str) -> None:
        vhost = urllib.parse.quote(self._vhost(slice_name), safe="")
        _http(self.admin_url, f"/api/vhosts/{vhost}", "PUT", {})
        user = urllib.parse.unquote(self._parts.username or "guest")
        # Without permissions the vhost exists but the app cannot use it.
        _http(
            self.admin_url,
            f"/api/permissions/{vhost}/{urllib.parse.quote(user, safe='')}",
            "PUT",
            {"configure": ".*", "write": ".*", "read": ".*"},
        )

    def resolve(self, slice_name: str) -> str:
        parts = self._parts
        userinfo = ""
        if parts.username:
            userinfo = parts.username + (f":{parts.password}" if parts.password else "") + "@"
        host = parts.hostname or "localhost"
        vhost = urllib.parse.quote(self._vhost(slice_name), safe="")
        return f"{self.amqp_scheme}://{userinfo}{host}:{self.amqp_port}/{vhost}"

    def teardown(self, slice_name: str) -> None:
        vhost = urllib.parse.quote(self._vhost(slice_name), safe="")
        _http(self.admin_url, f"/api/vhosts/{vhost}", "DELETE")


# --------------------------------------------------------------------------- #
#  Kafka / Redpanda — one topic prefix per worktree
# --------------------------------------------------------------------------- #
class KafkaAdapter(Adapter):
    """One topic prefix per worktree.

    Kafka has no namespace primitive, so the slice is a prefix: your app publishes
    to ``<prefix>.orders`` instead of ``orders``. The prefix is the path of the
    resolved address, and teardown deletes exactly the topics carrying it.

    This is the weakest isolation in the set — it is enforced by your app honouring
    the prefix, not by the broker. Consumer groups are *not* namespaced.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        url = config.get("admin_url") or os.environ.get("BINKY_KAFKA_URL") or ""
        if not url:
            raise AdapterError("kafka adapter needs an admin_url (or $BINKY_KAFKA_URL)")
        parts = urllib.parse.urlsplit(url if "://" in url else f"kafka://{url}")
        self.bootstrap = parts.netloc or "localhost:9092"
        self.topics = [t for t in (config.get("topics") or "").split(",") if t.strip()]
        self.partitions = int(config.get("partitions") or 1)
        self.replication = int(config.get("replication") or 1)

    def _admin(self):
        kafka = _driver("kafka", "kafka")
        try:
            return kafka.KafkaAdminClient(bootstrap_servers=self.bootstrap)
        except Exception as exc:
            raise AdapterError(f"could not connect to kafka at {self.bootstrap}: {exc}") from exc

    @staticmethod
    def _prefix(slice_name: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", slice_name)

    def provision(self, slice_name: str) -> None:
        if not self.topics:
            return  # topics are created by the app on first publish
        from kafka.admin import NewTopic

        prefix = self._prefix(slice_name)
        admin = self._admin()
        wanted = [
            NewTopic(f"{prefix}.{t.strip()}", self.partitions, self.replication)
            for t in self.topics
        ]
        try:
            existing = set(admin.list_topics())
            missing = [t for t in wanted if t.name not in existing]
            if missing:
                admin.create_topics(missing)
        finally:
            admin.close()

    def resolve(self, slice_name: str) -> str:
        return f"kafka://{self.bootstrap}/{self._prefix(slice_name)}"

    def teardown(self, slice_name: str) -> None:
        prefix = self._prefix(slice_name) + "."
        admin = self._admin()
        try:
            doomed = [t for t in admin.list_topics() if t.startswith(prefix)]
            if doomed:
                admin.delete_topics(doomed)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(f"could not delete kafka topics for {slice_name}: {exc}") from exc
        finally:
            admin.close()


# --------------------------------------------------------------------------- #
#  Qdrant — one collection prefix per worktree
# --------------------------------------------------------------------------- #
class QdrantAdapter(Adapter):
    """One collection prefix per worktree.

    Qdrant collections are flat and carry their own vector schema (size, distance
    metric) that only your app knows — so `provision` is a no-op and your code
    creates ``<prefix>__embeddings`` itself. Teardown drops every collection under
    the prefix, which is the part Binky can own honestly.
    """

    SEP: ClassVar[str] = "__"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = (
            config.get("admin_url") or os.environ.get("BINKY_QDRANT_URL") or "http://localhost:6333"
        ).rstrip("/")
        self.api_key = config.get("api_key")

    def _get(self, path: str, method: str = "GET") -> dict | None:
        headers = {"api-key": self.api_key} if self.api_key else None
        return _http(self.admin_url, path, method, headers=headers)

    @staticmethod
    def _prefix(slice_name: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", slice_name)

    def provision(self, slice_name: str) -> None:
        # Reachability check only: creating a collection needs a vector config we
        # do not have, and failing here is far better than failing at first write.
        self._get("/collections")

    def resolve(self, slice_name: str) -> str:
        return f"{self.admin_url}/{self._prefix(slice_name)}"

    def teardown(self, slice_name: str) -> None:
        prefix = self._prefix(slice_name)
        payload = self._get("/collections") or {}
        names = [c["name"] for c in payload.get("result", {}).get("collections", [])]
        for name in names:
            if name == prefix or name.startswith(prefix + self.SEP):
                self._get(f"/collections/{urllib.parse.quote(name, safe='')}", "DELETE")


# --------------------------------------------------------------------------- #
#  ClickHouse — one database per worktree
# --------------------------------------------------------------------------- #
class ClickHouseAdapter(Adapter):
    """One database per worktree, over ClickHouse's HTTP interface.

    Same shape as the Postgres adapter — ``CREATE DATABASE`` / ``DROP DATABASE`` —
    and it needs no driver at all: ClickHouse accepts SQL as a plain HTTP POST body,
    so this adapter is stdlib-only.

    No ``clone``. ClickHouse has no ``CREATE DATABASE ... AS``; copying would mean
    ``INSERT INTO dest SELECT * FROM source`` per table, which is O(rows) — exactly
    the cost ``clone_from`` exists to avoid. Reporting ``clone: false`` makes
    ``clone_from`` fall back to per-worktree migrate+seed, which is honest, rather
    than dressing a full table copy up as an instant snapshot.

    ``admin_url`` is the HTTP endpoint (default port 8123), e.g.
    ``http://default:@localhost:8123``; it is also what the resolved address is
    built from, with the slice's database as the path.
    """

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = config.get("admin_url") or os.environ.get("BINKY_CLICKHOUSE_URL")
        if not self.admin_url:
            raise AdapterError(
                "clickhouse adapter needs an admin_url pointing at the HTTP interface "
                "(e.g. http://default:@localhost:8123), or $BINKY_CLICKHOUSE_URL"
            )

    def _query(self, sql: str) -> str:
        root, headers = _split_auth(self.admin_url)
        request = urllib.request.Request(
            root + "/", method="POST", data=sql.encode(), headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read().decode(errors="replace")
        except urllib.error.HTTPError as exc:
            # ClickHouse reports SQL errors as 500 with the message in the body.
            detail = exc.read().decode(errors="replace").strip()[:300]
            raise AdapterError(f"clickhouse query failed: {exc.code} {detail}") from exc
        except OSError as exc:
            raise AdapterError(f"could not reach clickhouse at {root}: {exc}") from exc

    @staticmethod
    def _ident(slice_name: str) -> str:
        # A ClickHouse database becomes a directory on disk, so keep it to the same
        # conservative alphabet the other SQL adapters use and assert it, since the
        # name is interpolated into DDL.
        name = _fit(slice_name, 63)
        if not re.fullmatch(r"[A-Za-z0-9_]+", name):
            raise AdapterError(f"unsafe clickhouse database name: {slice_name!r}")
        return name

    def provision(self, slice_name: str) -> None:
        self._query(f'CREATE DATABASE IF NOT EXISTS "{self._ident(slice_name)}"')

    def resolve(self, slice_name: str) -> str:
        return _with_dbname(self.admin_url, self._ident(slice_name))

    def teardown(self, slice_name: str) -> None:
        self._query(f'DROP DATABASE IF EXISTS "{self._ident(slice_name)}"')


# --------------------------------------------------------------------------- #
#  Elasticsearch / OpenSearch — one index prefix per worktree
# --------------------------------------------------------------------------- #
class ElasticsearchAdapter(Adapter):
    """One index prefix per worktree, for Elasticsearch or OpenSearch.

    Both speak the same index CRUD API, so one class serves both names.

    Like Kafka's, this slice is a *prefix*: your app writes to ``<prefix>-products``
    instead of ``products``. It is enforced by your app honouring the prefix, not by
    the server — Elasticsearch has no tenant primitive below the cluster.

    ``provision`` only checks reachability by default, because an index's mappings
    belong to your app, not to Binky; list ``options.indices`` to have them created
    empty up front. Teardown enumerates indices and deletes the ones under the prefix
    by exact name — never by wildcard, which ES 8 refuses by default
    (``action.destructive_requires_name``) and which would be a bad thing to teach.
    """

    SEP: ClassVar[str] = "-"

    def __init__(self, config: dict) -> None:
        super().__init__(config)
        self.admin_url = (
            config.get("admin_url")
            or os.environ.get("BINKY_ELASTICSEARCH_URL")
            or "http://localhost:9200"
        ).rstrip("/")
        self.api_key = config.get("api_key")
        self.indices = [i.strip() for i in (config.get("indices") or "").split(",") if i.strip()]

    def _call(self, path: str, method: str = "GET", body: dict | None = None):
        headers = {"authorization": f"ApiKey {self.api_key}"} if self.api_key else None
        return _http(self.admin_url, path, method, body, headers)

    @staticmethod
    def _prefix(slice_name: str) -> str:
        # Index names are lowercase and cannot contain \/*?"<>| , # or a leading
        # -, _ or +. The slice name is already [a-z0-9_], so this is belt-and-braces.
        name = re.sub(r"[^a-z0-9_.-]", "-", slice_name.lower()).lstrip("-_+.")
        return _fit(name, 200)  # ES caps index names at 255 bytes; leave room for the suffix

    def _owned(self, slice_name: str) -> list[str]:
        prefix = self._prefix(slice_name)
        listing = self._call("/_cat/indices?format=json&h=index") or []
        names = [row["index"] for row in listing if isinstance(row, dict) and "index" in row]
        return [n for n in names if n == prefix or n.startswith(prefix + self.SEP)]

    def provision(self, slice_name: str) -> None:
        # Reachability first: failing here beats failing at your app's first write.
        self._call("/")
        prefix = self._prefix(slice_name)
        existing = set(self._owned(slice_name))
        for index in self.indices:
            name = f"{prefix}{self.SEP}{index}"
            if name not in existing:
                self._call(f"/{urllib.parse.quote(name, safe='')}", "PUT", {})

    def resolve(self, slice_name: str) -> str:
        return f"{self.admin_url}/{self._prefix(slice_name)}"

    def teardown(self, slice_name: str) -> None:
        for name in self._owned(slice_name):
            self._call(f"/{urllib.parse.quote(name, safe='')}", "DELETE")
