import psycopg2
import os
import sys
import threading
from psycopg2.pool import ThreadedConnectionPool
from dotenv import load_dotenv

if getattr(sys, 'frozen', False):
    base_path = os.path.dirname(sys.executable)
else:
    base_path = os.path.dirname(os.path.abspath(__file__))

env_path = os.path.join(base_path, '.env')
load_dotenv(env_path, override=True)

MAX_CONN = 10  # keep well under 15 (multiply by number of workers!)

_pool = None
_pool_lock = threading.Lock()
_slots = threading.BoundedSemaphore(MAX_CONN)  # makes extra requests wait instead of failing


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                url = os.getenv("DATABASE_URL")
                if not url:
                    raise Exception(f"DATABASE_URL not found in {env_path}")
                _pool = ThreadedConnectionPool(1, MAX_CONN, dsn=url)
    return _pool


class _PooledConnection:
    """Behaves like a psycopg2 connection, but close() returns it to the pool."""

    def __init__(self, conn):
        self._conn = conn
        self._closed = False

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __enter__(self):
        return self._conn.__enter__()

    def __exit__(self, *args):
        return self._conn.__exit__(*args)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            if not self._conn.closed:
                self._conn.rollback()  # never hand back a dirty transaction
        except Exception:
            pass
        try:
            _get_pool().putconn(self._conn, close=bool(self._conn.closed))
        finally:
            _slots.release()


def get_connection():
    _slots.acquire()  # blocks if all MAX_CONN are in use
    try:
        conn = _get_pool().getconn()
        if conn.closed:  # stale connection dropped by the server
            _get_pool().putconn(conn, close=True)
            conn = _get_pool().getconn()
    except Exception:
        _slots.release()
        raise
    return _PooledConnection(conn)