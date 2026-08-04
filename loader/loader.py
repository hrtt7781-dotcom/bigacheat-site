"""
Biga Cheat Loader

Kullanıcı site hesabıyla (kullanıcı adı + şifre) giriş yapar; premium
durumuna göre "Ücretsiz Hile" veya "Ücretli Hile" (kişiye özel filigranlı
paket) seçeneklerini sunar ve seçilen içeriği indirip çalıştırır.

Özellikler:
  - Site hesabıyla giriş (username + password)
  - Ücretsiz Hile / Ücretli Hile iki sekme (premium şartı otomatik)
  - Kalan premium gün sayısını gösterir
  - Otomatik güncelleme kontrolü (site /api/loader/version ile)
  - İndirilen geçici dosyaları temizler (injector kapandıktan sonra)

Derlemek için:  build.bat  (PyInstaller gerekir)
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from tkinter import messagebox, ttk

BASE_URL = "https://biga-cheat-site.onrender.com"
APP_NAME = "Biga Cheat Loader"
EXE_NAME = "CS2_Injector.exe"
FREE_EXE_NAME = "Biga Cheat-Cs2-Modified.exe"
VERSION = "1.1.0"


def api_post(path, payload):
    req = urllib.request.Request(BASE_URL + path, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read()
        except Exception:
            return exc.code, b""


def api_get(path, timeout=15):
    req = urllib.request.Request(BASE_URL + path, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def check_for_update():
    """Site sürümünü alır. Güncelleme varsa (version, url) döner, yoksa None."""
    try:
        status, body = api_get("/api/loader/version")
        if status != 200:
            return None
        data = json.loads(body.decode("utf-8", "replace"))
        server_version = str(data.get("version", ""))
        if not server_version or server_version == VERSION:
            return None
        return server_version, str(data.get("download_url", ""))
    except Exception:
        return None


def request_login(username, password):
    try:
        status, body = api_post("/api/loader/login", {"username": username, "password": password})
    except Exception as exc:
        raise RuntimeError(f"Sunucuya bağlanılamadı: {exc}")
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (ValueError, TypeError):
        raise RuntimeError("Sunucudan geçersiz yanıt geldi.")
    if status != 200 or not data.get("ok"):
        err = data.get("error", "bilinmeyen hata")
        raise RuntimeError(f"Giriş başarısız ({err}). Kullanıcı adı/şifre hatalı.")
    return data.get("token"), bool(data.get("premium")), int(data.get("premium_until", 0) or 0)


def request_download(token, dest_path, dl_type="paid"):
    req = urllib.request.Request(
        BASE_URL + "/api/loader/download",
        data=json.dumps({"token": token, "type": dl_type}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RuntimeError(f"İndirme başarısız (HTTP {resp.status})")
            data = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_data = json.loads(exc.read().decode("utf-8", "replace"))
            msg = err_data.get("error", f"HTTP {exc.code}")
            raise RuntimeError(f"İndirme başarısız ({msg})")
        except (ValueError, TypeError):
            raise RuntimeError(f"İndirme başarısız (HTTP {exc.code})")
    except Exception as exc:
        raise RuntimeError(f"İndirme hatası: {exc}")
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return dest_path


def cleanup_after_exit(extract_dir, temp_dir):
    """Injector kapanana kadar bekler, sonra geçici klasörleri siler."""

    def run():
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                shutil.rmtree(temp_dir)
                return
            except (OSError, PermissionError):
                time.sleep(1)
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()


class LoaderApp:
    BG = "#070b12"
    BG2 = "#0c1320"
    PANEL = "#111a29"
    CARD = "#101a2a"
    CARD2 = "#1a1400"
    GOLD = "#ffd700"
    GOLD_DIM = "#8a6d1f"
    TEXT = "#e6edf5"
    MUTED = "#8fa3b8"
    ACCENT = "#65d9ff"

    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("480x660")
        root.resizable(False, False)
        root.configure(bg=self.BG)

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
            style.configure(
                "Loader.Horizontal.TProgressbar",
                troughcolor="#0d1522",
                background=self.GOLD,
                lightcolor=self.GOLD,
                darkcolor=self.GOLD,
                bordercolor=self.BG,
                thickness=6,
            )
        except tk.TclError:
            pass

        container = tk.Frame(root, bg=self.BG)
        container.pack(fill="both", expand=True, padx=30, pady=22)

        title = tk.Label(container, text="BIGA CHEAT", fg=self.GOLD, bg=self.BG, font=("Segoe UI", 26, "bold"))
        title.pack(pady=(0, 0))
        tk.Frame(container, bg=self.GOLD_DIM, height=1).pack(fill="x", pady=(4, 8))
        sub = tk.Label(container, text="PREMIUM LOADER", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 10))
        sub.pack(pady=(0, 2))

        self.user_label = tk.Label(container, text="KULLANICI ADI", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 9, "bold"))
        self.user_label.pack(anchor="w", pady=(18, 4))
        self.username = tk.Entry(container, bg=self.PANEL, fg="white", insertbackground=self.GOLD, relief="flat", highlightthickness=1, highlightbackground="#2a3a4d", highlightcolor=self.GOLD, font=("Segoe UI", 12))
        self.username.pack(fill="x", ipady=7)

        self.pass_label = tk.Label(container, text="ŞİFRE", fg=self.MUTED, bg=self.BG, font=("Segoe UI", 9, "bold"))
        self.pass_label.pack(anchor="w", pady=(14, 4))
        self.password = tk.Entry(container, bg=self.PANEL, fg="white", insertbackground=self.GOLD, relief="flat", show="*", highlightthickness=1, highlightbackground="#2a3a4d", highlightcolor=self.GOLD, font=("Segoe UI", 12))
        self.password.pack(fill="x", ipady=7)
        self.password.bind("<Return>", lambda _e: self.login())

        self.login_btn = tk.Button(container, text="GİRİŞ YAP", command=self.login, bg=self.GOLD, fg="#0a0f16", activebackground="#ffe44d", activeforeground="#0a0f16", relief="flat", font=("Segoe UI", 12, "bold"), cursor="hand2")
        self.login_btn.pack(fill="x", ipady=10, pady=(18, 0))

        self.tab_frame = tk.Frame(container, bg=self.BG)
        self.tab_btn_free = tk.Button(
            self.tab_frame,
            text="🆓\nÜCRETSİZ HİLE\nBedava sürüm",
            command=lambda: self.select_tab("free"),
            bg=self.CARD, fg="white", activebackground="#1a3f63", activeforeground="white",
            relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2",
            disabledforeground="#5b7085", justify="center",
            width=14, padx=10, pady=16,
        )
        self.tab_btn_paid = tk.Button(
            self.tab_frame,
            text="👑\nÜCRETLİ HİLE\nPremium paket",
            command=lambda: self.select_tab("paid"),
            bg=self.CARD2, fg=self.GOLD, activebackground="#4a3a00", activeforeground=self.GOLD,
            relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2",
            disabledforeground="#5b7085", justify="center",
            width=14, padx=10, pady=16,
        )
        self.tab_btn_free.pack(side="left", expand=True, fill="both", padx=(0, 6), ipady=12)
        self.tab_btn_paid.pack(side="left", expand=True, fill="both", padx=(6, 0), ipady=12)

        self.start_btn = tk.Button(container, text="BAŞLAT", command=self.start_selected, bg=self.GOLD, fg="#0a0f16", activebackground="#ffe44d", activeforeground="#0a0f16", relief="flat", font=("Segoe UI", 13, "bold"), cursor="hand2")
        self.start_btn.pack_forget()

        self.status = tk.Label(container, text="Hazır", fg=self.ACCENT, bg=self.BG, font=("Segoe UI", 10), wraplength=420, justify="left")
        self.status.pack(fill="x", pady=(18, 0))

        self.progress = ttk.Progressbar(container, mode="indeterminate", style="Loader.Horizontal.TProgressbar")
        self.progress.pack(fill="x", pady=(12, 0))
        self.progress.pack_forget()

        footer = tk.Frame(container, bg=self.BG)
        footer.pack(fill="x", side="bottom", pady=(14, 0))
        self.info = tk.Label(footer, text=f"BigaCheat Loader v{VERSION}", fg="#5b7085", bg=self.BG, font=("Segoe UI", 9))
        self.info.pack(side="left")
        tk.Label(footer, text="TÜM HAKLARI SAKLIDIR", fg="#3d4f63", bg=self.BG, font=("Segoe UI", 8)).pack(side="right")

    def set_status(self, text, color="#65d9ff"):
        self.status.config(text=text, fg=color)
        self.root.update_idletasks()

    def set_loading(self, on):
        if on:
            self.login_btn.config(state="disabled", text="İşleniyor...")
            self.tab_btn_free.config(state="disabled")
            self.tab_btn_paid.config(state="disabled")
            self.start_btn.config(state="disabled")
            self.progress.pack(fill="x", pady=(12, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.login_btn.config(state="normal", text="GİRİŞ YAP")
            self.update_tabs()

    def update_tabs(self):
        if not getattr(self, "logged_in", False):
            return
        self.tab_frame.pack(fill="x", pady=(14, 0))
        if not getattr(self, "selected", None):
            self.selected = "paid" if getattr(self, "premium", False) else "free"
        self.refresh_tabs()

    def refresh_tabs(self):
        selected = getattr(self, "selected", "free")
        free_selected = selected == "free"
        self.tab_btn_free.config(
            state="normal",
            bg="#1d5c9e" if free_selected else self.CARD,
            fg="white",
            relief="solid" if free_selected else "flat",
            borderwidth=2,
            highlightthickness=0,
        )
        paid_enabled = getattr(self, "premium", False)
        paid_selected = selected == "paid"
        self.tab_btn_paid.config(
            state="normal" if paid_enabled else "disabled",
            bg="#8a6d1f" if (paid_selected and paid_enabled) else self.CARD2,
            fg=self.GOLD,
            relief="solid" if (paid_selected and paid_enabled) else "flat",
            borderwidth=2,
            highlightthickness=0,
        )
        self.start_btn.config(state="normal" if (paid_enabled or selected == "free") else "disabled", text="BAŞLAT")
        self.start_btn.pack(fill="x", pady=(14, 0))

    def select_tab(self, tab):
        if tab == "paid" and not getattr(self, "premium", False):
            messagebox.showwarning(APP_NAME, "Ücretli Hile için premium üyelik gerekli. Bakiye yükleyip Ücretli Hileler sayfasından erişim satın al.")
            return
        self.selected = tab
        self.refresh_tabs()

    def start_selected(self):
        tab = getattr(self, "selected", None)
        if not tab:
            messagebox.showwarning(APP_NAME, "Önce bir sekme seç.")
            return
        self.start_download(tab)

    def login(self):
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            messagebox.showwarning(APP_NAME, "Kullanıcı adı ve şifre gerekli.")
            return
        self.set_loading(True)
        self.set_status("Giriş yapılıyor...")
        self.root.after(50, self.work_login, username, password)

    def work_login(self, username, password):
        updating = False
        try:
            update = check_for_update()
            if update:
                updating = True
                self.set_status(f"Güncelleme bulundu (v{update[0]}), indiriliyor...")
                self.apply_update(update[1])
                return

            token, premium, until = request_login(username, password)
            self.token = token
            self.premium = bool(premium)
            self.until = int(until or 0)
            self.logged_in = True
            days = max(0, (self.until - int(time.time())) // 86400) if self.until else 0
            if self.premium:
                self.set_status(f"Giriş başarılı — premium üyeliğin aktif, kalan süre: {days} gün. Bir seçim yap.", "#7cf29c")
            else:
                self.set_status("Giriş başarılı — ücretsiz sürümü indirebilirsin.", "#7cf29c")
            self.update_tabs()
        except RuntimeError as exc:
            self.set_status(str(exc), "#ff6b6b")
            messagebox.showerror(APP_NAME, str(exc))
        except Exception as exc:
            self.set_status(f"Beklenmeyen hata: {exc}", "#ff6b6b")
            messagebox.showerror(APP_NAME, f"Beklenmeyen hata: {exc}")
        finally:
            if not updating:
                self.set_loading(False)

    def loader_dir(self):
        if getattr(sys, "frozen", False) and sys.executable:
            base = os.path.dirname(os.path.abspath(sys.executable))
            if os.access(base, os.W_OK):
                return base
        home = os.path.expanduser("~")
        downloads = os.path.join(home, "Downloads")
        if os.path.isdir(downloads) and os.access(downloads, os.W_OK):
            return downloads
        return tempfile.gettempdir()

    def start_download(self, dl_type):
        if dl_type == "paid" and not getattr(self, "premium", False):
            messagebox.showwarning(APP_NAME, "Ücretli Hile için premium üyelik gerekli. Bakiye yükleyip Ücretli Hileler sayfasından erişim satın al.")
            return
        if not getattr(self, "token", None):
            messagebox.showwarning(APP_NAME, "Önce giriş yap.")
            return
        self.set_loading(True)
        self.set_status("Paket indiriliyor..." if dl_type == "paid" else "Ücretsiz sürüm indiriliyor...")
        self.root.after(50, self.work_download, dl_type)

    def work_download(self, dl_type):
        temp_dir = None
        try:
            temp_dir = tempfile.mkdtemp(prefix="bigacheat_")
            if dl_type == "paid":
                zip_path = os.path.join(temp_dir, "premium.zip")
                request_download(self.token, zip_path, "paid")
                self.set_status("Paket açılıyor...")
                extract_dir = os.path.join(temp_dir, "cheat")
                os.makedirs(extract_dir, exist_ok=True)
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(extract_dir)
                exe = os.path.join(extract_dir, EXE_NAME)
                if not os.path.isfile(exe):
                    raise RuntimeError(f"{EXE_NAME} bulunamadı.")
                self.set_status("CS2_Injector.exe başlatılıyor...")
                if os.name == "nt":
                    try:
                        import ctypes
                        ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, None, extract_dir, 1)
                    except Exception:
                        subprocess.Popen([exe], cwd=extract_dir)
                else:
                    subprocess.Popen([exe], cwd=extract_dir)
                cleanup_after_exit(extract_dir, temp_dir)
                temp_dir = None  # temizlik thread'e devredildi
                msg = "Giriş başarılı.\nCS2_Injector.exe başlatıldı."
                if self.until:
                    msg += f"\nKalan premium süren: {max(0, (self.until - int(time.time())) // 86400)} gün."
                msg += "\n\nDosya paylaşımı yasaktır — arşiv senin adına kayıtlı."
            else:
                dest_dir = self.loader_dir()
                os.makedirs(dest_dir, exist_ok=True)
                free_path = os.path.join(dest_dir, FREE_EXE_NAME)
                request_download(self.token, free_path, "free")
                self.set_status("Ücretsiz sürüm başlatılıyor...")
                if os.name == "nt":
                    try:
                        import ctypes
                        ctypes.windll.shell32.ShellExecuteW(None, "runas", free_path, None, dest_dir, 1)
                    except Exception:
                        subprocess.Popen([free_path], cwd=dest_dir)
                else:
                    subprocess.Popen([free_path], cwd=dest_dir)
                temp_dir = None
                msg = f"Giriş başarılı.\nÜcretsiz sürüm başlatıldı.\nDosya kalıcı olarak kaydedildi:\n{dest_dir}\n\nYüksek avantajlar için Ücretli Hile'yi deneyebilirsin."
            self.set_status("Tamamlandı!", "#7cf29c")
            messagebox.showinfo(APP_NAME, msg)
        except RuntimeError as exc:
            self.set_status(str(exc), "#ff6b6b")
            messagebox.showerror(APP_NAME, str(exc))
        except Exception as exc:
            self.set_status(f"Beklenmeyen hata: {exc}", "#ff6b6b")
            messagebox.showerror(APP_NAME, f"Beklenmeyen hata: {exc}")
        finally:
            if temp_dir:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
            if getattr(self, "logged_in", False):
                self.set_loading(False)

    def apply_update(self, url):
        me = sys.executable if getattr(sys, "frozen", False) else None
        if not me or not os.path.isfile(me):
            self.set_status("Çalışan exe bulunamadı, güncelleme yapılamadı.", "#ff6b6b")
            self.set_loading(False)
            return
        new_exe = os.path.join(os.path.dirname(me), f"_new_{os.path.basename(me)}")
        try:
            req = urllib.request.Request(BASE_URL + url, method="GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                with open(new_exe, "wb") as fh:
                    while True:
                        chunk = resp.read(1024 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
        except Exception as exc:
            self.set_status(f"Güncelleme indirilemedi: {exc}", "#ff6b6b")
            self.set_loading(False)
            return
        updater = os.path.join(os.path.dirname(me), "_update.bat")
        script = (
            "@echo off\r\n"
            ":loop\r\n"
            f'taskkill /F /IM "{os.path.basename(me)}" >nul 2>&1\r\n'
            "timeout /t 1 /nobreak >nul\r\n"
            f'copy /Y "{new_exe}" "{me}" >nul\r\n'
            f'if not exist "{me}" goto loop\r\n'
            f'start "" "{me}"\r\n'
            f'del "{new_exe}"\r\n'
            f'del "%~f0"\r\n'
        )
        with open(updater, "w") as fh:
            fh.write(script)
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(["cmd", "/c", updater], creationflags=creationflags)
        except Exception:
            pass
        self.root.destroy()


def main():
    if platform.system() == "Windows" and "--self-test" not in sys.argv:
        root = tk.Tk()
        LoaderApp(root)
        root.mainloop()
    else:
        print(f"{APP_NAME} v{VERSION} self-test OK (Python {sys.version.split()[0]})")


if __name__ == "__main__":
    main()
