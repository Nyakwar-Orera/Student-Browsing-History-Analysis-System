# app/database/koha_connection.py
"""
Thread-Safe Bounded Koha Database Connection Pool
- Hard cap of 3 simultaneous Koha connections (prevents 'Too many connections')
- Circuit breaker: after 3 consecutive failures, back off for 60 s before retrying
- Query-level TTL cache (10 min) so the same SQL is never re-executed within the window
- Graceful degradation: every public method returns [] / None on failure — never raises
"""
import os
import threading
import logging
import time
from datetime import datetime
from contextlib import contextmanager
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("koha_connection")

# ── Pool / Circuit-Breaker Configuration ─────────────────────────────────────
MAX_CONNECTIONS = int(os.getenv("KOHA_POOL_SIZE", 3))       # hard cap on Koha connections
CONNECT_TIMEOUT = int(os.getenv("KOHA_CONNECT_TIMEOUT", 8))
READ_TIMEOUT    = int(os.getenv("KOHA_READ_TIMEOUT", 30))
CACHE_TTL       = int(os.getenv("KOHA_CACHE_TTL", 600))     # 10 min query cache
CB_FAILURE_THRESHOLD = 3     # open circuit after N consecutive failures
CB_RECOVERY_SECONDS  = 60   # seconds to wait before retrying after circuit opens


