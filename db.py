import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import time
from contextlib import contextmanager

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
IS_SERVERLESS = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV") or not os.access(BASE_DIR, os.W_OK))
DATA_DIR = os.path.join(tempfile.gettempdir(), "espadazo_data") if IS_SERVERLESS else os.path.join(BASE_DIR, "data")
DB_PATH = os.environ.get("SQLITE_DB_PATH") or os.path.join(DATA_DIR, "database.db")
SEED_JSON_PATH = os.path.join(BASE_DIR, "data", "db.json")
LEGACY_JSON_PATH = SEED_JSON_PATH
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
RECEIPTS_CACHE_DIR = os.path.join(DATA_DIR, "receipts_cache")

_init_lock = threading.Lock()
_initialized = False


def hash_pin(pin: str) -> str:
    """Genera un hash PBKDF2-HMAC-SHA256 con salt aleatorio para el PIN administrativo."""
    pin_clean = str(pin or "").strip()
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin_clean.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
    return f"pbkdf2_sha256${salt}${digest}"


def verify_pin(stored_value: str, provided_pin: str) -> bool:
    """Verifica el PIN en tiempo constante soportando hashes PBKDF2 y migración heredada."""
    stored = str(stored_value or "")
    candidate = str(provided_pin or "").strip()
    if stored.startswith("pbkdf2_sha256$"):
        parts = stored.split("$")
        if len(parts) != 3:
            return False
        _, salt, expected_hex = parts
        digest = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt.encode("utf-8"), 120_000).hex()
        return hmac.compare_digest(digest, expected_hex)
    return hmac.compare_digest(stored, candidate)


