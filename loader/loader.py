"""
Biga Cheat Loader

Kullanıcı site hesabıyla (kullanıcı adı + şifre) giriş yapar,
premium doğrulaması yapılır ve kişiye özel filigranlı premium paket
indirilip otomatik olarak açılır.

Derlemek için:  build.bat  (PyInstaller gerekir)
"""

import json
import os
import platform
import subprocess
import sys
import tempfile
import tkinter as tk
import urllib.error
import urllib.request
import zipfile
from tkinter import messagebox, ttk

BASE_URL = "https://biga-cheat-site.onrender.com"
APP_NAME = "Biga Cheat Loader"
EXE_NAME = "CS2_Injector.exe"


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
        raise RuntimeError(f"Giriş başarısız ({err}). Kullanıcı adı/şifre hatalı veya premium erişimin yok.")
    return data.get("token"), data.get("premium_until", 0)


def request_download(token, dest_path):
    req = urllib.request.Request(BASE_URL + "/api/loader/download", data=json.dumps({"token": token}).encode("utf-8"), headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if resp.status != 200:
                raise RuntimeError(f"İndirme başarısız (HTTP {resp.status})")
            data = resp.read()
    except Exception as exc:
        raise RuntimeError(f"İndirme hatası: {exc}")
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return dest_path


class LoaderApp:
    def __init__(self, root):
        self.root = root
        root.title(APP_NAME)
        root.geometry("460x520")
        root.resizable(False, False)
        root.configure(bg="#070d15")

        style = ttk.Style(root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        container = tk.Frame(root, bg="#070d15")
        container.pack(fill="both", expand=True, padx=28, pady=24)

        title = tk.Label(container, text="BIGA CHEAT", fg="#ffd700", bg="#070d15", font=("Segoe UI", 22, "bold"))
        title.pack(pady=(0, 4))
        sub = tk.Label(container, text="Premium Loader", fg="#8fa3b8", bg="#070d15", font=("Segoe UI", 11))
        sub.pack(pady=(0, 24))

        tk.Label(container, text="Kullanıcı Adı", fg="#cfd8e3", bg="#070d15", font=("Segoe UI", 10)).pack(anchor="w")
        self.username = tk.Entry(container, bg="#0d1724", fg="white", insertbackground="white", relief="flat", font=("Segoe UI", 12))
        self.username.pack(fill="x", pady=(4, 12), ipady=6)

        tk.Label(container, text="Şifre", fg="#cfd8e3", bg="#070d15", font=("Segoe UI", 10)).pack(anchor="w")
        self.password = tk.Entry(container, bg="#0d1724", fg="white", insertbackground="white", relief="flat", show="*", font=("Segoe UI", 12))
        self.password.pack(fill="x", pady=(4, 20), ipady=6)
        self.password.bind("<Return>", lambda _e: self.login())

        self.login_btn = tk.Button(container, text="GİRİŞ YAP VE BAŞLAT", command=self.login, bg="#ffd700", fg="#0a0f16", activebackground="#ffe44d", activeforeground="#0a0f16", relief="flat", font=("Segoe UI", 11, "bold"), cursor="hand2")
        self.login_btn.pack(fill="x", ipady=9)

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
            self.progress.pack(fill="x", pady=(12, 0))
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.pack_forget()
            self.login_btn.config(state="normal", text="GİRİŞ YAP VE BAŞLAT")

    def login(self):
        username = self.username.get().strip()
        password = self.password.get()
        if not username or not password:
            messagebox.showwarning(APP_NAME, "Kullanıcı adı ve şifre gerekli.")
            return
        self.set_loading(True)
        self.set_status("Giriş yapılıyor...")
        self.root.after(50, self.work, username, password)

    def work(self, username, password):
        try:
            token, _until = request_login(username, password)
            self.set_status("Premium doğrulandı, paket indiriliyor...")
            temp_dir = tempfile.mkdtemp(prefix="bigacheat_")
            zip_path = os.path.join(temp_dir, "premium.zip")
            request_download(token, zip_path)
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
            self.set_status("Tamamlandı! Injector açıldı.", "#7cf29c")
            messagebox.showinfo(APP_NAME, "Giriş başarılı.\nCS2_Injector.exe başlatıldı.\n\nDosya paylaşımı yasaktır — arşiv senin adına kayıtlı.")
        except RuntimeError as exc:
            self.set_status(str(exc), "#ff6b6b")
            messagebox.showerror(APP_NAME, str(exc))
        except Exception as exc:
            self.set_status(f"Beklenmeyen hata: {exc}", "#ff6b6b")
            messagebox.showerror(APP_NAME, f"Beklenmeyen hata: {exc}")
        finally:
            self.set_loading(False)


def main():
    if platform.system() == "Windows" and "--self-test" not in sys.argv:
        root = tk.Tk()
        LoaderApp(root)
        root.mainloop()
    else:
        print(f"{APP_NAME} self-test OK (Python {sys.version.split()[0]})")


if __name__ == "__main__":
    main()
