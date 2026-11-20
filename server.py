import base64
import io
import json
import os
import secrets
import socket
import threading
import time
import uuid
from urllib.parse import urlparse

import qrcode
from flask import Flask, jsonify, request, send_file, send_from_directory
from PIL import Image, ImageDraw, ImageFont, ImageOps
from waitress import serve

from db import (
    BACKUP_DIR,
    BASE_DIR,
    DATA_DIR,
    IS_SERVERLESS,
    backup_database,
    get_all_settings,
    get_db,
    hash_pin,
    init_db,
    next_sequence,
    row_to_dish,
    row_to_order,
    row_to_receipt,
    row_to_table,
    set_setting,
    verify_pin,
)
from pdf_ticket import get_or_create_receipt_pdf, get_receipt_pdf_bytes, invalidate_receipt_pdf_cache
from box_manager import export_box_package, import_box_package, reset_or_create_new_box
from cloud_sync import (
    SENSITIVE_SETTING_KEYS,
    _dish_to_cloud,
    _order_to_cloud,
    _receipt_to_cloud,
    _table_to_cloud,
    delete_entity_from_cloud,
    full_bidirectional_sync,
    get_cloud_config,
    get_schema_sql,
    is_cloud_enabled,
    maybe_pull_cloud,
    pull_cloud_updates,
    push_entity_to_cloud,
    upload_image_to_supabase,
)

ASSETS_DIR = os.path.join(BASE_DIR, "assets")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads") if IS_SERVERLESS else os.path.join(ASSETS_DIR, "uploads")
FONTS_DIR = os.path.join(ASSETS_DIR, "fonts")
try:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    if not IS_SERVERLESS:
        os.makedirs(FONTS_DIR, exist_ok=True)
except OSError:
    pass

app = Flask(__name__, static_folder=None)

_rate_lock = threading.Lock()
LOGIN_ATTEMPTS = {}
ORDER_RATE_LIMITS = {}


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# Inicializar SQLite WAL y migrar db.json si corresponde (compatible con Vercel, Gunicorn y Waitress)
init_db(get_local_ip())
if is_cloud_enabled():
    try:
        maybe_pull_cloud(min_interval=0.0)
    except Exception:
        pass


