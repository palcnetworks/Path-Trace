"""
dbclient.py — Access layer over the four SONiC Redis databases.

RedisBackend is the only
concrete backend here — it talks to a real SONiC Redis (a real switch or SONiC VS). The
DBBackend interface it implements is what the rest of the engine (walker, correlator)
depends on, so anything else that speaks the same interface — such as
tests/mock_backend.py's MockBackend — works without this module ever knowing it exists.

DBBackend exposes:
  get_hash(db, key)          -> dict | None      (HGETALL, None if key absent)
  scan_type(db, type_prefix) -> list[(key, dict)] (all objects whose key starts prefix)
  scan_prefix(db, key_prefix)-> list[(key, dict)] (all keys under `db` starting with prefix)
  logs()                     -> list[str]         (syslog lines for the correlator)

SONiC stores each object as a Redis hash. In ASIC_DB the key is
'ASIC_STATE:SAI_OBJECT_TYPE_*:oid:0x...'; we scan by the type prefix.
"""

from __future__ import annotations
from typing import Optional
from .resolver import DB_INDEX

ASIC_KEY_PREFIX = "ASIC_STATE:"


class DBBackend:
    def get_hash(self, db: str, key: str) -> Optional[dict]:
        raise NotImplementedError

    def scan_type(self, db: str, type_prefix: str) -> list[tuple[str, dict]]:
        raise NotImplementedError

    def scan_prefix(self, db: str, key_prefix: str) -> list[tuple[str, dict]]:
        """List (key, fields) for all keys in `db` starting with `key_prefix`.
        Used to enumerate keys of an object type."""
        raise NotImplementedError

    def logs(self) -> list[str]:
        """Return syslog lines (used by the correlator). Real backend tails files."""
        return []


class RedisBackend(DBBackend):
    def __init__(self, host="127.0.0.1", port=6379, syslog_path="/var/log/syslog"):
        import redis  # imported lazily so callers that never touch Redis need not have it
        self._redis = redis
        self._host, self._port = host, port
        self._conns: dict[int, "redis.Redis"] = {}
        self._syslog_path = syslog_path

    def _conn(self, db: str):
        idx = DB_INDEX[db]
        if idx not in self._conns:
            self._conns[idx] = self._redis.Redis(
                host=self._host, port=self._port, db=idx, decode_responses=True
            )
        return self._conns[idx]

    def get_hash(self, db: str, key: str) -> Optional[dict]:
        c = self._conn(db)
        if not c.exists(key):
            return None
        return c.hgetall(key)

    def scan_type(self, db: str, type_prefix: str) -> list[tuple[str, dict]]:
        c = self._conn(db)
        pattern = f"{ASIC_KEY_PREFIX}{type_prefix}:*"
        out = []
        for k in c.scan_iter(match=pattern, count=500):
            fields = c.hgetall(k)
            fields["__key__"] = k
            out.append((k, fields))
        return out

    def scan_prefix(self, db: str, key_prefix: str) -> list[tuple[str, dict]]:
        c = self._conn(db)
        out = []
        for k in c.scan_iter(match=f"{key_prefix}*", count=500):
            fields = c.hgetall(k)
            fields["__key__"] = k
            out.append((k, fields))
        return out

    def logs(self) -> list[str]:
        # Real switch: read the syslog file.
        try:
            with open(self._syslog_path, "r", errors="ignore") as f:
                # last ~2000 lines is plenty for a single-object correlation window
                lines = f.readlines()[-2000:]
                if lines:
                    return lines
        except OSError:
            pass
        # SONiC VS fallback: seeded syslog stashed in STATE_DB by tests/seed.py.
        try:
            from .resolver import DB_INDEX
            c = self._redis.Redis(host=self._host, port=self._port,
                                  db=DB_INDEX["STATE_DB"], decode_responses=True)
            return [l + "\n" for l in c.lrange("__pathtrace_syslog__", 0, -1)]
        except Exception:
            return []


class CachedBackend(DBBackend):
    """Read-through cache over another backend, for one run.

    Audit and dependency traces ask for the same scans (e.g. every ASIC_DB host interface)
    hundreds of times; against a live switch the state is effectively a snapshot for the few
    seconds a run takes, so cache it. Never used for single-object traces.
    """

    def __init__(self, inner: DBBackend):
        self._inner = inner
        self._hash: dict = {}
        self._type: dict = {}
        self._prefix: dict = {}
        self._logs = None

    def get_hash(self, db: str, key: str) -> Optional[dict]:
        k = (db, key)
        if k not in self._hash:
            self._hash[k] = self._inner.get_hash(db, key)
        return self._hash[k]

    def scan_type(self, db: str, type_prefix: str) -> list[tuple[str, dict]]:
        k = (db, type_prefix)
        if k not in self._type:
            self._type[k] = self._inner.scan_type(db, type_prefix)
        return self._type[k]

    def scan_prefix(self, db: str, key_prefix: str) -> list[tuple[str, dict]]:
        k = (db, key_prefix)
        if k not in self._prefix:
            self._prefix[k] = self._inner.scan_prefix(db, key_prefix)
        return self._prefix[k]

    def logs(self) -> list[str]:
        if self._logs is None:
            self._logs = self._inner.logs()
        return self._logs
