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
VERSION = "1.0.0"


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
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("460x560")
        root.resizable(False, False)
        root.configure(bg="#070d15")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        container = tk.Frame(root, bg="#070d15")
        container.pack(fill="both", expand=True, padx=28, pady=20)

        title = tk.Label(container, text="BIGA CHEAT", fg="#ffd700", bg="#070d15", font=("Segoe UI", 22, "bold"))
        title.pack(pady=(0, 2))
        sub = tk.Label(container, text="Premium Loader", fg="#8fa3b8", bg="#070d15", font=("Segoe UI", 11))
        sub.pack(pady=(0, 18))

        self.info = tk.Label(container, text=f"v{VERSION}", fg="#5b7085", bg="#070d15", font=("Segoe UI", 9))
        self.info.pack(pady=(0, 14))

        tk.Label(container, text="Kullanıcı Adı", fg="#cfd8e3", bg="#070d15", font=("Segoe UI", 10)).pack(anchor="w")
        self.username = tk.Entry(container, bg="#0d1724", fg="white", insertbackground="white", relief="flat", font=("Segoe UI", 12))
        self.username.pack(fill="x", pady=(4, 12), ipady=6)

        tk.Label(container, text="Şifre", fg="#cfd8e3", bg="#070d15", font=("Segoe UI", 10)).pack(anchor="w")
        self.password = tk.Entry(container, bg="#0d1724", fg="white", insertbackground="white", relief="flat", show="*", font=("Segoe UI", 12))
        self.password.pack(fill="x", pady=(4, 16), ipady=6)
        self.password.bind("<Return>", lambda _e: self.login())

        self.login_btn = tk.Button(container, text="GİRİŞ YAP", command=self.login, bg="#ffd700", fg="#0a0f16", activebackground="#ffe44d", activeforeground="#0a0f16", relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2")
        self.login_btn.pack(fill="x", ipady=9)

        self.tab_frame = tk.Frame(container, bg="#070d15")
        self.tab_btn_free = tk.Button(self.tab_frame, text="🆓 ÜCRETSİZ HİLE", command=lambda: self.start_download("free"), bg="#123456", fg="white", activebackground="#1a4a75", activeforeground="white", relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2", disabledforeground="#5b7085")
        self.tab_btn_paid = tk.Button(self.tab_frame, text="💎 ÜCRETLİ HİLE", command=lambda: self.start_download("paid"), bg="#3d2f00", fg="#ffd700", activebackground="#5a4400", activeforeground="#ffd700", relief="flat", font=("Segoe UI", 10, "bold"), cursor="hand2", disabledforeground="#5b7085")

        self.status = tk.Label(container, text="Hazır", fg="#65d9ff", bg="#070d15", font=("Segoe UI", 10), wraplength=400, justify="left")
        self.status.pack(fill="x", pady=(16, 0))

        self.progress = ttk.Progressbar(container, mode="indeterminate")
        self.progress.pack(fill="x", pady=(12, 0))
        self.progress.pack_forget()

    def set_status(self, text, color="#65d9ff"):
        self.status.config(text=text, fg=color)
        self.root.update_idletasks()

    def set_loading(self, on):
        if on:
            self.login_btn.config(state="disabled", text="İşleniyor...")
            self.tab_btn_free.config(state="disabled")
            self.tab_btn_paid.config(state="disabled")
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
        self.tab_btn_free.config(state="normal")
        self.tab_btn_paid.config(state="normal" if self.premium else "disabled")

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
                free_path = os.path.join(temp_dir, "Biga-Cheat-Cs2.exe")
                request_download(self.token, free_path, "free")
                self.set_status("Ücretsiz sürüm başlatılıyor...")
                if os.name == "nt":
                    try:
                        import ctypes
                        ctypes.windll.shell32.ShellExecuteW(None, "runas", free_path, None, temp_dir, 1)
                    except Exception:
                        subprocess.Popen([free_path], cwd=temp_dir)
                else:
                    subprocess.Popen([free_path], cwd=temp_dir)
                cleanup_after_exit(temp_dir, temp_dir)
                temp_dir = None
                msg = "Giriş başarılı.\nÜcretsiz sürüm başlatıldı.\n\nYüksek avantajlar için Ücretli Hile'yi deneyebilirsin."
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
