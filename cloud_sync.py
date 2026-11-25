import json
import os
import re
import ssl
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from db import BASE_DIR, DB_PATH, IS_SERVERLESS, get_db

SCHEMA_SQL_PATH = os.path.join(BASE_DIR, "supabase_schema.sql")
_sync_lock = threading.Lock()
_last_pull_ts = 0.0
_schema_verified_for_url = set()

SENSITIVE_SETTING_KEYS = {
    "supabase_key",
    "supabase_service_key",
    "postgres_url",
    "host_ip",
}


def _load_env_file_vars() -> dict:
    """Lee variables de .env.local o .env sin requerir python-dotenv."""
    env_vars = {}
    candidates = [
        os.path.join(BASE_DIR, ".env"),
        os.path.join(BASE_DIR, ".env.local"),
    ]
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    line = raw_line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip()
                    if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
                        v = v[1:-1]
                    if k:
                        env_vars[k] = v
        except OSError:
            pass
    return env_vars


def _get_env(key: str, file_vars: dict) -> str:
    return (os.environ.get(key) or file_vars.get(key) or "").strip()


def _is_service_or_secret_key(key: str) -> bool:
    if not key:
        return False
    if key.startswith("sb_secret_"):
        return True
    parts = key.split(".")
    if len(parts) == 3:
        try:
            import base64

            payload_b64 = parts[1] + "=" * (-len(parts[1]) % 4)
            payload = json.loads(base64.urlsafe_b64decode(payload_b64).decode("utf-8"))
            if payload.get("role") == "service_role":
                return True
        except Exception:
            pass
    return False


def _read_settings_from_conn(conn=None) -> dict:
    if conn is not None:
        try:
            rows = conn.execute("SELECT key, value FROM settings").fetchall()
            out = {}
            for r in rows:
                try:
                    out[r["key"]] = json.loads(r["value"])
                except Exception:
                    out[r["key"]] = r["value"]
            return out
        except Exception:
            return {}
    if os.path.exists(DB_PATH):
        try:
            tmp_conn = sqlite3.connect(DB_PATH, timeout=2.0)
            tmp_conn.row_factory = sqlite3.Row
            try:
                rows = tmp_conn.execute("SELECT key, value FROM settings").fetchall()
                out = {}
                for r in rows:
                    try:
                        out[r["key"]] = json.loads(r["value"])
                    except Exception:
                        out[r["key"]] = r["value"]
                return out
            finally:
                tmp_conn.close()
        except Exception:
            pass
    return {}


def get_cloud_config(conn=None) -> dict:
    """Resuelve credenciales Supabase desde settings SQLite, variables de entorno y .env.local."""
    file_vars = _load_env_file_vars()
    settings = _read_settings_from_conn(conn)

    s_url = str(settings.get("supabase_url") or "").strip().rstrip("/")
    env_url = (
        _get_env("SUPABASE_URL", file_vars)
        or _get_env("NEXT_PUBLIC_SUPABASE_URL", file_vars)
        or _get_env("STORAGE_SUPABASE_URL", file_vars)
    ).rstrip("/")
    supabase_url = s_url or env_url

    s_key = str(settings.get("supabase_key") or "").strip()
    env_service_key = (
        _get_env("SUPABASE_SERVICE_ROLE_KEY", file_vars)
        or _get_env("STORAGE_SUPABASE_SERVICE_ROLE_KEY", file_vars)
        or _get_env("SUPABASE_KEY", file_vars)
    )
    env_anon_key = (
        _get_env("SUPABASE_ANON_KEY", file_vars)
        or _get_env("NEXT_PUBLIC_SUPABASE_ANON_KEY", file_vars)
        or _get_env("STORAGE_NEXT_PUBLIC_SUPABASE_ANON_KEY", file_vars)
        or _get_env("NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY", file_vars)
    )

    supabase_key = s_key or env_service_key or env_anon_key

    s_anon = str(settings.get("supabase_anon_key") or "").strip()
    if s_anon and not _is_service_or_secret_key(s_anon):
        public_anon_key = s_anon
    elif env_anon_key and not _is_service_or_secret_key(env_anon_key):
        public_anon_key = env_anon_key
    elif s_key and not _is_service_or_secret_key(s_key):
        public_anon_key = s_key
    else:
        public_anon_key = ""

    s_pg = str(settings.get("postgres_url") or "").strip()
    env_pg = (
        _get_env("POSTGRES_URL_NON_POOLING", file_vars)
        or _get_env("POSTGRES_URL", file_vars)
        or _get_env("DATABASE_URL", file_vars)
        or _get_env("STORAGE_POSTGRES_URL_NON_POOLING", file_vars)
        or _get_env("STORAGE_POSTGRES_URL", file_vars)
    )
    postgres_url = s_pg or env_pg

    return {
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
        "supabase_anon_key": public_anon_key,
        "postgres_url": postgres_url,
    }


