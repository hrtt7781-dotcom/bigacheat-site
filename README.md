# Biga Cheat web sitesi

Bu klasör, kayıt/giriş sistemi ve oturum açmış kullanıcılar için dosya indirme alanı içerir.

## Çalıştırma

Uygulama başlamadan önce zorunlu ortam değişkenleri:

```powershell
$env:APP_SECRET     = "uzun-rastgele-bir-deger"   # zorunlu, sessiz anahtar
$env:COOKIE_SECURE  = "0"                          # yerel test için "0"
$env:ADMIN_USERNAME = "admin"                      # opsiyonel, varsayılan "admin"
$env:ADMIN_PASSWORD = "gizli-admin-sifresi"        # zorunlu, yönetici şifresi
python .\app.py
```

Yerel test adresi: `http://127.0.0.1:8080`

HTTPS kullanan bir hosting üzerinde `COOKIE_SECURE=1` ayarla. Yeni sürümü yayınlamak için `.exe` dosyasını `downloads` klasörüne `Biga Cheat-Cs2-Modified.exe` adıyla koy.

Uygulama yalnızca standart Python kütüphanesini kullanır; ek paket gerektirmez. Public deploy için Python çalıştırabilen bir hosting (Render, Railway, Fly.io veya VPS) ve kalıcı disk/SQLite desteği gerekir.

## Yönetici paneli

Panel adresi `/admin`; burada kayıtlı kullanıcı adlarını, bakiyeleri, ödeme kodlarını ve sistem günlüklerini görebilirsin. Yönetici kullanıcı adı ve şifresi **kaynak dosyaya yazılmaz**, yalnızca `ADMIN_USERNAME` / `ADMIN_PASSWORD` ortam değişkenlerinden okunur. `APP_SECRET` boşsa uygulama başlamaz (güvenlik gereği).

`Dockerfile` ve `render.yaml` deploy için hazırdır. SQLite kullandığın için hosting tarafında kalıcı disk/volume bağla; aksi halde yeniden deploy sonrasında kayıtlar silinebilir.

## Sağlık ve SEO

- `/health` — Render benzeri sağlayıcıların health check uç noktası
- `/robots.txt` ve `/sitemap.xml` otomatik sunulur
