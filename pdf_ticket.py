import os
import tempfile
import pymupdf
import qrcode

from db import RECEIPTS_CACHE_DIR


def get_receipt_cache_path(receipt_id: str) -> str:
    safe_id = "".join(c for c in str(receipt_id) if c.isalnum() or c in ("-", "_"))
    return os.path.join(RECEIPTS_CACHE_DIR, f"{safe_id}.pdf")


def invalidate_receipt_pdf_cache(receipt_id: str) -> None:
    path = get_receipt_cache_path(receipt_id)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _draw_center(page: pymupdf.Page, width: float, y: float, text: str, fontname: str, fontsize: float, color=(0.07, 0.09, 0.15)) -> None:
    tw = pymupdf.get_text_length(text, fontname=fontname, fontsize=fontsize)
    x = max(12.0, (width - tw) / 2.0)
    page.insert_text((x, y), text, fontname=fontname, fontsize=fontsize, color=color)


def _draw_right(page: pymupdf.Page, right_x: float, y: float, text: str, fontname: str, fontsize: float, color=(0.07, 0.09, 0.15)) -> None:
    tw = pymupdf.get_text_length(text, fontname=fontname, fontsize=fontsize)
    page.insert_text((right_x - tw, y), text, fontname=fontname, fontsize=fontsize, color=color)


def _draw_rule(page: pymupdf.Page, x1: float, x2: float, y: float, width: float = 0.8, color=(0.07, 0.09, 0.15), dashes: str = None) -> None:
    kwargs = {"color": color, "width": width}
    if dashes:
        kwargs["dashes"] = dashes
    page.draw_line((x1, y), (x2, y), **kwargs)


def _draw_vector_qr(page: pymupdf.Page, payload: str, x: float, y: float, size: float) -> None:
    qr = qrcode.QRCode(version=2, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=1, border=1)
    qr.add_data(payload)
    qr.make(fit=True)
    matrix = qr.get_matrix()
    n = len(matrix)
    cell = size / n
    page.draw_rect(pymupdf.Rect(x, y, x + size, y + size), color=None, fill=(1, 1, 1))
    dark = (0.07, 0.09, 0.15)
    for r_idx, row in enumerate(matrix):
        for c_idx, val in enumerate(row):
            if val:
                rx = x + c_idx * cell
                ry = y + r_idx * cell
                page.draw_rect(pymupdf.Rect(rx, ry, rx + cell + 0.05, ry + cell + 0.05), color=None, fill=dark)