def is_cloud_enabled(conn=None) -> bool:
    cfg = get_cloud_config(conn)
    return bool(cfg["supabase_url"].startswith("http") and len(cfg["supabase_key"]) >= 16)


def get_schema_sql() -> str:
    if os.path.exists(SCHEMA_SQL_PATH):
        with open(SCHEMA_SQL_PATH, "r", encoding="utf-8") as f:
            return f.read()
    return ""


def _split_sql_statements(sql_text: str) -> list:
    """Divide SQL respetando bloques $$ ... $$ y cadenas '...'."""
    statements = []
    buf = []
    in_single_quote = False
    dollar_tag = None
    i = 0
    n = len(sql_text)

    while i < n:
        ch = sql_text[i]
        if in_single_quote:
            buf.append(ch)
            if ch == "'":
                if i + 1 < n and sql_text[i + 1] == "'":
                    buf.append("'")
                    i += 1
                else:
                    in_single_quote = False
            i += 1
            continue

        if dollar_tag is not None:
            if sql_text.startswith(dollar_tag, i):
                buf.append(dollar_tag)
                i += len(dollar_tag)
                dollar_tag = None
                continue
            buf.append(ch)
            i += 1
            continue

        if ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            newline_idx = sql_text.find("\n", i)
            if newline_idx == -1:
                break
            i = newline_idx + 1
            continue

        if ch == "'":
            in_single_quote = True
            buf.append(ch)
            i += 1
            continue

        if ch == "$":
            m = re.match(r"\$[A-Za-z0-9_]*\$", sql_text[i:])
            if m:
                dollar_tag = m.group(0)
                buf.append(dollar_tag)
                i += len(dollar_tag)
                continue

        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