class KohaDatabasePool:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.config = {
            "host":            os.getenv("KOHA_HOST", "152.55.221.78"),
            "port":            int(os.getenv("KOHA_PORT", 3306)),
            "user":            os.getenv("KOHA_USER", "library_read"),
            "password":        os.getenv("KOHA_PASSWORD", "Library@5152"),
            "database":        os.getenv("KOHA_DATABASE", "koha_library"),
            "charset":         "utf8mb4",
            "autocommit":      True,
            "connect_timeout": CONNECT_TIMEOUT,
            "read_timeout":    READ_TIMEOUT,
            "write_timeout":   10,
        }

        # Query cache
        self._cache: dict = {}
        self._cache_lock = threading.Lock()

        # Circuit-breaker state
        self._cb_failures   = 0
        self._cb_open_until = 0.0   # epoch time when circuit is allowed to close again
        self._cb_lock       = threading.Lock()

        # Bounded pool (lazy import so startup never fails if DBUtils is absent)
        self._pool      = None
        self._pool_lock = threading.Lock()
        self._pool_error: str = ""

    # ── Pool Initialisation ───────────────────────────────────────────────────

    def _ensure_pool(self) -> bool:
        """Lazily build the DBUtils PooledDB pool. Returns True if pool is ready."""
        if self._pool is not None:
            return True
        with self._pool_lock:
            if self._pool is not None:   # double-check after acquiring lock
                return True
            try:
                import pymysql
                from dbutils.pooled_db import PooledDB
                self._pool = PooledDB(
                    creator=pymysql,
                    maxconnections=MAX_CONNECTIONS,   # hard cap
                    mincached=0,                      # don't pre-open connections
                    maxcached=2,                      # keep at most 2 idle sockets
                    maxshared=0,                      # no sharing between threads
                    blocking=True,                    # wait (don't raise) when pool full
                    maxusage=None,                    # no per-connection query limit
                    ping=1,                           # ping before using a connection
                    **self.config,
                )
                logger.info(f"[Koha DB] PooledDB ready — max {MAX_CONNECTIONS} connections")
                return True
            except ImportError:
                # DBUtils not installed — fall back to direct single-connection approach
                self._pool_error = "DBUtils not installed; using fallback single-connection mode"
                logger.warning(f"[Koha DB] {self._pool_error}")
                return False
            except Exception as e:
                self._pool_error = str(e)
                logger.warning(f"[Koha DB] Pool init failed: {e}")
                return False

    # ── Circuit Breaker ───────────────────────────────────────────────────────

    def _circuit_open(self) -> bool:
        """Returns True if the circuit is open (too many recent failures)."""
        with self._cb_lock:
            if self._cb_failures >= CB_FAILURE_THRESHOLD:
                if time.time() < self._cb_open_until:
                    return True
                else:
                    # Recovery window elapsed — reset and allow one probe
                    self._cb_failures = 0
                    self._cb_open_until = 0.0
            return False

    def _record_success(self):
        with self._cb_lock:
            self._cb_failures = 0
            self._cb_open_until = 0.0

    def _record_failure(self):
        with self._cb_lock:
            self._cb_failures += 1
            if self._cb_failures >= CB_FAILURE_THRESHOLD:
                self._cb_open_until = time.time() + CB_RECOVERY_SECONDS
                logger.warning(
                    f"[Koha DB] Circuit opened after {self._cb_failures} failures — "
                    f"pausing for {CB_RECOVERY_SECONDS}s"
                )

    # ── Connection Context Manager ────────────────────────────────────────────

    @contextmanager
    def get_connection(self):
        """Yield a live connection from the pool (or a fresh direct connection as fallback)."""
        try:
            import pymysql
        except ImportError:
            raise ConnectionError(
                "[Koha DB] pymysql is not installed — run: pip install pymysql"
            )

        if self._circuit_open():
            raise ConnectionError("[Koha DB] Circuit is open — refusing connection attempt")

        conn = None
        try:
            if self._ensure_pool() and self._pool is not None:
                conn = self._pool.connection()
            else:
                # Fallback: direct connection (not pooled, but at least bounded by circuit-breaker)
                conn = pymysql.connect(**self.config)

            self._record_success()
            yield conn

        except ConnectionError:
            raise
        except Exception as e:
            self._record_failure()
            logger.warning(f"[Koha DB] Failed to connect to {self.config['host']}: {e}")
            raise
        finally:
            if conn is not None:
                try:
                    conn.close()   # returns to pool (PooledDB) or closes directly
                except Exception:
                    pass

    # ── Public API ────────────────────────────────────────────────────────────

    def is_available(self) -> bool:
        """
        Lightweight availability check using circuit-breaker state only.
        Does NOT open a new connection — avoids the connection-storm anti-pattern.
        """
        if self._circuit_open():
            return False
        # If we've never failed, assume available (optimistic); errors will open circuit
        return True

    # Keep is_connected() as a soft alias so existing callers don't break,
    # but make it non-probing (no extra SQL) to prevent connection storms.
    def is_connected(self) -> bool:
        return self.is_available()

    def query(self, sql: str, params=None) -> list:
        """Execute SQL with TTL caching. Returns [] on any failure."""
        cache_key = f"{sql}|{params}"
        now = datetime.now()

        with self._cache_lock:
            if cache_key in self._cache:
                ts, data = self._cache[cache_key]
                if (now - ts).total_seconds() < CACHE_TTL:
                    return data

        if not self.is_available():
            return []

        try:
            import pymysql
        except ImportError:
            logger.debug("[Koha DB] pymysql not installed — run fix_and_start.bat to install")
            return []

        try:
            with self.get_connection() as conn:
                with conn.cursor(pymysql.cursors.DictCursor) as cur:
                    cur.execute(sql, params or ())
                    result = list(cur.fetchall())
            with self._cache_lock:
                self._cache[cache_key] = (now, result)
            return result
        except ConnectionError:
            return []
        except Exception as e:
            logger.warning(f"[Koha DB] Query failed: {e}")
            return []

    def query_one(self, sql: str, params=None):
        """Execute SQL and return first row dict, or None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def clear_cache(self):
        with self._cache_lock:
            self._cache.clear()

    def status(self) -> dict:
        """Return diagnostic status dict for health-check endpoints."""
        return {
            "circuit_open":   self._circuit_open(),
            "cb_failures":    self._cb_failures,
            "cb_open_until":  self._cb_open_until,
            "pool_ready":     self._pool is not None,
            "max_connections": MAX_CONNECTIONS,
            "cache_entries":  len(self._cache),
        }


# Global singleton
koha_db = KohaDatabasePool()