def _configure_connection(conn: sqlite3.Connection) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def get_db(write: bool = False):
    """Context manager ACID para SQLite WAL. Usa BEGIN IMMEDIATE en escrituras."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10.0, isolation_level=None, check_same_thread=False)
    try:
        _configure_connection(conn)
        if write:
            conn.execute("BEGIN IMMEDIATE;")
        yield conn
        if write and conn.in_transaction:
            conn.execute("COMMIT;")
    except Exception:
        if write and conn.in_transaction:
            try:
                conn.execute("ROLLBACK;")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()


def _create_schema(conn: sqlite3.Connection) -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS sequences (
            name TEXT PRIMARY KEY,
            val INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS dishes (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            category TEXT NOT NULL,
            price REAL NOT NULL,
            img TEXT,
            images TEXT NOT NULL DEFAULT '[]',
            badge TEXT DEFAULT '',
            desc TEXT DEFAULT '',
            options TEXT NOT NULL DEFAULT '[]',
            published INTEGER NOT NULL DEFAULT 1,
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS tables (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            capacity INTEGER NOT NULL DEFAULT 4,
            status TEXT NOT NULL DEFAULT 'libre',
            sort_order INTEGER NOT NULL DEFAULT 0
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY,
            seq INTEGER,
            table_id TEXT NOT NULL,
            table_name TEXT NOT NULL,
            service_mode TEXT NOT NULL,
            time TEXT NOT NULL,
            timestamp REAL NOT NULL,
            items TEXT NOT NULL,
            total REAL NOT NULL,
            notes TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pedido',
            paid INTEGER NOT NULL DEFAULT 0,
            paid_at TEXT DEFAULT ''
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY,
            seq INTEGER UNIQUE NOT NULL,
            number TEXT UNIQUE NOT NULL,
            issued_at TEXT NOT NULL,
            table_id TEXT DEFAULT '',
            table_name TEXT DEFAULT '',
            mode TEXT DEFAULT '',
            customer_name TEXT DEFAULT 'CLIENTES VARIOS',
            customer_doc TEXT DEFAULT '00000000',
            customer_phone TEXT DEFAULT '',
            payment_method TEXT DEFAULT 'YAPE / PLIN',
            op_number TEXT DEFAULT '',
            hash TEXT DEFAULT '',
            items TEXT NOT NULL,
            op_gravada REAL NOT NULL DEFAULT 0,
            igv REAL NOT NULL DEFAULT 0,
            total REAL NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_orders_table_paid ON orders(table_id, paid)",
        "CREATE INDEX IF NOT EXISTS idx_orders_status_paid ON orders(status, paid)",
        "CREATE INDEX IF NOT EXISTS idx_orders_timestamp ON orders(timestamp)",
    ]
    for stmt in statements:
        conn.execute(stmt)


def _extract_numeric_suffix(value: str, default: int = 0) -> int:
    match = re.search(r"(\d+)$", str(value or ""))
    return int(match.group(1)) if match else default


def _migrate_legacy_json(conn: sqlite3.Connection, local_ip: str) -> None:
    if not os.path.exists(LEGACY_JSON_PATH):
        return
    with open(LEGACY_JSON_PATH, "r", encoding="utf-8") as f:
        legacy = json.load(f)

    bak_path = os.path.join(DATA_DIR, "db.json.pre_sqlite_bak")
    if not os.path.exists(bak_path):
        try:
            os.makedirs(DATA_DIR, exist_ok=True)
            shutil.copy2(LEGACY_JSON_PATH, bak_path)
        except OSError:
            pass

    raw_settings = dict(legacy.get("settings") or {})
    raw_pin = str(raw_settings.pop("admin_pin", "1234") or "1234")
    if "admin_pin_hash" not in raw_settings:
        raw_settings["admin_pin_hash"] = hash_pin(raw_pin)
    raw_settings.setdefault("restaurant_name", "Espadazo Marino")
    raw_settings.setdefault("phone", "51987654321")
    raw_settings.setdefault("host_ip", local_ip)
    raw_settings.setdefault("port", 8080)
    raw_settings.setdefault("public_url", "")
    raw_settings.setdefault("auto_bell", True)

    for k, v in raw_settings.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (str(k), json.dumps(v, ensure_ascii=False)),
        )

    for idx, d in enumerate(legacy.get("dishes") or []):
        images = d.get("images") if isinstance(d.get("images"), list) else ([d["img"]] if d.get("img") else [])
        conn.execute(
            """
            INSERT OR REPLACE INTO dishes (id, name, category, price, img, images, badge, desc, options, published, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(d.get("id") or f"dish_{idx}"),
                str(d.get("name") or "Platillo"),
                str(d.get("category") or "ceviches"),
                float(d.get("price", 0.0)),
                str(d.get("img") or (images[0] if images else "")),
                json.dumps(images, ensure_ascii=False),
                str(d.get("badge") or ""),
                str(d.get("desc") or ""),
                json.dumps(d.get("options") or [], ensure_ascii=False),
                1 if d.get("published", True) else 0,
                idx,
            ),
        )

    for idx, t in enumerate(legacy.get("tables") or []):
        conn.execute(
            """
            INSERT OR REPLACE INTO tables (id, name, capacity, status, sort_order)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(t.get("id")),
                str(t.get("name") or f"Mesa {t.get('id')}"),
                int(t.get("capacity", 4)),
                str(t.get("status") or "libre"),
                idx,
            ),
        )

    max_order_seq = 100
    seen_order_ids = set()
    base_ts = time.time() - len(legacy.get("orders") or [])
    for idx, o in enumerate(legacy.get("orders") or []):
        raw_oid = str(o.get("id") or f"ORD-{101 + idx:04d}")
        oid = raw_oid if raw_oid not in seen_order_ids else f"{raw_oid}-{idx}"
        seen_order_ids.add(oid)
        seq_val = _extract_numeric_suffix(raw_oid, 101 + idx)
        if seq_val > max_order_seq:
            max_order_seq = seq_val
        conn.execute(
            """
            INSERT OR REPLACE INTO orders
            (id, seq, table_id, table_name, service_mode, time, timestamp, items, total, notes, status, paid, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                oid,
                seq_val,
                str(o.get("table_id") or "1"),
                str(o.get("table_name") or "Mesa 1"),
                str(o.get("service_mode") or "En Barra"),
                str(o.get("time") or time.strftime("%I:%M %p")),
                float(o.get("timestamp") or (base_ts + idx)),
                json.dumps(o.get("items") or [], ensure_ascii=False),
                float(o.get("total", 0.0)),
                str(o.get("notes") or ""),
                str(o.get("status") or "pedido"),
                1 if o.get("paid", False) else 0,
                str(o.get("paid_at") or ""),
            ),
        )

    max_receipt_seq = 4812
    for idx, r in enumerate(legacy.get("receipts") or []):
        rec_id = str(r.get("id") or f"TCK-{4813 + idx}")
        seq_val = _extract_numeric_suffix(rec_id, 4813 + idx)
        if seq_val > max_receipt_seq:
            max_receipt_seq = seq_val
        conn.execute(
            """
            INSERT OR REPLACE INTO receipts
            (id, seq, number, issued_at, table_id, table_name, mode, customer_name, customer_doc,
             customer_phone, payment_method, op_number, hash, items, op_gravada, igv, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec_id,
                seq_val,
                str(r.get("number") or f"T001 - {seq_val:08d}"),
                str(r.get("issued_at") or time.strftime("%d/%m/%Y %H:%M:%S")),
                str(r.get("table_id") or ""),
                str(r.get("table_name") or ""),
                str(r.get("mode") or ""),
                str(r.get("customer_name") or "CLIENTES VARIOS"),
                str(r.get("customer_doc") or "00000000"),
                str(r.get("customer_phone") or ""),
                str(r.get("payment_method") or "YAPE / PLIN"),
                str(r.get("op_number") or ""),
                str(r.get("hash") or ""),
                json.dumps(r.get("items") or [], ensure_ascii=False),
                float(r.get("op_gravada", 0.0)),
                float(r.get("igv", 0.0)),
                float(r.get("total", 0.0)),
            ),
        )

    conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('order', ?)", (max_order_seq,))
    conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('receipt', ?)", (max_receipt_seq,))


def _seed_defaults(conn: sqlite3.Connection, local_ip: str) -> None:
    defaults = {
        "restaurant_name": "Espadazo Marino",
        "admin_pin_hash": hash_pin("1234"),
        "phone": "51987654321",
        "host_ip": local_ip,
        "port": 8080,
        "public_url": "",
        "auto_bell": True,
    }
    for k, v in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (k, json.dumps(v, ensure_ascii=False)),
        )
    conn.execute("INSERT OR IGNORE INTO sequences (name, val) VALUES ('order', 100)")
    conn.execute("INSERT OR IGNORE INTO sequences (name, val) VALUES ('receipt', 4812)")


def init_db(local_ip: str = "127.0.0.1") -> None:
    """Inicializa SQLite WAL, migra db.json si la base esta vacia y asegura valores base."""
    global _initialized
    with _init_lock:
        if _initialized and os.path.exists(DB_PATH):
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(BACKUP_DIR, exist_ok=True)
        os.makedirs(RECEIPTS_CACHE_DIR, exist_ok=True)
        with get_db(write=True) as conn:
            _create_schema(conn)
            has_data = (
                conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] > 0
                or conn.execute("SELECT COUNT(*) FROM dishes").fetchone()[0] > 0
                or conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0] > 0
                or conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] > 0
            )
            if not has_data and os.path.exists(LEGACY_JSON_PATH):
                _migrate_legacy_json(conn, local_ip)
            else:
                _seed_defaults(conn, local_ip)
        _initialized = True


def next_sequence(conn: sqlite3.Connection, name: str, initial: int = 0) -> int:
    """Incrementa atomicamente una secuencia dentro de una transaccion BEGIN IMMEDIATE."""
    conn.execute("INSERT OR IGNORE INTO sequences (name, val) VALUES (?, ?)", (name, initial))
    conn.execute("UPDATE sequences SET val = val + 1 WHERE name = ?", (name,))
    row = conn.execute("SELECT val FROM sequences WHERE name = ?", (name,)).fetchone()
    return int(row["val"])


def get_all_settings(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    result = {}
    for row in rows:
        try:
            result[row["key"]] = json.loads(row["value"])
        except Exception:
            result[row["key"]] = row["value"]
    return result


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (str(key), json.dumps(value, ensure_ascii=False)),
    )


def row_to_dish(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "category": row["category"],
        "price": float(row["price"]),
        "img": row["img"] or "",
        "images": json.loads(row["images"] or "[]"),
        "badge": row["badge"] or "",
        "desc": row["desc"] or "",
        "options": json.loads(row["options"] or "[]"),
        "published": bool(row["published"]),
    }


def row_to_table(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "name": row["name"],
        "capacity": int(row["capacity"]),
        "status": row["status"],
    }


def row_to_order(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "table_id": row["table_id"],
        "table_name": row["table_name"],
        "service_mode": row["service_mode"],
        "time": row["time"],
        "timestamp": float(row["timestamp"]),
        "items": json.loads(row["items"] or "[]"),
        "total": float(row["total"]),
        "notes": row["notes"] or "",
        "status": row["status"],
        "paid": bool(row["paid"]),
        "paid_at": row["paid_at"] or "",
    }


def row_to_receipt(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "number": row["number"],
        "issued_at": row["issued_at"],
        "table_id": row["table_id"] or "",
        "table_name": row["table_name"] or "",
        "mode": row["mode"] or "",
        "customer_name": row["customer_name"] or "CLIENTES VARIOS",
        "customer_doc": row["customer_doc"] or "00000000",
        "customer_phone": row["customer_phone"] or "",
        "payment_method": row["payment_method"] or "YAPE / PLIN",
        "op_number": row["op_number"] or "",
        "hash": row["hash"] or "",
        "items": json.loads(row["items"] or "[]"),
        "op_gravada": float(row["op_gravada"]),
        "igv": float(row["igv"]),
        "total": float(row["total"]),
    }


def backup_database(max_backups: int = 7) -> str:
    """Crea un respaldo consistente en caliente usando sqlite3.backup() y rota los antiguos."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    dst_path = os.path.join(BACKUP_DIR, f"database_{ts}.db")
    with sqlite3.connect(DB_PATH, timeout=10.0) as src, sqlite3.connect(dst_path) as dst:
        src.backup(dst)
    backups = sorted(
        [
            os.path.join(BACKUP_DIR, f)
            for f in os.listdir(BACKUP_DIR)
            if f.startswith("database_") and f.endswith(".db")
        ]
    )
    for old_path in backups[:-max_backups]:
        try:
            os.remove(old_path)
        except OSError:
            pass
    return dst_path