def ensure_postgres_schema(postgres_url: str) -> tuple[bool, str]:
    """Ejecuta supabase_schema.sql de forma idempotente usando pg8000 si hay URI de Postgres."""
    pg_uri = (postgres_url or "").strip()
    if not pg_uri:
        return False, "No hay URI de PostgreSQL configurada."

    if pg_uri in _schema_verified_for_url:
        return True, "Esquema verificado previamente."

    try:
        import pg8000.native
    except ImportError:
        return False, "pg8000 no instalado; ejecuta el SQL manualmente en Supabase SQL Editor."

    sql_text = get_schema_sql()
    if not sql_text:
        return False, "No se encontró supabase_schema.sql."

    parsed = urllib.parse.urlparse(pg_uri)
    user = urllib.parse.unquote(parsed.username or "postgres")
    password = urllib.parse.unquote(parsed.password or "")
    host = parsed.hostname or "localhost"
    port = int(parsed.port or 5432)
    database = (parsed.path or "/postgres").lstrip("/") or "postgres"

    ssl_ctx = None
    qs = urllib.parse.parse_qs(parsed.query)
    sslmode = (qs.get("sslmode", [""])[0] or "").lower()
    if sslmode in ("require", "verify-ca", "verify-full") or "supabase" in host or "neon" in host:
        ssl_ctx = ssl.create_default_context()
        if sslmode in ("", "require"):
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

    conn = None
    try:
        conn = pg8000.native.Connection(
            user=user,
            password=password,
            host=host,
            port=port,
            database=database,
            ssl_context=ssl_ctx,
            timeout=10,
        )
        for stmt in _split_sql_statements(sql_text):
            conn.run(stmt)
        _schema_verified_for_url.add(pg_uri)
        return True, "Tablas, políticas RLS y Realtime creados/verificados en Supabase."
    except Exception as e:
        return False, f"Error aplicando esquema SQL en Postgres: {e}"
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def supabase_request(
    method: str,
    table_or_path: str,
    data=None,
    query: str = "",
    headers_extra: dict = None,
    conn=None,
    timeout: float = 6.0,
) -> tuple[bool, int, object]:
    """Ejecuta una peticion HTTP contra PostgREST o Storage REST de Supabase."""
    cfg = get_cloud_config(conn)
    base_url = cfg["supabase_url"]
    api_key = cfg["supabase_key"]
    if not base_url or not api_key:
        return False, 0, {"error": "Supabase no configurado"}

    if table_or_path.startswith("/"):
        url = f"{base_url}{table_or_path}"
    else:
        url = f"{base_url}/rest/v1/{table_or_path}"
    if query:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}{query}"

    headers = {
        "apikey": api_key,
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if headers_extra:
        headers.update(headers_extra)

    body_bytes = None
    if data is not None:
        if isinstance(data, (bytes, bytearray)):
            body_bytes = bytes(data)
        else:
            body_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body_bytes, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return True, resp.status, None
            ctype = resp.headers.get("Content-Type", "")
            if "json" in ctype or raw.startswith(b"[") or raw.startswith(b"{"):
                return True, resp.status, json.loads(raw.decode("utf-8"))
            return True, resp.status, raw.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as he:
        err_body = he.read().decode("utf-8", errors="replace")
        try:
            err_parsed = json.loads(err_body)
        except Exception:
            err_parsed = {"message": err_body or str(he)}
        return False, he.code, err_parsed
    except Exception as e:
        return False, 0, {"message": str(e)}


def upload_image_to_supabase(filename: str, raw_webp_bytes: bytes, conn=None) -> str | None:
    """Sube un WebP optimizado al bucket publico 'dishes-media' de Supabase Storage."""
    cfg = get_cloud_config(conn)
    if not cfg["supabase_url"] or not cfg["supabase_key"]:
        return None

    safe_name = os.path.basename(filename)
    path = f"/storage/v1/object/dishes-media/{urllib.parse.quote(safe_name)}"
    ok, status, _ = supabase_request(
        "POST",
        path,
        data=raw_webp_bytes,
        headers_extra={
            "Content-Type": "image/webp",
            "x-upsert": "true",
        },
        conn=conn,
        timeout=10.0,
    )
    if not ok and status in (400, 404):
        # Intentar crear el bucket si el key es service_role y no existe
        supabase_request(
            "POST",
            "/storage/v1/bucket",
            data={"id": "dishes-media", "name": "dishes-media", "public": True},
            conn=conn,
        )
        ok, status, _ = supabase_request(
            "POST",
            path,
            data=raw_webp_bytes,
            headers_extra={
                "Content-Type": "image/webp",
                "x-upsert": "true",
            },
            conn=conn,
            timeout=10.0,
        )
    if ok:
        return f"{cfg['supabase_url']}/storage/v1/object/public/dishes-media/{urllib.parse.quote(safe_name)}"
    return None


def _dish_to_cloud(row_dict: dict, sort_order: int = 0) -> dict:
    imgs = row_dict.get("images")
    if isinstance(imgs, str):
        try:
            imgs = json.loads(imgs)
        except Exception:
            imgs = []
    opts = row_dict.get("options")
    if isinstance(opts, str):
        try:
            opts = json.loads(opts)
        except Exception:
            opts = []
    return {
        "id": str(row_dict["id"]),
        "name": str(row_dict.get("name") or ""),
        "category": str(row_dict.get("category") or "ceviches"),
        "price": float(row_dict.get("price") or 0),
        "img": str(row_dict.get("img") or ""),
        "images": imgs if isinstance(imgs, list) else [],
        "badge": str(row_dict.get("badge") or ""),
        "desc": str(row_dict.get("desc") or ""),
        "options": opts if isinstance(opts, list) else [],
        "published": bool(row_dict.get("published", True)),
        "sort_order": int(row_dict.get("sort_order", sort_order)),
    }


def _table_to_cloud(row_dict: dict, sort_order: int = 0) -> dict:
    return {
        "id": str(row_dict["id"]),
        "name": str(row_dict.get("name") or ""),
        "capacity": int(row_dict.get("capacity") or 4),
        "status": str(row_dict.get("status") or "libre"),
        "sort_order": int(row_dict.get("sort_order", sort_order)),
    }


def _order_to_cloud(row_dict: dict) -> dict:
    items = row_dict.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    seq_val = row_dict.get("seq")
    if seq_val is None:
        m = re.search(r"(\d+)$", str(row_dict.get("id") or ""))
        seq_val = int(m.group(1)) if m else None
    return {
        "id": str(row_dict["id"]),
        "seq": int(seq_val) if seq_val is not None else None,
        "table_id": str(row_dict.get("table_id") or "1"),
        "table_name": str(row_dict.get("table_name") or "Mesa 1"),
        "service_mode": str(row_dict.get("service_mode") or "En Barra"),
        "time": str(row_dict.get("time") or ""),
        "timestamp": float(row_dict.get("timestamp") or time.time()),
        "items": items if isinstance(items, list) else [],
        "total": float(row_dict.get("total") or 0),
        "notes": str(row_dict.get("notes") or ""),
        "status": str(row_dict.get("status") or "pedido"),
        "paid": bool(row_dict.get("paid", False)),
        "paid_at": str(row_dict.get("paid_at") or ""),
    }


def _receipt_to_cloud(row_dict: dict) -> dict:
    items = row_dict.get("items")
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = []
    seq_val = row_dict.get("seq")
    if seq_val is None:
        m = re.search(r"(\d+)$", str(row_dict.get("id") or ""))
        seq_val = int(m.group(1)) if m else 4813
    return {
        "id": str(row_dict["id"]),
        "seq": int(seq_val),
        "number": str(row_dict.get("number") or ""),
        "issued_at": str(row_dict.get("issued_at") or ""),
        "table_id": str(row_dict.get("table_id") or ""),
        "table_name": str(row_dict.get("table_name") or ""),
        "mode": str(row_dict.get("mode") or ""),
        "customer_name": str(row_dict.get("customer_name") or "CLIENTES VARIOS"),
        "customer_doc": str(row_dict.get("customer_doc") or "00000000"),
        "customer_phone": str(row_dict.get("customer_phone") or ""),
        "payment_method": str(row_dict.get("payment_method") or "YAPE / PLIN"),
        "op_number": str(row_dict.get("op_number") or ""),
        "hash": str(row_dict.get("hash") or ""),
        "items": items if isinstance(items, list) else [],
        "op_gravada": float(row_dict.get("op_gravada") or 0),
        "igv": float(row_dict.get("igv") or 0),
        "total": float(row_dict.get("total") or 0),
    }


def push_entity_to_cloud(table: str, record_or_list, on_conflict: str = "id", conn=None) -> bool:
    """Inserta o actualiza uno o varios registros en Supabase PostgREST."""
    if not is_cloud_enabled(conn):
        return False
    if isinstance(record_or_list, list) and not record_or_list:
        return True
    ok, status, resp = supabase_request(
        "POST",
        table,
        data=record_or_list,
        query=f"on_conflict={urllib.parse.quote(on_conflict)}",
        headers_extra={"Prefer": "resolution=merge-duplicates,return=minimal"},
        conn=conn,
    )
    if not ok:
        print(f"[CLOUD SYNC WARN] push {table} ({status}): {resp}")
    return ok


def delete_entity_from_cloud(table: str, col: str, val: str, conn=None) -> bool:
    """Elimina registros coincidentes en Supabase PostgREST."""
    if not is_cloud_enabled(conn):
        return False
    ok, status, resp = supabase_request(
        "DELETE",
        table,
        query=f"{urllib.parse.quote(col)}=eq.{urllib.parse.quote(str(val))}",
        conn=conn,
    )
    if not ok:
        print(f"[CLOUD SYNC WARN] delete {table} ({status}): {resp}")
    return ok


def _upsert_local_dish(conn, d: dict) -> None:
    imgs = d.get("images") if isinstance(d.get("images"), list) else []
    opts = d.get("options") if isinstance(d.get("options"), list) else []
    conn.execute(
        """
        INSERT OR REPLACE INTO dishes
        (id, name, category, price, img, images, badge, desc, options, published, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(d["id"]),
            str(d.get("name") or ""),
            str(d.get("category") or "ceviches"),
            float(d.get("price") or 0),
            str(d.get("img") or (imgs[0] if imgs else "")),
            json.dumps(imgs, ensure_ascii=False),
            str(d.get("badge") or ""),
            str(d.get("desc") or ""),
            json.dumps(opts, ensure_ascii=False),
            1 if d.get("published", True) else 0,
            int(d.get("sort_order") or 0),
        ),
    )


def _upsert_local_table(conn, t: dict) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO tables (id, name, capacity, status, sort_order)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            str(t["id"]),
            str(t.get("name") or ""),
            int(t.get("capacity") or 4),
            str(t.get("status") or "libre"),
            int(t.get("sort_order") or 0),
        ),
    )


def _upsert_local_order(conn, o: dict) -> None:
    items = o.get("items") if isinstance(o.get("items"), list) else []
    seq_val = o.get("seq")
    if seq_val is None:
        m = re.search(r"(\d+)$", str(o.get("id") or ""))
        seq_val = int(m.group(1)) if m else 101
    conn.execute(
        """
        INSERT OR REPLACE INTO orders
        (id, seq, table_id, table_name, service_mode, time, timestamp, items, total, notes, status, paid, paid_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(o["id"]),
            int(seq_val),
            str(o.get("table_id") or "1"),
            str(o.get("table_name") or "Mesa 1"),
            str(o.get("service_mode") or "En Barra"),
            str(o.get("time") or ""),
            float(o.get("timestamp") or time.time()),
            json.dumps(items, ensure_ascii=False),
            float(o.get("total") or 0),
            str(o.get("notes") or ""),
            str(o.get("status") or "pedido"),
            1 if o.get("paid", False) else 0,
            str(o.get("paid_at") or ""),
        ),
    )


def _upsert_local_receipt(conn, r: dict) -> None:
    items = r.get("items") if isinstance(r.get("items"), list) else []
    seq_val = r.get("seq")
    if seq_val is None:
        m = re.search(r"(\d+)$", str(r.get("id") or ""))
        seq_val = int(m.group(1)) if m else 4813
    conn.execute(
        """
        INSERT OR REPLACE INTO receipts
        (id, seq, number, issued_at, table_id, table_name, mode, customer_name, customer_doc,
         customer_phone, payment_method, op_number, hash, items, op_gravada, igv, total)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(r["id"]),
            int(seq_val),
            str(r.get("number") or ""),
            str(r.get("issued_at") or ""),
            str(r.get("table_id") or ""),
            str(r.get("table_name") or ""),
            str(r.get("mode") or ""),
            str(r.get("customer_name") or "CLIENTES VARIOS"),
            str(r.get("customer_doc") or "00000000"),
            str(r.get("customer_phone") or ""),
            str(r.get("payment_method") or "YAPE / PLIN"),
            str(r.get("op_number") or ""),
            str(r.get("hash") or ""),
            json.dumps(items, ensure_ascii=False),
            float(r.get("op_gravada") or 0),
            float(r.get("igv") or 0),
            float(r.get("total") or 0),
        ),
    )


def _fetch_all_cloud_tables(conn=None) -> tuple[bool, dict, str]:
    ok_d, st_d, r_dishes = supabase_request("GET", "dishes", query="select=*&order=sort_order.asc", conn=conn)
    if not ok_d:
        err_msg = ""
        if isinstance(r_dishes, dict):
            err_msg = r_dishes.get("message") or r_dishes.get("error") or str(r_dishes)
        if st_d == 404 or "PGRST205" in str(r_dishes) or "does not exist" in err_msg:
            return False, {}, "Las tablas no existen en Supabase. Ejecuta 'supabase_schema.sql' en tu SQL Editor o ingresa la URI de PostgreSQL para crearlas automáticamente."
        return False, {}, f"Error conectando con Supabase (HTTP {st_d}): {err_msg}"

    ok_t, _, r_tables = supabase_request("GET", "tables", query="select=*&order=sort_order.asc", conn=conn)
    ok_o, _, r_orders = supabase_request("GET", "orders", query="select=*&order=timestamp.asc", conn=conn)
    ok_r, _, r_receipts = supabase_request("GET", "receipts", query="select=*", conn=conn)
    ok_s, _, r_settings = supabase_request("GET", "settings", query="select=*", conn=conn)
    ok_q, _, r_sequences = supabase_request("GET", "sequences", query="select=*", conn=conn)

    if not (ok_t and ok_o and ok_r):
        return False, {}, "Algunas tablas (tables/orders/receipts) faltan en Supabase. Ejecuta 'supabase_schema.sql'."

    return (
        True,
        {
            "dishes": r_dishes or [],
            "tables": r_tables or [],
            "orders": r_orders or [],
            "receipts": r_receipts or [],
            "settings": r_settings or [] if ok_s else [],
            "sequences": r_sequences or [] if ok_q else [],
        },
        "",
    )


def pull_cloud_updates(conn, min_interval: float = 3.0) -> bool:
    """Sincroniza actualizaciones ligeras desde Supabase hacia SQLite local con control de frecuencia."""
    global _last_pull_ts
    now = time.time()
    if min_interval > 0 and (now - _last_pull_ts) < min_interval:
        return False
    if not is_cloud_enabled(conn):
        return False
    if not _sync_lock.acquire(blocking=False):
        return False
    try:
        _last_pull_ts = now
        ok, remote, _ = _fetch_all_cloud_tables(conn=conn)
        if not ok:
            return False

        # Si la nube esta totalmente vacia pero localmente tenemos platos/mesas, sembrar una vez
        if len(remote["dishes"]) == 0 and len(remote["tables"]) == 0:
            local_dishes_cnt = conn.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]
            if local_dishes_cnt > 0:
                return _push_all_local_to_cloud(conn) > 0

        # En Vercel (serverless) Supabase es la unica fuente de verdad: se eliminan
        # registros locales (semilla db.json / restos) que ya no existen en la nube.
        if IS_SERVERLESS:
            for tbl in ("dishes", "tables", "orders", "receipts"):
                remote_ids = {str(x["id"]) for x in remote[tbl]}
                for lr in conn.execute(f"SELECT id FROM {tbl}").fetchall():
                    if str(lr["id"]) not in remote_ids:
                        conn.execute(f"DELETE FROM {tbl} WHERE id = ?", (lr["id"],))

        for d in remote["dishes"]:
            _upsert_local_dish(conn, d)
        for t in remote["tables"]:
            _upsert_local_table(conn, t)
        for o in remote["orders"]:
            _upsert_local_order(conn, o)
        for r in remote["receipts"]:
            _upsert_local_receipt(conn, r)
        for s in remote["settings"]:
            k = str(s.get("key") or "")
            if k and k not in SENSITIVE_SETTING_KEYS:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(s.get("value") or "")))
        for q in remote["sequences"]:
            qname = str(q.get("name") or "")
            qval = int(q.get("val") or 0)
            if qname:
                if IS_SERVERLESS:
                    conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES (?, ?)", (qname, qval))
                    continue
                cur_row = conn.execute("SELECT val FROM sequences WHERE name = ?", (qname,)).fetchone()
                cur_val = int(cur_row["val"]) if cur_row else 0
                if qval > cur_val:
                    conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES (?, ?)", (qname, qval))
        return True
    except Exception as e:
        print(f"[CLOUD PULL WARN] {e}")
        return False
    finally:
        _sync_lock.release()


def maybe_pull_cloud(min_interval: float = 3.0) -> bool:
    """Atajo ligero para rutas GET: evita abrir transaccion de escritura si aun no toca sincronizar."""
    if min_interval > 0 and (time.time() - _last_pull_ts) < min_interval:
        return False
    if not is_cloud_enabled():
        return False
    try:
        with get_db(write=True) as conn:
            return pull_cloud_updates(conn, min_interval=min_interval)
    except Exception:
        return False


def _push_all_local_to_cloud(conn) -> int:
    pushed = 0
    dishes = [
        _dish_to_cloud(dict(r), r["sort_order"])
        for r in conn.execute("SELECT * FROM dishes ORDER BY sort_order ASC, rowid ASC").fetchall()
    ]
    if dishes and push_entity_to_cloud("dishes", dishes, conn=conn):
        pushed += len(dishes)

    tables = [
        _table_to_cloud(dict(r), r["sort_order"])
        for r in conn.execute("SELECT * FROM tables ORDER BY sort_order ASC, rowid ASC").fetchall()
    ]
    if tables and push_entity_to_cloud("tables", tables, conn=conn):
        pushed += len(tables)

    orders = [_order_to_cloud(dict(r)) for r in conn.execute("SELECT * FROM orders").fetchall()]
    if orders and push_entity_to_cloud("orders", orders, conn=conn):
        pushed += len(orders)

    receipts = [_receipt_to_cloud(dict(r)) for r in conn.execute("SELECT * FROM receipts").fetchall()]
    if receipts and push_entity_to_cloud("receipts", receipts, conn=conn):
        pushed += len(receipts)

    settings_rows = [
        {"key": str(r["key"]), "value": str(r["value"])}
        for r in conn.execute("SELECT key, value FROM settings").fetchall()
        if str(r["key"]) not in SENSITIVE_SETTING_KEYS
    ]
    if settings_rows:
        push_entity_to_cloud("settings", settings_rows, on_conflict="key", conn=conn)

    seq_rows = [
        {"name": str(r["name"]), "val": int(r["val"])}
        for r in conn.execute("SELECT name, val FROM sequences").fetchall()
    ]
    if seq_rows:
        push_entity_to_cloud("sequences", seq_rows, on_conflict="name", conn=conn)

    return pushed


def full_bidirectional_sync(conn) -> dict:
    """Sincroniza completamente SQLite local <-> Supabase Cloud, aplicando esquema SQL si hay Postgres URI."""
    global _last_pull_ts
    cfg = get_cloud_config(conn)
    if not cfg["supabase_url"] or not cfg["supabase_key"]:
        return {"success": False, "error": "Faltan la URL de Supabase o la API Key."}

    schema_note = ""
    if cfg["postgres_url"]:
        ok_pg, msg_pg = ensure_postgres_schema(cfg["postgres_url"])
        schema_note = msg_pg
        if not ok_pg and "no instalado" not in msg_pg:
            print(f"[CLOUD SCHEMA WARN] {msg_pg}")

    with _sync_lock:
        ok, remote, err = _fetch_all_cloud_tables(conn=conn)
        if not ok:
            return {"success": False, "error": err, "schema_note": schema_note}

        # 1. Subir semilla inicial si la nube esta vacia
        if len(remote["dishes"]) == 0 and len(remote["tables"]) == 0 and len(remote["orders"]) == 0:
            _push_all_local_to_cloud(conn)
        else:
            remote_dish_ids = {str(d["id"]) for d in remote["dishes"]}
            remote_table_ids = {str(t["id"]) for t in remote["tables"]}
            remote_order_ids = {str(o["id"]) for o in remote["orders"]}
            remote_receipt_ids = {str(r["id"]) for r in remote["receipts"]}

            # Subir registros locales que aun no existen en Supabase
            local_only_dishes = [
                _dish_to_cloud(dict(r), r["sort_order"])
                for r in conn.execute("SELECT * FROM dishes").fetchall()
                if str(r["id"]) not in remote_dish_ids
            ]
            if local_only_dishes:
                push_entity_to_cloud("dishes", local_only_dishes, conn=conn)

            local_only_tables = [
                _table_to_cloud(dict(r), r["sort_order"])
                for r in conn.execute("SELECT * FROM tables").fetchall()
                if str(r["id"]) not in remote_table_ids
            ]
            if local_only_tables:
                push_entity_to_cloud("tables", local_only_tables, conn=conn)

            local_only_orders = [
                _order_to_cloud(dict(r))
                for r in conn.execute("SELECT * FROM orders").fetchall()
                if str(r["id"]) not in remote_order_ids
            ]
            if local_only_orders:
                push_entity_to_cloud("orders", local_only_orders, conn=conn)

            local_only_receipts = [
                _receipt_to_cloud(dict(r))
                for r in conn.execute("SELECT * FROM receipts").fetchall()
                if str(r["id"]) not in remote_receipt_ids
            ]
            if local_only_receipts:
                push_entity_to_cloud("receipts", local_only_receipts, conn=conn)

            # Bajar estado remoto de Supabase hacia SQLite local
            for d in remote["dishes"]:
                _upsert_local_dish(conn, d)
            for t in remote["tables"]:
                _upsert_local_table(conn, t)
            for o in remote["orders"]:
                _upsert_local_order(conn, o)
            for r in remote["receipts"]:
                _upsert_local_receipt(conn, r)
            for s in remote["settings"]:
                k = str(s.get("key") or "")
                if k and k not in SENSITIVE_SETTING_KEYS:
                    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (k, str(s.get("value") or "")))

            # Mantener secuencias al maximo entre local y nube
            max_order_seq = conn.execute("SELECT COALESCE(MAX(seq), 100) FROM orders").fetchone()[0]
            max_receipt_seq = conn.execute("SELECT COALESCE(MAX(seq), 4812) FROM receipts").fetchone()[0]
            for q in remote["sequences"]:
                if q.get("name") == "order":
                    max_order_seq = max(max_order_seq, int(q.get("val") or 100))
                elif q.get("name") == "receipt":
                    max_receipt_seq = max(max_receipt_seq, int(q.get("val") or 4812))
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('order', ?)", (max_order_seq,))
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('receipt', ?)", (max_receipt_seq,))
            push_entity_to_cloud(
                "sequences",
                [{"name": "order", "val": max_order_seq}, {"name": "receipt", "val": max_receipt_seq}],
                on_conflict="name",
                conn=conn,
            )

        _last_pull_ts = time.time()
        stats = {
            "dishes": int(conn.execute("SELECT COUNT(*) FROM dishes").fetchone()[0]),
            "tables": int(conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]),
            "orders": int(conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]),
            "receipts": int(conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]),
        }

    return {
        "success": True,
        "mode": "vercel-hybrid-supabase" if IS_SERVERLESS else "local-hybrid-supabase",
        "schema_note": schema_note,
        "stats": stats,
    }