def _allowed_origin(origin: str) -> bool:
    if not origin:
        return False
    env_allowed = [o.strip().rstrip("/") for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    clean = origin.rstrip("/")
    if clean in env_allowed:
        return True
    parsed = urlparse(clean)
    host = (parsed.hostname or "").lower()
    if (
        host in ("localhost", "127.0.0.1")
        or host.startswith("192.168.")
        or host.startswith("10.")
        or host.endswith(".vercel.app")
    ):
        return True
    try:
        with get_db() as conn:
            pub = str(get_all_settings(conn).get("public_url") or "").strip().rstrip("/")
        if pub and clean == pub:
            return True
    except Exception:
        pass
    return False


@app.after_request
def add_security_and_cache_headers(response):
    if request.path.startswith("/api/") or request.path == "/admin":
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"

    origin = request.headers.get("Origin")
    if origin and _allowed_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Vary"] = "Origin"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    return response


@app.route("/api/<path:_any>", methods=["OPTIONS"])
def api_options(_any):
    return ("", 204)


def _load_portable_font(filename: str, size: int):
    path = os.path.join(FONTS_DIR, filename)
    if os.path.exists(path):
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            pass
    return ImageFont.load_default()


def consolidate_table_orders(table_id: str, orders: list) -> dict:
    """Consolida todas las ordenes no pagadas (cuenta abierta) de una mesa y agrupa items repetidos."""
    unpaid = [o for o in orders if str(o.get("table_id")) == str(table_id) and not o.get("paid", False)]
    pending_count = len([o for o in unpaid if o.get("status") == "pedido"])
    delivered_count = len([o for o in unpaid if o.get("status") == "entregado"])
    total_amount = sum(float(o.get("total", 0)) for o in unpaid)

    grouped = {}
    for o in unpaid:
        is_pending = o.get("status") == "pedido"
        for it in o.get("items", []):
            name = str(it.get("name", "Platillo")).strip()
            option = str(it.get("option", "")).strip()
            unit_price = float(it.get("price", 0))
            qty = int(it.get("qty", 1))
            key = (name.lower(), option.lower())

            if key not in grouped:
                grouped[key] = {
                    "name": name,
                    "option": option,
                    "unit_price": unit_price,
                    "qty": 0,
                    "qty_pending": 0,
                    "qty_delivered": 0,
                    "subtotal": 0.0,
                }
            grouped[key]["qty"] += qty
            if is_pending:
                grouped[key]["qty_pending"] += qty
            else:
                grouped[key]["qty_delivered"] += qty
            grouped[key]["subtotal"] += unit_price * qty

    return {
        "is_occupied": len(unpaid) > 0,
        "active_orders": pending_count,
        "delivered_unpaid_orders": delivered_count,
        "unpaid_orders_count": len(unpaid),
        "open_total": total_amount,
        "consolidated_items": list(grouped.values()),
    }


def _normalize_dish_images(d: dict) -> dict:
    raw_imgs = d.get("images")
    if isinstance(raw_imgs, list) and len(raw_imgs) > 0:
        clean = [str(x).strip() for x in raw_imgs if str(x).strip()][:5]
    elif d.get("img"):
        clean = [str(d["img"]).strip()]
    else:
        clean = []
    d_copy = dict(d)
    d_copy["images"] = clean
    d_copy["img"] = clean[0] if clean else "assets/duo_espadazo.webp"
    return d_copy


def _resolve_base_url(settings: dict) -> str:
    public_url = str(settings.get("public_url") or "").strip().rstrip("/")
    if public_url:
        return public_url
    if IS_SERVERLESS:
        try:
            return request.host_url.rstrip("/")
        except RuntimeError:
            pass
    host_ip = settings.get("host_ip") or get_local_ip()
    port = settings.get("port", 8080)
    return f"http://{host_ip}:{port}"


# --- WEB ROUTES ---


@app.route("/")
def public_menu():
    return send_from_directory(BASE_DIR, "index.html")


@app.route("/admin")
def admin_panel():
    return send_from_directory(BASE_DIR, "admin.html")


@app.route("/assets/<path:filename>")
def serve_assets(filename):
    if filename.startswith("uploads/") and IS_SERVERLESS:
        rel_name = filename[len("uploads/"):]
        cand = os.path.join(UPLOAD_DIR, rel_name)
        if os.path.exists(cand):
            return send_from_directory(UPLOAD_DIR, rel_name)
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/QR_MESA_ESPADAZO_MARINO.png")
def serve_main_qr():
    return send_from_directory(BASE_DIR, "QR_MESA_ESPADAZO_MARINO.png")


@app.route("/qr_table/<table_id>")
def generate_table_qr(table_id):
    maybe_pull_cloud(min_interval=3.0)
    with get_db() as conn:
        row = conn.execute("SELECT * FROM tables WHERE id = ?", (str(table_id),)).fetchone()
        settings = get_all_settings(conn)

    table_name = row["name"] if row else f"Mesa {table_id}"
    target_url = f"{_resolve_base_url(settings)}/?mesa={table_id}"

    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=10, border=2)
    qr.add_data(target_url)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="#1A1F26", back_color="#FFFFFF").convert("RGBA")

    W, H = 800, 1150
    card = Image.new("RGBA", (W, H), (248, 246, 242, 255))
    draw = ImageDraw.Draw(card)

    draw.rectangle([(0, 0), (W, 190)], fill="#FFFFFF")
    draw.rectangle([(0, 190), (W, 196)], fill="#C95332")

    logo_path = os.path.join(ASSETS_DIR, "logo_cropped.png")
    if os.path.exists(logo_path):
        logo_img = Image.open(logo_path).convert("RGBA")
        lw, lh = logo_img.size
        tw = 400
        th = int(lh * (tw / lw))
        logo_resized = logo_img.resize((tw, th), Image.Resampling.LANCZOS)
        card.paste(logo_resized, ((W - tw) // 2, 25), logo_resized)

    font_table = _load_portable_font("Roboto-Bold.ttf", 44)
    font_sub = _load_portable_font("Roboto-Bold.ttf", 24)
    font_instr = _load_portable_font("Roboto-Regular.ttf", 20)
    font_foot = _load_portable_font("Roboto-Regular.ttf", 16)

    draw.text((W // 2, 165), "— COCINA DEL MAR —", font=font_foot, fill="#C95332", anchor="mm")
    draw.text((W // 2, 255), f"¡BIENVENIDO A {table_name.upper()}!", font=font_table, fill="#1A1F26", anchor="mm")
    draw.text((W // 2, 310), "Escanea este código para ver la Carta Digital", font=font_sub, fill="#C95332", anchor="mm")
    draw.text((W // 2, 345), "Tus pedidos se asignarán automáticamente a tu mesa", font=font_instr, fill="#64707D", anchor="mm")

    qr_size = 460
    qr_resized = qr_img.resize((qr_size, qr_size), Image.Resampling.LANCZOS)
    box_pad = 20
    b_x1 = (W - qr_size) // 2 - box_pad
    b_y1 = 400 - box_pad
    b_x2 = (W + qr_size) // 2 + box_pad
    b_y2 = 400 + qr_size + box_pad
    draw.rounded_rectangle([(b_x1, b_y1), (b_x2, b_y2)], radius=20, fill="#FFFFFF", outline="#E8E3DA", width=2)
    card.paste(qr_resized, ((W - qr_size) // 2, 400), qr_resized)

    draw.text((W // 2, 920), "1. Abre tu cámara  •  2. Elige tus platos  •  3. Pide directo a cocina", font=font_instr, fill="#1A1F26", anchor="mm")
    draw.text((W // 2, 955), f"Enlace: {target_url}", font=font_foot, fill="#64707D", anchor="mm")

    draw.rectangle([(0, H - 75), (W, H)], fill="#141A21")
    draw.text((W // 2, H - 38), "Espadazo Marino • Sistema de Barra & Mesas Digitales", font=font_foot, fill="#94A3B8", anchor="mm")

    buf = io.BytesIO()
    card.convert("RGB").save(buf, format="PNG")
    buf.seek(0)
    return send_file(buf, mimetype="image/png", as_attachment=False)


# --- API ROUTES ---


@app.route("/api/health", methods=["GET"])
def api_health():
    with get_db() as conn:
        orders_cnt = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        cloud_on = is_cloud_enabled(conn)
    return jsonify(
        {
            "ok": True,
            "engine": "sqlite-wal+supabase" if cloud_on else "sqlite-wal",
            "cloud_enabled": cloud_on,
            "environment": "vercel-cloud" if IS_SERVERLESS else "local-pc",
            "orders": orders_cnt,
        }
    )


@app.route("/api/login", methods=["POST"])
def api_login():
    client_ip = request.remote_addr or "unknown"
    now = time.time()
    with _rate_lock:
        attempt = dict(LOGIN_ATTEMPTS.get(client_ip, {"count": 0, "locked_until": 0}))
        if attempt["locked_until"] > now:
            remaining = int(attempt["locked_until"] - now)
            return jsonify({"success": False, "error": f"Demasiados intentos. Bloqueado temporalmente por {remaining}s."}), 429

    data = request.get_json(silent=True) or {}
    pin = str(data.get("pin", "")).strip()

    maybe_pull_cloud(min_interval=3.0)
    with get_db() as conn:
        settings = get_all_settings(conn)
    stored_pin = settings.get("admin_pin_hash") or settings.get("admin_pin") or "1234"

    if verify_pin(str(stored_pin), pin):
        with _rate_lock:
            LOGIN_ATTEMPTS.pop(client_ip, None)
        if not str(stored_pin).startswith("pbkdf2_sha256$"):
            new_hash = hash_pin(pin)
            with get_db(write=True) as conn:
                set_setting(conn, "admin_pin_hash", new_hash)
                conn.execute("DELETE FROM settings WHERE key = 'admin_pin'")
            if is_cloud_enabled():
                push_entity_to_cloud(
                    "settings",
                    {"key": "admin_pin_hash", "value": json.dumps(new_hash, ensure_ascii=False)},
                    on_conflict="key",
                )
        return jsonify({"success": True, "token": "adm_session_ok"})

    with _rate_lock:
        attempt["count"] += 1
        if attempt["count"] >= 5:
            attempt["locked_until"] = now + 300
            LOGIN_ATTEMPTS[client_ip] = attempt
            return jsonify({"success": False, "error": "5 intentos fallidos. Bloqueado por 5 minutos por seguridad."}), 429
        LOGIN_ATTEMPTS[client_ip] = attempt
        rem_attempts = 5 - attempt["count"]

    return jsonify({"success": False, "error": f"PIN incorrecto ({rem_attempts} intentos restantes)"}), 401


@app.route("/api/stats", methods=["GET"])
def api_stats():
    maybe_pull_cloud(min_interval=3.0)
    with get_db() as conn:
        counts = conn.execute(
            """
            SELECT
                SUM(CASE WHEN status = 'pedido' AND paid = 0 THEN 1 ELSE 0 END) AS active_orders_count,
                SUM(CASE WHEN status = 'entregado' AND paid = 0 THEN 1 ELSE 0 END) AS delivered_open_count,
                SUM(CASE WHEN paid = 1 THEN 1 ELSE 0 END) AS finalized_orders_count,
                SUM(CASE WHEN status = 'entregado' THEN 1 ELSE 0 END) AS delivered_orders_count,
                COUNT(*) AS total_orders_count,
                COALESCE(SUM(total), 0.0) AS total_sales_today
            FROM orders
            """
        ).fetchone()
        tables_total = conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0]
        occupied_count = conn.execute(
            """
            SELECT COUNT(DISTINCT t.id)
            FROM tables t
            JOIN orders o ON o.table_id = t.id AND o.paid = 0
            """
        ).fetchone()[0]
        settings = get_all_settings(conn)
        cloud_cfg = get_cloud_config(conn)
        cloud_on = is_cloud_enabled(conn)

    return jsonify(
        {
            "active_orders_count": int(counts["active_orders_count"] or 0),
            "delivered_open_count": int(counts["delivered_open_count"] or 0),
            "finalized_orders_count": int(counts["finalized_orders_count"] or 0),
            "delivered_orders_count": int(counts["delivered_orders_count"] or 0),
            "total_orders_count": int(counts["total_orders_count"] or 0),
            "total_sales_today": float(counts["total_sales_today"] or 0.0),
            "tables_occupied": int(occupied_count or 0),
            "tables_total": int(tables_total or 0),
            "broadcast_msg": settings.get("broadcast_msg", ""),
            "phone": settings.get("phone", "51987654321"),
            "public_url": settings.get("public_url", ""),
            "detected_ip": get_local_ip(),
            "cloud_enabled": cloud_on,
            "environment": "cloud" if IS_SERVERLESS else "local",
            "supabase_url": cloud_cfg.get("supabase_url", ""),
            "supabase_anon_key": cloud_cfg.get("supabase_anon_key", ""),
        }
    )


# --- IMAGE UPLOAD & WEBP OPTIMIZATION (MAX 5 PHOTOS) ---


@app.route("/api/upload-image", methods=["POST"])
def api_upload_image():
    files = request.files.getlist("images") or request.files.getlist("file")
    if not files:
        return jsonify({"success": False, "error": "No se recibieron imágenes."}), 400

    os.makedirs(UPLOAD_DIR, exist_ok=True)
    uploaded = []
    for f in files[:5]:
        if not f or not f.filename:
            continue
        raw_bytes = f.read()
        if not raw_bytes:
            continue
        original_kb = round(len(raw_bytes) / 1024.0, 1)
        try:
            im = Image.open(io.BytesIO(raw_bytes))
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                bg = Image.new("RGB", im.size, (255, 255, 255))
                if im.mode == "P":
                    im = im.convert("RGBA")
                bg.paste(im, mask=im.split()[-1] if im.mode in ("RGBA", "LA") else None)
                im = bg
            elif im.mode != "RGB":
                im = im.convert("RGB")

            im.thumbnail((960, 960), Image.Resampling.LANCZOS)

            fname = f"dish_{int(time.time())}_{uuid.uuid4().hex[:6]}.webp"
            webp_buf = io.BytesIO()
            im.save(webp_buf, format="WEBP", quality=82, method=6)
            webp_bytes = webp_buf.getvalue()
            optimized_kb = round(len(webp_bytes) / 1024.0, 1)

            cloud_url = upload_image_to_supabase(fname, webp_bytes) if is_cloud_enabled() else None
            if cloud_url:
                saved_path = cloud_url
            elif IS_SERVERLESS:
                b64 = base64.b64encode(webp_bytes).decode("ascii")
                saved_path = f"data:image/webp;base64,{b64}"
            else:
                out_path = os.path.join(UPLOAD_DIR, fname)
                with open(out_path, "wb") as out_f:
                    out_f.write(webp_bytes)
                saved_path = f"assets/uploads/{fname}"

            uploaded.append(
                {
                    "path": saved_path,
                    "original_kb": original_kb,
                    "optimized_kb": optimized_kb,
                    "width": im.width,
                    "height": im.height,
                }
            )
        except Exception as e:
            print(f"[ERROR] Optimizando imagen {f.filename}: {e}")

    if not uploaded:
        return jsonify({"success": False, "error": "Ninguna imagen válida pudo procesarse."}), 400

    return jsonify({"success": True, "uploaded": uploaded})


# --- DISHES CRUD ---


@app.route("/api/dishes", methods=["GET", "POST"])
def api_dishes():
    if request.method == "GET":
        maybe_pull_cloud(min_interval=3.0)
        client_mode = request.args.get("client") == "1"
        with get_db() as conn:
            if client_mode:
                rows = conn.execute("SELECT * FROM dishes WHERE published = 1 ORDER BY sort_order ASC, rowid ASC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM dishes ORDER BY sort_order ASC, rowid ASC").fetchall()
        dishes = [_normalize_dish_images(row_to_dish(r)) for r in rows]
        return jsonify(dishes)

    data = request.get_json(silent=True) or {}
    dish_id = str(data.get("id") or f"dish_{int(time.time())}_{uuid.uuid4().hex[:4]}")

    with get_db(write=True) as conn:
        existing_row = conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
        existing = row_to_dish(existing_row) if existing_row else None

        raw_imgs = data.get("images")
        if isinstance(raw_imgs, list):
            clean_imgs = [str(x).strip() for x in raw_imgs if str(x).strip()][:5]
        elif data.get("img"):
            clean_imgs = [str(data["img"]).strip()]
        elif existing:
            clean_imgs = _normalize_dish_images(existing)["images"]
        else:
            clean_imgs = []

        if not clean_imgs:
            return jsonify({"success": False, "error": "Debes subir al menos 1 fotografía (máx. 5)."}), 400

        options = data.get("options") or (existing.get("options") if existing else ["Ají Suave", "Ají Medio", "Ají Bravo"])
        sort_order = (
            int(existing_row["sort_order"])
            if existing_row is not None
            else int(conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM dishes").fetchone()[0])
        )
        new_dish = {
            "id": dish_id,
            "name": data.get("name", "Nuevo Platillo"),
            "category": data.get("category", "ceviches"),
            "price": float(data.get("price", 30.0)),
            "img": clean_imgs[0],
            "images": clean_imgs,
            "badge": data.get("badge", "RECOMENDADO"),
            "desc": data.get("desc", ""),
            "options": options,
            "published": bool(data.get("published", True)),
        }
        conn.execute(
            """
            INSERT OR REPLACE INTO dishes (id, name, category, price, img, images, badge, desc, options, published, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new_dish["id"],
                new_dish["name"],
                new_dish["category"],
                new_dish["price"],
                new_dish["img"],
                json.dumps(new_dish["images"], ensure_ascii=False),
                new_dish["badge"],
                new_dish["desc"],
                json.dumps(new_dish["options"], ensure_ascii=False),
                1 if new_dish["published"] else 0,
                sort_order,
            ),
        )

    if is_cloud_enabled():
        push_entity_to_cloud("dishes", _dish_to_cloud(new_dish, sort_order))

    return jsonify({"success": True, "dish": new_dish})


@app.route("/api/dishes/<dish_id>", methods=["DELETE", "PATCH"])
def api_dish_detail(dish_id):
    updated_dish = None
    with get_db(write=True) as conn:
        if request.method == "DELETE":
            conn.execute("DELETE FROM dishes WHERE id = ?", (dish_id,))
        else:
            data = request.get_json(silent=True) or {}
            if "published" in data:
                conn.execute("UPDATE dishes SET published = ? WHERE id = ?", (1 if data["published"] else 0, dish_id))
            if "price" in data:
                conn.execute("UPDATE dishes SET price = ? WHERE id = ?", (float(data["price"]), dish_id))
            row = conn.execute("SELECT * FROM dishes WHERE id = ?", (dish_id,)).fetchone()
            if row:
                updated_dish = dict(row)

    if is_cloud_enabled():
        if request.method == "DELETE":
            delete_entity_from_cloud("dishes", "id", dish_id)
        elif updated_dish:
            push_entity_to_cloud("dishes", _dish_to_cloud(updated_dish))
    return jsonify({"success": True})


# --- TABLES CRUD & CUENTA CONSOLIDADA ---


@app.route("/api/tables", methods=["GET", "POST"])
def api_tables():
    if request.method == "GET":
        maybe_pull_cloud(min_interval=3.0)
        with get_db() as conn:
            t_rows = conn.execute("SELECT * FROM tables ORDER BY sort_order ASC, rowid ASC").fetchall()
            o_rows = conn.execute("SELECT * FROM orders WHERE paid = 0").fetchall()
        tables = [row_to_table(r) for r in t_rows]
        unpaid_orders = [row_to_order(r) for r in o_rows]
        res = []
        for t in tables:
            summary = consolidate_table_orders(t["id"], unpaid_orders)
            t_copy = dict(t)
            t_copy.update(summary)
            t_copy["status"] = "ocupada" if summary["is_occupied"] else "libre"
            t_copy["qr_url"] = f"/qr_table/{t['id']}"
            res.append(t_copy)
        return jsonify(res)

    data = request.get_json(silent=True) or {}
    t_id = str(data.get("id") or f"{int(time.time())}{uuid.uuid4().hex[:2]}")
    new_table = {
        "id": t_id,
        "name": data.get("name", f"Mesa {t_id}"),
        "capacity": int(data.get("capacity", 4)),
        "status": "libre",
    }
    with get_db(write=True) as conn:
        sort_order = int(conn.execute("SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tables").fetchone()[0])
        conn.execute(
            "INSERT OR REPLACE INTO tables (id, name, capacity, status, sort_order) VALUES (?, ?, ?, ?, ?)",
            (new_table["id"], new_table["name"], new_table["capacity"], new_table["status"], sort_order),
        )
    if is_cloud_enabled():
        push_entity_to_cloud("tables", _table_to_cloud(new_table, sort_order))
    return jsonify({"success": True, "table": new_table})


@app.route("/api/tables/<table_id>/settle", methods=["POST"])
def api_table_settle(table_id):
    """Cobra toda la cuenta acumulada de la mesa y la libera atomica y transaccionalmente."""
    paid_time = time.strftime("%I:%M %p")
    settled_order_rows = []
    updated_table_row = None
    with get_db(write=True) as conn:
        table_row = conn.execute("SELECT * FROM tables WHERE id = ?", (str(table_id),)).fetchone()
        unpaid_rows = conn.execute("SELECT * FROM orders WHERE table_id = ? AND paid = 0", (str(table_id),)).fetchall()
        unpaid_orders = [row_to_order(r) for r in unpaid_rows]
        summary_snapshot = consolidate_table_orders(table_id, unpaid_orders)
        settled_count = len(unpaid_orders)
        settled_total = sum(float(o.get("total", 0)) for o in unpaid_orders)

        conn.execute(
            "UPDATE orders SET paid = 1, status = 'entregado', paid_at = ? WHERE table_id = ? AND paid = 0",
            (paid_time, str(table_id)),
        )
        conn.execute("UPDATE tables SET status = 'libre' WHERE id = ?", (str(table_id),))

        if unpaid_rows:
            ids = [str(r["id"]) for r in unpaid_rows]
            placeholders = ",".join("?" for _ in ids)
            settled_order_rows = [
                dict(r) for r in conn.execute(f"SELECT * FROM orders WHERE id IN ({placeholders})", ids).fetchall()
            ]
        t_after = conn.execute("SELECT * FROM tables WHERE id = ?", (str(table_id),)).fetchone()
        if t_after:
            updated_table_row = dict(t_after)

    if is_cloud_enabled():
        if settled_order_rows:
            push_entity_to_cloud("orders", [_order_to_cloud(r) for r in settled_order_rows])
        if updated_table_row:
            push_entity_to_cloud("tables", _table_to_cloud(updated_table_row))

    return jsonify(
        {
            "success": True,
            "settled_count": settled_count,
            "settled_total": settled_total,
            "table_id": str(table_id),
            "table_name": table_row["name"] if table_row else f"Mesa {table_id}",
            "consolidated_items": summary_snapshot.get("consolidated_items", []),
        }
    )


@app.route("/api/tables/<table_id>", methods=["DELETE"])
def api_table_delete(table_id):
    with get_db(write=True) as conn:
        conn.execute("DELETE FROM tables WHERE id = ?", (str(table_id),))
    if is_cloud_enabled():
        delete_entity_from_cloud("tables", "id", str(table_id))
    return jsonify({"success": True})


# --- ORDERS MANAGEMENT (ACID + SEQUENCE ATOMICA) ---


def _check_order_rate_limit(client_ip: str) -> bool:
    now = time.time()
    window = 60.0
    limit = 120
    with _rate_lock:
        timestamps = [ts for ts in ORDER_RATE_LIMITS.get(client_ip, []) if now - ts < window]
        if len(timestamps) >= limit:
            ORDER_RATE_LIMITS[client_ip] = timestamps
            return False
        timestamps.append(now)
        ORDER_RATE_LIMITS[client_ip] = timestamps
        return True


@app.route("/api/orders", methods=["GET", "POST"])
def api_orders():
    if request.method == "GET":
        maybe_pull_cloud(min_interval=3.0)
        status_filter = request.args.get("status")
        with get_db() as conn:
            if status_filter == "pedido":
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status = 'pedido' AND paid = 0 ORDER BY rowid DESC"
                ).fetchall()
            elif status_filter == "entregado":
                rows = conn.execute(
                    "SELECT * FROM orders WHERE status = 'entregado' AND paid = 0 ORDER BY rowid DESC"
                ).fetchall()
            elif status_filter == "finalizado":
                rows = conn.execute("SELECT * FROM orders WHERE paid = 1 ORDER BY rowid DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM orders ORDER BY rowid DESC").fetchall()
        return jsonify([row_to_order(r) for r in rows])

    client_ip = request.remote_addr or "unknown"
    if not _check_order_rate_limit(client_ip):
        return jsonify({"success": False, "error": "Demasiados pedidos desde esta IP. Intenta en unos segundos."}), 429

    data = request.get_json(silent=True) or {}
    now_ts = time.time()
    now_str = time.strftime("%I:%M %p")
    req_tid = str(data.get("table_id") or "").strip()
    req_tname = str(data.get("table_name") or "").strip()

    updated_table_row = None
    with get_db(write=True) as conn:
        if is_cloud_enabled(conn):
            pull_cloud_updates(conn, min_interval=1.0)
        tables_list = [row_to_table(r) for r in conn.execute("SELECT * FROM tables ORDER BY sort_order ASC, rowid ASC").fetchall()]
        resolved_table = None
        if req_tid:
            resolved_table = next((t for t in tables_list if str(t["id"]) == req_tid), None)
        if not resolved_table and req_tname:
            norm_name = req_tname.lower()
            resolved_table = next(
                (t for t in tables_list if t["name"].lower() == norm_name or str(t["id"]).lower() == norm_name),
                None,
            )
        if not resolved_table and not req_tid:
            if "llevar" in str(data.get("service_mode", "")).lower():
                resolved_table = next((t for t in tables_list if str(t["id"]) == "llevar"), None)
            elif tables_list:
                resolved_table = tables_list[0]

        final_tid = str(resolved_table["id"]) if resolved_table else (req_tid or "1")
        final_tname = resolved_table["name"] if resolved_table else (req_tname or f"Mesa {final_tid}")

        seq = next_sequence(conn, "order", initial=100)
        order_id = f"ORD-{seq:04d}"

        new_order = {
            "id": order_id,
            "table_id": final_tid,
            "table_name": final_tname,
            "service_mode": data.get("service_mode", "En Barra"),
            "time": now_str,
            "timestamp": now_ts,
            "items": data.get("items", []),
            "total": float(data.get("total", 0.0)),
            "notes": data.get("notes", ""),
            "status": "pedido",
            "paid": False,
        }

        conn.execute(
            """
            INSERT INTO orders
            (id, seq, table_id, table_name, service_mode, time, timestamp, items, total, notes, status, paid, paid_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pedido', 0, '')
            """,
            (
                new_order["id"],
                seq,
                new_order["table_id"],
                new_order["table_name"],
                new_order["service_mode"],
                new_order["time"],
                new_order["timestamp"],
                json.dumps(new_order["items"], ensure_ascii=False),
                new_order["total"],
                new_order["notes"],
            ),
        )
        conn.execute("UPDATE tables SET status = 'ocupada' WHERE id = ?", (final_tid,))
        t_row = conn.execute("SELECT * FROM tables WHERE id = ?", (final_tid,)).fetchone()
        if t_row:
            updated_table_row = dict(t_row)

    if is_cloud_enabled():
        push_entity_to_cloud("orders", _order_to_cloud({**new_order, "seq": seq}))
        push_entity_to_cloud("sequences", {"name": "order", "val": seq}, on_conflict="name")
        if updated_table_row:
            push_entity_to_cloud("tables", _table_to_cloud(updated_table_row))

    return jsonify({"success": True, "order": new_order})


@app.route("/api/orders/<order_id>", methods=["PATCH", "DELETE"])
def api_order_detail(order_id):
    target = None
    target_row = None
    with get_db(write=True) as conn:
        if request.method == "DELETE":
            conn.execute("DELETE FROM orders WHERE id = ?", (order_id,))
        else:
            row = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
            if not row:
                return jsonify({"success": False, "error": "Orden no encontrada"}), 404
            target = row_to_order(row)
            data = request.get_json(silent=True) or {}

            if target.get("paid", False) and not data.get("force"):
                return jsonify({"success": False, "error": "La comanda ya fue cobrada y finalizada."}), 409

            new_status = data["status"] if "status" in data else ("entregado" if target["status"] == "pedido" else "pedido")
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (new_status, order_id))
            target["status"] = new_status
            target_row = dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())

    if is_cloud_enabled():
        if request.method == "DELETE":
            delete_entity_from_cloud("orders", "id", order_id)
        elif target_row:
            push_entity_to_cloud("orders", _order_to_cloud(target_row))

    if request.method == "DELETE":
        return jsonify({"success": True})
    return jsonify({"success": True, "order": target})


# --- TICKETS DE CONTROL INTERNO / PRE-CUENTA ---


@app.route("/api/receipts", methods=["POST"])
def api_receipts():
    payload = request.get_json(silent=True) or {}
    with get_db(write=True) as conn:
        if is_cloud_enabled(conn):
            pull_cloud_updates(conn, min_interval=1.0)
        seq = next_sequence(conn, "receipt", initial=4812)
        series_num = f"T001 - {seq:08d}"
        rec_id = f"TCK-{seq}"
        total = round(float(payload.get("total", 0.0)), 2)
        op_gravada = round(total / 1.18, 2) if total > 0 else 0.0
        igv = round(total - op_gravada, 2)
        op_num = f"#OP-{secrets.randbelow(900000) + 100000}"
        hash_code = secrets.token_hex(5)

        rec = {
            "id": rec_id,
            "number": series_num,
            "issued_at": time.strftime("%d/%m/%Y %H:%M:%S"),
            "table_id": str(payload.get("table_id", "")),
            "table_name": payload.get("table_name", "En Barra"),
            "mode": payload.get("mode", f"CONSUMO ({str(payload.get('table_name', 'SALON')).upper()})"),
            "customer_name": (payload.get("customer_name") or "CLIENTES VARIOS").strip().upper(),
            "customer_doc": (payload.get("customer_doc") or "00000000").strip(),
            "customer_phone": (payload.get("customer_phone") or "").strip(),
            "payment_method": (payload.get("payment_method") or "YAPE / PLIN").strip().upper(),
            "op_number": op_num,
            "hash": hash_code,
            "items": payload.get("items", []),
            "op_gravada": op_gravada,
            "igv": igv,
            "total": total,
        }

        conn.execute(
            """
            INSERT INTO receipts
            (id, seq, number, issued_at, table_id, table_name, mode, customer_name, customer_doc,
             customer_phone, payment_method, op_number, hash, items, op_gravada, igv, total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec["id"],
                seq,
                rec["number"],
                rec["issued_at"],
                rec["table_id"],
                rec["table_name"],
                rec["mode"],
                rec["customer_name"],
                rec["customer_doc"],
                rec["customer_phone"],
                rec["payment_method"],
                rec["op_number"],
                rec["hash"],
                json.dumps(rec["items"], ensure_ascii=False),
                rec["op_gravada"],
                rec["igv"],
                rec["total"],
            ),
        )
        settings = get_all_settings(conn)

    if is_cloud_enabled():
        push_entity_to_cloud("receipts", _receipt_to_cloud({**rec, "seq": seq}))
        push_entity_to_cloud("sequences", {"name": "receipt", "val": seq}, on_conflict="name")

    base_url = _resolve_base_url(settings)
    return jsonify(
        {
            "success": True,
            "receipt": rec,
            "pdf_url": f"/api/receipts/{rec['id']}/pdf",
            "external_pdf_url": f"{base_url}/api/receipts/{rec['id']}/pdf",
        }
    )


@app.route("/api/receipts/<receipt_id>", methods=["PATCH"])
def api_receipt_update(receipt_id):
    data = request.get_json(silent=True) or {}
    rec_row = None
    with get_db(write=True) as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "Ticket no encontrado"}), 404
        rec = row_to_receipt(row)
        cust_name = (data.get("customer_name") or rec["customer_name"]).strip().upper()
        cust_doc = (data.get("customer_doc") or rec["customer_doc"]).strip()
        cust_phone = str(data.get("customer_phone") if "customer_phone" in data else rec["customer_phone"]).strip()
        pay_method = (data.get("payment_method") or rec["payment_method"]).strip().upper()
        conn.execute(
            """
            UPDATE receipts
            SET customer_name = ?, customer_doc = ?, customer_phone = ?, payment_method = ?
            WHERE id = ?
            """,
            (cust_name, cust_doc, cust_phone, pay_method, receipt_id),
        )
        rec.update(
            {
                "customer_name": cust_name,
                "customer_doc": cust_doc,
                "customer_phone": cust_phone,
                "payment_method": pay_method,
            }
        )
        rec_row = dict(conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone())
    invalidate_receipt_pdf_cache(receipt_id)
    if is_cloud_enabled() and rec_row:
        push_entity_to_cloud("receipts", _receipt_to_cloud(rec_row))
    return jsonify({"success": True, "receipt": rec})


@app.route("/api/receipts/<receipt_id>/pdf", methods=["GET"])
def api_receipt_pdf(receipt_id):
    with get_db() as conn:
        row = conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if not row:
            return jsonify({"error": "Ticket no encontrado"}), 404
        rec = row_to_receipt(row)
        settings = get_all_settings(conn)

    pdf_buf = io.BytesIO(get_receipt_pdf_bytes(rec, settings))
    pdf_buf.seek(0)
    safe_fname = rec["number"].replace(" ", "")
    as_att = request.args.get("inline") != "1"
    return send_file(
        pdf_buf,
        mimetype="application/pdf",
        as_attachment=as_att,
        download_name=f"Ticket_{safe_fname}.pdf",
    )


# --- SETTINGS, CLOUD SYNC & BACKUPS ---


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "GET":
        maybe_pull_cloud(min_interval=3.0)
        with get_db() as conn:
            s = get_all_settings(conn)
            cfg = get_cloud_config(conn)
            cloud_on = is_cloud_enabled(conn)
        s.pop("admin_pin_hash", None)
        s.pop("admin_pin", None)
        s.pop("supabase_key", None)
        s.pop("supabase_service_key", None)
        s.pop("postgres_url", None)
        s["admin_pin_configured"] = True
        s["detected_local_ip"] = get_local_ip()
        s["cloud_enabled"] = cloud_on
        s["supabase_url"] = cfg.get("supabase_url", "")
        s["supabase_key_configured"] = bool(cfg.get("supabase_key"))
        s["postgres_url_configured"] = bool(cfg.get("postgres_url"))
        return jsonify(s)

    data = request.get_json(silent=True) or {}
    cloud_push_rows = []
    with get_db(write=True) as conn:
        for k, v in data.items():
            if k == "admin_pin":
                pin_val = str(v or "").strip()
                if pin_val:
                    new_hash = hash_pin(pin_val)
                    set_setting(conn, "admin_pin_hash", new_hash)
                    conn.execute("DELETE FROM settings WHERE key = 'admin_pin'")
                    cloud_push_rows.append({"key": "admin_pin_hash", "value": json.dumps(new_hash, ensure_ascii=False)})
            elif k in ("supabase_key", "postgres_url"):
                val_str = str(v or "").strip()
                if val_str:
                    set_setting(conn, k, val_str)
            elif k not in ("admin_pin_hash", "detected_local_ip", "cloud_enabled", "supabase_key_configured", "postgres_url_configured", "admin_pin_configured"):
                set_setting(conn, k, v)
                if k not in SENSITIVE_SETTING_KEYS:
                    cloud_push_rows.append({"key": str(k), "value": json.dumps(v, ensure_ascii=False)})
        updated = get_all_settings(conn)
        cfg = get_cloud_config(conn)
        cloud_on = is_cloud_enabled(conn)

    if cloud_on and cloud_push_rows:
        push_entity_to_cloud("settings", cloud_push_rows, on_conflict="key")

    updated.pop("admin_pin_hash", None)
    updated.pop("admin_pin", None)
    updated.pop("supabase_key", None)
    updated.pop("supabase_service_key", None)
    updated.pop("postgres_url", None)
    updated["admin_pin_configured"] = True
    updated["cloud_enabled"] = cloud_on
    updated["supabase_url"] = cfg.get("supabase_url", "")
    updated["supabase_key_configured"] = bool(cfg.get("supabase_key"))
    updated["postgres_url_configured"] = bool(cfg.get("postgres_url"))
    return jsonify({"success": True, "settings": updated})


@app.route("/api/cloud/status", methods=["GET"])
def api_cloud_status():
    with get_db() as conn:
        cfg = get_cloud_config(conn)
        cloud_on = is_cloud_enabled(conn)
    return jsonify(
        {
            "cloud_enabled": cloud_on,
            "environment": "vercel-cloud" if IS_SERVERLESS else "local-pc",
            "supabase_url": cfg.get("supabase_url", ""),
            "supabase_key_configured": bool(cfg.get("supabase_key")),
            "postgres_url_configured": bool(cfg.get("postgres_url")),
            "schema_sql": get_schema_sql(),
        }
    )


@app.route("/api/cloud/sync", methods=["POST"])
def api_cloud_sync():
    data = request.get_json(silent=True) or {}
    with get_db(write=True) as conn:
        s_url = str(data.get("supabase_url") or "").strip().rstrip("/")
        s_key = str(data.get("supabase_key") or "").strip()
        pg_url = str(data.get("postgres_url") or "").strip()

        if s_url:
            set_setting(conn, "supabase_url", s_url)
        if s_key:
            set_setting(conn, "supabase_key", s_key)
        if pg_url:
            set_setting(conn, "postgres_url", pg_url)

        res = full_bidirectional_sync(conn)

    status_code = 200 if res.get("success") else 400
    return jsonify(res), status_code


@app.route("/api/backup", methods=["POST"])
def api_backup():
    path = backup_database()
    return jsonify({"success": True, "backup_file": os.path.basename(path)})


@app.route("/api/box/export", methods=["GET"])
def api_box_export():
    try:
        pkg_path = export_box_package()
        return send_file(
            pkg_path,
            as_attachment=True,
            mimetype="application/zip",
            download_name=os.path.basename(pkg_path),
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/box/import", methods=["POST"])
def api_box_import():
    pkg = request.files.get("package") or request.files.get("file")
    if not pkg:
        return jsonify({"success": False, "error": "No se seleccionó ningún archivo de caja (.caja o .zip)."}), 400
    try:
        raw_bytes = pkg.read()
        res = import_box_package(raw_bytes)
        return jsonify(res)
    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Error al importar caja: {str(e)}"}), 500


@app.route("/api/box/new", methods=["POST"])
def api_box_new():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "new_shift")
    try:
        res = reset_or_create_new_box(mode)
        return jsonify(res)
    except ValueError as ve:
        return jsonify({"success": False, "error": str(ve)}), 400
    except Exception as e:
        return jsonify({"success": False, "error": f"Error al reiniciar caja: {str(e)}"}), 500


def _daily_backup_loop():
    while True:
        try:
            today_prefix = f"database_{time.strftime('%Y%m%d')}_"
            os.makedirs(BACKUP_DIR, exist_ok=True)
            has_today = any(f.startswith(today_prefix) for f in os.listdir(BACKUP_DIR))
            if not has_today:
                backup_database()
        except Exception as e:
            print(f"[WARN] Error en respaldo automatico: {e}")
        time.sleep(12 * 3600)


def _cloud_sync_loop():
    while True:
        time.sleep(4)
        try:
            if is_cloud_enabled():
                maybe_pull_cloud(min_interval=3.5)
        except Exception as e:
            print(f"[WARN] Error en sincronizacion cloud: {e}")


def start_background_workers():
    threading.Thread(target=_daily_backup_loop, daemon=True).start()
    threading.Thread(target=_cloud_sync_loop, daemon=True).start()


if __name__ == "__main__":
    local_ip = get_local_ip()
    port = int(os.environ.get("PORT", 8080))
    start_background_workers()
    print("=" * 64)
    print("  ESPADAZO MARINO - SERVIDOR PRODUCCION (WAITRESS + HIBRIDO CLOUD)")
    print(f"  • Acceso Local: http://localhost:{port}")
    print(f"  • Acceso Red Wi-Fi: http://{local_ip}:{port}")
    print(f"  • Panel de Administración: http://localhost:{port}/admin")
    print("=" * 64)
    serve(app, host="0.0.0.0", port=port, threads=16)
