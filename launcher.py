import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.request
import webbrowser

# Asegurar ruta base correcta tanto en desarrollo como dentro del .exe compilado
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

os.chdir(BASE_DIR)
sys.path.insert(0, BASE_DIR)

from box_manager import export_box_package, import_box_package, reset_or_create_new_box
from db import get_all_settings, get_db, init_db, set_setting


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


class EspadazoLauncherApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Espadazo Marino — Centro de Control de Caja")
        self.geometry("540x660")
        self.resizable(False, False)
        self.configure(bg="#0F172A")

        self.server_thread = None
        self.tunnel_process = None
        self.is_running = False
        self.public_url = ""
        self.local_ip = get_local_ip()
        self.port = 8080

        init_db(self.local_ip)
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self):
        # Header Brand
        header = tk.Frame(self, bg="#1E293B", padx=20, pady=16)
        header.pack(fill="x")

        title_lbl = tk.Label(
            header,
            text="ESPADAZO MARINO",
            font=("Segoe UI", 16, "bold"),
            fg="#F8FAFC",
            bg="#1E293B",
        )
        title_lbl.pack(anchor="w")

        sub_lbl = tk.Label(
            header,
            text="Sistema de Control de Caja, Barra & Carta Virtual",
            font=("Segoe UI", 9),
            fg="#94A3B8",
            bg="#1E293B",
        )
        sub_lbl.pack(anchor="w")

        # Container principal
        body = tk.Frame(self, bg="#0F172A", padx=20, pady=16)
        body.pack(fill="both", expand=True)

        # Card de Estado
        self.status_card = tk.Frame(body, bg="#1E293B", bd=1, relief="flat", padx=16, pady=14)
        self.status_card.pack(fill="x", pady=(0, 12))

        self.status_dot = tk.Label(
            self.status_card,
            text="● CAJA CERRADA",
            font=("Segoe UI", 11, "bold"),
            fg="#EF4444",
            bg="#1E293B",
        )
        self.status_dot.pack(anchor="w")

        self.status_desc = tk.Label(
            self.status_card,
            text="El servidor local y la carta por Internet están en pausa. Abre la caja para habilitar comandas y mesas.",
            font=("Segoe UI", 8),
            fg="#94A3B8",
            bg="#1E293B",
            wraplength=460,
            justify="left",
        )
        self.status_desc.pack(anchor="w", pady=(4, 8))

        # Progress bar
        self.progress = ttk.Progressbar(self.status_card, mode="indeterminate", length=460)

        # Boton Principal Abrir / Cerrar Caja
        self.btn_toggle_box = tk.Button(
            self.status_card,
            text="🔓 ABRIR CAJA & ACTIVAR SISTEMA",
            font=("Segoe UI", 11, "bold"),
            bg="#C95332",
            fg="#FFFFFF",
            activebackground="#B04223",
            activeforeground="#FFFFFF",
            relief="flat",
            cursor="hand2",
            pady=10,
            command=self._toggle_system,
        )
        self.btn_toggle_box.pack(fill="x", pady=(6, 0))

        # Card de Enlaces y Conexión
        self.links_card = tk.Frame(body, bg="#1E293B", bd=1, relief="flat", padx=16, pady=12)
        self.links_card.pack(fill="x", pady=(0, 12))

        tk.Label(
            self.links_card,
            text="DIRECCIONES ACTIVAS",
            font=("Segoe UI", 8, "bold"),
            fg="#64748B",
            bg="#1E293B",
        ).pack(anchor="w")

        self.lbl_local = tk.Label(
            self.links_card,
            text=f"• En esta PC:  http://localhost:{self.port}/admin",
            font=("Consolas", 8),
            fg="#CBD5E1",
            bg="#1E293B",
        )
        self.lbl_local.pack(anchor="w", pady=1)

        self.lbl_wifi = tk.Label(
            self.links_card,
            text=f"• Wi-Fi Local:  http://{self.local_ip}:{self.port}/admin",
            font=("Consolas", 8),
            fg="#CBD5E1",
            bg="#1E293B",
        )
        self.lbl_wifi.pack(anchor="w", pady=1)

        self.lbl_public = tk.Label(
            self.links_card,
            text="• Internet 4G: (se activa al abrir la caja)",
            font=("Consolas", 8),
            fg="#E2E8F0",
            bg="#1E293B",
        )
        self.lbl_public.pack(anchor="w", pady=1)

        # Botones de Accion Web
        actions_frame = tk.Frame(body, bg="#0F172A")
        actions_frame.pack(fill="x", pady=(0, 14))

        self.btn_open_admin = tk.Button(
            actions_frame,
            text="🌐 Abrir Panel Bento (Admin)",
            font=("Segoe UI", 9, "bold"),
            bg="#0284C7",
            fg="#FFFFFF",
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=7,
            command=self._open_admin,
            state="disabled",
        )
        self.btn_open_admin.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.btn_open_menu = tk.Button(
            actions_frame,
            text="📱 Ver Carta",
            font=("Segoe UI", 9, "bold"),
            bg="#334155",
            fg="#FFFFFF",
            relief="flat",
            cursor="hand2",
            padx=12,
            pady=7,
            command=self._open_menu,
            state="disabled",
        )
        self.btn_open_menu.pack(side="left", fill="x", expand=True, padx=(4, 0))

        # Seccion de Gestion y Migracion de Caja
        box_frame = tk.LabelFrame(
            body,
            text="  MIGRACIÓN & GESTIÓN DE CAJA  ",
            font=("Segoe UI", 8, "bold"),
            fg="#94A3B8",
            bg="#0F172A",
            padx=12,
            pady=10,
        )
        box_frame.pack(fill="x")

        tk.Label(
            box_frame,
            text="Mueve todo tu negocio a otra computadora o respalda en 1 clic:",
            font=("Segoe UI", 8),
            fg="#64748B",
            bg="#0F172A",
        ).pack(anchor="w", pady=(0, 8))

        btns_box = tk.Frame(box_frame, bg="#0F172A")
        btns_box.pack(fill="x")

        self.btn_export = tk.Button(
            btns_box,
            text="📦 Migrar Caja (Exportar)",
            font=("Segoe UI", 8, "bold"),
            bg="#1E293B",
            fg="#38BDF8",
            relief="flat",
            cursor="hand2",
            pady=6,
            command=self._export_box,
        )
        self.btn_export.pack(side="left", fill="x", expand=True, padx=(0, 3))

        self.btn_import = tk.Button(
            btns_box,
            text="📥 Importar Caja",
            font=("Segoe UI", 8, "bold"),
            bg="#1E293B",
            fg="#4ADE80",
            relief="flat",
            cursor="hand2",
            pady=6,
            command=self._import_box,
        )
        self.btn_import.pack(side="left", fill="x", expand=True, padx=3)

        self.btn_new_shift = tk.Button(
            btns_box,
            text="🔄 Nuevo Turno",
            font=("Segoe UI", 8, "bold"),
            bg="#1E293B",
            fg="#FBBF24",
            relief="flat",
            cursor="hand2",
            pady=6,
            command=self._new_shift,
        )
        self.btn_new_shift.pack(side="left", fill="x", expand=True, padx=(3, 0))

    def _toggle_system(self):
        if not self.is_running:
            self._start_system_async()
        else:
            self._stop_system()

    def _start_system_async(self):
        self.btn_toggle_box.config(state="disabled", text="⏳ ACTIVANDO SISTEMA...", bg="#475569")
        self.progress.pack(fill="x", pady=(4, 6))
        self.progress.start(10)
        self.status_desc.config(text="Iniciando servidor local y habilitando cartas de cada mesa...")
        threading.Thread(target=self._start_system_worker, daemon=True).start()

    def _start_system_worker(self):
        # 1. Iniciar servidor Waitress
        try:
            import server as srv_mod

            if not self.server_thread:
                if hasattr(srv_mod, "start_background_workers"):
                    srv_mod.start_background_workers()
                self.server_thread = threading.Thread(
                    target=lambda: srv_mod.serve(srv_mod.app, host="0.0.0.0", port=self.port, threads=16),
                    daemon=True,
                )
                self.server_thread.start()
        except Exception as e:
            self.after(0, lambda: self._on_start_error(f"Error al iniciar servidor: {e}"))
            return

        # 2. Esperar respuesta de salud
        healthy = False
        for _ in range(25):
            time.sleep(0.2)
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/health", timeout=1.0) as r:
                    if r.status == 200:
                        healthy = True
                        break
            except Exception:
                pass

        if not healthy:
            self.after(0, lambda: self._on_start_error("El servidor no respondió a tiempo."))
            return

        # 3. Iniciar Túnel Cloudflare
        cloudflared_bin = os.path.join(BASE_DIR, "cloudflared.exe")
        if os.path.exists(cloudflared_bin):
            try:
                # Ocultar ventana de comandos en Windows (CREATE_NO_WINDOW)
                creation_flags = 0x08000000 if sys.platform == "win32" else 0
                self.tunnel_process = subprocess.Popen(
                    [cloudflared_bin, "tunnel", "--url", f"http://localhost:{self.port}"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    creationflags=creation_flags,
                )
                threading.Thread(target=self._watch_tunnel_output, daemon=True).start()
            except Exception as te:
                print(f"[WARN] No se pudo arrancar el tunel: {te}")

        self.after(0, self._on_start_success)

    def _watch_tunnel_output(self):
        if not self.tunnel_process:
            return
        pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
        for line in self.tunnel_process.stdout:
            match = pattern.search(line)
            if match:
                url = match.group(0)
                self.public_url = url
                try:
                    with get_db(write=True) as conn:
                        set_setting(conn, "public_url", url)
                except Exception:
                    pass
                self.after(0, lambda u=url: self._update_public_url_ui(u))
                break

    def _update_public_url_ui(self, url: str):
        self.lbl_public.config(text=f"• Internet 4G:  {url}", fg="#38BDF8")

    def _on_start_success(self):
        self.is_running = True
        self.progress.stop()
        self.progress.pack_forget()

        self.status_dot.config(text="● CAJA ABIERTA & EN LÍNEA", fg="#10B981")
        self.status_desc.config(
            text="Sistema activo al 100%. Las cartas de cada mesa y el panel de cocina están recibiendo pedidos."
        )
        self.btn_toggle_box.config(
            state="normal",
            text="🔒 CERRAR CAJA / DETENER SISTEMA",
            bg="#334155",
            activebackground="#1E293B",
        )
        self.btn_open_admin.config(state="normal")
        self.btn_open_menu.config(state="normal")

    def _on_start_error(self, err_msg):
        self.is_running = False
        self.progress.stop()
        self.progress.pack_forget()
        self.btn_toggle_box.config(state="normal", text="🔓 ABRIR CAJA & ACTIVAR SISTEMA", bg="#C95332")
        messagebox.showerror("Error de Activación", err_msg)

    def _stop_system(self):
        if self.tunnel_process:
            try:
                self.tunnel_process.terminate()
            except Exception:
                pass
            self.tunnel_process = None

        self.is_running = False
        self.status_dot.config(text="● CAJA CERRADA", fg="#EF4444")
        self.status_desc.config(
            text="El servidor local y la carta por Internet están en pausa. Abre la caja para habilitar comandas y mesas."
        )
        self.btn_toggle_box.config(
            text="🔓 ABRIR CAJA & ACTIVAR SISTEMA",
            bg="#C95332",
            activebackground="#B04223",
        )
        self.btn_open_admin.config(state="disabled")
        self.btn_open_menu.config(state="disabled")
        self.lbl_public.config(text="• Internet 4G: (se activa al abrir la caja)", fg="#E2E8F0")

    def _open_admin(self):
        webbrowser.open(f"http://localhost:{self.port}/admin")

    def _open_menu(self):
        webbrowser.open(f"http://localhost:{self.port}/")

    def _export_box(self):
        try:
            pkg_path = export_box_package()
            target_file = filedialog.asksaveasfilename(
                title="Guardar Paquete de Caja para Migrar",
                initialfile=os.path.basename(pkg_path),
                defaultextension=".caja",
                filetypes=[("Paquete Espadazo", "*.caja"), ("Archivos ZIP", "*.zip")],
            )
            if target_file:
                import shutil

                shutil.copy2(pkg_path, target_file)
                messagebox.showinfo(
                    "Caja Exportada",
                    f"¡Paquete de caja generado con éxito!\n\nArchivo guardado en:\n{target_file}\n\nPuedes llevar este archivo en un USB a la otra computadora e importarlo.",
                )
        except Exception as e:
            messagebox.showerror("Error al Exportar", f"No se pudo exportar la caja: {e}")

    def _import_box(self):
        src_file = filedialog.askopenfilename(
            title="Seleccionar Paquete de Caja a Importar",
            filetypes=[("Paquete Espadazo", "*.caja"), ("Archivos ZIP", "*.zip")],
        )
        if not src_file:
            return

        confirm = messagebox.askyesno(
            "Confirmar Importación",
            f"¿Deseas restaurar la caja desde:\n{os.path.basename(src_file)}?\n\nSe generará un respaldo automático preventivo del estado actual de esta PC.",
        )
        if not confirm:
            return

        try:
            with open(src_file, "rb") as f:
                res = import_box_package(f.read())
            messagebox.showinfo(
                "Caja Importada con Éxito",
                "¡Caja restaurada correctamente!\nEl menú, fotografías de platos, configuración y ventas ya están activos en esta computadora.",
            )
        except Exception as e:
            messagebox.showerror("Error al Importar", f"No se pudo importar el archivo: {e}")

    def _new_shift(self):
        confirm = messagebox.askyesno(
            "Iniciar Nuevo Turno",
            "¿Deseas iniciar un NUEVO TURNO / DÍA?\n\n• Se guardará una copia de seguridad automática de todo.\n• Las órdenes se archivarán y las mesas quedarán libres.\n• Tu menú de platos y ajustes se conservarán intactos.",
        )
        if not confirm:
            return
        try:
            res = reset_or_create_new_box("new_shift")
            messagebox.showinfo("Nuevo Turno Iniciado", res.get("message", "Turno iniciado con éxito."))
        except Exception as e:
            messagebox.showerror("Error", f"No se pudo iniciar el nuevo turno: {e}")

    def _on_close(self):
        if self.is_running:
            if messagebox.askyesno("Salir", "¿Deseas cerrar la caja y detener el servidor?"):
                self._stop_system()
                self.destroy()
        else:
            self.destroy()


if __name__ == "__main__":
    app = EspadazoLauncherApp()
    app.mainloop()
