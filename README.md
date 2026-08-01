# Biga Cheat web sitesi

Bu klasör, kayıt/giriş sistemi ve oturum açmış kullanıcılar için dosya indirme alanı içerir.

## Çalıştırma

```powershell
$env:APP_SECRET = "uzun-rastgele-bir-deger"
$env:COOKIE_SECURE = "0"
python .\app.py
```

Yerel test adresi: `http://127.0.0.1:8080`

Yayınlamadan önce `APP_SECRET` değerini değiştir. HTTPS kullanan bir hosting üzerinde `COOKIE_SECURE=1` ayarla. Yeni sürümü yayınlamak için `.exe` dosyasını `downloads` klasörüne `Biga Cheat-Cs2-Modified.exe` adıyla koy.

Uygulama yalnızca standart Python kütüphanesini kullanır; ek paket gerektirmez. Public deploy için Python çalıştırabilen bir hosting (Render, Railway, Fly.io veya VPS) ve kalıcı disk/SQLite desteği gerekir.

## Yönetici paneli

Yayın ortamında `ADMIN_USERNAME` ve `ADMIN_PASSWORD` değişkenlerini ayarla. Panel adresi `/admin`; burada kayıtlı kullanıcı adlarını ve yayınlanan dosya boyutunu görebilirsin. Yönetici şifresini kaynak dosyasına yazma.

`Dockerfile` ve `render.yaml` deploy için hazırdır. SQLite kullandığın için hosting tarafında kalıcı disk/volume bağla; aksi halde yeniden deploy sonrasında kayıtlar silinebilir.
