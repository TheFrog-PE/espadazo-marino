import io
import json
import os
import shutil
import sqlite3
import tempfile
import time
import zipfile

from db import BACKUP_DIR, BASE_DIR, DATA_DIR, DB_PATH, IS_SERVERLESS, backup_database, get_all_settings, get_db

ASSETS_DIR = os.path.join(BASE_DIR, "assets")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads") if IS_SERVERLESS else os.path.join(ASSETS_DIR, "uploads")


def export_box_package() -> str:
    """Empaqueta database.db + assets/uploads + manifest en un archivo zip (.caja) exportable."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_pkg = os.path.join(BACKUP_DIR, f"espadazo_caja_{ts}.caja")

    with tempfile.TemporaryDirectory() as tmp_dir:
        snap_db = os.path.join(tmp_dir, "database.db")
        with sqlite3.connect(DB_PATH, timeout=10.0) as src, sqlite3.connect(snap_db) as dst:
            src.backup(dst)

        with sqlite3.connect(snap_db) as conn:
            conn.row_factory = sqlite3.Row
            stats = {
                "dishes": conn.execute("SELECT COUNT(*) FROM dishes").fetchone()[0],
                "tables": conn.execute("SELECT COUNT(*) FROM tables").fetchone()[0],
                "orders": conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                "receipts": conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0],
            }
            sett = get_all_settings(conn)

        manifest = {
            "app": "Espadazo Marino",
            "version": "2.0.0",
            "exported_at": time.strftime("%d/%m/%Y %H:%M:%S"),
            "timestamp": time.time(),
            "restaurant_name": sett.get("restaurant_name", "Espadazo Marino"),
            "stats": stats,
        }

        with zipfile.ZipFile(out_pkg, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(snap_db, arcname="database.db")
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            if os.path.exists(UPLOAD_DIR):
                for root, _, files in os.walk(UPLOAD_DIR):
                    for f in files:
                        full_p = os.path.join(root, f)
                        rel_p = os.path.relpath(full_p, UPLOAD_DIR)
                        zf.write(full_p, arcname=f"uploads/{rel_p}")

    return out_pkg


def import_box_package(pkg_path_or_bytes) -> dict:
    """Verifica y restaura un paquete de caja (.caja o .zip) sobre la instancia actual."""
    if isinstance(pkg_path_or_bytes, bytes):
        zip_source = io.BytesIO(pkg_path_or_bytes)
    else:
        zip_source = pkg_path_or_bytes

    if not zipfile.is_zipfile(zip_source):
        raise ValueError("El archivo seleccionado no es un paquete válido de Espadazo Marino.")

    if hasattr(zip_source, "seek"):
        zip_source.seek(0)

    with zipfile.ZipFile(zip_source, "r") as zf:
        namelist = zf.namelist()
        if "database.db" not in namelist:
            raise ValueError("El paquete no contiene una base de datos válida (database.db).")

        db_bytes = zf.read("database.db")
        if not db_bytes.startswith(b"SQLite format 3\x00"):
            raise ValueError("La base de datos del paquete está dañada o no tiene formato SQLite válido.")

        manifest = {}
        if "manifest.json" in namelist:
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception:
                pass

        # 1. Respaldo preventivo antes de sobrescribir
        if os.path.exists(DB_PATH):
            backup_database()

        # 2. Restaurar database.db atomicamente
        data_dir = os.path.dirname(DB_PATH)
        os.makedirs(data_dir, exist_ok=True)
        fd, tmp_db = tempfile.mkstemp(dir=data_dir, suffix=".db.tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(db_bytes)

            # Limpiar archivos WAL/SHM huérfanos anteriores
            for ext in ("-wal", "-shm"):
                p = DB_PATH + ext
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

            os.replace(tmp_db, DB_PATH)
        finally:
            if os.path.exists(tmp_db):
                try:
                    os.remove(tmp_db)
                except OSError:
                    pass

        # 3. Restaurar fotos de platos
        os.makedirs(UPLOAD_DIR, exist_ok=True)
        restored_files = 0
        for name in namelist:
            if name.startswith("uploads/") and not name.endswith("/"):
                rel_fname = os.path.relpath(name, "uploads")
                target_p = os.path.join(UPLOAD_DIR, rel_fname)
                os.makedirs(os.path.dirname(target_p), exist_ok=True)
                with open(target_p, "wb") as out_f:
                    out_f.write(zf.read(name))
                restored_files += 1

    return {
        "success": True,
        "manifest": manifest,
        "restored_images": restored_files,
    }


def reset_or_create_new_box(mode: str = "new_shift") -> dict:
    """
    mode='new_shift': Archiva y limpia ventas/órdenes, libera todas las mesas, conserva platos y configuración.
    mode='clean_start': Archiva todo e inicia una caja limpia con menú base y mesas en blanco.
    """
    backup_file = backup_database()
    with get_db(write=True) as conn:
        if mode == "new_shift":
            conn.execute("DELETE FROM orders;")
            conn.execute("UPDATE tables SET status = 'libre';")
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('order', 100);")
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('receipt', 4812);")
            msg = "Nueva caja / turno iniciado: órdenes reiniciadas, mesas liberadas, carta conservada."
        elif mode == "clean_start":
            conn.execute("DELETE FROM orders;")
            conn.execute("DELETE FROM receipts;")
            conn.execute("UPDATE tables SET status = 'libre';")
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('order', 100);")
            conn.execute("INSERT OR REPLACE INTO sequences (name, val) VALUES ('receipt', 4812);")
            msg = "Caja limpia total iniciada con carta base."
        else:
            raise ValueError(f"Modo no reconocido: {mode}")

    return {
        "success": True,
        "mode": mode,
        "message": msg,
        "backup_created": os.path.basename(backup_file),
    }