def generate_vector_ticket_pdf(rec: dict, settings: dict) -> bytes:
    """Genera un PDF vectorial liviano (texto seleccionable, ~5 KB) para Ticket de Control Interno."""
    rest_name = (settings.get("restaurant_name") or "ESPADAZO MARINO").upper()
    phone_cfg = settings.get("phone") or "51987654321"
    items = rec.get("items") or []

    # Ancho estandar ticketera termica 80mm (~226.8 pt)
    W = 226.8
    margin = 12.0
    right_x = W - margin

    extra_rows = sum(2 if it.get("option") else 1 for it in items)
    H = max(410.0, 315.0 + extra_rows * 14.5)

    doc = pymupdf.open()
    page = doc.new_page(width=W, height=H)

    dark = (0.07, 0.09, 0.15)
    muted = (0.30, 0.33, 0.39)
    green = (0.02, 0.47, 0.34)
    y = 22.0

    _draw_center(page, W, y, rest_name, "hebo", 12.5, dark)
    y += 12.0
    _draw_center(page, W, y, "Barra Marina Artesanal - Lima, Perú", "helv", 7.2, muted)
    y += 10.0
    _draw_center(page, W, y, f"WhatsApp / Pedidos: +{phone_cfg}", "helv", 7.2, muted)
    y += 9.0

    _draw_rule(page, margin, right_x, y, width=1.2, color=dark)
    y += 12.0
    _draw_center(page, W, y, "PRE-CUENTA / TICKET DE CONTROL INTERNO", "hebo", 8.0, dark)
    y += 11.0
    _draw_center(page, W, y, str(rec.get("number") or rec.get("id") or "T001-00000001"), "cobo", 8.8, dark)
    y += 8.0
    _draw_rule(page, margin, right_x, y, width=1.2, color=dark)
    y += 13.0

    meta_rows = [
        ("FECHA:", str(rec.get("issued_at") or "")),
        ("CLIENTE:", str(rec.get("customer_name") or "CLIENTES VARIOS")[:24]),
        ("DOC REF:", str(rec.get("customer_doc") or "00000000")[:16]),
        ("CONSUMO:", str(rec.get("table_name") or "SALON").upper()[:22]),
        ("TURNO:", "ADMIN - CONTROL INTERNO"),
    ]
    for lbl, val in meta_rows:
        page.insert_text((margin, y), lbl, fontname="cour", fontsize=6.8, color=muted)
        _draw_right(page, right_x, y, val, "cobo", 7.0, dark)
        y += 10.5

    y += 2.0
    _draw_rule(page, margin, right_x, y, width=0.6, color=muted, dashes="[2 2] 0")
    y += 10.0

    page.insert_text((margin, y), "CANT", fontname="cobo", fontsize=6.8, color=dark)
    page.insert_text((margin + 26, y), "DESCRIPCIÓN", fontname="cobo", fontsize=6.8, color=dark)
    _draw_right(page, right_x - 38, y, "P.U.", "cobo", 6.8, dark)
    _draw_right(page, right_x, y, "TOTAL", "cobo", 6.8, dark)
    y += 5.0
    _draw_rule(page, margin, right_x, y, width=0.8, color=dark)
    y += 10.0

    for it in items:
        qty = int(it.get("qty", 1) or 1)
        name = str(it.get("name", "Platillo"))[:20]
        opt = str(it.get("option", "")).strip()
        unit_p = float(it.get("unit_price", it.get("price", 0.0)))
        sub = float(it.get("subtotal", unit_p * qty))

        page.insert_text((margin, y), f"{qty:>2}x", fontname="cour", fontsize=7.0, color=dark)
        page.insert_text((margin + 26, y), name, fontname="cobo", fontsize=7.0, color=dark)
        _draw_right(page, right_x - 38, y, f"{unit_p:.2f}", "cour", 7.0, muted)
        _draw_right(page, right_x, y, f"{sub:.2f}", "cobo", 7.0, dark)
        y += 10.0
        if opt:
            page.insert_text((margin + 28, y), f"- {opt[:26]}", fontname="cour", fontsize=6.3, color=muted)
            y += 9.0

    y += 2.0
    _draw_rule(page, margin, right_x, y, width=0.6, color=muted, dashes="[2 2] 0")
    y += 11.0

    total = float(rec.get("total", 0.0))
    op_grav = float(rec.get("op_gravada", round(total / 1.18, 2)))
    igv = float(rec.get("igv", round(total - op_grav, 2)))

    for lbl, val in [
        ("BASE REF. (sin IGV):", f"S/ {op_grav:.2f}"),
        ("IGV REF. (18% incl.):", f"S/ {igv:.2f}"),
    ]:
        page.insert_text((margin, y), lbl, fontname="cour", fontsize=6.8, color=muted)
        _draw_right(page, right_x, y, val, "cobo", 7.0, dark)
        y += 10.0

    y += 2.0
    _draw_rule(page, margin, right_x, y, width=0.8, color=dark)
    y += 12.0
    page.insert_text((margin, y), "TOTAL CONSUMO:", fontname="hebo", fontsize=9.2, color=dark)
    _draw_right(page, right_x, y, f"S/ {total:.2f}", "hebo", 10.2, dark)
    y += 8.0
    _draw_rule(page, margin, right_x, y, width=0.6, color=muted, dashes="[2 2] 0")
    y += 11.0

    for lbl, val, clr in [
        ("MEDIO DE PAGO:", str(rec.get("payment_method") or "YAPE / PLIN"), dark),
        ("REF. INTERNA:", str(rec.get("op_number") or "#OP-000000"), dark),
        ("CONTROL:", "CANCELADO EN LOCAL", green),
    ]:
        page.insert_text((margin, y), lbl, fontname="cour", fontsize=6.6, color=muted)
        _draw_right(page, right_x, y, val[:22], "cobo", 6.8, clr)
        y += 10.0

    y += 5.0
    qr_size = 46.0
    qr_payload = f"TICKET_INTERNO|{rec.get('id','')}|{rec.get('number','')}|{total:.2f}|{rec.get('hash','')}"
    _draw_vector_qr(page, qr_payload, margin, y, qr_size)

    tx = margin + qr_size + 7.0
    page.insert_text((tx, y + 10.0), "DOCUMENTO SIN VALOR TRIBUTARIO", fontname="hebo", fontsize=6.6, color=dark)
    page.insert_text((tx, y + 20.0), "Ticket de control interno / pre-cuenta.", fontname="helv", fontsize=6.0, color=muted)
    page.insert_text((tx, y + 29.0), "Solicite su Boleta o Factura oficial", fontname="helv", fontsize=6.0, color=muted)
    page.insert_text((tx, y + 38.0), "en caja (SUNAT SOL / App Emprender).", fontname="helv", fontsize=6.0, color=muted)
    page.insert_text((tx, y + 46.0), f"REF: {rec.get('hash', '')}", fontname="cour", fontsize=5.8, color=muted)

    y += qr_size + 10.0
    _draw_rule(page, margin, right_x, y, width=0.6, color=muted, dashes="[2 2] 0")
    y += 11.0
    _draw_center(page, W, y, f"¡GRACIAS POR VISITAR {rest_name}!", "hebo", 7.2, dark)
    y += 10.0
    _draw_center(page, W, y, "Pesca fresca del día • Salteado al instante", "helv", 6.5, muted)

    # Recortar altura exacta al contenido generado
    final_h = min(H, y + 14.0)
    page.set_cropbox(pymupdf.Rect(0, 0, W, final_h))

    pdf_bytes = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return pdf_bytes


def get_or_create_receipt_pdf(rec: dict, settings: dict) -> str:
    """Devuelve la ruta en cache en disco del ticket PDF; si no existe, lo genera y cachea atomicamente."""
    os.makedirs(RECEIPTS_CACHE_DIR, exist_ok=True)
    cache_path = get_receipt_cache_path(rec["id"])
    if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        return cache_path

    pdf_bytes = generate_vector_ticket_pdf(rec, settings)
    fd, tmp_path = tempfile.mkstemp(dir=RECEIPTS_CACHE_DIR, suffix=".pdf.tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_bytes)
        os.replace(tmp_path, cache_path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
    return cache_path


def get_receipt_pdf_bytes(rec: dict, settings: dict) -> bytes:
    """Obtiene los bytes del PDF desde cache si es posible, o en memoria sin fallar ante FS de solo lectura."""
    try:
        cache_path = get_or_create_receipt_pdf(rec, settings)
        with open(cache_path, "rb") as f:
            return f.read()
    except Exception:
        return generate_vector_ticket_pdf(rec, settings)
