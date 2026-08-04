from __future__ import annotations

import datetime
import hashlib
import hmac
import html
import io
import json
import math
import os
import random
import re
import secrets
import sqlite3
import threading
import time
import zipfile
from contextlib import contextmanager
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse, quote, unquote


ROOT = Path(__file__).resolve().parent

# DATABASE_URL verilirse PostgreSQL (örn. Neon); verilmezse yerelde SQLite.
# Render'da kalıcı veri için DATABASE_URL ortam değişkenini kendi Neon
# hesabından al ve panelden set et; aksi halde /data diskine SQLite yazılır.
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg2  # noqa: E402
    import psycopg2.errors  # noqa: E402
    import psycopg2.extras  # noqa: E402

    DB_UNIQUE_ERROR = psycopg2.errors.UniqueViolation
else:
    DB_UNIQUE_ERROR = sqlite3.IntegrityError


def _resolve_data_dir() -> Path:
    render = os.environ.get("RENDER") == "true"
    candidate = Path("/data")
    if candidate.is_dir():
        return candidate
    # SQLite kullanıyorken kalıcı disk şarttır; Postgres ile veri haricidir.
    if render and not IS_POSTGRES:
        raise RuntimeError(
            "Render'da kalıcı disk /data olarak bağlanmamış. "
            "render.yaml'deki disk bölümünü kontrol et ve diski servise bağla; "
            "aksi halde tüm kayıtlar her yeniden başlatmada silinir."
        )
    return ROOT


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "data.sqlite3"
DOWNLOAD_PATH = ROOT / "downloads" / "Biga Cheat-Cs2-Modified.exe"
LOADER_PATH = ROOT / "downloads" / "BigaCheat-Loader.exe"
PAID_CHEATS_DIR = ROOT / "paid_cheats"
PROJECTS_PATH = DATA_DIR / "projects"
# Ücretli Hileler'e bakiye ile süreli erişim planları (gün -> TL)
PAID_CHEATS_PLANS = {
    "30": {"days": 30, "price": 350.0},
    "90": {"days": 90, "price": 800.0},
}
APP_SECRET = os.environ.get("APP_SECRET", "").encode()
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "0") == "1"
SESSION_TTL = 60 * 60 * 24 * 14
MAX_PROJECT_SIZE = 25 * 1024 * 1024
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
ADMIN_TTL = 60 * 60 * 12
LOADER_TTL = 60 * 5  # loader token ömrü: 5 dakika
LOADER_VERSION = "1.1.0"  # loader güncelleme kontrolü için sürüm (loader.py VERSION ile eşleşmeli)
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
FREE_SPIN_COOLDOWN = 60 * 60 * 4  # 4 saat

# In-memory rate limiter: { (ip, action): [timestamps] }
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS: dict[tuple[str, str], list[int]] = {}
# (limit, window_seconds) per action
RATE_LIMITS: dict[str, tuple[int, int]] = {
    "register": (5, 300),
    "login": (5, 300),
    "payment/submit": (5, 300),
    "admin/login": (5, 300),
    "wheel/spin": (10, 300),
    "loader/login": (10, 300),
    "chat/send": (1, 10),
    "cases/open": (3, 60),
    "market/list": (3, 60),
    "market/buy": (5, 60),
    "market/cancel": (5, 60),
    "inventory/sell": (5, 60),
    "trade/send": (5, 60),
    "trade/respond": (10, 60),
}

# CS2 benzeri kasa sistemi
TIER_INFO = {
    "consumer": {"name": "Tüketici Sınıfı", "color": "#b0c3d9", "star": "★"},
    "industrial": {"name": "Endüstriyel Sınıf", "color": "#5e98d9", "star": "★★"},
    "restricted": {"name": "Kısıtlı Sınıf", "color": "#8847ff", "star": "★★★"},
    "classified": {"name": "Gizli Sınıf", "color": "#d32ce6", "star": "★★★★"},
    "covert": {"name": "Gizlilik Sınıfı", "color": "#eb4b4b", "star": "★★★★★"},
    "extraordinary": {"name": "OLAĞANÜSTÜ", "color": "#ffd700", "star": "★★★★★★"},
}
TIER_ORDER = ["consumer", "industrial", "restricted", "classified", "covert", "extraordinary"]

# ---------------------------------------------------------------------------
# SKIN SİSTEMİ: 25 skin, kasalara fiyat/nadirliklerine göre dağıtılmıştır.
# Her skin'in canlı pazar fiyatı market_price() ile zaman içinde dalgalanır.
# ---------------------------------------------------------------------------
SKINS = {
    "mp5-desert": {"name": "MP5-SD | Çöl Tozu", "tier": "consumer", "icon": "🔫", "value": 15.0, "case": "kumbara"},
    "p250-pasli": {"name": "P250 | Paslı", "tier": "consumer", "icon": "🔫", "value": 18.0, "case": "kumbara"},
    "ump-sokak": {"name": "UMP-45 | Sokak Sanatı", "tier": "consumer", "icon": "🔫", "value": 20.0, "case": "mavzer"},
    "mac10-alev": {"name": "MAC-10 | Alev", "tier": "industrial", "icon": "🔫", "value": 38.0, "case": "mavzer"},
    "mp7-neon": {"name": "MP7 | Neon Yansıma", "tier": "restricted", "icon": "🔫", "value": 75.0, "case": "mavzer"},
    "sg553-kamuflaj": {"name": "SG 553 | Kamuflaj", "tier": "industrial", "icon": "🔫", "value": 45.0, "case": "cepane"},
    "galil-renk": {"name": "Galil AR | Neon", "tier": "restricted", "icon": "🔫", "value": 85.0, "case": "cepane"},
    "famas-kristal": {"name": "FAMAS | Kristal", "tier": "classified", "icon": "🔫", "value": 140.0, "case": "cepane"},
    "awp-gece": {"name": "AWP | Gece Avcısı", "tier": "industrial", "icon": "🎯", "value": 55.0, "case": "savascı"},
    "m4a4-asindirici": {"name": "M4A4 | Aşındırıcı", "tier": "restricted", "icon": "🔫", "value": 105.0, "case": "savascı"},
    "usps-altincizgi": {"name": "USP-S | Altın Çizgi", "tier": "classified", "icon": "🔫", "value": 165.0, "case": "savascı"},
    "ak47-pas": {"name": "AK-47 | Pas", "tier": "restricted", "icon": "🔫", "value": 95.0, "case": "yildiz"},
    "deagle-mekanik": {"name": "Desert Eagle | Mekanik", "tier": "classified", "icon": "🔫", "value": 175.0, "case": "yildiz"},
    "awp-medusa": {"name": "AWP | Medusa Bakışı", "tier": "covert", "icon": "🎯", "value": 310.0, "case": "yildiz"},
    "m4a1s-buz": {"name": "M4A1-S | Buz Deseni", "tier": "restricted", "icon": "🔫", "value": 125.0, "case": "spektrum"},
    "mp9-gokkusagi": {"name": "MP9 | Gökkuşağı", "tier": "classified", "icon": "🔫", "value": 225.0, "case": "spektrum"},
    "awp-neon": {"name": "AWP | Neon Işık", "tier": "covert", "icon": "🎯", "value": 390.0, "case": "spektrum"},
    "ak47-buzkral": {"name": "AK-47 | Buz Kraliçesi", "tier": "classified", "icon": "🔫", "value": 285.0, "case": "buzcagi"},
    "awp-kiyamet": {"name": "AWP | Kıyamet", "tier": "covert", "icon": "🎯", "value": 530.0, "case": "buzcagi"},
    "m4a4-lav": {"name": "M4A4 | Lav Damarı", "tier": "classified", "icon": "🔫", "value": 345.0, "case": "yanardag"},
    "deagle-ejder": {"name": "Desert Eagle | Ejder", "tier": "covert", "icon": "🔫", "value": 630.0, "case": "yanardag"},
    "ak47-altinaslan": {"name": "AK-47 | Altın Aslan", "tier": "covert", "icon": "🔫", "value": 560.0, "case": "efsane"},
    "karambit-golge": {"name": "Karambit | Gölge Hançeri", "tier": "extraordinary", "icon": "🔪", "value": 1150.0, "case": "efsane"},
    "awp-imparator": {"name": "AWP | İmparator", "tier": "covert", "icon": "🎯", "value": 820.0, "case": "imparator"},
    "karambit-kizil": {"name": "Karambit | Kızıl İmparator", "tier": "extraordinary", "icon": "🔪", "value": 1850.0, "case": "imparator"},
}

# weight değerleri: yüksek tier daha nadir çıkar.
# items listesi: ("skin_key", weight) veya ("balance", weight, TL) / ("premium", weight, gün)
_CASE_DEFS = {
    "kumbara": {"name": "Kumbara Kasa", "price": 40.0, "icon": "🐷", "color": "#b0c3d9", "tag": "BAŞLANGIÇ",
                "items": [("mp5-desert", 60), ("p250-pasli", 40), ("balance", 20, 10), ("premium", 8, 1)]},
    "mavzer": {"name": "Mavzer Kasa", "price": 60.0, "icon": "🔫", "color": "#5e98d9", "tag": "GÜNCEL",
               "items": [("ump-sokak", 55), ("mac10-alev", 30), ("mp7-neon", 15), ("balance", 15, 15), ("premium", 6, 1)]},
    "cepane": {"name": "Cepane Kasa", "price": 80.0, "icon": "📦", "color": "#8847ff", "tag": "POPÜLER",
               "items": [("sg553-kamuflaj", 45), ("galil-renk", 35), ("famas-kristal", 20), ("balance", 15, 20), ("premium", 5, 2)]},
    "savascı": {"name": "Savaşçı Kasa", "price": 110.0, "icon": "🪖", "color": "#8847ff", "tag": "POPÜLER",
                "items": [("awp-gece", 45), ("m4a4-asindirici", 35), ("usps-altincizgi", 20), ("balance", 14, 25), ("premium", 5, 2)]},
    "yildiz": {"name": "Yıldız Kasa", "price": 140.0, "icon": "⭐", "color": "#d32ce6", "tag": "GÜNCEL",
               "items": [("ak47-pas", 50), ("deagle-mekanik", 35), ("awp-medusa", 15), ("balance", 12, 35), ("premium", 4, 3)]},
    "spektrum": {"name": "Spektrum Kasa", "price": 180.0, "icon": "🌈", "color": "#d32ce6", "tag": "GÜNCEL",
                 "items": [("m4a1s-buz", 45), ("mp9-gokkusagi", 40), ("awp-neon", 15), ("balance", 12, 45), ("premium", 4, 3)]},
    "buzcagi": {"name": "Buz Çağı Kasa", "price": 220.0, "icon": "❄️", "color": "#5e98d9", "tag": "ÖZEL",
                "items": [("ak47-buzkral", 65), ("awp-kiyamet", 35), ("balance", 10, 55), ("premium", 4, 5)]},
    "yanardag": {"name": "Yanardağ Kasa", "price": 280.0, "icon": "🌋", "color": "#eb4b4b", "tag": "ÖZEL",
                 "items": [("m4a4-lav", 65), ("deagle-ejder", 35), ("balance", 10, 70), ("premium", 4, 7)]},
    "efsane": {"name": "Efsane Kasa", "price": 360.0, "icon": "👑", "color": "#eb4b4b", "tag": "EKSKLUSİF",
               "items": [("ak47-altinaslan", 80), ("karambit-golge", 20), ("balance", 10, 90), ("premium", 4, 10)]},
    "imparator": {"name": "İmparator Kasa", "price": 500.0, "icon": "💎", "color": "#ffd700", "tag": "EKSKLUSİF",
                  "items": [("awp-imparator", 80), ("karambit-kizil", 20), ("balance", 10, 125), ("premium", 4, 14)]},
}

CASES = {}
for _ck, _cd in _CASE_DEFS.items():
    _items = []
    for _entry in _cd["items"]:
        if _entry[0] == "balance":
            _items.append({
                "name": f"{_entry[2]:g} TL Bakiye", "tier": "consumer", "type": "balance",
                "icon": "💰", "value": float(_entry[2]), "weight": _entry[1],
            })
        elif _entry[0] == "premium":
            _items.append({
                "name": f"{_entry[2]:g} Gün Premium", "tier": "covert", "type": "premium",
                "icon": "👑", "value": float(_entry[2]), "weight": _entry[1],
            })
        else:
            _skin_key, _weight = _entry[0], _entry[1]
            _s = SKINS[_skin_key]
            _items.append({
                "name": _s["name"], "tier": _s["tier"], "type": "skin",
                "skin": _skin_key, "icon": _s["icon"], "value": _s["value"], "weight": _weight,
            })
    _cd["items"] = _items
    CASES[_ck] = _cd

# ---------------------------------------------------------------------------
# CANLI PAZAR FİYAT MOTORU
# market_price(skin_key, t): deterministik, zamanla oynayan fiyat. Sunucu
# durumuna bağlı değildir; aynı anda her kullanıcı aynı fiyatı görür.
# ---------------------------------------------------------------------------
_PRICE_SPAN = 3600.0  # fiyat döngüsünün ana periyodu (1 saat)


def _skin_seed(key: str) -> int:
    return int(hashlib.md5(key.encode()).hexdigest(), 16)


def market_price(skin_key: str, t: float | None = None) -> float:
    skin = SKINS.get(skin_key)
    if skin is None:
        return 0.0
    t = time.time() if t is None else t
    seed = _skin_seed(skin_key)
    p1 = (seed % 100) / 100.0
    p2 = ((seed >> 8) % 100) / 100.0
    p3 = ((seed >> 16) % 100) / 100.0
    w1 = math.sin(2 * math.pi * t / _PRICE_SPAN + p1 * 6.283)
    w2 = 0.6 * math.sin(2 * math.pi * t / (_PRICE_SPAN * 3.7) + p2 * 6.283)
    w3 = 0.35 * math.sin(2 * math.pi * t / (_PRICE_SPAN * 0.13) + p3 * 6.283)
    w4 = 0.18 * math.sin(2 * math.pi * t / 47.0 + p1 * 4.0)
    factor = 1.0 + 0.14 * w1 + 0.08 * w2 + 0.05 * w3 + 0.02 * w4
    factor = max(0.6, min(1.6, factor))
    return round(skin["value"] * factor, 2)


def market_history(skin_key: str, points: int = 40, step: float = 90.0) -> list[float]:
    t = time.time()
    return [market_price(skin_key, t - (points - 1 - i) * step) for i in range(points)]


def market_change_pct(skin_key: str, window: float = 3600.0) -> float:
    now = time.time()
    cur = market_price(skin_key, now)
    prev = market_price(skin_key, now - window)
    if prev == 0:
        return 0.0
    return round((cur - prev) / prev * 100.0, 2)


def rate_allowed(ip: str, action: str) -> bool:
    limit, window = RATE_LIMITS.get(action, (10, 300))
    now = int(time.time())
    key = (ip, action)
    with _RATE_LOCK:
        stamps = [t for t in _RATE_BUCKETS.get(key, []) if now - t < window]
        if len(stamps) >= limit:
            _RATE_BUCKETS[key] = stamps
            return False
        stamps.append(now)
        _RATE_BUCKETS[key] = stamps
        return True


WHEELS = {
    "ucretsiz": {
        "name": "\u00dccretsiz \u00c7ark",
        "cost": 0,
        "icon": "\U0001f3b0",
        "border": "#4ade80",
        "rewards": [1, 2, 3, 5, 7, 10, 15, 25],
        "weights": [30, 25, 18, 12, 7, 4, 2.5, 1.5],
    },
    "bronz": {
        "name": "Bronz \u00c7ark",
        "cost": 25,
        "icon": "\U0001f949",
        "border": "#cd7f32",
        "rewards": [5, 10, 15, 25, 40, 60, 85, 125],
        "weights": [30, 25, 18, 12, 7, 4, 2.5, 1.5],
    },
    "gumus": {
        "name": "G\u00fcm\u00fc\u015f \u00c7ark",
        "cost": 50,
        "icon": "\U0001f948",
        "border": "#c0c0c0",
        "rewards": [10, 20, 35, 55, 80, 120, 175, 275],
        "weights": [30, 25, 18, 12, 7, 4, 2.5, 1.5],
    },
    "altin": {
        "name": "Alt\u0131n \u00c7ark",
        "cost": 100,
        "icon": "\U0001f947",
        "border": "#ffd700",
        "rewards": [25, 50, 80, 120, 175, 275, 500, 1000],
        "weights": [30, 25, 18, 12, 7, 4, 2.5, 1.5],
    },
}


_db_migrated = threading.Lock()
_db_done = False


def _migrate(connection) -> None:
    global _db_done
    if _db_done:
        return
    with _db_migrated:
        if _db_done:
            return
        if IS_POSTGRES:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    id BIGSERIAL PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    is_premium INTEGER NOT NULL DEFAULT 0,
                    premium_until BIGINT NOT NULL DEFAULT 0,
                    balance DOUBLE PRECISION NOT NULL DEFAULT 0,
                    last_daily_claim BIGINT NOT NULL DEFAULT 0,
                    daily_streak INTEGER NOT NULL DEFAULT 0,
                    last_free_spin BIGINT NOT NULL DEFAULT 0,
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_lower ON users (LOWER(username))")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_until BIGINT NOT NULL DEFAULT 0")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS bio TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_link TEXT NOT NULL DEFAULT ''")
            connection.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS status_text TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS friends (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    friend_id BIGINT NOT NULL REFERENCES users(id),
                    created_at BIGINT NOT NULL,
                    UNIQUE(user_id, friend_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS friend_requests (
                    id BIGSERIAL PRIMARY KEY,
                    sender_id BIGINT NOT NULL REFERENCES users(id),
                    receiver_id BIGINT NOT NULL REFERENCES users(id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at BIGINT NOT NULL,
                    UNIQUE(sender_id, receiver_id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS private_messages (
                    id BIGSERIAL PRIMARY KEY,
                    sender_id BIGINT NOT NULL REFERENCES users(id),
                    receiver_id BIGINT NOT NULL REFERENCES users(id),
                    body TEXT NOT NULL DEFAULT '',
                    image TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS case_inventory (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    case_key TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    item_tier TEXT NOT NULL,
                    item_value DOUBLE PRECISION NOT NULL DEFAULT 0,
                    skin_key TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute("ALTER TABLE case_inventory ADD COLUMN IF NOT EXISTS skin_key TEXT NOT NULL DEFAULT ''")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS market_listings (
                    id BIGSERIAL PRIMARY KEY,
                    seller_id BIGINT NOT NULL REFERENCES users(id),
                    inv_id BIGINT NOT NULL REFERENCES case_inventory(id),
                    skin_key TEXT NOT NULL,
                    price DOUBLE PRECISION NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS trade_requests (
                    id BIGSERIAL PRIMARY KEY,
                    sender_id BIGINT NOT NULL REFERENCES users(id),
                    receiver_id BIGINT NOT NULL REFERENCES users(id),
                    sender_inv_id BIGINT NOT NULL REFERENCES case_inventory(id),
                    receiver_inv_id BIGINT NOT NULL REFERENCES case_inventory(id),
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    expires_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    size BIGINT NOT NULL,
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS updates (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tag TEXT NOT NULL DEFAULT 'GÜNCELLEME',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS payments (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    platform TEXT NOT NULL DEFAULT 'STEAM',
                    code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'BEKLEMEDE',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute("ALTER TABLE payments ADD COLUMN IF NOT EXISTS platform TEXT NOT NULL DEFAULT 'STEAM'")
            connection.execute(
                """CREATE TABLE IF NOT EXISTS logs (
                    id BIGSERIAL PRIMARY KEY,
                    event TEXT NOT NULL,
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS downloads (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    filename TEXT NOT NULL,
                    serial TEXT NOT NULL,
                    ip TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_messages (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL REFERENCES users(id),
                    username TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    image TEXT NOT NULL DEFAULT '',
                    created_at BIGINT NOT NULL
                )"""
            )
        else:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    is_premium INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL
                )"""
            )
            try:
                connection.execute("ALTER TABLE users ADD COLUMN is_premium INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            try:
                connection.execute("ALTER TABLE users ADD COLUMN premium_until INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            try:
                connection.execute("ALTER TABLE users ADD COLUMN balance REAL NOT NULL DEFAULT 0.0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            try:
                connection.execute("ALTER TABLE users ADD COLUMN last_daily_claim INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            try:
                connection.execute("ALTER TABLE users ADD COLUMN daily_streak INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            try:
                connection.execute("ALTER TABLE users ADD COLUMN last_free_spin INTEGER NOT NULL DEFAULT 0")
                connection.commit()
            except sqlite3.OperationalError:
                pass

            for col, ddl in (("avatar", "TEXT NOT NULL DEFAULT ''"), ("bio", "TEXT NOT NULL DEFAULT ''"), ("profile_link", "TEXT NOT NULL DEFAULT ''"), ("status_text", "TEXT NOT NULL DEFAULT ''")):
                try:
                    connection.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
                    connection.commit()
                except sqlite3.OperationalError:
                    pass

            connection.execute(
                """CREATE TABLE IF NOT EXISTS friends (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    friend_id INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    UNIQUE(user_id, friend_id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS friend_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    receiver_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    UNIQUE(sender_id, receiver_id),
                    FOREIGN KEY(sender_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS private_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    receiver_id INTEGER NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    image TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(sender_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS case_inventory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    case_key TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    item_tier TEXT NOT NULL,
                    item_value REAL NOT NULL DEFAULT 0,
                    skin_key TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            try:
                connection.execute("ALTER TABLE case_inventory ADD COLUMN skin_key TEXT NOT NULL DEFAULT ''")
                connection.commit()
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """CREATE TABLE IF NOT EXISTS market_listings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    seller_id INTEGER NOT NULL,
                    inv_id INTEGER NOT NULL,
                    skin_key TEXT NOT NULL,
                    price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(seller_id) REFERENCES users(id),
                    FOREIGN KEY(inv_id) REFERENCES case_inventory(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS trade_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id INTEGER NOT NULL,
                    receiver_id INTEGER NOT NULL,
                    sender_inv_id INTEGER NOT NULL,
                    receiver_inv_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(sender_id) REFERENCES users(id),
                    FOREIGN KEY(receiver_id) REFERENCES users(id)
                )"""
            )

            connection.execute(
                """CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS updates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    body TEXT NOT NULL,
                    tag TEXT NOT NULL DEFAULT 'GÜNCELLEME',
                    created_at INTEGER NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    platform TEXT NOT NULL DEFAULT 'STEAM',
                    code TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'BEKLEMEDE',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            try:
                connection.execute("ALTER TABLE payments ADD COLUMN platform TEXT NOT NULL DEFAULT 'STEAM'")
                connection.commit()
            except sqlite3.OperationalError:
                pass
            connection.execute(
                """CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    filename TEXT NOT NULL,
                    serial TEXT NOT NULL,
                    ip TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    username TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    image TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )"""
            )
        if connection.execute("SELECT COUNT(*) AS n FROM updates").fetchone()["n"] == 0:
            connection.execute(
                "INSERT INTO updates(title, body, tag, created_at) VALUES(?,?,?,?)",
                (
                    "Biga Cheat başlangıç sürümü",
                    "CS2 için yeni sürüm alanı, güvenli indirme ve topluluk projeleri yayında.",
                    "YAYIN",
                    int(time.time()),
                ),
            )
        connection.commit()
        _db_done = True


class _Proxy:
    """connection.execute(...) erişimini SQLite ve Postgres için birleştirir."""

    def __init__(self, raw) -> None:
        self._raw = raw

    def execute(self, sql: str, params: tuple = ()):
        if IS_POSTGRES:
            cursor = self._raw.cursor(cursor_factory=psycopg2.extras.DictCursor)
            cursor.execute(sql.replace("?", "%s"), params)
            return cursor
        return self._raw.execute(sql, params)

    def insert_id(self, sql: str, params: tuple = ()) -> int:
        if IS_POSTGRES:
            cursor = self._raw.cursor()
            cursor.execute(sql.replace("?", "%s") + " RETURNING id", params)
            row = cursor.fetchone()
            return int(row[0]) if row else 0
        return int(self._raw.execute(sql, params).lastrowid)

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        self._raw.close()


@contextmanager
def db():
    if IS_POSTGRES:
        raw = psycopg2.connect(DATABASE_URL, sslmode="require")
    else:
        raw = sqlite3.connect(DB_PATH, timeout=30.0)
        raw.row_factory = sqlite3.Row
    connection = _Proxy(raw)
    _migrate(connection)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def log_event(event: str) -> None:
    with db() as connection:
        connection.execute("INSERT INTO logs(event, created_at) VALUES(?,?)", (event, int(time.time())))



def password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${derived.hex()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        method, salt_hex, digest_hex = stored.split("$", 2)
        if method != "scrypt":
            return False
        candidate = password_hash(password, bytes.fromhex(salt_hex)).split("$", 2)[2]
        return hmac.compare_digest(candidate, digest_hex)
    except (ValueError, TypeError):
        return False


def token_digest(token: str) -> str:
    return hmac.new(APP_SECRET, token.encode(), hashlib.sha256).hexdigest()


def admin_cookie_value() -> str:
    expires = int(time.time()) + ADMIN_TTL
    nonce = secrets.token_urlsafe(24)
    payload = f"{ADMIN_USERNAME}:{expires}:{nonce}"
    signature = hmac.new(APP_SECRET, ("admin:" + payload).encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def is_admin_cookie(value: str | None) -> bool:
    if not value:
        return False
    parts = value.split(":", 3)
    if len(parts) != 4:
        return False
    username, expires, nonce, signature = parts
    if username != ADMIN_USERNAME or not nonce:
        return False
    try:
        if int(expires) < int(time.time()):
            return False
    except ValueError:
        return False
    payload = f"{username}:{expires}:{nonce}"
    expected = hmac.new(APP_SECRET, ("admin:" + payload).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


def loader_token_value(user_id: int) -> str:
    expires = int(time.time()) + LOADER_TTL
    nonce = secrets.token_urlsafe(16)
    payload = f"{user_id}:{expires}:{nonce}"
    signature = hmac.new(APP_SECRET, ("loader:" + payload).encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_loader_token(value: str | None) -> int | None:
    if not value:
        return None
    parts = value.split(":", 3)
    if len(parts) != 4:
        return None
    user_id, expires, nonce, signature = parts
    if not nonce:
        return None
    try:
        uid = int(user_id)
        if int(expires) < int(time.time()):
            return None
    except ValueError:
        return None
    payload = f"{uid}:{expires}:{nonce}"
    expected = hmac.new(APP_SECRET, ("loader:" + payload).encode(), hashlib.sha256).hexdigest()
    return uid if hmac.compare_digest(signature, expected) else None


def esc(value: str) -> str:
    return html.escape(value, quote=True)


def parse_multipart(body: bytes, content_type: str) -> dict[str, dict[str, object]]:
    marker = "boundary="
    if marker not in content_type:
        return {}
    boundary = content_type.split(marker, 1)[1].split(";", 1)[0].strip().strip('"').encode()
    result: dict[str, dict[str, object]] = {}
    for raw_part in body.split(b"--" + boundary):
        part = raw_part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if part.endswith(b"--"):
            part = part[:-2].rstrip(b"\r\n")
        header_blob, separator, data = part.partition(b"\r\n\r\n")
        if not separator:
            continue
        headers = header_blob.decode("utf-8", "replace").split("\r\n")
        disposition = next((h for h in headers if h.lower().startswith("content-disposition:")), "")
        name_match = re.search(r'name="([^"]+)"', disposition)
        if not name_match:
            continue
        filename_match = re.search(r'filename="([^"]*)"', disposition)
        result[name_match.group(1)] = {"filename": filename_match.group(1) if filename_match else "", "data": data.rstrip(b"\r\n")}
    return result


def human_size(size: int) -> str:
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.2f} MB"
    return f"{max(1, size // 1024)} KB"


def generate_license_serial() -> str:
    return "BC-" + "-".join(secrets.token_hex(3).upper() for _ in range(3))


def make_watermarked_zip(username: str, serial: str, files: list[Path]) -> bytes:
    """Kişiye özel LICENSE.txt + seri numarasını ZIP yorumuna gömen arşiv üretir."""
    now = time.strftime("%Y-%m-%d %H:%M", time.localtime())
    license_txt = f"""BIGA CHEAT - LİSANS SERTİFİKASI
==================================
Bu arşiv kişiye özel üretilmiştir ve yalnızca lisans sahibine aittir.

Kullanıcı        : {username}
Lisans No        : {serial}
Üretim Tarihi    : {now}

Bu dosyayı paylaşmak yasaktır. Bu arşiv veya içindeki lisans numarası
bir başkasında görülürse, lisans sahibinin premium erişimi kalıcı olarak
iptal edilir ve hesabı dondurulur.

© Biga Cheat
"""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("LICENSE.txt", license_txt.encode("utf-8"))
        for f in files:
            if f.is_file() and f.name.lower() != "readme.md":
                zf.write(f, arcname=f.name)
        zf.comment = f"BigaCheat|{serial}|{username}".encode("utf-8")
    return buffer.getvalue()


def format_date(timestamp: int) -> str:
    return time.strftime("%d.%m.%Y", time.localtime(timestamp))


PLATFORM_NAMES = {
    "STEAM": "Steam",
    "GPLAY": "Google Play",
}


def validate_game_code(platform: str, code: str) -> bool:
    """Steam ve Google Play kodlarının biçimini doğrular; kopya/yanlış formatı eler."""
    code = code.strip().upper()
    if platform == "STEAM":
        return bool(re.fullmatch(r"[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}", code))
    if platform == "GPLAY":
        return bool(re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}-[A-Z0-9]{4}", code))
    return False


def page(title: str, body: str, user: str | None = None, message: str = "", message_type: str = "", is_premium: bool = False, csrf_token: str = "", body_csrf: str = "") -> str:
    premium_badge = ' <span class="premium-badge">PREMIUM</span>' if is_premium else ""
    csrf_input = f'<input type="hidden" name="csrf_token" value="{esc(csrf_token)}">' if csrf_token else ""
    body_data_attr = f' data-csrf="{esc(body_csrf)}"' if body_csrf else ""
    
    balance_val = 0.0
    if user:
        try:
            with db() as connection:
                row = connection.execute("SELECT balance FROM users WHERE username=?", (user,)).fetchone()
                if row:
                    balance_val = float(row["balance"])
        except Exception:
            pass
            
    balance_pill = f'<span class="balance-pill">Bakiye: {balance_val:.2f} TL</span>' if user else ""
    account = (
        f'<a class="ghost button" href="/updates">Güncellemeler</a><a class="ghost button" href="/paid-cheats" style="color: #ffd700; border-color: #ffd7003d;">💎 Ücretli Hileler</a><a class="ghost button" href="/projects">Projeler</a><a class="ghost button" href="/chat" style="color: #65d9ff; border-color: #65d9ff3d;">💬 Sohbet</a><a class="ghost button" href="/friends" style="color: #b78bff; border-color: #b78bff3d;">👥 Arkadaşlar</a><a class="ghost button" href="/inventory" style="color: #65d9ff; border-color: #65d9ff3d;">🎒 Envanter</a><a class="ghost button" href="/market" style="color: #4ade80; border-color: #4ade803d;">🛒 Pazar</a><a class="ghost button" href="/trade" style="color: #ffd700; border-color: #ffd7003d;">🔁 Takas</a><a class="ghost button" href="/cases" style="color: #ffd700; border-color: #ffd7003d;">🎁 Kasalar</a><a class="ghost button" href="/wheel" style="color: #ff6b6b; border-color: #ff6b6b3d;">🎡 Çark</a><a class="ghost button" href="/daily" style="color: #ffd700; border-color: #ffd7003d;">Günlük Ödül</a><a class="ghost button" href="/payment" style="color: #65d9ff; border-color: #65d9ff3d;">Bakiye Yükle</a><a class="ghost button" href="/admin">Yönetim</a>{balance_pill}<a class="user-pill" href="/profile/{quote(user)}" title="Profili görüntüle">{esc(user)}{premium_badge}</a><form method="post" action="/logout" class="inline">{csrf_input}<button class="ghost" type="submit">Çıkış</button></form>'
        if user
        else '<a class="ghost button" href="/updates">Güncellemeler</a><a class="ghost button" href="/paid-cheats" style="color: #ffd700; border-color: #ffd7003d;">💎 Ücretli Hileler</a><a class="ghost button" href="/projects">Projeler</a><a class="ghost button" href="/chat" style="color: #65d9ff; border-color: #65d9ff3d;">💬 Sohbet</a><a class="ghost button" href="/market" style="color: #4ade80; border-color: #4ade803d;">🛒 Pazar</a><a class="ghost button" href="/admin">Yönetim</a><a class="ghost button" href="/login">Giriş</a><a class="button primary" href="/register">Kayıt ol</a>'
    )
    msg_class = "notice " + message_type if message_type else "notice"
    notice = f'<div class="{msg_class}">{esc(message)}</div>' if message else ""
    return f"""<!doctype html>
<html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · Biga Cheat</title>
<meta name="description" content="Biga Cheat - CS2 topluluğu için sürüm duyuruları, projeler ve indirme alanı.">
<meta name="robots" content="index,follow">
<link rel="canonical" href="https://biga-cheat-site.onrender.com/">
<meta property="og:type" content="website">
<meta property="og:site_name" content="Biga Cheat">
<meta property="og:title" content="{esc(title)} · Biga Cheat">
<meta property="og:description" content="Biga Cheat - CS2 topluluğu için sürüm duyuruları, projeler ve indirme alanı.">
<meta property="og:url" content="https://biga-cheat-site.onrender.com/">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="{esc(title)} · Biga Cheat">
<meta name="twitter:description" content="Biga Cheat - CS2 topluluğu için sürüm duyuruları, projeler ve indirme alanı.">
<link rel="icon" type="image/png" href="/static/favicon.png">
<link rel="apple-touch-icon" href="/static/favicon.png">
<link rel="stylesheet" href="/static/style.css"></head>
<body{body_data_attr}><div class="orb orb-a"></div><div class="orb orb-b"></div><div class="orb orb-c"></div>
<header class="topbar"><a class="brand" href="/"><img class="brand-logo" src="/static/favicon.png" alt="Biga Cheat logo" width="34" height="34"><span>Biga Cheat</span></a><nav>{account}</nav></header>
<main>{notice}{body}</main><footer><b>Biga Cheat</b> · güvenli indirme alanı · Tüm hakları saklıdır © 2026</footer>
<script>
    const notice = document.querySelector('.notice.success');
    if (notice && (notice.textContent.includes('ödülü') || notice.textContent.includes('bakiye') || notice.textContent.includes('Bakiye'))) {{
        window.addEventListener('load', () => {{
            try {{
                const AudioContext = window.AudioContext || window.webkitAudioContext;
                const ctx = new AudioContext();
                
                // Note 1 (C5)
                const osc1 = ctx.createOscillator();
                const gain1 = ctx.createGain();
                osc1.type = 'sine';
                osc1.frequency.setValueAtTime(523.25, ctx.currentTime);
                gain1.gain.setValueAtTime(0.08, ctx.currentTime);
                gain1.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.1);
                osc1.connect(gain1);
                gain1.connect(ctx.destination);
                osc1.start();
                osc1.stop(ctx.currentTime + 0.1);
                
                // Note 2 (E5)
                setTimeout(() => {{
                    const osc2 = ctx.createOscillator();
                    const gain2 = ctx.createGain();
                    osc2.type = 'sine';
                    osc2.frequency.setValueAtTime(659.25, ctx.currentTime);
                    gain2.gain.setValueAtTime(0.08, ctx.currentTime);
                    gain2.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.15);
                    osc2.connect(gain2);
                    gain2.connect(ctx.destination);
                    osc2.start();
                    osc2.stop(ctx.currentTime + 0.15);
                }}, 85);
                
                // Note 3 (G5)
                setTimeout(() => {{
                    const osc3 = ctx.createOscillator();
                    const gain3 = ctx.createGain();
                    osc3.type = 'sine';
                    osc3.frequency.setValueAtTime(783.99, ctx.currentTime);
                    gain3.gain.setValueAtTime(0.12, ctx.currentTime);
                    gain3.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
                    osc3.connect(gain3);
                    gain3.connect(ctx.destination);
                    osc3.start();
                    osc3.stop(ctx.currentTime + 0.3);
                }}, 170);
            }} catch(e) {{
                console.error(e);
            }}
        }});
    }}
</script>
</body></html>"""


def form_page(title: str, action: str, submit: str, fields: str, user: str | None, message: str = "", message_type: str = "", is_premium: bool = False, csrf_token: str = "") -> str:
    return page(title, f"""<section class="auth-card"><div class="eyebrow">BIGA CHEAT</div><h1>{esc(title)}</h1><p class="muted">Hesabınla devam et.</p>
<form method="post" action="{action}" class="form">{fields}<button class="button primary wide" type="submit">{submit}</button></form>
<p class="switch">{'Hesabın yok mu? <a href="/register">Kayıt ol</a>' if action == '/login' else 'Zaten hesabın var mı? <a href="/login">Giriş yap</a>'}</p></section>""", user, message, message_type, is_premium, csrf_token)


def csrf_for(handler: BaseHTTPRequestHandler) -> str:
    token = handler.session_cookie()
    if token:
        return token
    admin_tok = handler.admin_cookie()
    if admin_tok:
        return admin_tok
    return ""


def generate_captcha() -> tuple[str, str]:
    num1 = random.randint(2, 9)
    num2 = random.randint(2, 9)
    ans = num1 + num2
    expires = int(time.time()) + 300
    payload = f"{expires}:{ans}"
    sig = hmac.new(APP_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{num1} + {num2}", f"{payload}:{sig}"


def verify_captcha_cookie(cookie_val: str | None, answer: str) -> bool:
    if not cookie_val or not answer:
        return False
    parts = cookie_val.split(":", 2)
    if len(parts) != 3:
        return False
    expires, ans_str, sig = parts
    try:
        if int(expires) < int(time.time()):
            return False
    except ValueError:
        return False
    payload = f"{expires}:{ans_str}"
    expected = hmac.new(APP_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    return ans_str == answer.strip()



class Handler(BaseHTTPRequestHandler):
    server_version = "BigaCheat/1.0"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def session_cookie(self) -> str | None:
        cookies = self.headers.get("Cookie", "")
        match = re.search(r"\bsession_v2=([^;]+)", cookies)
        return match.group(1) if match else None

    def admin_cookie(self) -> str | None:
        cookies = self.headers.get("Cookie", "")
        match = re.search(r"\badmin_session_v2=([^;]+)", cookies)
        return match.group(1) if match else None

    def current_user(self) -> tuple[str, int] | None:
        token = self.session_cookie()
        if not token:
            return None
        with db() as connection:
            row = connection.execute(
                "SELECT users.username, users.id FROM sessions JOIN users ON users.id=sessions.user_id WHERE sessions.token_hash=? AND sessions.expires_at>?",
                (token_digest(token), int(time.time())),
            ).fetchone()
        return (row["username"], row["id"]) if row else None

    def is_user_premium(self, user_id: int) -> bool:
        with db() as connection:
            row = connection.execute("SELECT is_premium, premium_until FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            return False
        return bool(row["is_premium"]) or row["premium_until"] > int(time.time())

    def premium_expiry(self, user_id: int) -> int:
        with db() as connection:
            row = connection.execute("SELECT premium_until FROM users WHERE id=?", (user_id,)).fetchone()
        return int(row["premium_until"]) if row else 0

    def set_cookie(self, token: str, max_age: int = SESSION_TTL) -> None:
        flags = "Path=/; HttpOnly; SameSite=Lax"
        if COOKIE_SECURE:
            flags += "; Secure"
        self.send_header("Set-Cookie", f"session_v2={token}; Max-Age={max_age}; {flags}")

    def set_admin_cookie(self, value: str, max_age: int = ADMIN_TTL) -> None:
        flags = "Path=/; HttpOnly; SameSite=Lax"
        if COOKIE_SECURE:
            flags += "; Secure"
        self.send_header("Set-Cookie", f"admin_session_v2={value}; Max-Age={max_age}; {flags}")

    def captcha_cookie(self) -> str | None:
        cookie = self.headers.get("Cookie", "")
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == "captcha" and value:
                return value
        return None

    def client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def send_html(self, content: str, status: int = 200, cookies: list[tuple[str, str]] | None = None) -> None:
        data = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.send_header("Cache-Control", "no-store")
        if cookies:
            for name, value in cookies:
                self.send_header("Set-Cookie", f"{name}={value}; Path=/; HttpOnly; SameSite=Lax")
        self.end_headers()
        self.wfile.write(data)

    def rate_limit_response(self) -> None:
        self.send_html(
            page(
                "Çok fazla istek",
                '<section class="auth-card"><h1>Yavaşla</h1><p class="muted">Çok fazla deneme yaptın. Lütfen birkaç dakika bekle.</p></section>',
                "",
            ),
            HTTPStatus.TOO_MANY_REQUESTS,
        )

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.send_header("Cache-Control", "no-store")
        if COOKIE_SECURE:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        self.end_headers()
        self.wfile.write(body)

    def parse_json(self) -> dict[str, str]:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 1_000_000))
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}



    def verify_csrf(self, fields: dict[str, str] | dict[str, dict[str, object]], is_multipart: bool = False) -> bool:
        expected = csrf_for(self)
        if not expected:
            return False
        if is_multipart:
            token = str(fields.get("csrf_token", {}).get("data", b"").decode("utf-8", "replace")).strip()
        else:
            token = fields.get("csrf_token", "").strip()
        return hmac.compare_digest(token, expected)

    def redirect(self, location: str, token: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if token is not None:
            self.set_cookie(token)
        self.end_headers()

    def redirect_admin(self, location: str, value: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if value is not None:
            self.set_admin_cookie(value)
        self.end_headers()

    def parse_form(self) -> dict[str, str]:
        try:
            length = max(0, min(int(self.headers.get("Content-Length", "0")), 16_384))
        except (ValueError, TypeError):
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace")
        return {key: values[0] for key, values in parse_qs(raw).items() if values}

    def do_GET(self) -> None:
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        query = parse_qs(parsed_url.query)
        message = query.get("msg", [""])[0]
        message_type = query.get("msg_type", [""])[0]
        user = self.current_user()
        username = user[0] if user else None
        is_premium = self.is_user_premium(user[1]) if user else False
        csrf_tok = csrf_for(self)

        if path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", "2")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(b"ok")
            return
        elif path == "/api/loader/version":
            self.send_json({"version": LOADER_VERSION, "download_url": "/downloads/BigaCheat-Loader.exe"})
            return
        elif path == "/downloads/BigaCheat-Loader.exe":
            if not LOADER_PATH.is_file():
                self.send_html(page("Dosya yok", '<section class="auth-card"><h1>Loader henüz yüklenmedi</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(LOADER_PATH.stat().st_size))
            self.send_header("Content-Disposition", 'attachment; filename="BigaCheat-Loader.exe"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            with LOADER_PATH.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
            return
        elif path == "/robots.txt":
            robots = "User-agent: *\nAllow: /\nDisallow: /admin/\nDisallow: /login\nDisallow: /register\n"
            data = robots.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        elif path == "/sitemap.xml":
            base = "https://biga-cheat-site.onrender.com"
            urls = ["", "/updates", "/projects", "/wheel", "/daily", "/payment", "/login", "/register"]
            lines = ['<?xml version="1.0" encoding="UTF-8"?>', '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
            for u in urls:
                lines.append(f"<url><loc>{base}{u}</loc><changefreq>daily</changefreq></url>")
            lines.append("</urlset>")
            data = "\n".join(lines).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        elif path == "/chat/api/messages":
            if not user:
                self.send_json({"ok": False, "error": "giris_gerekli"}, 401)
                return
            try:
                after = int(query.get("after", ["0"])[0] or 0)
            except ValueError:
                after = 0
            with db() as connection:
                rows = connection.execute(
                    "SELECT id, username, body, image, created_at FROM chat_messages WHERE id>? ORDER BY id ASC LIMIT 200",
                    (after,),
                ).fetchall()
            messages = []
            for row in rows:
                messages.append({
                    "id": row["id"],
                    "username": row["username"],
                    "body": row["body"],
                    "image": row["image"],
                    "time": time.strftime("%H:%M", time.localtime(row["created_at"])),
                })
            self.send_json({"ok": True, "messages": messages})
            return
        elif path == "/":
            status = "Hesabınla giriş yaparak sürümü indirebilirsin." if not user else "Hesabın hazır. Güncel sürümü aşağıdan indirebilirsin."
            download_btn = '<a class="button primary" href="/download">Loader’ı indir</a>' if user else '<a class="button primary" href="/register">Ücretsiz hesap oluştur</a><a class="button ghost" href="/login">Giriş yap</a>'
            body = f"""<section class="hero"><img class="hero-logo" src="/static/logo.png" alt="Biga Cheat logo" width="150" height="150"><div class="eyebrow">CS2 İÇİN ÖZEL SÜRÜM</div><h1>CS2 için Biga Cheat<span>.</span></h1><p class="lead">Özel altyapı, otomatik koruma ve tek hesaptan yönetilen premium CS2 sürümü.</p>
<div class="hero-actions">{download_btn}</div></section>
<section class="product-grid">
<article class="product-card free"><span class="product-emoji">🆓</span><h2>Ücretsiz Hile</h2><p>Herkes için hazır ücretsiz sürüm. Kayıt ol, loader’ı indir ve oyuna gir. Sınırsız kullanım.</p><span class="product-tag">BEDAVA</span><div class="product-price">₺0 <small>herkes için</small></div><a class="button" href="/register">Şimdi Başla</a></article>
<article class="product-card paid"><span class="product-emoji">👑</span><h2>Ücretli Hile</h2><p>Kişiye özel filigranlı premium paket. Daha yüksek avantajlar ve öncelikli destek.</p><span class="product-tag">PREMIUM</span><div class="product-price">₺350 <small>30 gün · ₺800 / 90 gün</small></div><a class="button" href="/paid-cheats">Erişim Satın Al</a></article>
</section>
<section class="steps"><h2>Nasıl Aktif Edilir?</h2><div class="steps-grid"><div class="step"><div class="step-num">1</div><h3>Hesap Oluştur</h3><p>Ücretsiz kayıt ol ve hesabına giriş yap.</p></div><div class="step"><div class="step-num">2</div><h3>Loader’ı İndir</h3><p>Güncel loader sürümünü indir ve çalıştır.</p></div><div class="step"><div class="step-num">3</div><h3>Giriş Yap</h3><p>Loader üzerinden site kullanıcı adınla giriş yap.</p></div><div class="step"><div class="step-num">4</div><h3>BAŞLAT’a Bas</h3><p>Seçtiğin sürüm launcher üzerinden çalışır.</p></div></div></section>
<section class="features"><h2>Biga Cheat Avantajları</h2><div class="feature-grid"><div class="feature"><span class="feature-icon">🛡️</span><h3>Özel Altyapı</h3><p>Her açılışta yenilenen, güvenli çalışma mimarisi.</p></div><div class="feature"><span class="feature-icon">⚡</span><h3>Tek Tıkla Başlat</h3><p>Loader üzerinden tek tuşla sürümü çalıştır.</p></div><div class="feature"><span class="feature-icon">🔒</span><h3>Kişiye Özel Lisans</h3><p>Hesabına bağlı filigranlı premium paketler.</p></div><div class="feature"><span class="feature-icon">🎁</span><h3>Günlük Ödül</h3><p>Her gün çark ve günlük ödül ile bakiye kazan.</p></div><div class="feature"><span class="feature-icon">📈</span><h3>Düzenli Güncelleme</h3><p>Sürümler otomatik olarak loader’a iletilir.</p></div><div class="feature"><span class="feature-icon">🤝</span><h3>Topluluk</h3><p>Projeler ve duyurular ile toplulukla iç içe.</p></div></div></section>
<section class="reviews"><h2>Kullanıcı Yorumları</h2><div class="review-grid"><div class="review"><blockquote>“Loader üzerinden tek tıkla açılıyor, hiçbir şey uğraştırmıyor. Daha önce bu kadar kolayını görmedim.”</blockquote><cite>Sas<small>Premium Üye</small></cite></div><div class="review"><blockquote>“Kayıt olup saniyeler içinde oyuna girebiliyorsun. Site de loader da tertemiz.”</blockquote><cite>Legit Şükrü<small>Topluluk Üyesi</small></cite></div><div class="review"><blockquote>“Günlük ödül ve çarktan bakiye topluyorum, premium erişimi bakiyemle alıyorum. Çok mantıklı.”</blockquote><cite>Dreads<small>Premium Üye</small></cite></div></div></section>
<p class="status">{status}</p>"""
            with db() as connection:
                latest_updates = connection.execute("SELECT title, body, tag, created_at FROM updates ORDER BY created_at DESC LIMIT 2").fetchall()
            update_cards = "".join(f'<a class="update-mini" href="/updates"><span class="update-tag">{esc(row["tag"])}</span><strong>{esc(row["title"])}</strong><small>{format_date(row["created_at"])}</small></a>' for row in latest_updates)
            body += f'<section class="update-strip"><div><div class="eyebrow">SON DUYURULAR</div><h2>Güncellemeler</h2></div><div class="update-mini-list">{update_cards}</div><a class="button ghost" href="/updates">Tümünü gör</a></section>'
            self.send_html(page("Ana sayfa", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/chat":
            if not user:
                self.redirect("/login")
                return
            body = f"""<section class="page-head"><div><div class="eyebrow">TOPLULUK SOFRASI</div><h1>💬 Sohbet</h1><p class="lead">Herkesin konuştuğu tek ortak alan. Mesaj at, emoji gönder, fotoğraf paylaş.</p></div></section>
<section class="chat-wrap">
  <div class="chat-box" id="chat-box"></div>
  <div class="chat-input-row">
    <button type="button" class="chat-emoji-btn" id="chat-emoji-btn" title="Emoji">😊</button>
    <input type="text" id="chat-text" maxlength="400" placeholder="Mesajını yaz...">
    <label class="chat-photo-btn" title="Fotoğraf gönder">📷<input type="file" id="chat-photo" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none;"></label>
    <button type="button" class="button primary" id="chat-send">Gönder</button>
  </div>
  <div class="chat-emoji-panel" id="chat-emoji-panel" style="display:none;"></div>
</section>
<script>
const CHAT_EMOJIS = ["😀","😁","😂","🤣","😊","😎","🤩","😍","🥰","😜","🤪","😴","🤔","😅","🙃","😉","👍","👎","👏","🙏","💪","🔥","⚡","🚀","🎯","🏆","💯","❤️","💛","💚","💙","🖤","💎","🎁","👑","🍀","🤝","✌️","🤙","👌","😈","🫡","🥶","🤯","🫠","😭","🥹","😤","🤮","🤡","💀","☠️","🫵","👇","➡️","✅","❌","⚠️","💬","🗣️"];
let lastMsgId = 0;
const box = document.getElementById("chat-box");
const textEl = document.getElementById("chat-text");
const sendBtn = document.getElementById("chat-send");
const emojiBtn = document.getElementById("chat-emoji-btn");
const emojiPanel = document.getElementById("chat-emoji-panel");
const photoEl = document.getElementById("chat-photo");

function escHtml(s) {{
  return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}}

function bubble(m) {{
  const mine = m.me ? " me" : "";
  const img = m.image ? `<img class="chat-img" src="${{m.image}}" alt="foto">` : "";
  const nameLink = `/profile/${encodeURIComponent(m.username)}`;
  return `<div class="chat-msg${{mine}}"><div class="chat-head"><b><a class="chat-name" href="${{nameLink}}">${{escHtml(m.username)}}</a></b><time>${{escHtml(m.time)}}</time></div><div class="chat-body">${{img}}<span>${{escHtml(m.body)}}
</span></div></div>`;
}}

function renderMessages(msgs) {{
  for (const m of msgs) {{
    if (m.id <= lastMsgId) continue;
    box.insertAdjacentHTML("beforeend", bubble(m));
    if (m.id > lastMsgId) lastMsgId = m.id;
  }}
  box.scrollTop = box.scrollHeight;
}}

async function poll() {{
  try {{
    const r = await fetch("/chat/api/messages?after=" + lastMsgId, {{cache:"no-store"}});
    if (r.ok) {{
      const data = await r.json();
      renderMessages(data.messages || []);
    }}
  }} catch(e) {{}}
}}

function sendText() {{
  const body = textEl.value.trim();
  if (!body) return;
  const payload = {{body: body, csrf_token: document.body.dataset.csrf}};
  fetch("/chat/send", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(payload)}})
    .then(r => r.json())
    .then(d => {{
      if (d.ok) {{ textEl.value = ""; poll(); }}
      else alert(d.error || "Gönderilemedi.");
    }});
}}

photoEl.addEventListener("change", () => {{
  const f = photoEl.files[0];
  if (!f) return;
  if (f.size > 300 * 1024) {{ alert("Fotoğraf en fazla 300 KB olmalı."); photoEl.value = ""; return; }}
  const reader = new FileReader();
  reader.onload = () => {{
    fetch("/chat/send", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify({{image: reader.result, csrf_token: document.body.dataset.csrf}})}})
      .then(r => r.json())
      .then(d => {{ if (d.ok) {{ photoEl.value = ""; poll(); }} else alert(d.error || "Gönderilemedi."); }});
  }};
  reader.readAsDataURL(f);
}});

emojiBtn.addEventListener("click", () => {{
  emojiPanel.style.display = emojiPanel.style.display === "none" ? "flex" : "none";
}});

CHAT_EMOJIS.forEach(e => {{
  const b = document.createElement("button");
  b.type = "button"; b.className = "chat-emoji-item"; b.textContent = e;
  b.addEventListener("click", () => {{ textEl.value += e; textEl.focus(); }});
  emojiPanel.appendChild(b);
}});

sendBtn.addEventListener("click", sendText);
textEl.addEventListener("keydown", (ev) => {{ if (ev.key === "Enter") {{ ev.preventDefault(); sendText(); }} }});

poll();
setInterval(poll, 1500);
</script>"""
            self.send_html(page("Sohbet", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/profile/edit":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                row = connection.execute("SELECT avatar, bio, profile_link, status_text FROM users WHERE username=?", (username,)).fetchone()
            avatar = row["avatar"] if row else ""
            bio = row["bio"] if row else ""
            plink = row["profile_link"] if row else ""
            pstatus = row["status_text"] if row else ""
            avatar_html = f'<img class="profile-avatar" src="{esc(avatar)}" alt="profil fotoğrafı">' if avatar else '<div class="profile-avatar profile-avatar-empty">👤</div>'
            body = f"""<section class="page-head"><div><div class="eyebrow">HESABIM</div><h1>Profil Düzenle</h1><p class="lead">Kullanıcı adın değiştirilemez; foto, bio, link ve durum mesajını düzenleyebilirsin.</p></div></section>
<section class="profile-wrap">
  <div class="profile-card">
    {avatar_html}
    <h2>{esc(username)}{' <span class="premium-badge">PREMIUM</span>' if is_premium else ''}</h2>
    <p class="muted">Hesap: <code>@{esc(username)}</code></p>
  </div>
  <form method="post" action="/profile/update" class="form auth-card" id="profile-form" style="max-width: 640px;">
    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
    <label>Profil fotoğrafı (en fazla 300 KB)<input type="file" name="avatar" id="avatar-input" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none;"><span class="avatar-picker" id="avatar-picker">📷 Fotoğraf Seç</span></label>
    <input type="hidden" name="avatar_data" id="avatar-data">
    <label>Durum mesajı<input type="text" name="status_text" maxlength="80" value="{esc(pstatus)}" placeholder="Örn. CS2'deyim"></label>
    <label>Bio<textarea name="bio" maxlength="300" rows="4" placeholder="Kendinden kısaca bahset...">{esc(bio)}</textarea></label>
    <label>Link (Steam / Discord / İnstagram)<input type="url" name="profile_link" maxlength="200" value="{esc(plink)}" placeholder="https://steamcommunity.com/id/..."></label>
    <button class="button primary wide" type="submit">Kaydet</button>
  </form>
</section>
<script>
  const ap = document.getElementById("avatar-picker");
  const ai = document.getElementById("avatar-input");
  const ad = document.getElementById("avatar-data");
  ap.addEventListener("click", () => ai.click());
  ai.addEventListener("change", () => {{
    const f = ai.files[0];
    if (!f) return;
    if (f.size > 300 * 1024) {{ alert("Fotoğraf en fazla 300 KB olmalı."); ai.value = ""; return; }}
    const rd = new FileReader();
    rd.onload = () => {{ ad.value = rd.result; ap.textContent = "✅ Fotoğraf Seçildi"; }};
    rd.readAsDataURL(f);
  }});
  document.getElementById("profile-form").addEventListener("submit", (ev) => {{
    ev.preventDefault();
    const f = ev.target;
    fetch(f.action, {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{csrf_token: document.body.dataset.csrf, avatar_data: ad.value, status_text: f.status_text.value, bio: f.bio.value, profile_link: f.profile_link.value}})}})
      .then(r => r.json())
      .then(d => {{ if (d.ok) {{ alert("✅ Profil güncellendi."); location.reload(); }} else alert(d.error || "Güncellenemedi."); }});
  }});
</script>"""
            self.send_html(page("Profil Düzenle", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path.startswith("/profile/"):
            target_name = unquote(path[len("/profile/"):])
            if not target_name:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            with db() as connection:
                target = connection.execute("SELECT id, username, avatar, bio, profile_link, status_text, created_at, premium_until, is_premium FROM users WHERE LOWER(username)=LOWER(?)", (target_name,)).fetchone()
            if not target:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1><p class="muted">Bu kullanıcı bulunamadı.</p></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            t_avatar = target["avatar"]
            avatar_html = f'<img class="profile-avatar" src="{esc(t_avatar)}" alt="profil fotoğrafı">' if t_avatar else '<div class="profile-avatar profile-avatar-empty">👤</div>'
            t_name = target["username"]
            t_status = target["status_text"]
            t_bio = target["bio"]
            t_link = target["profile_link"]
            t_prem = bool(target["is_premium"]) or target["premium_until"] > int(time.time())
            member_since = time.strftime("%d.%m.%Y", time.localtime(target["created_at"]))
            if user and username.lower() == t_name.lower():
                action_area = f'<a class="button primary" href="/profile/edit">✏️ Profilini Düzenle</a>'
            else:
                action_area = ""
                if user:
                    with db() as connection:
                        rel = connection.execute("SELECT status FROM friend_requests WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)", (target["id"], user[1], user[1], target["id"])).fetchone()
                        are_friends = connection.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (user[1], target["id"])).fetchone()
                    if are_friends:
                        action_area = f'<a class="button primary" href="/dm/{quote(t_name)}">💬 Özelden Mesaj</a>'
                    elif rel and rel["status"] == "pending":
                        action_area = '<button class="button" disabled style="opacity:.6;">İstek Bekliyor</button>'
                    else:
                        action_area = f'<form method="post" action="/friends/request" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="target" value="{esc(t_name)}"><button class="button primary" type="submit">➕ Arkadaş Ekle</button></form>'
                else:
                    action_area = '<a class="button" href="/login">Giriş yap</a>'
            link_html = f'<a class="profile-link" href="{esc(t_link)}" target="_blank" rel="noopener">🔗 {esc(t_link)}</a>' if t_link else ""
            status_html = f'<span class="profile-status">{esc(t_status)}</span>' if t_status else ""
            body = f"""<section class="profile-wrap" style="max-width: 720px; margin: 0 auto;">
  <div class="profile-card">
    {avatar_html}
    <h2>{esc(t_name)}{' <span class="premium-badge">PREMIUM</span>' if t_prem else ''}</h2>
    {status_html}
    <p class="muted">Topluluk üyesi · Katılım: {member_since}</p>
    <p class="profile-bio">{esc(t_bio)}</p>
    {link_html}
    <div style="margin-top: 20px;">{action_area}</div>
  </div>
</section>"""
            self.send_html(page(f"@{t_name}", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/friends":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                requests_in = connection.execute(
                    "SELECT fr.id, u.username, u.avatar, u.status_text FROM friend_requests fr JOIN users u ON u.id=fr.sender_id WHERE fr.receiver_id=? AND fr.status='pending' ORDER BY fr.created_at DESC",
                    (user[1],),
                ).fetchall()
                requests_out = connection.execute(
                    "SELECT u.username, u.avatar, u.status_text FROM friend_requests fr JOIN users u ON u.id=fr.receiver_id WHERE fr.sender_id=? AND fr.status='pending' ORDER BY fr.created_at DESC",
                    (user[1],),
                ).fetchall()
                friend_rows = connection.execute(
                    "SELECT u.username, u.avatar, u.status_text FROM friends f JOIN users u ON u.id=f.friend_id WHERE f.user_id=? ORDER BY u.username ASC",
                    (user[1],),
                ).fetchall()
            def friend_item(u, is_out=False, req_id=0):
                av = u["avatar"]
                av_html = f'<img class="mini-avatar" src="{esc(av)}" alt="">' if av else '<span class="mini-avatar empty">👤</span>'
                st = f'<span class="profile-status">{esc(u["status_text"])}</span>' if u["status_text"] else ""
                nm = esc(u["username"])
                if is_out:
                    btn = '<span class="muted" style="font-size:13px;">⏳ Bekleniyor</span>'
                else:
                    btn = f'<form method="post" action="/friends/accept" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="req_id" value="{req_id}"><button class="button small primary" type="submit">Kabul Et</button></form><form method="post" action="/friends/decline" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="req_id" value="{req_id}"><button class="button small ghost" type="submit" style="border-color:#ff44663d;color:#ff4466;">Reddet</button></form>'
                return f'<div class="friend-row">{av_html}<a class="friend-name" href="/profile/{quote(nm)}">{nm}</a>{st}<div class="friend-actions">{btn}</div></div>'
            def friend_row(u):
                av = u["avatar"]
                av_html = f'<img class="mini-avatar" src="{esc(av)}" alt="">' if av else '<span class="mini-avatar empty">👤</span>'
                st = f'<span class="profile-status">{esc(u["status_text"])}</span>' if u["status_text"] else ""
                nm = esc(u["username"])
                return f'<div class="friend-row">{av_html}<a class="friend-name" href="/profile/{quote(nm)}">{nm}</a>{st}<div class="friend-actions"><a class="button small ghost" href="/dm/{quote(nm)}">💬 Mesaj</a></div></div>'
            in_html = "".join(friend_item(r, req_id=r["id"]) for r in requests_in) or '<p class="muted">Bekleyen arkadaşlık isteği yok.</p>'
            out_html = "".join(friend_item(r, is_out=True) for r in requests_out) or '<p class="muted">Gönderdiğin istek yok.</p>'
            friends_html = "".join(friend_row(r) for r in friend_rows) or '<p class="muted">Henüz arkadaşın yok. Profillerden ya da sohbetten arkadaş ekleyebilirsin.</p>'
            body = f"""<section class="page-head"><div><div class="eyebrow">TOPLULUK</div><h1>👥 Arkadaşlar</h1><p class="lead">Arkadaşlarınla özel sohbet et, profillerini görüntüle.</p></div></section>
<section class="friends-layout">
  <div class="auth-card">
    <h2>Yeni arkadaş ekle</h2>
    <form method="get" action="/friends" class="form">
      <label>Kullanıcı adı (örn. Sas)<input type="text" name="q" maxlength="24" placeholder="Kullanıcı adı ara..." value="{esc(query.get('q',[''])[0])}"></label>
      <button class="button primary wide" type="submit">Ara</button>
    </form>
    <div id="q-results" style="margin-top: 16px;"></div>
  </div>
  <div class="auth-card">
    <h2>👋 Gelen istekler</h2>
    {in_html}
  </div>
  <div class="auth-card">
    <h2>⏳ Giden istekler</h2>
    {out_html}
  </div>
  <div class="auth-card">
    <h2>🤝 Arkadaşların</h2>
    {friends_html}
  </div>
</section>"""
            q = query.get("q", [""])[0].strip()
            if q:
                with db() as connection:
                    results = connection.execute("SELECT username, avatar, status_text FROM users WHERE LOWER(username) LIKE LOWER(?) AND LOWER(username)!=LOWER(?) ORDER BY username LIMIT 12", (f"%{q}%", username)).fetchall()
                if results:
                    res_html = "".join(friend_row(r) for r in results)
                    body = body.replace('id="q-results">', f'id="q-results">{res_html}')
                else:
                    body = body.replace('id="q-results">', f'id="q-results"><p class="muted">Sonuç yok.</p>')
            self.send_html(page("Arkadaşlar", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/dm/api/messages":
            if not user:
                self.send_json({"ok": False, "error": "giris_gerekli"}, 401)
                return
            peer_name = query.get("peer", [""])[0]
            try:
                after = int(query.get("after", ["0"])[0] or 0)
            except ValueError:
                after = 0
            with db() as connection:
                peer = connection.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?)", (peer_name,)).fetchone()
            if not peer:
                self.send_json({"ok": False, "error": "Kullanıcı bulunamadı."}, 404)
                return
            with db() as connection:
                are_friends = connection.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (user[1], peer["id"])).fetchone()
            if not are_friends:
                self.send_json({"ok": False, "error": "Sadece arkadaşlarınla özel konuşabilirsin."}, 403)
                return
            with db() as connection:
                rows = connection.execute(
                    "SELECT id, sender_id, body, image, created_at FROM private_messages WHERE id>? AND ((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)) ORDER BY id ASC LIMIT 200",
                    (after, user[1], peer["id"], peer["id"], user[1]),
                ).fetchall()
            messages = []
            for row in rows:
                messages.append({
                    "id": row["id"],
                    "me": row["sender_id"] == user[1],
                    "body": row["body"],
                    "image": row["image"],
                    "time": time.strftime("%H:%M", time.localtime(row["created_at"])),
                })
            self.send_json({"ok": True, "messages": messages})
            return
        elif path.startswith("/dm/"):
            if not user:
                self.redirect("/login")
                return
            peer_name = unquote(path[len("/dm/"):])
            with db() as connection:
                peer = connection.execute("SELECT id, username, avatar, status_text FROM users WHERE LOWER(username)=LOWER(?)", (peer_name,)).fetchone()
            if not peer:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1><p class="muted">Bu kullanıcı bulunamadı.</p></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            with db() as connection:
                are_friends = connection.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (user[1], peer["id"])).fetchone()
            if not are_friends:
                self.redirect(f"/profile/{quote(peer['username'])}?msg=Once+arkadas+ekle.&msg_type=error")
                return
            peer_av = peer["avatar"]
            peer_av_html = f'<img class="mini-avatar" src="{esc(peer_av)}" alt="">' if peer_av else '<span class="mini-avatar empty">👤</span>'
            peer_st = f'<span class="profile-status">{esc(peer["status_text"])}</span>' if peer["status_text"] else ""
            body = f"""<section class="page-head"><div><div class="eyebrow">ÖZEL MESAJ</div><h1>{peer_av_html} {esc(peer['username'])} {peer_st}</h1><p class="lead">Sadece sen ve {esc(peer['username'])} görebiliyorsunuz.</p></div></section>
<section class="chat-wrap">
  <div class="chat-box" id="chat-box"></div>
  <div class="chat-input-row">
    <button type="button" class="chat-emoji-btn" id="chat-emoji-btn" title="Emoji">😊</button>
    <input type="text" id="chat-text" maxlength="400" placeholder="Mesajını yaz...">
    <label class="chat-photo-btn" title="Fotoğraf gönder">📷<input type="file" id="chat-photo" accept="image/png,image/jpeg,image/gif,image/webp" style="display:none;"></label>
    <button type="button" class="button primary" id="chat-send">Gönder</button>
  </div>
  <div class="chat-emoji-panel" id="chat-emoji-panel" style="display:none;"></div>
</section>
<script>
const PEER = {json.dumps(peer["username"], ensure_ascii=False)};
const CHAT_EMOJIS = ["😀","😁","😂","🤣","😊","😎","🤩","😍","🥰","😜","🤪","😴","🤔","😅","🙃","😉","👍","👎","👏","🙏","💪","🔥","⚡","🚀","🎯","🏆","💯","❤️","💛","💚","💙","🖤","💎","🎁","👑","🍀","🤝","✌️","🤙","👌","😈","🫡","🥶","🤯","🫠","😭","🥹","😤","🤮","🤡","💀","☠️","🫵","👇","➡️","✅","❌","⚠️","💬","🗣️"];
let lastMsgId = 0;
const box = document.getElementById("chat-box");
const textEl = document.getElementById("chat-text");
const sendBtn = document.getElementById("chat-send");
const emojiBtn = document.getElementById("chat-emoji-btn");
const emojiPanel = document.getElementById("chat-emoji-panel");
const photoEl = document.getElementById("chat-photo");
function escHtml(s) {{ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }}
function bubble(m) {{
  const mine = m.me ? " me" : "";
  const img = m.image ? `<img class="chat-img" src="${{m.image}}" alt="foto">` : "";
  return `<div class="chat-msg${{mine}}"><div class="chat-head"><b>${{escHtml(PEER)}}</b><time>${{escHtml(m.time)}}</time></div><div class="chat-body">${{img}}<span>${{escHtml(m.body)}}
</span></div></div>`;
}}
function renderMessages(msgs) {{
  for (const m of msgs) {{
    if (m.id <= lastMsgId) continue;
    box.insertAdjacentHTML("beforeend", bubble(m));
    if (m.id > lastMsgId) lastMsgId = m.id;
  }}
  box.scrollTop = box.scrollHeight;
}}
async function poll() {{
  try {{
    const r = await fetch("/dm/api/messages?peer=" + encodeURIComponent(PEER) + "&after=" + lastMsgId, {{cache:"no-store"}});
    if (r.ok) {{ const data = await r.json(); renderMessages(data.messages || []); }}
  }} catch(e) {{}}
}}
function sendText() {{
  const body = textEl.value.trim();
  if (!body) return;
  const payload = {{body: body, to: PEER, csrf_token: document.body.dataset.csrf}};
  fetch("/dm/send", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify(payload)}})
    .then(r => r.json())
    .then(d => {{ if (d.ok) {{ textEl.value = ""; poll(); }} else alert(d.error || "Gönderilemedi."); }});
}}
photoEl.addEventListener("change", () => {{
  const f = photoEl.files[0];
  if (!f) return;
  if (f.size > 300 * 1024) {{ alert("Fotoğraf en fazla 300 KB olmalı."); photoEl.value = ""; return; }}
  const reader = new FileReader();
  reader.onload = () => {{
    fetch("/dm/send", {{method:"POST", headers:{{"Content-Type":"application/json"}}, body: JSON.stringify({{image: reader.result, to: PEER, csrf_token: document.body.dataset.csrf}})}})
      .then(r => r.json())
      .then(d => {{ if (d.ok) {{ photoEl.value = ""; poll(); }} else alert(d.error || "Gönderilemedi."); }});
  }};
  reader.readAsDataURL(f);
}});
emojiBtn.addEventListener("click", () => {{ emojiPanel.style.display = emojiPanel.style.display === "none" ? "flex" : "none"; }});
CHAT_EMOJIS.forEach(e => {{ const b = document.createElement("button"); b.type = "button"; b.className = "chat-emoji-item"; b.textContent = e; b.addEventListener("click", () => {{ textEl.value += e; textEl.focus(); }}); emojiPanel.appendChild(b); }});
sendBtn.addEventListener("click", sendText);
textEl.addEventListener("keydown", (ev) => {{ if (ev.key === "Enter") {{ ev.preventDefault(); sendText(); }} }});
poll();
setInterval(poll, 1500);
</script>"""
            self.send_html(page(f"Özel: {peer['username']}", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/cases":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                bal_row = connection.execute("SELECT balance FROM users WHERE id=?", (user[1],)).fetchone()
            balance_val = float(bal_row["balance"]) if bal_row else 0.0
            sel_key = query.get("c", [""])[0]
            if sel_key not in CASES:
                sel_key = "kumbara"
            case = CASES[sel_key]
            total_w = sum(i["weight"] for i in case["items"])
            def item_html(it):
                tier = TIER_INFO[it["tier"]]
                label = it["name"]
                pct = it["weight"] / total_w * 100
                if it.get("type") == "balance":
                    live_txt = f'💰 {it["value"]:g} TL anında'
                elif it.get("type") == "premium":
                    live_txt = f'👑 {it["value"]:g} gün premium'
                else:
                    live = market_price(it.get("skin", it["name"]))
                    live_txt = f'{live:.0f} TL'
                return f'<div class="case-item" data-tier="{it["tier"]}" style="--tier:{tier["color"]}"><span class="case-item-icon">{it.get("icon", "🎁")}</span><span class="case-item-star">{tier["star"]}</span><span class="case-item-name">{esc(label)}</span><span class="case-item-pct">%{pct:.2f} · {live_txt}</span></div>'
            item_rows = "".join(item_html(i) for i in case["items"])
            case_cards = "".join(
                f'<a class="case-card{" active" if k == sel_key else ""}" href="/cases?c={k}" style="--case:{c["color"]}"><span class="case-icon">{c["icon"]}</span><h3>{esc(c["name"])}</h3><span class="case-tag">{c["tag"]}</span><span class="case-price">₺{c["price"]:.0f}</span></a>'
                for k, c in CASES.items()
            )
            balance_pill = f'<div class="case-balance">Bakiyen: <strong>{balance_val:.2f} TL</strong></div>'
            body = f"""<section class="page-head"><div><div class="eyebrow">KASA AÇMA</div><h1>🎁 Kasalar</h1><p class="lead">Bakiyenle kasa aç, CS2 tarzı <strong style="color:#ffd700;">skinler</strong> kazan. Kazandığını <a href="/market">🛒 Pazar</a>'da sat veya takas et!</p></div>{balance_pill}</section>
<section class="case-grid">{case_cards}</section>
<section class="case-viewer" data-case="{sel_key}">
  <div class="case-viewer-head"><span class="case-icon big">{case["icon"]}</span><div><h2>{esc(case["name"])} <small>₺{case["price"]:.0f} ile aç</small></h2><p class="muted">{esc(case["tag"])} kasa · {len(case["items"])} ödül</p></div>
  <button class="button primary big-open" id="case-open-btn">🔓 Kasayı Aç (₺{case["price"]:.0f})</button></div>
  <div class="case-items">{item_rows}</div>
  <div class="case-odds"><div class="eyebrow">ÇIKMA ORANLARI</div><div class="case-items">{item_rows}</div></div>
</section>
<div class="case-overlay" id="case-overlay">
  <div class="case-unbox-track">
    <div class="case-unbox" id="case-unbox"></div>
    <div class="case-marker" id="case-marker"></div>
  </div>
  <div class="case-reveal" id="case-reveal"></div>
  <button class="button primary" id="case-close" style="display:none;">Tamam</button>
</div>
<script>
const CASES_JS = {json.dumps({k: {"name": c["name"], "price": c["price"], "icon": c["icon"], "color": c["color"], "items": [{"name": i["name"], "tier": i["tier"], "type": i["type"], "icon": i.get("icon", "🎁"), "skin": i.get("skin", ""), "value": i["value"]} for i in c["items"]]} for k, c in CASES.items()}, ensure_ascii=False)};
const TIERS = {json.dumps(TIER_INFO, ensure_ascii=False)};
const openBtn = document.getElementById("case-open-btn");
const overlay = document.getElementById("case-overlay");
const unbox = document.getElementById("case-unbox");
const reveal = document.getElementById("case-reveal");
const closeBtn = document.getElementById("case-close");
let busy = false;

function tickSound(freq) {{
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.type = "square"; o.frequency.value = freq || 320;
    g.gain.setValueAtTime(0.04, ctx.currentTime);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.06);
    o.connect(g); g.connect(ctx.destination);
    o.start(); o.stop(ctx.currentTime + 0.06);
  }} catch(e) {{}}
}}

function winSound(rarity) {{
  try {{
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const seq = {{
      consumer: [[330, .08], [330, .08], [392, .14]],
      industrial: [[392, .08], [392, .08], [523, .16]],
      restricted: [[392, .08], [523, .08], [659, .12], [784, .3]],
      classified: [[392, .08], [523, .08], [659, .08], [784, .14], [1046, .45]],
      covert: [[392, .09], [523, .09], [659, .09], [784, .1], [1046, .14], [1318, .55]],
      extraordinary: [[392, .1], [523, .1], [659, .1], [784, .12], [1046, .14], [1318, .16], [1568, .7]]
    }}[rarity] || [[330, .1], [330, .1]];
    let t = ctx.currentTime;
    seq.forEach(n => {{
      const o = ctx.createOscillator();
      const g = ctx.createGain();
      o.type = "triangle"; o.frequency.value = n[0];
      g.gain.setValueAtTime(0.14, t);
      g.gain.exponentialRampToValueAtTime(0.0001, t + n[1]);
      o.connect(g); g.connect(ctx.destination);
      o.start(t); o.stop(t + n[1] + 0.05);
      t += n[1] + 0.03;
    }});
  }} catch(e) {{}}
}}

function revealHtml(d) {{
  const tier = TIERS[d.item.tier] || {{name: "Ödül", color: "#fff"}};
  const icon = d.item.icon || (d.item.tier === "extraordinary" ? "👑" : "🎉");
  let marketLine = '';
  if (d.item.type === "balance") marketLine = '<p class="muted">💰 ' + d.item.value.toFixed(0) + ' TL bakiyene anında eklendi!</p>';
  else if (d.item.type === "premium") marketLine = '<p class="muted">👑 ' + d.item.value.toFixed(0) + ' gün premium erişim eklendi!</p>';
  else if (d.item.value != null) marketLine = '<p class="muted">Pazar değeri: ' + d.item.value.toFixed(0) + ' TL baz</p>';
  return '<div class="case-reveal-inner" style="--tier:' + tier.color + '"><span class="case-reveal-icon">' + icon + '</span><span class="case-reveal-rarity">' + tier.star + '</span><h2>' + d.item.name + '</h2><p style="color:' + tier.color + ';font-weight:700;">' + tier.name + '</p>' + marketLine + (d.balance != null ? '<p class="muted">Yeni bakiyen: ' + d.balance.toFixed(2) + ' TL</p>' : '') + '</div>';
}}

function startRoll(ck, d) {{
  const CELLS = 56;
  const winIdx = 34 + Math.floor(Math.random() * 6);
  const cells = [];
  for (let i = 0; i < CELLS; i++) {{
    const item = i === winIdx ? d.item : CASES_JS[ck].items[Math.floor(Math.random() * CASES_JS[ck].items.length)];
    const el = document.createElement("div");
    el.className = "unbox-cell";
    el.style.setProperty("--tier", TIERS[item.tier].color);
    el.textContent = item.icon || (item.tier === "extraordinary" ? "💎" : "🎁");
    unbox.appendChild(el);
    cells.push(el);
  }}
  const markerX = unbox.clientWidth / 2;
  let step = 0;
  let lastUnder = -1;

  function stepDelay(i) {{
    const p = i / (CELLS - 1);
    if (p < 0.45) return 24 + Math.random() * 14;
    const q = (p - 0.45) / 0.55;
    return 40 + Math.pow(q, 2.6) * 620;
  }}

  function tickSoundFreq(i) {{
    const p = i / (CELLS - 1);
    return 880 - p * 520;
  }}

  function tick() {{
    const targetCell = step >= CELLS - 1 ? cells[winIdx] : cells[step];
    unbox.scrollLeft = Math.max(0, targetCell.offsetLeft + targetCell.offsetWidth / 2 - markerX);
    let best = -1, bestDist = Infinity;
    cells.forEach((c, i) => {{
      const cx = c.offsetLeft + c.offsetWidth / 2 - unbox.scrollLeft;
      const dist = Math.abs(cx - markerX);
      if (dist < bestDist) {{ bestDist = dist; best = i; }}
    }});
    cells.forEach(c => c.classList.remove("under-marker"));
    if (best >= 0) {{
      cells[best].classList.add("under-marker");
      if (best !== lastUnder) {{ tickSound(tickSoundFreq(step)); lastUnder = best; }}
    }}
    if (step < CELLS - 1) {{
      step++;
      setTimeout(tick, stepDelay(step));
    }} else {{
      finishRoll(d, cells[winIdx]);
    }}
  }}
  tick();
}}

function finishRoll(d, winCell) {{
  winCell.classList.add("win");
  winSound(d.item.tier);
  const delay = {{consumer: 350, industrial: 500, restricted: 800, classified: 1200, covert: 1800, extraordinary: 2500}}[d.item.tier] || 500;
  setTimeout(() => {{
    reveal.style.display = "flex";
    reveal.innerHTML = revealHtml(d);
    closeBtn.style.display = "inline-block";
    busy = false;
  }}, delay);
}}

openBtn.addEventListener("click", () => {{
  if (busy) return;
  busy = true;
  const ck = document.querySelector(".case-viewer").dataset.case;
  overlay.style.display = "flex";
  reveal.style.display = "none";
  closeBtn.style.display = "none";
  unbox.innerHTML = "";
  fetch("/cases/open", {{
    method: "POST",
    headers: {{"Content-Type": "application/json"}},
    body: JSON.stringify({{case_key: ck, csrf_token: document.body.dataset.csrf}})
  }})
  .then(r => r.json())
  .then(d => {{
    if (!d.ok) {{
      reveal.style.display = "flex";
      reveal.innerHTML = '<h2 style="color:#ff6b6b;">' + d.error + '</h2>';
      closeBtn.style.display = "inline-block";
      busy = false;
      return;
    }}
    startRoll(ck, d);
  }})
  .catch(() => {{ busy = false; reveal.style.display = "flex"; reveal.innerHTML = '<h2 style="color:#ff6b6b;">Bağlantı hatası</h2>'; closeBtn.style.display = "inline-block"; }});
}});

closeBtn.addEventListener("click", () => {{
  overlay.style.display = "none";
  location.reload();
}});
</script>"""
            self.send_html(page("Kasalar", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/market":
            if not user:
                self.redirect("/login")
                return
            bal_row = None
            with db() as connection:
                bal_row = connection.execute("SELECT balance FROM users WHERE id=?", (user[1],)).fetchone()
            mkt_balance = float(bal_row["balance"]) if bal_row else 0.0
            mkt_pill = f'<div class="case-balance">Bakiyen: <strong>{mkt_balance:.2f} TL</strong></div>'
            with db() as connection:
                my_inv = connection.execute(
                    "SELECT id, item_name, item_tier, item_value, skin_key FROM case_inventory WHERE user_id=? ORDER BY item_value DESC", (user[1],)
                ).fetchall()
                listed_ids = set(r["inv_id"] for r in connection.execute(
                    "SELECT inv_id FROM market_listings WHERE seller_id=? AND status='active'", (user[1],)
                ).fetchall())
                active_listings = connection.execute(
                    """SELECT l.id AS lid, l.price, l.created_at, l.inv_id, l.skin_key,
                              ci.item_name, ci.item_tier, u.username AS seller
                       FROM market_listings l
                       JOIN case_inventory ci ON ci.id = l.inv_id
                       JOIN users u ON u.id = l.seller_id
                       WHERE l.status='active' ORDER BY l.created_at DESC LIMIT 40"""
                ).fetchall()
            sell_options = "".join(
                f'<option value="{r["id"]}"{" disabled" if r["id"] in listed_ids else ""}>{esc(r["item_name"])} ({TIER_INFO[r["item_tier"]]["name"]} · {market_price(r["skin_key"] or r["item_name"]):.0f} TL)</option>'
                for r in my_inv
            )
            market_rows = ""
            for key, s in SKINS.items():
                price = market_price(key)
                chg = market_change_pct(key)
                tier = TIER_INFO[s["tier"]]
                hist = market_history(key, 40)
                lo, hi = min(hist), max(hist)
                span = (hi - lo) or 1
                pts = []
                w, h = 120, 32
                for i, v in enumerate(hist):
                    x = i / (len(hist) - 1) * w
                    y = h - (v - lo) / span * (h - 4) - 2
                    pts.append(f"{x:.1f},{y:.1f}")
                spark = f'<svg width="{w}" height="{h}" class="mkt-spark"><polyline points="{" ".join(pts)}" fill="none" stroke="{"#4ade80" if chg >= 0 else "#ff6b6b"}" stroke-width="2"/></svg>'
                arrow = "▲" if chg >= 0 else "▼"
                chg_cls = "up" if chg >= 0 else "down"
                market_rows += f'<div class="mkt-row" data-key="{key}" style="--tier:{tier["color"]}"><span class="mkt-icon">{s["icon"]}</span><span class="mkt-name"><b>{esc(s["name"])}</b><small>{tier["star"]} {tier["name"]}</small></span><span class="mkt-spark-cell">{spark}</span><span class="mkt-price">{price:.2f} TL</span><span class="chg-{chg_cls}">{arrow} %{abs(chg):.1f}</span><span class="mkt-base">Baz {s["value"]:.0f} TL</span></div>'
            listing_rows = ""
            for l in active_listings:
                seller_is_me = (l["seller"] == username)
                buy_btn = f'<form method="post" action="/market/buy" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="listing_id" value="{l["lid"]}"><button class="button primary small" type="submit">Satın Al</button></form>' if not seller_is_me else f'<form method="post" action="/market/cancel" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="listing_id" value="{l["lid"]}"><button class="ghost small" type="submit">Geri Çek</button></form>'
                listing_rows += f'<div class="inv-item" style="--tier:{TIER_INFO[l["item_tier"]]["color"]}"><span class="case-item-star">{TIER_INFO[l["item_tier"]]["star"]}</span><span class="inv-name">{esc(l["item_name"])}</span><span class="inv-meta">{esc(l["seller"])} · {l["price"]:.2f} TL</span>{buy_btn}</div>'
            sell_form = f'''<section class="auth-card" style="max-width:720px;margin:0 auto;margin-top:26px;">
<div class="eyebrow">SKİNİNİ SAT</div><h2>İstediğin fiyata listele</h2>
<form method="post" action="/market/list" class="form">
<input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
<label>Skin Seç<select name="inv_id" required>{sell_options or '<option disabled>Önce kasa aç, envanterin boş</option>'}</select></label>
<label>Satış Fiyatı (TL)<input type="number" name="price" min="1" step="0.01" required placeholder="Örn: 150"></label>
<button class="button primary wide" type="submit">🛒 Pazara Koy</button>
</form></section>'''
            body = f"""<section class="page-head"><div><div class="eyebrow">CANLI PAZAR</div><h1>🛒 Skin Pazarı</h1><p class="lead">Tüm skinlerin <strong>canlı fiyatı</strong> sürekli dalgalanır. Skinini istediğin fiyata sat veya satılıklardan al!</p></div>{mkt_pill}</section>
<section class="mkt-table"><div class="mkt-head"><span>SKIN</span><span>GRAFİK (son 1 saat)</span><span>CANLI FİYAT</span><span>DEĞİŞİM</span><span>BAZ</span></div>{market_rows}</section>
<section class="auth-card" style="max-width:720px;margin:0 auto;margin-top:26px;"><div class="eyebrow">SATILIK SKİNLER</div><h2>Alım yap</h2><div class="inv-list">{listing_rows or '<p class="muted">Henüz satılık skin yok. İlk sen listele!</p>'}</div></section>
{sell_form}
<p class="muted" style="text-align:center;margin-top:20px;"><a href="/cases">🎁 Kasa aç →</a> · <a href="/inventory">🎒 Envanter →</a> · <a href="/trade">🔁 Takas →</a></p>
<script>
setInterval(() => {{ fetch("/market/api/prices").then(r => r.json()).then(d => {{
  const rows = document.querySelectorAll(".mkt-row");
  rows.forEach(r => {{
    const key = r.dataset.key; if (!key || !d[key]) return;
    const priceEl = r.querySelector(".mkt-price");
    const chgEl = r.querySelector(".chg-up, .chg-down");
    priceEl.textContent = d[key].price.toFixed(2) + " TL";
    const up = d[key].chg >= 0;
    chgEl.textContent = (up ? "▲ %" : "▼ %") + Math.abs(d[key].chg).toFixed(1);
    chgEl.className = up ? "chg-up" : "chg-down";
  }});
}}).catch(() => {{}}); }}, 5000);
</script>"""
            self.send_html(page("Pazar", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/market/api/prices":
            data = {k: {"price": market_price(k), "chg": market_change_pct(k), "history": market_history(k, 40)} for k in SKINS}
            self.send_json(data)
        elif path == "/trade":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                my_inv = connection.execute(
                    "SELECT id, item_name, item_tier, item_value, skin_key FROM case_inventory WHERE user_id=? ORDER BY item_value DESC", (user[1],)
                ).fetchall()
                listed_ids = set(r["inv_id"] for r in connection.execute(
                    "SELECT inv_id FROM market_listings WHERE seller_id=? AND status='active'", (user[1],)
                ).fetchall())
                pending_out = connection.execute(
                    """SELECT tr.id, tr.sender_inv_id, tr.receiver_inv_id, tr.status, tr.created_at,
                              ci1.item_name AS from_item, ci2.item_name AS to_item, u.username AS to_user
                       FROM trade_requests tr
                       JOIN case_inventory ci1 ON ci1.id = tr.sender_inv_id
                       JOIN case_inventory ci2 ON ci2.id = tr.receiver_inv_id
                       JOIN users u ON u.id = tr.receiver_id
                       WHERE tr.sender_id=? AND tr.status='pending' ORDER BY tr.created_at DESC""", (user[1],)
                ).fetchall()
                pending_in = connection.execute(
                    """SELECT tr.id, tr.sender_inv_id, tr.receiver_inv_id, tr.status, tr.created_at,
                              ci1.item_name AS from_item, ci1.item_tier AS from_tier, ci2.item_name AS to_item, u.username AS from_user
                       FROM trade_requests tr
                       JOIN case_inventory ci1 ON ci1.id = tr.sender_inv_id
                       JOIN case_inventory ci2 ON ci2.id = tr.receiver_inv_id
                       JOIN users u ON u.id = tr.sender_id
                       WHERE tr.receiver_id=? AND tr.status='pending' ORDER BY tr.created_at DESC""", (user[1],)
                ).fetchall()
                offer_options = "".join(
                    f'<option value="{r["id"]}"{" disabled" if r["id"] in listed_ids else ""}>{esc(r["item_name"])} ({market_price(r["skin_key"] or r["item_name"]):.0f} TL)</option>'
                    for r in my_inv
                )
                want_options = "".join(
                    f'<option value="{r["id"]}">{esc(r["item_name"])} · {esc(r["username"])}</option>'
                    for r in connection.execute(
                        """SELECT ci.id, ci.item_name, ci.skin_key, u.username FROM case_inventory ci
                           JOIN users u ON u.id = ci.user_id WHERE ci.user_id <> ? ORDER BY ci.item_value DESC LIMIT 30""", (user[1],)
                    ).fetchall()
                )
                out_rows = "".join(
                    f'<div class="inv-item"><span class="inv-name">{esc(r["from_item"])}</span><span class="inv-meta">→ {esc(r["to_user"])} · {esc(r["to_item"])} · <a href="/trade/cancel?cancel={r["id"]}&csrf_token={quote(csrf_tok)}" class="link">İptal</a></span></div>'
                    for r in pending_out
                )
                in_rows = ""
                for r in pending_in:
                    tier = TIER_INFO[r["from_tier"]]
                    in_rows += f'<div class="inv-item" style="--tier:{tier["color"]}"><span class="inv-name">{esc(r["from_user"])} → {esc(r["from_item"])}</span><span class="inv-meta">senin: {esc(r["to_item"])}</span><form method="post" action="/trade/respond" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="req_id" value="{r["id"]}"><input type="hidden" name="action" value="accept"><button class="button primary small" type="submit">Kabul</button></form><form method="post" action="/trade/respond" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="req_id" value="{r["id"]}"><input type="hidden" name="action" value="decline"><button class="ghost small" type="submit">Reddet</button></form></div>'
                trade_form = f'''<section class="auth-card" style="max-width:720px;margin:0 auto;margin-top:26px;">
<div class="eyebrow">TAKAS İSTE</div><h2>Skinini teklif et</h2>
<form method="post" action="/trade/send" class="form">
<input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
<label>Vereceğin Skin<select name="sender_inv_id" required>{offer_options or '<option disabled>Önce kasa aç, envanterin boş</option>'}</select></label>
<label>İstediğin Skin<select name="receiver_inv_id" required>{want_options or '<option disabled>Pazarda/başka kullanıcıda skin yok</option>'}</select></label>
<button class="button primary wide" type="submit">🔁 Takas İsteği Gönder</button>
</form></section>'''
                body = f"""<section class="page-head"><div><div class="eyebrow">TAKAS</div><h1>🔁 Takas Sistemi</h1><p class="lead">Skinini başka bir kullanıcının skinine teklif et. Karşı taraf <strong>onaylamadan</strong> takas gerçekleşmez.</p></div></section>
<section class="auth-card" style="max-width:720px;margin:0 auto;"><div class="eyebrow">GELEN TAKASLAR</div><h2>Onay bekleyen istekler</h2><div class="inv-list">{in_rows or '<p class="muted">Gelen takas isteği yok.</p>'}</div></section>
<section class="auth-card" style="max-width:720px;margin:0 auto;margin-top:20px;"><div class="eyebrow">GİDEN TAKASLAR</div><h2>Senin isteklerin</h2><div class="inv-list">{out_rows or '<p class="muted">Giden takas isteği yok.</p>'}</div></section>
{trade_form}
<p class="muted" style="text-align:center;margin-top:20px;"><a href="/inventory">🎒 Envanter →</a> · <a href="/market">🛒 Pazar →</a></p>"""
            self.send_html(page("Takas", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/trade/cancel":
            if not user:
                self.redirect("/login")
                return
            flat = {k: v[0] if isinstance(v, list) else v for k, v in query.items()}
            if not self.verify_csrf(flat):
                self.redirect("/trade?msg=CSRF+hatasi&msg_type=error")
                return
            req_id = flat.get("cancel", "")
            with db() as connection:
                req = connection.execute("SELECT id, sender_id, status FROM trade_requests WHERE id=?", (req_id,)).fetchone()
                if req and req["sender_id"] == user[1] and req["status"] == "pending":
                    connection.execute("UPDATE trade_requests SET status='declined' WHERE id=?", (req["id"],))
            self.redirect("/trade?msg=Istek+iptal+edildi&msg_type=success")
            return
        elif path == "/inventory":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                rows = connection.execute("SELECT id, case_key, item_name, item_tier, item_value, skin_key, created_at FROM case_inventory WHERE user_id=? ORDER BY created_at DESC LIMIT 60", (user[1],)).fetchall()
                listed = set(r["inv_id"] for r in connection.execute("SELECT inv_id FROM market_listings WHERE seller_id=? AND status='active'", (user[1],)).fetchall())
            if not rows:
                body = f"""<section class="page-head"><div><div class="eyebrow">ENVANTER</div><h1>🎒 Envanterin</h1><p class="lead">Kasalardan kazandığın skinler burada birikir. <strong>Hızlı Sat</strong> ile canlı fiyattan anında sat ya da istediğin fiyata <strong>Pazara</strong> koy.</p></div></section>
<section class="auth-card" style="max-width:560px;margin:0 auto;"><div class="empty-state"><h2>Envanter boş</h2><p class="muted">Henüz hiç kasa açmadın. <a href="/cases">Kasalar</a> sekmesinden şansını dene!</p></div></section>"""
                self.send_html(page("Envanter", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
                return
            def inv_card(r):
                tier = TIER_INFO[r["item_tier"]]
                price = market_price(r["skin_key"] or r["item_name"])
                chg = market_change_pct(r["skin_key"] or r["item_name"])
                arrow = "▲" if chg >= 0 else "▼"
                chg_cls = "up" if chg >= 0 else "down"
                if r["id"] in listed:
                    listed_tag = f'<span class="inv-meta">📦 Pazardaysın · <a href="/market" class="link">Pazara git</a></span>'
                    actions = ""
                else:
                    listed_tag = ""
                    if price > 0:
                        actions = f'<form method="post" action="/inventory/sell" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="inv_id" value="{r["id"]}"><button class="button primary small" type="submit">💸 Hızlı Sat ({price:.0f} TL)</button></form><form method="post" action="/market/list" class="inline"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><input type="hidden" name="inv_id" value="{r["id"]}"><input type="number" name="price" min="1" step="0.01" placeholder="Fiyat" style="width:80px;padding:6px;border-radius:8px;border:1px solid #ffffff22;background:#0a0e17;color:#fff;" required><button class="ghost small" type="submit">Pazara Koy</button></form>'
                    else:
                        actions = '<span class="inv-meta">(satılamaz ödül)</span>'
                return f'<div class="inv-item" style="--tier:{tier["color"]}"><span class="case-item-star">{tier["star"]}</span><span class="inv-name">{esc(r["item_name"])}</span><span class="inv-meta">{time.strftime("%d.%m.%Y", time.localtime(r["created_at"]))} · {price:.2f} TL <span class="chg-{chg_cls}">{arrow}%{abs(chg):.1f}</span></span>{listed_tag}<div class="inv-actions">{actions}</div></div>'
            inv_cards = "".join(inv_card(r) for r in rows)
            body = f"""<section class="page-head"><div><div class="eyebrow">ENVANTER</div><h1>🎒 Envanterin</h1><p class="lead">Kasalardan kazandığın skinler burada birikir. <strong>Hızlı Sat</strong> ile canlı fiyattan anında sat ya da istediğin fiyata <strong>Pazara</strong> koy.</p></div></section>
<section class="case-grid" style="max-width:720px;margin:0 auto;"><div class="inv-list">{inv_cards}</div></section>
<p class="muted" style="text-align:center;margin-top:20px;"><a href="/cases">🎁 Yeni kasa aç →</a> · <a href="/market">🛒 Pazar →</a> · <a href="/trade">🔁 Takas yap →</a></p>"""
            self.send_html(page("Envanter", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok, body_csrf=csrf_tok))
        elif path == "/login":
            q_text, c_val = generate_captcha()
            fields = f'<label>Kullanıcı adı<input name="username" autocomplete="username" required maxlength="24"></label><label>Şifre<input name="password" type="password" autocomplete="current-password" required></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
            self.send_html(form_page("Giriş yap", "/login", "Giriş yap", fields, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), cookies=[("captcha", c_val)])
        elif path == "/register":
            q_text, c_val = generate_captcha()
            fields = f'<label>Kullanıcı adı<input name="username" autocomplete="username" required minlength="3" maxlength="24" pattern="[A-Za-z0-9_]+"></label><label>Şifre<input name="password" type="password" autocomplete="new-password" required minlength="8"></label><label>Şifre tekrar<input name="password2" type="password" autocomplete="new-password" required minlength="8"></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
            self.send_html(form_page("Kayıt ol", "/register", "Hesap oluştur", fields, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), cookies=[("captcha", c_val)])
        elif path == "/paid-cheats":
            if not user:
                self.redirect("/login")
                return
            files = []
            if PAID_CHEATS_DIR.is_dir():
                for f in sorted(PAID_CHEATS_DIR.iterdir(), key=lambda p: p.name.lower()):
                    if f.is_file() and f.name.lower() != "readme.md":
                        files.append(f)
            if not is_premium:
                with db() as connection:
                    bal_row = connection.execute("SELECT balance FROM users WHERE id=?", (user[1],)).fetchone()
                balance_val = float(bal_row["balance"]) if bal_row else 0.0
                plan_options = ""
                for key, plan in PAID_CHEATS_PLANS.items():
                    plan_options += f'<label><input type="radio" name="plan" value="{key}" required> {plan["days"]} gün — {plan["price"]:.0f} TL</label><br>'
                body = f"""<section class="page-head"><div><div class="eyebrow">PREMIUM VAULT</div><h1>Ücretli Hileler</h1><p class="lead">Özel premium sürümlere erişmek için bakiyenle süreli erişim satın al. Günlük ödül ve çarklardan kazandığın bakiyeyi kullan.</p></div></section>
<section class="auth-card upload-card" style="margin-top: 20px; width: min(600px, calc(100% - 40px));">
    <div class="eyebrow">ERİŞİM SATIN AL</div>
    <h1>Bakiyen: {balance_val:.2f} TL</h1>
    <form method="post" action="/paid-cheats/purchase" class="form">
        <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
        <label>Süre Seç
            <div style="display: flex; flex-direction: column; gap: 8px; margin-top: 8px;">{plan_options}</div>
        </label>
        <button class="button primary wide" type="submit">Bakiyemle Al</button>
    </form>
    <p class="muted" style="font-size: 13px; margin-top: 12px;">Bakiyen yetersizse <a href="/payment">Bakiye Yükle</a> sayfasından Steam/Google Play koduyla yükleyebilirsin.</p>
</section>
<section class="auth-card" style="margin-top: 30px; max-width: none;"><div class="eyebrow">İÇERİK</div><h2>Bu alanda neler var?</h2><div class="project-list">{file_cards if (file_cards := "".join(f'<article class="project-card"><div><span class="panel-icon">PREMIUM</span><h2>{esc(f.name)}</h2><p class="muted">{human_size(f.stat().st_size)}</p></div></article>' for f in files)) else '<div class="empty-state"><h2>İçerik hazır değil</h2><p class="muted">Yönetici henüz premium içerik yüklemedi.</p></div>'}</div></section>"""
                self.send_html(page("Ücretli Hileler", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
                return
            expiry = self.premium_expiry(user[1])
            if expiry > 0:
                days_left = max(0, (expiry - int(time.time())) // 86400)
                expiry_box = f'<div class="notice" style="margin-bottom: 20px;">Erişim süreniz aktif — kalan süre: <strong>{days_left} gün</strong></div>'
            else:
                expiry_box = ""
            file_cards = ""
            for f in files:
                file_cards += f'<article class="project-card"><div><span class="panel-icon">PREMIUM</span><h2>{esc(f.name)}</h2><p class="muted">{human_size(f.stat().st_size)}</p></div></article>'
            body = f"""<section class="page-head"><div><div class="eyebrow">PREMIUM VAULT</div><h1>Ücretli Hileler</h1><p class="lead">Premium üyelerin indirebileceği özel sürümler.</p></div></section>
{expiry_box}
<section class="project-list">{file_cards or '<div class="empty-state"><h2>İçerik hazır değil</h2><p class="muted">Yönetici henüz premium içerik yüklemedi.</p></div>'}</section>
<section class="auth-card" style="margin-top: 30px; max-width: none;">
    <div class="eyebrow">ÇALIŞTIRMA</div>
    <h2>Premium Hile Nasıl Çalıştırılır?</h2>
    <p class="muted" style="margin: 10px 0 18px;">Premium hile web üzerinden dosya indirme ile değil, <strong>Biga Cheat Loader</strong> üzerinden çalıştırılır. Hesabınla giriş yapman yeterli — dosya launcher tarafından geçici olarak yönetilir, ayrıca masaüstüne inmez.</p>
    <div class="steps-grid" style="grid-template-columns: repeat(2, 1fr); margin-top: 6px;">
        <div class="step"><div class="step-num">1</div><h3>Loader'ı İndir</h3><p><a href="/download" style="color: #ffd76a;">Buradan</a> güncel loader'ı indir ve çalıştır.</p></div>
        <div class="step"><div class="step-num">2</div><h3>Giriş Yap</h3><p>Loader'da site kullanıcı adın ve şifrenle giriş yap.</p></div>
        <div class="step"><div class="step-num">3</div><h3>Ücretli Hile Seç</h3><p>Premium üyeliğin aktifse Ücretli Hile sekmesi açılır.</p></div>
        <div class="step"><div class="step-num">4</div><h3>BAŞLAT'a Bas</h3><p>Premium paket launcher üzerinden indirilir ve çalışır.</p></div>
    </div>
</section>
<section class="reviews" style="margin-top: 40px;"><h2>Premium Kullanıcı Yorumları</h2><div class="review-grid"><div class="review"><blockquote>“Premium paketi loader üzerinden tek tuşla açıyorum, hiç uğraştırmıyor. Filigran meseleleri de kafamızı meşgul etmiyor artık.”</blockquote><cite>Kral Kartal<small>Premium Üye</small></cite></div><div class="review"><blockquote>“Bakiyemi günlük ödül ve çarkla topladım, premium erişimi onunla aldım. Sistem gerçekten iyi kurulmuş.”</blockquote><cite>Gece Şahini<small>Premium Üye</small></cite></div><div class="review"><blockquote>“Loader'dan seçiyorsun, BAŞLAT'a basıyorsun, iş bitti. Site de arayüz de tertemiz, aferin ekibe.”</blockquote><cite>Dijital Kurt<small>Premium Üye</small></cite></div></div></section>"""
            self.send_html(page("Ücretli Hileler", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/paid-cheats/download":
            if not user:
                self.redirect("/login")
                return
            if not is_premium:
                self.redirect("/payment")
                return
            files = []
            if PAID_CHEATS_DIR.is_dir():
                for f in sorted(PAID_CHEATS_DIR.iterdir(), key=lambda p: p.name.lower()):
                    if f.is_file() and f.name.lower() != "readme.md":
                        files.append(f)
            if not files:
                self.send_html(page("Dosya yok", '<section class="auth-card"><h1>İçerik hazır değil</h1><p class="muted">Yönetici henüz premium içerik yüklemedi.</p></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            serial = generate_license_serial()
            zip_data = make_watermarked_zip(username or "?", serial, files)
            with db() as connection:
                connection.execute("INSERT INTO downloads(user_id, filename, serial, ip, created_at) VALUES(?,?,?,?,?)", (user[1], "paid-cheats.zip", serial, self.client_ip(), int(time.time())))
            log_event(f"[İNDİRME] '{username}' ücretli içeriği indirdi (lisans {serial}).")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_data)))
            self.send_header("Content-Disposition", 'attachment; filename="BigaCheat-Premium.zip"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            self.wfile.write(zip_data)
        elif path.startswith("/admin/downloads/paid/"):
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            fname = unquote(path.rsplit("/", 1)[1])
            safe = Path(fname).name
            file_path = PAID_CHEATS_DIR / safe
            if not file_path.is_file() or PAID_CHEATS_DIR.resolve() not in file_path.resolve().parents:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>Dosya bulunamadı</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(file_path.stat().st_size))
            self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            with file_path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
        elif path == "/admin/downloads/free":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not DOWNLOAD_PATH.is_file():
                self.send_html(page("Dosya yok", '<section class="auth-card"><h1>Dosya hazır değil</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(DOWNLOAD_PATH.stat().st_size))
            self.send_header("Content-Disposition", 'attachment; filename="Biga Cheat-Cs2-Modified.exe"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            with DOWNLOAD_PATH.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
        elif path == "/admin/downloads/loader":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not LOADER_PATH.is_file():
                self.send_html(page("Dosya yok", '<section class="auth-card"><h1>Loader hazır değil</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(LOADER_PATH.stat().st_size))
            self.send_header("Content-Disposition", 'attachment; filename="BigaCheat-Loader.exe"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            with LOADER_PATH.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
        elif path == "/download":
            if not user:
                self.redirect("/login")
                return
            if not LOADER_PATH.is_file():
                self.send_html(page("Dosya yok", '<section class="auth-card"><h1>Loader hazır değil</h1><p class="muted">Yönetici henüz loader yüklemedi.</p></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            size = LOADER_PATH.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", 'attachment; filename="BigaCheat-Loader.exe"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            with LOADER_PATH.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
        elif path == "/projects":
            with db() as connection:
                projects = connection.execute("SELECT projects.id, projects.name, projects.filename, projects.size, projects.created_at, users.username FROM projects JOIN users ON users.id=projects.user_id ORDER BY projects.created_at DESC").fetchall()
            cards = "".join(f'<article class="project-card"><div><span class="panel-icon">PROJECT</span><h2>{esc(row["name"])}</h2><p class="muted">{esc(row["filename"])} · {human_size(row["size"])} · {esc(row["username"])}</p></div><a class="button ghost" href="/projects/download/{row["id"]}">İndir</a></article>' for row in projects)
            upload = '<a class="button primary" href="/projects/new">Proje yükle</a>' if user else '<a class="button primary" href="/login">Giriş yap ve yükle</a>'
            body = f"""<section class="page-head"><div><div class="eyebrow">COMMUNITY PROJECTS</div><h1>Projeler</h1><p class="lead">CS2 topluluğunun arşivlerini tek yerde keşfet.</p></div>{upload}</section><section class="project-list">{cards or '<div class="empty-state"><h2>Henüz proje yok</h2><p class="muted">İlk projeyi sen yükle.</p></div>'}</section>"""
            self.send_html(page("Projeler", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/updates":
            with db() as connection:
                updates = connection.execute("SELECT title, body, tag, created_at FROM updates ORDER BY created_at DESC").fetchall()
            cards = "".join(f'<article class="update-card"><div class="update-card-top"><span class="update-tag">{esc(row["tag"])}</span><time>{format_date(row["created_at"])}</time></div><h2>{esc(row["title"])}</h2><p>{esc(row["body"])}</p></article>' for row in updates)
            body = f'''<section class="page-head"><div><div class="eyebrow">RELEASE NOTES</div><h1>Güncellemeler</h1><p class="lead">Biga Cheat sürümleri, duyuruları ve topluluk haberleri.</p></div></section><section class="updates-list">{cards or '<div class="empty-state"><h2>Henüz duyuru yok</h2><p class="muted">Yeni bir gelişme olduğunda burada yayınlanır.</p></div>'}'''
            self.send_html(page("Güncellemeler", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/projects/new":
            if not user:
                self.redirect("/login")
                return
            body = f"""<section class="auth-card upload-card"><div class="eyebrow">COMMUNITY PROJECTS</div><h1>Proje yükle</h1><p class="muted">Arşiv dosyası yükle. En fazla 25 MB; ZIP, 7Z, RAR veya TAR.GZ.</p><form method="post" action="/projects/upload" enctype="multipart/form-data" class="form"><input type="hidden" name="csrf_token" value="{esc(csrf_tok)}"><label>Proje adı<input name="project_name" maxlength="80" required></label><label>Arşiv dosyası<input name="project_file" type="file" accept=".zip,.7z,.rar,.tar.gz,.tgz" required></label><button class="button primary wide" type="submit">Projeyi yayınla</button></form></section>"""
            self.send_html(page("Proje yükle", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path.startswith("/projects/download/"):
            if not user:
                self.redirect("/login")
                return
            try:
                project_id = int(path.rsplit("/", 1)[1])
            except ValueError:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            with db() as connection:
                project = connection.execute("SELECT filename, stored_path FROM projects WHERE id=?", (project_id,)).fetchone()
            project_path = DATA_DIR / project["stored_path"] if project else None
            if not project or not project_path.is_file() or PROJECTS_PATH not in project_path.resolve().parents:
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>Proje bulunamadı</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            size = project_path.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Content-Disposition", f'attachment; filename="{Path(project["filename"]).name}"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            with project_path.open("rb") as file:
                while chunk := file.read(1024 * 1024):
                    self.wfile.write(chunk)
        elif path == "/payment":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                rows = connection.execute("SELECT platform, code, amount, status, created_at FROM payments WHERE user_id=? ORDER BY created_at DESC", (user[1],)).fetchall()
            
            payment_rows = ""
            for r in rows:
                code_raw = r["code"]
                if len(code_raw) > 4:
                    masked_code = "XXXX-XXXX-XXXX-" + code_raw[-4:]
                else:
                    masked_code = code_raw
                
                platform_esc = esc(PLATFORM_NAMES.get(r["platform"], r["platform"]))
                status_esc = esc(r["status"])
                if r["status"] == "ONAYLANDI":
                    status_class = "onaylandi"
                elif r["status"] == "REDDEDİLDİ":
                    status_class = "reddedildi"
                else:
                    status_class = "beklemede"
                
                status_tag = f'<span class="status-tag {status_class}">{status_esc}</span>'
                payment_rows += f'<tr><td>{platform_esc}</td><td>{esc(masked_code)}</td><td>{esc(r["amount"])}</td><td>{format_date(r["created_at"])}</td><td>{status_tag}</td></tr>'

            table_content = f"""
            <div class="table-card" style="margin-top: 30px; max-width: none; padding: 20px 0 0 0; background: transparent; border: none;">
                <h2 style="font-size: 17px; margin-bottom: 12px;">Geçmiş Yüklemeleriniz</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Platform</th>
                            <th>Kod (Maskeli)</th>
                            <th>Tutar</th>
                            <th>Tarih</th>
                            <th>Durum</th>
                        </tr>
                    </thead>
                    <tbody>
                        {payment_rows}
                    </tbody>
                </table>
            </div>
            """ if rows else '<div class="empty-state" style="margin-top: 30px;"><h2>Henüz bakiye yükleme bildiriminiz yok</h2><p class="muted">Aşağıdaki formu kullanarak kod bildirebilirsiniz.</p></div>'

            body = f"""
            <section class="auth-card upload-card" style="margin-top: 65px; width: min(650px, calc(100% - 40px));">
                <div class="eyebrow">BAKİYE YÜKLEME ALANI</div>
                <h1>Bakiye Yükle</h1>
                <p class="lead" style="font-size: 15px; margin: 10px 0 20px;">Biga Cheat bakiyenizi yüklemek için bir Steam veya Google Play hediye kodu gönderin. Kodunuz format olarak doğrulanır ve yönetici onayladığında bakiye hesabınıza otomatik yüklenir.</p>
                
                <form method="post" action="/payment/submit" class="form">
                    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                    <label>Platform
                        <select name="platform" required style="width: 100%; border: 1px solid #ffffff18; border-radius: 9px; background: #070d15; color: white; padding: 13px 14px; font: inherit; outline: none;">
                            <option value="STEAM">Steam Hediye Kodu</option>
                            <option value="GPLAY">Google Play Hediye Kodu</option>
                        </select>
                    </label>
                    <label>Hediye Kartı Tutarı
                        <select name="amount" required style="width: 100%; border: 1px solid #ffffff18; border-radius: 9px; background: #070d15; color: white; padding: 13px 14px; font: inherit; outline: none;">
                            <option value="100 TL">100 TL</option>
                            <option value="250 TL">250 TL</option>
                            <option value="500 TL">500 TL</option>
                        </select>
                    </label>
                    <label>Kodunuz
                        <input name="code" placeholder="Steam: XXXXX-XXXXX-XXXXX | Google: XXXX-XXXX-XXXX-XXXX" required maxlength="50" autocomplete="off">
                    </label>
                    <button class="button primary wide" type="submit">Bakiye Bildir</button>
                </form>
                {table_content}
            </section>
            """
            self.send_html(page("Bakiye Yükle", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/daily":
            if not user:
                self.redirect("/login")
                return
            with db() as connection:
                row = connection.execute("SELECT daily_streak, last_daily_claim FROM users WHERE id=?", (user[1],)).fetchone()
            daily_streak = row["daily_streak"] if row else 0
            last_daily_claim = row["last_daily_claim"] if row else 0
            
            def is_today(t: int) -> bool:
                return datetime.date.fromtimestamp(t) == datetime.date.today()
            def is_yesterday(t: int) -> bool:
                return (datetime.date.today() - datetime.date.fromtimestamp(t)).days == 1
                
            claimed_today = is_today(last_daily_claim)
            
            if claimed_today:
                active_day = ((daily_streak - 1) % 7) + 1
            else:
                if is_yesterday(last_daily_claim) or last_daily_claim == 0:
                    active_day = (daily_streak % 7) + 1
                else:
                    active_day = 1
            
            rewards = {1: 10.0, 2: 15.0, 3: 20.0, 4: 25.0, 5: 30.0, 6: 35.0, 7: 50.0}
            
            cards = ""
            for day in range(1, 8):
                amt = rewards[day]
                if day < active_day or (day == active_day and claimed_today):
                    status_class = "claimed"
                    status_lbl = "Alındı ✓"
                elif day == active_day and not claimed_today:
                    status_class = "active"
                    status_lbl = "Kazan!"
                else:
                    status_class = ""
                    status_lbl = f"Gün {day}"
                    
                cards += f"""
                <div class="streak-card {status_class}">
                    <h3>{status_lbl}</h3>
                    <div class="amount">{amt:.0f} TL</div>
                </div>
                """
            
            claim_btn = ""
            if claimed_today:
                claim_btn = '<button class="button ghost wide" disabled style="background: #ffffff04; border-color: #ffffff0a; width: 100%;">Bugünün Ödülü Alındı</button><p class="muted" style="font-size:13px; text-align:center; margin-top:10px;">Yarın yeni ödül için tekrar gel kanka!</p>'
            else:
                next_amt = rewards[active_day]
                claim_btn = f"""
                <form method="post" action="/daily/claim" class="form">
                    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                    <button class="button primary wide" type="submit" style="font-size: 16px; padding: 15px; box-shadow: 0 10px 30px #216fe044; width: 100%;">{next_amt:.0f} TL Ödülü Al</button>
                </form>
                """
                
            body = f"""
            <section class="auth-card upload-card" style="margin-top: 65px; width: min(650px, calc(100% - 40px));">
                <div class="eyebrow">DAILY STREAK REWARDS</div>
                <h1>Günlük Ödül</h1>
                <p class="lead" style="font-size: 15px; margin: 10px 0 20px;">Sitede aktif kalarak her gün daha fazla bakiye kazan! 7 gün boyunca kesintisiz giriş yap, son gün 50 TL büyük ödülü kap.</p>
                
                <div class="streak-grid">
                    {cards}
                </div>
                
                <div style="background: #ffffff04; border: 1px solid var(--line); padding: 18px; border-radius: 12px; margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <span style="font-size: 12px; color: var(--muted); display: block;">Mevcut Seriniz</span>
                        <strong style="font-size: 18px; color: #65d9ff;">{daily_streak} Gün Kesintisiz</strong>
                    </div>
                    <div>
                        <span style="font-size: 12px; color: var(--muted); display: block;">Toplam Seri Kazanımı</span>
                        <strong style="font-size: 18px; color: #00ff88;">En Çok 50 TL/Gün</strong>
                    </div>
                </div>
                
                {claim_btn}
            </section>
            """
            self.send_html(page("Günlük Ödül", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/wheel":
            if not user:
                self.redirect("/login")
                return
            won = query.get("won", [""])[0]
            won_wheel = query.get("wh", [""])[0]
            won_seg = query.get("seg", [""])[0]

            with db() as connection:
                urow = connection.execute("SELECT balance, last_free_spin FROM users WHERE id=?", (user[1],)).fetchone()
            user_balance = float(urow["balance"]) if urow else 0.0
            last_free = urow["last_free_spin"] if urow else 0
            free_available = (int(time.time()) - last_free) >= FREE_SPIN_COOLDOWN
            free_remaining = max(0, FREE_SPIN_COOLDOWN - (int(time.time()) - last_free))
            free_h = free_remaining // 3600
            free_m = (free_remaining % 3600) // 60

            seg_colors = ['#ff6b6b','#ffd93d','#6bcb77','#4d96ff','#ff8c42','#c084fc','#22d3ee','#f472b6']

            wheel_cards = ""
            wheel_order = ["ucretsiz", "bronz", "gumus", "altin"]
            for wkey in wheel_order:
                w = WHEELS[wkey]
                cost_lbl = "Ücretsiz" if w["cost"] == 0 else f"{w['cost']} TL"
                disabled = ""
                btn_text = f"Çevir ({cost_lbl})"
                if wkey == "ucretsiz" and not free_available:
                    disabled = "disabled"
                    btn_text = f"Bekle ({free_h}s {free_m}dk)"
                elif wkey != "ucretsiz" and user_balance < w["cost"]:
                    disabled = "disabled"
                    btn_text = f"Yetersiz Bakiye ({cost_lbl})"

                reward_tags = "".join(f'<span class="wheel-reward-tag" style="border-color:{seg_colors[i]}44;color:{seg_colors[i]}">{r} TL</span>' for i, r in enumerate(w["rewards"]))

                wheel_cards += f"""
                <div class="wheel-option-card" style="border-color: {w['border']}44;">
                    <div class="wheel-option-header">
                        <span style="font-size:32px;">{w['icon']}</span>
                        <h2>{w['name']}</h2>
                        <span class="wheel-cost" style="color:{w['border']};">{cost_lbl}</span>
                    </div>
                    <div class="wheel-rewards-grid">{reward_tags}</div>
                    <form method="post" action="/wheel/spin">
                        <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                        <input type="hidden" name="wheel" value="{wkey}">
                        <button class="button primary wide" type="submit" {disabled} style="border-color:{w['border']};background:{w['border']}22;color:{w['border']};">{btn_text}</button>
                    </form>
                </div>
                """

            # Build win overlay if won param exists
            win_overlay = ""
            if won and won_wheel and won_seg:
                try:
                    won_val = float(won)
                    seg_idx = int(won_seg)
                    wdata = WHEELS.get(won_wheel, WHEELS["ucretsiz"])
                    # Calculate rotation: each segment = 45deg, spin multiple full rotations + land on segment
                    target_angle = 360 * 5 + (360 - seg_idx * 45 - 22.5)
                    segments_html = ""
                    for i, r in enumerate(wdata["rewards"]):
                        segments_html += f'<div class="wseg" style="--i:{i};background:{seg_colors[i]};">{r}</div>'
                    win_overlay = f"""
                    <div class="wheel-overlay" id="wheelOverlay">
                        <div class="wheel-overlay-inner">
                            <h2 style="color:#ffd700;margin-bottom:15px;">🎉 {wdata['name']}</h2>
                            <div class="wheel-container">
                                <div class="wheel-pointer">▼</div>
                                <div class="wheel-disc" id="wheelDisc" style="--target:{target_angle}deg;">
                                    {segments_html}
                                </div>
                            </div>
                            <div class="wheel-result" id="wheelResult" style="display:none;">
                                <h3>🎉 Tebrikler!</h3>
                                <div class="won-amount">{won_val:.0f} TL</div>
                                <p class="muted">Bakiyenize eklendi!</p>
                            </div>
                            <button class="button ghost" onclick="document.getElementById('wheelOverlay').style.display='none'" id="closeBtn" style="display:none;margin-top:15px;">Kapat</button>
                        </div>
                    </div>
                    <script>
                    (function(){{ 
                        const disc=document.getElementById('wheelDisc');
                        const result=document.getElementById('wheelResult');
                        const closeBtn=document.getElementById('closeBtn');
                        setTimeout(()=>{{ disc.classList.add('spinning'); }}, 100);
                        setTimeout(()=>{{ result.style.display='block'; closeBtn.style.display='inline-block'; 
                            try {{ const A=window.AudioContext||window.webkitAudioContext; const c=new A();
                            const o1=c.createOscillator(); const g1=c.createGain(); o1.type='sine'; o1.frequency.setValueAtTime(523.25,c.currentTime); g1.gain.setValueAtTime(0.08,c.currentTime); g1.gain.exponentialRampToValueAtTime(0.01,c.currentTime+0.1); o1.connect(g1); g1.connect(c.destination); o1.start(); o1.stop(c.currentTime+0.1);
                            setTimeout(()=>{{ const o2=c.createOscillator(); const g2=c.createGain(); o2.type='sine'; o2.frequency.setValueAtTime(659.25,c.currentTime); g2.gain.setValueAtTime(0.08,c.currentTime); g2.gain.exponentialRampToValueAtTime(0.01,c.currentTime+0.15); o2.connect(g2); g2.connect(c.destination); o2.start(); o2.stop(c.currentTime+0.15); }}, 85);
                            setTimeout(()=>{{ const o3=c.createOscillator(); const g3=c.createGain(); o3.type='sine'; o3.frequency.setValueAtTime(783.99,c.currentTime); g3.gain.setValueAtTime(0.12,c.currentTime); g3.gain.exponentialRampToValueAtTime(0.01,c.currentTime+0.3); o3.connect(g3); g3.connect(c.destination); o3.start(); o3.stop(c.currentTime+0.3); }}, 170);
                            }} catch(e){{}} }}, 4200);
                    }})();
                    </script>
                    """
                except (ValueError, KeyError):
                    pass

            body = f"""
            <section class="auth-card upload-card" style="margin-top: 65px; width: min(900px, calc(100% - 40px));">
                <div class="eyebrow">🎰 WHEEL OF FORTUNE</div>
                <h1>Çark Çevir</h1>
                <p class="lead" style="font-size: 15px; margin: 10px 0 20px;">Şansını dene! Ücretsiz çarkı 4 saatte bir çevir, ya da bakiyenle premium çarklardan büyük ödüller kazan.</p>
                <div class="wheel-options-grid">
                    {wheel_cards}
                </div>
            </section>
            {win_overlay}
            """
            self.send_html(page("Çark Çevir", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/admin/login":
            fields = '<label>Yönetici adı<input name="username" autocomplete="username" required></label><label>Yönetici şifresi<input name="password" type="password" autocomplete="current-password" required></label>'
            self.send_html(form_page("Yönetici girişi", "/admin/login", "Panele gir", fields, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))
        elif path == "/admin":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            with db() as connection:
                users = connection.execute("SELECT id, username, created_at, is_premium, premium_until, balance FROM users ORDER BY created_at DESC").fetchall()
                updates = connection.execute("SELECT id, title, body, tag, created_at FROM updates ORDER BY created_at DESC").fetchall()
                payments = connection.execute("SELECT payments.id, users.username, payments.platform, payments.code, payments.amount, payments.status, payments.created_at FROM payments JOIN users ON users.id=payments.user_id ORDER BY payments.created_at DESC").fetchall()
                dl_logs = connection.execute("SELECT downloads.id, users.username, downloads.filename, downloads.serial, downloads.ip, downloads.created_at FROM downloads JOIN users ON users.id=downloads.user_id ORDER BY downloads.created_at DESC LIMIT 30").fetchall()
                logs = connection.execute("SELECT event, created_at FROM logs ORDER BY created_at DESC LIMIT 25").fetchall()
                logs_count = connection.execute("SELECT COUNT(*) FROM logs").fetchone()[0]

            premium_count = sum(1 for u in users if u["is_premium"] or u["premium_until"] > int(time.time()))
            pending_count = sum(1 for p in payments if p["status"] == "BEKLEMEDE")

            user_rows = ""
            for row in users:
                is_prem_now = bool(row["is_premium"]) or row["premium_until"] > int(time.time())
                p_badge = ' <span class="premium-badge">PREMIUM</span>' if is_prem_now else '<span class="status-tag beklemede" style="background:#ffffff0a; color:#888;">STANDART</span>'
                if row["premium_until"] > int(time.time()):
                    exp_info = f'<span class="muted" style="font-size: 12px;">bitiş: {time.strftime("%Y-%m-%d", time.localtime(row["premium_until"]))}</span>'
                elif row["is_premium"]:
                    exp_info = '<span class="muted" style="font-size: 12px;">süresiz</span>'
                else:
                    exp_info = ""
                balance_val = float(row["balance"])
                
                balance_actions = f"""
                <form method="post" action="/admin/users/add_balance" class="inline" style="margin-left: 10px;">
                    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                    <input type="hidden" name="user_id" value="{row['id']}">
                    <input type="number" name="amount" placeholder="Tutar (TL)" style="width: 80px; padding: 4px 8px; border: 1px solid #ffffff18; border-radius: 6px; background: #070d15; color: white; font-size: 12px; display: inline;" required>
                    <button class="button small primary" type="submit" style="padding: 4px 8px; font-size: 12px;">Ekle</button>
                </form>
                """
                
                toggle_btn = f"""
                <form method="post" action="/admin/users/toggle_premium" class="inline">
                    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                    <input type="hidden" name="user_id" value="{row['id']}">
                    <button class="button small ghost" style="border-color:#ff44663d; color:#ff4466;" type="submit">İptal Et</button>
                </form>
                """ if row["is_premium"] else f"""
                <form method="post" action="/admin/users/toggle_premium" class="inline">
                    <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                    <input type="hidden" name="user_id" value="{row['id']}">
                    <button class="button small primary" type="submit">Premium Yap</button>
                </form>
                """
                user_rows += f"<tr><td>{esc(row['username'])}</td><td>{p_badge} {exp_info}</td><td>{balance_val:.2f} TL</td><td>{time.strftime('%Y-%m-%d %H:%M', time.localtime(row['created_at']))}</td><td style='display: flex; gap: 10px; align-items: center;'>{toggle_btn} {balance_actions}</td></tr>"

            update_rows = "".join(f"<tr><td><span class=\"update-tag\">{esc(row['tag'])}</span></td><td>{esc(row['title'])}</td><td>{format_date(row['created_at'])}</td></tr>" for row in updates)

            pending_rows = ""
            invoice_rows = ""
            for row in payments:
                status_esc = esc(row["status"])
                plat_esc = esc(PLATFORM_NAMES.get(row["platform"], row["platform"]))
                plat_badge = f'<span class="update-tag">{plat_esc}</span>'
                if row["status"] == "ONAYLANDI":
                    status_badge = f'<span class="status-tag onaylandi">{status_esc}</span>'
                    invoice_rows += f"<tr><td>{plat_badge}</td><td>{esc(row['username'])}</td><td><code>{esc(row['code'])}</code></td><td>{esc(row['amount'])}</td><td>{format_date(row['created_at'])}</td><td>{status_badge}</td></tr>"
                elif row["status"] == "REDDEDİLDİ":
                    status_badge = f'<span class="status-tag reddedildi">{status_esc}</span>'
                    invoice_rows += f"<tr><td>{plat_badge}</td><td>{esc(row['username'])}</td><td><code>{esc(row['code'])}</code></td><td>{esc(row['amount'])}</td><td>{format_date(row['created_at'])}</td><td>{status_badge}</td></tr>"
                else:
                    status_badge = f'<span class="status-tag beklemede">{status_esc}</span>'
                    actions = f"""
                    <form method="post" action="/admin/payments/approve" class="inline">
                        <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                        <input type="hidden" name="payment_id" value="{row['id']}">
                        <button class="button primary small" type="submit">Onayla</button>
                    </form>
                    <form method="post" action="/admin/payments/reject" class="inline">
                        <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
                        <input type="hidden" name="payment_id" value="{row['id']}">
                        <button class="button ghost small" style="border-color:#ff44663d; color:#ff4466;" type="submit">Reddet</button>
                    </form>
                    """
                    pending_rows += f"<tr><td>{plat_badge}</td><td>{esc(row['username'])}</td><td><code>{esc(row['code'])}</code></td><td>{esc(row['amount'])}</td><td>{format_date(row['created_at'])}</td><td>{status_badge}</td><td>{actions}</td></tr>"

            log_rows = "".join(f"<tr><td>{format_date(row['created_at'])} {time.strftime('%H:%M:%S', time.localtime(row['created_at']))}</td><td>{esc(row['event'])}</td></tr>" for row in logs)

            dl_rows = "".join(
                f"<tr><td>{esc(row['username'])}</td><td><code>{esc(row['serial'])}</code></td><td>{esc(row['ip'])}</td><td>{format_date(row['created_at'])} {time.strftime('%H:%M:%S', time.localtime(row['created_at']))}</td></tr>"
                for row in dl_logs
            )

            file_status = f"{DOWNLOAD_PATH.stat().st_size / 1024 / 1024:.2f} MB" if DOWNLOAD_PATH.is_file() else "Dosya yok"
            loader_status = f"{LOADER_PATH.stat().st_size / 1024 / 1024:.2f} MB" if LOADER_PATH.is_file() else "Dosya yok"
            paid_files = [f.name for f in PAID_CHEATS_DIR.iterdir() if f.is_file()] if PAID_CHEATS_DIR.is_dir() else []
            paid_status = ", ".join(paid_files) if paid_files else "Dosya yok"
            
            body = f"""<section class="admin-head"><div><div class="eyebrow">CONTROL CENTER</div><h1>Yönetim paneli</h1><p class="muted">Kayıtlar, ödemeler, sistem günlükleri ve duyurular.</p></div><a class="button ghost" href="/admin/logout">Paneleden çık</a></section>
<section class="stats" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
    <div class="stat"><span>Kayıtlı Kullanıcı</span><strong>{len(users)}</strong></div>
    <div class="stat"><span>Premium Üye</span><strong>{premium_count}</strong></div>
    <div class="stat"><span>Bekleyen Ödeme</span><strong>{pending_count}</strong></div>
    <div class="stat"><span>Sistem Olay Kaydı</span><strong>{logs_count}</strong></div>
</section>

<section class="admin-grid" style="display: grid; grid-template-columns: 1fr; gap: 30px;">
    <section class="table-card">
        <h2>Kullanıcı Yönetimi (Premium & Yetkilendirme)</h2>
        <table>
            <thead>
                <tr>
                    <th>Kullanıcı Adı</th>
                    <th>Üyelik Tipi</th>
                    <th>Bakiye</th>
                    <th>Kayıt Tarihi</th>
                    <th>İşlem</th>
                </tr>
            </thead>
            <tbody>
                {user_rows or '<tr><td colspan="5" class="muted">Kayıtlı kullanıcı yok.</td></tr>'}
            </tbody>
        </table>
    </section>
</section>

<section class="admin-grid" style="display: grid; grid-template-columns: 1fr; gap: 30px; margin-top: 30px;">
    <section class="table-card">
        <h2>Bekleyen Ödeme Talepleri (Kod Onayları)</h2>
        <table>
            <thead>
                <tr>
                    <th>Platform</th>
                    <th>Kullanıcı</th>
                    <th>Kod</th>
                    <th>Tutar</th>
                    <th>Tarih</th>
                    <th>Durum</th>
                    <th>İşlem</th>
                </tr>
            </thead>
            <tbody>
                {pending_rows or '<tr><td colspan="7" class="muted">Bekleyen ödeme talebi yok.</td></tr>'}
            </tbody>
        </table>
    </section>
</section>

<section class="admin-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-top: 30px; align-items: start;">
    <section class="table-card">
        <h2>Fatura & Sonuçlanan Ödeme Geçmişi</h2>
        <table>
            <thead>
                <tr>
                    <th>Platform</th>
                    <th>Kullanıcı</th>
                    <th>Kod</th>
                    <th>Tutar</th>
                    <th>Tarih</th>
                    <th>Sonuç</th>
                </tr>
            </thead>
            <tbody>
                {invoice_rows or '<tr><td colspan="6" class="muted">Onaylanmış veya reddedilmiş ödeme bulunmuyor.</td></tr>'}
            </tbody>
        </table>
    </section>

    <section class="table-card">
        <h2>Duyuru Yayınla</h2>
        <form method="post" action="/admin/updates/create" class="form">
            <input type="hidden" name="csrf_token" value="{esc(csrf_tok)}">
            <label>Etiket<input name="tag" maxlength="24" value="GÜNCELLEME" required></label>
            <label>Başlık<input name="title" maxlength="100" required></label>
            <label>Metin<textarea name="body" maxlength="500" rows="4" required></textarea></label>
            <button class="button primary" type="submit">Duyuruyu yayınla</button>
        </form>
    </section>
</section>

<section class="admin-grid" style="display: grid; grid-template-columns: 1fr 1fr; gap: 30px; margin-top: 30px; align-items: start;">
    <section class="table-card">
        <h2>İndirme Alanı Durumu</h2>
        <p class="muted" style="margin-bottom: 12px;">Bedava sürüm (<code>downloads/</code>) ve ücretli sürümler (<code>paid_cheats/</code>) için yüklenmiş dosyalar. Yönetici olarak her dosyayı tek tek indirebilirsin.</p>
        <table>
            <thead>
                <tr>
                    <th>Bölüm</th>
                    <th>Dosya(lar)</th>
                    <th>Durum</th>
                    <th>İndir</th>
                </tr>
            </thead>
            <tbody>
                <tr><td>Bedava sürüm</td><td><code>Biga Cheat-Cs2-Modified.exe</code></td><td><span class="status-tag {'onaylandi' if DOWNLOAD_PATH.is_file() else 'reddedildi'}">{file_status}</span></td><td>{'<a class="button small primary" href="/admin/downloads/free">İndir</a>' if DOWNLOAD_PATH.is_file() else '-'}</td></tr>
                <tr><td>Loader</td><td><code>BigaCheat-Loader.exe</code></td><td><span class="status-tag {'onaylandi' if LOADER_PATH.is_file() else 'reddedildi'}">{loader_status}</span></td><td>{'<a class="button small primary" href="/admin/downloads/loader">İndir</a>' if LOADER_PATH.is_file() else '-'}</td></tr>
                <tr><td>Ücretli Hileler</td><td><code>{esc(paid_status)}</code></td><td><span class="status-tag {'onaylandi' if paid_files else 'reddedildi'}">{len(paid_files)} dosya</span></td><td>{" ".join(f'<a class="button small ghost" href="/admin/downloads/paid/{quote(f)}">İndir: {esc(f)}</a>' for f in paid_files) or '-'}</td></tr>
            </tbody>
        </table>
        <p class="muted" style="margin-top: 10px; font-size: 12px;">Dosyalar projenin <code>downloads/</code> ve <code>paid_cheats/</code> klasörlerine eklenir ve GitHub üzerinden canlıya yüklenir.</p>
    </section>

    <section class="table-card">
        <h2>Site Sistem Kayıtları (Son 25 Olay)</h2>
        <table>
            <thead>
                <tr>
                    <th style="width: 170px;">Zaman</th>
                    <th>Olay Günlüğü</th>
                </tr>
            </thead>
            <tbody>
                {log_rows or '<tr><td colspan="2" class="muted">Sistem günlüğü henüz boş.</td></tr>'}
            </tbody>
        </table>
    </section>

    <section class="table-card">
        <h2>Yayınlanan Duyurular</h2>
        <table>
            <thead>
                <tr>
                    <th>Etiket</th>
                    <th>Başlık</th>
                    <th>Tarih</th>
                </tr>
            </thead>
            <tbody>
                {update_rows or '<tr><td colspan="3" class="muted">Henüz duyuru yok.</td></tr>'}
            </tbody>
        </table>
    </section>
</section>

<section class="admin-grid" style="display: grid; grid-template-columns: 1fr; gap: 30px; margin-top: 30px;">
    <section class="table-card">
        <h2>Premium İndirme Takibi (Filigran)</h2>
        <p class="muted" style="margin-bottom: 12px;">Ücretli içerik indirmelerinin lisans numarası ve IP kayıtları. Bir dosya sızdıysa lisans numarasından kimin indirdiği tespit edilir.</p>
        <table>
            <thead>
                <tr>
                    <th>Kullanıcı</th>
                    <th>Lisans No</th>
                    <th>IP</th>
                    <th>Zaman</th>
                </tr>
            </thead>
            <tbody>
                {dl_rows or '<tr><td colspan="4" class="muted">Henüz ücretli indirme yapılmamış.</td></tr>'}
            </tbody>
        </table>
    </section>
</section>
"""
            self.send_html(page("Yönetim paneli", body, username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok))

        elif path == "/admin/logout":
            self.send_response(302)
            self.send_header("Location", "/admin/login")
            self.send_header("Set-Cookie", "admin_session_v2=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif path == "/static/style.css":
            css = (ROOT / "static" / "style.css").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/css; charset=utf-8")
            self.send_header("Content-Length", str(len(css)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(css)
        elif path == "/static/logo.png" or path == "/static/favicon.png":
            img_path = ROOT / "static" / ("favicon.png" if path.endswith("favicon.png") else "logo.png")
            if not img_path.is_file():
                self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)
                return
            img = img_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(img)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(img)
        else:
            self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1><p class="muted">Aradığın sayfa bulunamadı.</p></section>', username, message=message, message_type=message_type, is_premium=is_premium, csrf_token=csrf_tok), 404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        content_type = self.headers.get("Content-Type", "")
        is_multipart = content_type.lower().startswith("multipart/form-data")
        if is_multipart:
            fields: dict[str, str] = {}
        elif content_type.lower().startswith("application/json"):
            fields = self.parse_json()
        else:
            fields = self.parse_form()
        current = self.current_user()
        username = current[0] if current else None
        is_premium = self.is_user_premium(current[1]) if current else False
        csrf_tok = csrf_for(self)
        ip = self.client_ip()
        if path in ("/register", "/login", "/admin/login", "/payment/submit", "/wheel/spin") and not rate_allowed(ip, path.strip("/")):
            self.rate_limit_response()
            return
        if path == "/paid-cheats/purchase":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            plan_key = fields.get("plan", "").strip()
            plan = PAID_CHEATS_PLANS.get(plan_key)
            if not plan:
                self.redirect("/paid-cheats?msg=Gecersiz+plan&msg_type=error")
                return
            price = plan["price"]
            with db() as connection:
                row = connection.execute("SELECT balance, premium_until FROM users WHERE id=?", (current[1],)).fetchone()
                balance = float(row["balance"]) if row else 0.0
                if balance < price:
                    self.redirect("/paid-cheats?msg=Bakiyeniz+yetersiz.+Once+bakiye+yukleyin.&msg_type=error")
                    return
                base = max(int(time.time()), int(row["premium_until"]) if row else 0)
                new_until = base + plan["days"] * 86400
                connection.execute("UPDATE users SET balance=balance-?, premium_until=? WHERE id=?", (price, new_until, current[1]))
            log_event(f"[PREMIUM] '{username}' kullanıcısı bakiyesiyle {plan['days']} gün premium erişim satın aldı ({price:.0f} TL).")
            self.redirect("/paid-cheats?msg=Erisim+aktif.+Suresi:+{days}gn.&msg_type=success".replace("{days}", str(plan["days"])))
        elif path == "/chat/send":
            if not current:
                self.send_json({"ok": False, "error": "Giriş yapmalısın."}, 401)
                return
            if not self.verify_csrf(fields):
                self.send_json({"ok": False, "error": "CSRF doğrulaması başarısız."}, 403)
                return
            if not rate_allowed(f"{ip}:chat:{current[1]}", "chat/send"):
                self.send_json({"ok": False, "error": "Çok hızlı mesaj atıyorsun. Biraz bekle."}, 429)
                return
            body_text = fields.get("body", "").strip()
            image_data = fields.get("image", "").strip()
            if not body_text and not image_data:
                self.send_json({"ok": False, "error": "Mesaj boş olamaz."}, 400)
                return
            if len(body_text) > 400:
                self.send_json({"ok": False, "error": "Mesaj en fazla 400 karakter olabilir."}, 400)
                return
            if image_data:
                if len(image_data) > 400_000:
                    self.send_json({"ok": False, "error": "Fotoğraf çok büyük (en fazla 300 KB)."}, 400)
                    return
                if not image_data.startswith("data:image/"):
                    self.send_json({"ok": False, "error": "Geçersiz fotoğraf."}, 400)
                    return
            with db() as connection:
                connection.execute(
                    "INSERT INTO chat_messages(user_id, username, body, image, created_at) VALUES(?,?,?,?,?)",
                    (current[1], username or "?", body_text, image_data, int(time.time())),
                )
            self.send_json({"ok": True})
            return
        elif path == "/dm/send":
            if not current:
                self.send_json({"ok": False, "error": "Giriş yapmalısın."}, 401)
                return
            if not self.verify_csrf(fields):
                self.send_json({"ok": False, "error": "CSRF doğrulaması başarısız."}, 403)
                return
            if not rate_allowed(f"{ip}:dm:{current[1]}", "chat/send"):
                self.send_json({"ok": False, "error": "Çok hızlı mesaj atıyorsun. Biraz bekle."}, 429)
                return
            to_name = fields.get("to", "").strip()
            body_text = fields.get("body", "").strip()
            image_data = fields.get("image", "").strip()
            if not to_name:
                self.send_json({"ok": False, "error": "Alıcı belirtilmedi."}, 400)
                return
            if not body_text and not image_data:
                self.send_json({"ok": False, "error": "Mesaj boş olamaz."}, 400)
                return
            if len(body_text) > 400:
                self.send_json({"ok": False, "error": "Mesaj en fazla 400 karakter olabilir."}, 400)
                return
            if image_data:
                if len(image_data) > 400_000:
                    self.send_json({"ok": False, "error": "Fotoğraf çok büyük (en fazla 300 KB)."}, 400)
                    return
                if not image_data.startswith("data:image/"):
                    self.send_json({"ok": False, "error": "Geçersiz fotoğraf."}, 400)
                    return
            with db() as connection:
                peer = connection.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?)", (to_name,)).fetchone()
            if not peer:
                self.send_json({"ok": False, "error": "Kullanıcı bulunamadı."}, 404)
                return
            if peer["id"] == current[1]:
                self.send_json({"ok": False, "error": "Kendine mesaj atamazsın."}, 400)
                return
            with db() as connection:
                are_friends = connection.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (current[1], peer["id"])).fetchone()
            if not are_friends:
                self.send_json({"ok": False, "error": "Sadece arkadaşlarınla özel konuşabilirsin."}, 403)
                return
            with db() as connection:
                connection.execute(
                    "INSERT INTO private_messages(sender_id, receiver_id, body, image, created_at) VALUES(?,?,?,?,?)",
                    (current[1], peer["id"], body_text, image_data, int(time.time())),
                )
            self.send_json({"ok": True})
            return
        elif path == "/market/list":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            if not rate_allowed(f"{ip}:market:{current[1]}", "market/list"):
                self.redirect("/market?msg=Cok+hizli+islem+yapiyorsun&msg_type=error")
                return
            inv_id = fields.get("inv_id", "")
            try:
                price = float(fields.get("price", "0"))
            except ValueError:
                price = 0
            if price < 1 or price > 100000:
                self.redirect("/market?msg=Gecersiz+fiyat&msg_type=error")
                return
            with db() as connection:
                inv = connection.execute("SELECT id, user_id, skin_key FROM case_inventory WHERE id=?", (inv_id,)).fetchone()
                if not inv or inv["user_id"] != current[1]:
                    self.redirect("/market?msg=Bu+skin+sana+ait+degil&msg_type=error")
                    return
                already = connection.execute("SELECT 1 FROM market_listings WHERE inv_id=? AND status='active'", (inv_id,)).fetchone()
                if already:
                    self.redirect("/market?msg=Skin+zaten+pazarda&msg_type=error")
                    return
                connection.execute(
                    "INSERT INTO market_listings(seller_id, inv_id, skin_key, price, status, created_at) VALUES(?,?,?,?,'active',?)",
                    (current[1], inv_id, inv["skin_key"], price, int(time.time())),
                )
            log_event(f"[PAZAR] '{username}' skinini {price:.2f} TL'ye pazara koydu.")
            self.redirect("/market?msg=Skin+pazara+konuldu&msg_type=success")
            return
        elif path == "/market/buy":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            if not rate_allowed(f"{ip}:market:{current[1]}", "market/buy"):
                self.redirect("/market?msg=Cok+hizli+islem+yapiyorsun&msg_type=error")
                return
            listing_id = fields.get("listing_id", "")
            with db() as connection:
                listing = connection.execute(
                    """SELECT l.id AS lid, l.price, l.inv_id, l.seller_id, u2.username AS seller_name
                       FROM market_listings l JOIN users u2 ON u2.id = l.seller_id
                       WHERE l.id=? AND l.status='active'""", (listing_id,)
                ).fetchone()
                if not listing:
                    self.redirect("/market?msg=Ilani+bulamadim&msg_type=error")
                    return
                if listing["seller_id"] == current[1]:
                    self.redirect("/market?msg=Kendi+ilani+alamazsin&msg_type=error")
                    return
                buyer = connection.execute("SELECT balance FROM users WHERE id=?", (current[1],)).fetchone()
                if float(buyer["balance"]) < listing["price"]:
                    self.redirect("/market?msg=Yetersiz+bakiye&msg_type=error")
                    return
                connection.execute("UPDATE users SET balance=balance-? WHERE id=?", (listing["price"], current[1]))
                connection.execute("UPDATE users SET balance=balance+? WHERE id=?", (listing["price"], listing["seller_id"]))
                connection.execute("UPDATE case_inventory SET user_id=? WHERE id=?", (current[1], listing["inv_id"]))
                connection.execute("UPDATE market_listings SET status='sold' WHERE id=?", (listing["lid"],))
            log_event(f"[PAZAR] '{username}' {listing['seller_name']}'nun ilanını {listing['price']:.2f} TL'ye satın aldı.")
            self.redirect("/market?msg=Skin+satin+alindi&msg_type=success")
            return
        elif path == "/inventory/sell":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            if not rate_allowed(f"{ip}:market:{current[1]}", "inventory/sell"):
                self.redirect("/inventory?msg=Cok+hizli+islem+yapiyorsun&msg_type=error")
                return
            inv_id = fields.get("inv_id", "")
            with db() as connection:
                owner = connection.execute("SELECT user_id FROM case_inventory WHERE id=?", (inv_id,)).fetchone()
                if not owner:
                    self.redirect("/inventory?msg=Esya+bulunamadi&msg_type=error")
                    return
                if owner["user_id"] != current[1]:
                    self.redirect("/inventory?msg=Bu+esya+sana+ait+degil&msg_type=error")
                    return
                listed = connection.execute("SELECT 1 FROM market_listings WHERE inv_id=? AND status='active'", (inv_id,)).fetchone()
                if listed:
                    self.redirect("/inventory?msg=Esya+zaten+pazarda&msg_type=error")
                    return
                inv = connection.execute("SELECT id, item_name, skin_key, item_tier FROM case_inventory WHERE id=?", (inv_id,)).fetchone()
                price = market_price(inv["skin_key"] or inv["item_name"])
                if price <= 0:
                    self.redirect("/inventory?msg=Bu+esya+satilamaz&msg_type=error")
                    return
                connection.execute("UPDATE users SET balance=balance+? WHERE id=?", (price, current[1]))
                connection.execute("DELETE FROM case_inventory WHERE id=?", (inv_id,))
            log_event(f"[PAZAR] '{username}' envanterden {inv['item_name']}'i canlı fiyata sattı ({price:.2f} TL).")
            self.redirect(f"/inventory?msg=Satildi:+{quote(inv['item_name'])}+({price:.2f}+TL)&msg_type=success")
            return
        elif path == "/market/cancel":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            listing_id = fields.get("listing_id", "")
            with db() as connection:
                listing = connection.execute("SELECT id, seller_id FROM market_listings WHERE id=?", (listing_id,)).fetchone()
                if not listing or listing["seller_id"] != current[1]:
                    self.redirect("/market?msg=Ilani+bulamadim&msg_type=error")
                    return
                connection.execute("UPDATE market_listings SET status='cancelled' WHERE id=?", (listing["id"],))
            self.redirect("/market?msg=Ilani+gerisin+ciktin&msg_type=success")
            return
        elif path == "/trade/send":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            if not rate_allowed(f"{ip}:trade:{current[1]}", "trade/send"):
                self.redirect("/trade?msg=Cok+hizli+islem+yapiyorsun&msg_type=error")
                return
            sender_inv_id = fields.get("sender_inv_id", "")
            receiver_inv_id = fields.get("receiver_inv_id", "")
            with db() as connection:
                s_inv = connection.execute("SELECT id, user_id, item_name FROM case_inventory WHERE id=?", (sender_inv_id,)).fetchone()
                r_inv = connection.execute("SELECT id, user_id, item_name FROM case_inventory WHERE id=?", (receiver_inv_id,)).fetchone()
                if not s_inv or not r_inv:
                    self.redirect("/trade?msg=Skin+bulunamadi&msg_type=error")
                    return
                if s_inv["user_id"] != current[1] or r_inv["user_id"] == current[1]:
                    self.redirect("/trade?msg=Gecersiz+takas+secimi&msg_type=error")
                    return
                s_listed = connection.execute("SELECT 1 FROM market_listings WHERE inv_id=? AND status='active'", (sender_inv_id,)).fetchone()
                r_listed = connection.execute("SELECT 1 FROM market_listings WHERE inv_id=? AND status='active'", (receiver_inv_id,)).fetchone()
                if s_listed or r_listed:
                    self.redirect("/trade?msg=Pazardaki+skinler+takas+edilemez&msg_type=error")
                    return
                connection.execute(
                    "INSERT INTO trade_requests(sender_id, receiver_id, sender_inv_id, receiver_inv_id, status, created_at) VALUES(?,?,?,?,'pending',?)",
                    (current[1], r_inv["user_id"], sender_inv_id, receiver_inv_id, int(time.time())),
                )
            log_event(f"[TAKAS] '{username}' takas isteği gönderdi.")
            self.redirect("/trade?msg=Takas+istegi+gonderildi&msg_type=success")
            return
        elif path == "/trade/respond":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            if not rate_allowed(f"{ip}:trade:{current[1]}", "trade/respond"):
                self.redirect("/trade?msg=Cok+hizli+islem+yapiyorsun&msg_type=error")
                return
            req_id = fields.get("req_id", "")
            action = fields.get("action", "")
            accepted = False
            with db() as connection:
                req = connection.execute(
                    """SELECT tr.id, tr.sender_id, tr.receiver_id, tr.sender_inv_id, tr.receiver_inv_id, tr.status
                       FROM trade_requests tr WHERE tr.id=?""", (req_id,)
                ).fetchone()
                if not req or req["receiver_id"] != current[1] or req["status"] != "pending":
                    self.redirect("/trade?msg=Istek+bulunamadi&msg_type=error")
                    return
                if action == "accept":
                    s_inv = connection.execute("SELECT user_id FROM case_inventory WHERE id=?", (req["sender_inv_id"],)).fetchone()
                    r_inv = connection.execute("SELECT user_id FROM case_inventory WHERE id=?", (req["receiver_inv_id"],)).fetchone()
                    if not s_inv or not r_inv or s_inv["user_id"] != req["sender_id"] or r_inv["user_id"] != req["receiver_id"]:
                        self.redirect("/trade?msg=Takas+gecersiz+oldu&msg_type=error")
                        return
                    connection.execute("UPDATE case_inventory SET user_id=? WHERE id=?", (req["receiver_id"], req["sender_inv_id"]))
                    connection.execute("UPDATE case_inventory SET user_id=? WHERE id=?", (req["sender_id"], req["receiver_inv_id"]))
                    connection.execute("UPDATE trade_requests SET status='accepted' WHERE id=?", (req["id"],))
                    accepted = True
                else:
                    connection.execute("UPDATE trade_requests SET status='declined' WHERE id=?", (req["id"],))
            if accepted:
                log_event(f"[TAKAS] '{username}' takası onayladı → skinler değişti.")
                self.redirect("/trade?msg=Takas+gerceklesti&msg_type=success")
            else:
                self.redirect("/trade?msg=Takas+reddedildi&msg_type=success")
            return
        elif path == "/cases/open":
            if not current:
                self.send_json({"ok": False, "error": "Giriş yapmalısın."}, 401)
                return
            if not self.verify_csrf(fields):
                self.send_json({"ok": False, "error": "CSRF doğrulaması başarısız."}, 403)
                return
            if not rate_allowed(f"{ip}:cases:{current[1]}", "cases/open"):
                self.send_json({"ok": False, "error": "Çok hızlı kasa açıyorsun. Biraz bekle."}, 429)
                return
            case_key = fields.get("case_key", "").strip()
            case = CASES.get(case_key)
            if not case:
                self.send_json({"ok": False, "error": "Kasa bulunamadı."}, 404)
                return
            with db() as connection:
                urow = connection.execute("SELECT balance, premium_until FROM users WHERE id=?", (current[1],)).fetchone()
                if not urow:
                    self.send_json({"ok": False, "error": "Kullanıcı bulunamadı."}, 404)
                    return
                balance = float(urow["balance"])
                if balance < case["price"]:
                    self.send_json({"ok": False, "error": f"Yetersiz bakiye. Gerekli: ₺{case['price']:.0f}"}, 400)
                    return
                total_w = sum(i["weight"] for i in case["items"])
                roll = random.uniform(0, total_w)
                chosen = case["items"][-1]
                for it in case["items"]:
                    if roll < it["weight"]:
                        chosen = it
                        break
                    roll -= it["weight"]
                new_bal = balance - case["price"]
                reward_type = chosen.get("type", "skin")
                inv_id = 0
                premium_until = int(urow["premium_until"] or 0)
                if reward_type == "balance":
                    new_bal += chosen["value"]
                    connection.execute("UPDATE users SET balance=? WHERE id=?", (new_bal, current[1]))
                elif reward_type == "premium":
                    days = int(chosen["value"])
                    now = int(time.time())
                    new_premium = max(now, premium_until) + days * 86400
                    connection.execute("UPDATE users SET balance=?, premium_until=? WHERE id=?", (new_bal, new_premium, current[1]))
                else:
                    connection.execute("UPDATE users SET balance=? WHERE id=?", (new_bal, current[1]))
                    inv_id = connection.insert_id(
                        "INSERT INTO case_inventory(user_id, case_key, item_name, item_tier, item_value, skin_key, created_at) VALUES(?,?,?,?,?,?,?)",
                        (current[1], case_key, chosen["name"], chosen["tier"], chosen["value"], chosen.get("skin", ""), int(time.time())),
                    )
            tier = TIER_INFO[chosen["tier"]]
            log_event(f"[KASA] '{username}' {case['name']} açtı → {chosen['name']} ({tier['name']}).")
            self.send_json({
                "ok": True,
                "item": {"name": chosen["name"], "tier": chosen["tier"], "icon": chosen.get("icon", "🎁"), "skin": chosen.get("skin", ""), "value": chosen["value"], "type": reward_type},
                "balance": new_bal,
                "inv_id": inv_id,
                "premium_until": premium_until if reward_type == "premium" else None,
                "case_name": case["name"],
            })
            return
        elif path == "/profile/update":
            if not current:
                self.send_json({"ok": False, "error": "Giriş yapmalısın."}, 401)
                return
            if not self.verify_csrf(fields):
                self.send_json({"ok": False, "error": "CSRF doğrulaması başarısız."}, 403)
                return
            avatar_data = fields.get("avatar_data", "").strip()
            status_text = fields.get("status_text", "").strip()[:80]
            bio = fields.get("bio", "").strip()[:300]
            profile_link = fields.get("profile_link", "").strip()[:200]
            if avatar_data and not (avatar_data.startswith("data:image/") and len(avatar_data) <= 400_000):
                self.send_json({"ok": False, "error": "Geçersiz fotoğraf."}, 400)
                return
            with db() as connection:
                connection.execute("UPDATE users SET avatar=?, status_text=?, bio=?, profile_link=? WHERE id=?", (avatar_data, status_text, bio, profile_link, current[1]))
            self.send_json({"ok": True})
            return
        elif path == "/friends/request":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            target = fields.get("target", "").strip()
            with db() as connection:
                trow = connection.execute("SELECT id FROM users WHERE LOWER(username)=LOWER(?)", (target,)).fetchone()
            if not trow:
                self.redirect("/friends?msg=Kullanici+bulunamadi&msg_type=error")
                return
            if trow["id"] == current[1]:
                self.redirect("/friends?msg=Kendine+arkadas+ekleyemezsin&msg_type=error")
                return
            with db() as connection:
                already = connection.execute("SELECT 1 FROM friends WHERE user_id=? AND friend_id=?", (current[1], trow["id"])).fetchone()
                if already:
                    self.redirect("/friends?msg=Zaten+arkadassiniz&msg_type=error")
                    return
                existing = connection.execute("SELECT id, sender_id, status FROM friend_requests WHERE (sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)", (current[1], trow["id"], trow["id"], current[1])).fetchone()
                if existing:
                    if existing["status"] == "pending":
                        self.redirect("/friends?msg=Istek+zaten+bekliyor&msg_type=error")
                        return
                    if existing["status"] == "declined":
                        connection.execute("UPDATE friend_requests SET status='pending', created_at=? WHERE id=?", (int(time.time()), existing["id"]))
                    else:
                        connection.execute("UPDATE friend_requests SET status='pending', sender_id=?, receiver_id=?, created_at=? WHERE id=?", (current[1], trow["id"], int(time.time()), existing["id"]))
                else:
                    connection.execute("INSERT INTO friend_requests(sender_id, receiver_id, status, created_at) VALUES(?,?,'pending',?)", (current[1], trow["id"], int(time.time())))
            self.redirect(f"/profile/{quote(target)}?msg=Arkadaslik+istegi+gonderildi&msg_type=success")
            return
        elif path == "/friends/accept":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            try:
                req_id = int(fields.get("req_id", "0") or 0)
            except ValueError:
                req_id = 0
            with db() as connection:
                req = connection.execute("SELECT sender_id FROM friend_requests WHERE id=? AND receiver_id=? AND status='pending'", (req_id, current[1])).fetchone()
                if not req:
                    self.redirect("/friends?msg=Istek+bulunamadi&msg_type=error")
                    return
                connection.execute("UPDATE friend_requests SET status='accepted' WHERE id=?", (req_id,))
                now = int(time.time())
                connection.execute("INSERT INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)", (current[1], req["sender_id"], now))
                connection.execute("INSERT INTO friends(user_id, friend_id, created_at) VALUES(?,?,?)", (req["sender_id"], current[1], now))
            self.redirect("/friends?msg=Arkadaslik+kuruldu!&msg_type=success")
            return
        elif path == "/friends/decline":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            try:
                req_id = int(fields.get("req_id", "0") or 0)
            except ValueError:
                req_id = 0
            with db() as connection:
                connection.execute("UPDATE friend_requests SET status='declined' WHERE id=? AND receiver_id=?", (req_id, current[1]))
            self.redirect("/friends?msg=Istek+reddedildi&msg_type=success")
            return
        elif path == "/register":
            name = fields.get("username", "").strip()
            password = fields.get("password", "")
            captcha_ans = fields.get("captcha_answer", "")
            if not verify_captcha_cookie(self.captcha_cookie(), captcha_ans):
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" required maxlength="24" value="{esc(name)}"></label><label>Şifre<input name="password" type="password" required minlength="8"></label><label>Şifre tekrar<input name="password2" type="password" required minlength="8"></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Kayıt ol", "/register", "Hesap oluştur", fields_html, username, "Robot doğrulaması hatalı.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 400, cookies=[("captcha", c_val)])
                return
            if not USERNAME_RE.fullmatch(name):
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" required maxlength="24"></label><label>Şifre<input name="password" type="password" required minlength="8"></label><label>Şifre tekrar<input name="password2" type="password" required minlength="8"></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Kayıt ol", "/register", "Hesap oluştur", fields_html, username, "Kullanıcı adı 3-24 karakter olmalı; yalnızca harf, rakam ve _ kullanabilirsin.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 400, cookies=[("captcha", c_val)])
                return
            if len(password) < 8 or password != fields.get("password2", ""):
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" required maxlength="24" value="{esc(name)}"></label><label>Şifre<input name="password" type="password" required minlength="8"></label><label>Şifre tekrar<input name="password2" type="password" required minlength="8"></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Kayıt ol", "/register", "Hesap oluştur", fields_html, username, "Şifreler eşleşmeli ve en az 8 karakter olmalı.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 400, cookies=[("captcha", c_val)])
                return
            try:
                with db() as connection:
                    user_id = connection.insert_id("INSERT INTO users(username,password_hash,created_at) VALUES(?,?,?)", (name, password_hash(password), int(time.time())))
            except DB_UNIQUE_ERROR:
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" required maxlength="24"></label><label>Şifre<input name="password" type="password" required minlength="8"></label><label>Şifre tekrar<input name="password2" type="password" required minlength="8"></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Kayıt ol", "/register", "Hesap oluştur", fields_html, username, "Bu kullanıcı adı zaten kayıtlı.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 409, cookies=[("captcha", c_val)])
                return
            token = secrets.token_urlsafe(32)
            with db() as connection:
                connection.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_digest(token), user_id, int(time.time()) + SESSION_TTL))
            self.redirect("/", token)
        elif path == "/login":
            name = fields.get("username", "").strip()
            password = fields.get("password", "")
            captcha_ans = fields.get("captcha_answer", "")
            if not verify_captcha_cookie(self.captcha_cookie(), captcha_ans):
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" autocomplete="username" required maxlength="24" value="{esc(name)}"></label><label>Şifre<input name="password" type="password" autocomplete="current-password" required></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Giriş yap", "/login", "Giriş yap", fields_html, username, "Robot doğrulaması hatalı.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 400, cookies=[("captcha", c_val)])
                return
            with db() as connection:
                if IS_POSTGRES:
                    row = connection.execute("SELECT id,username,password_hash FROM users WHERE LOWER(username)=LOWER(?)", (name,)).fetchone()
                else:
                    row = connection.execute("SELECT id,username,password_hash FROM users WHERE username=? COLLATE NOCASE", (name,)).fetchone()
            if not row or not password_matches(password, row["password_hash"]):
                time.sleep(1.0)
                q_text, c_val = generate_captcha()
                fields_html = f'<label>Kullanıcı adı<input name="username" autocomplete="username" required maxlength="24"></label><label>Şifre<input name="password" type="password" autocomplete="current-password" required></label><label>Robot doğrulaması: <strong>{q_text} = ?</strong><input name="captcha_answer" required type="number" placeholder="Cevabı girin" autocomplete="off"></label>'
                self.send_html(form_page("Giriş yap", "/login", "Giriş yap", fields_html, username, "Kullanıcı adı veya şifre hatalı.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 401, cookies=[("captcha", c_val)])
                return
            token = secrets.token_urlsafe(32)
            with db() as connection:
                connection.execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_digest(token), row["id"], int(time.time()) + SESSION_TTL))
            self.redirect("/", token)
        elif path == "/api/loader/login":
            if not rate_allowed(ip, "loader/login"):
                self.send_json({"ok": False, "error": "rate_limited"}, HTTPStatus.TOO_MANY_REQUESTS)
                return
            name = fields.get("username", "").strip()
            password = fields.get("password", "")
            if not name or not password:
                self.send_json({"ok": False, "error": "eksik_bilgi"}, 400)
                return
            with db() as connection:
                if IS_POSTGRES:
                    row = connection.execute("SELECT id,username,password_hash FROM users WHERE LOWER(username)=LOWER(?)", (name,)).fetchone()
                else:
                    row = connection.execute("SELECT id,username,password_hash FROM users WHERE username=? COLLATE NOCASE", (name,)).fetchone()
            if not row or not password_matches(password, row["password_hash"]):
                time.sleep(1.0)
                self.send_json({"ok": False, "error": "kimlik_dogrulanamadi"}, 401)
                return
            is_prem = self.is_user_premium(row["id"])
            token = loader_token_value(row["id"])
            expiry = self.premium_expiry(row["id"]) if is_prem else 0
            log_event(f"[LOADER] '{row['username']}' loader'a giriş yaptı.")
            self.send_json({"ok": True, "token": token, "premium": is_prem, "premium_until": expiry, "username": row["username"]})
        elif path == "/api/loader/download":
            token = fields.get("token", "").strip()
            dl_type = fields.get("type", "paid").strip().lower()
            user_id = verify_loader_token(token)
            if not user_id:
                self.send_json({"ok": False, "error": "token_gecersiz"}, 401)
                return
            with db() as connection:
                row = connection.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                self.send_json({"ok": False, "error": "kullanici_yok"}, 404)
                return
            if dl_type == "free":
                if not DOWNLOAD_PATH.is_file():
                    self.send_json({"ok": False, "error": "icerik_yok"}, 404)
                    return
                log_event(f"[LOADER] '{row['username']}' ücretsiz sürümü loader'dan indirdi.")
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(DOWNLOAD_PATH.stat().st_size))
                self.send_header("Content-Disposition", 'attachment; filename="Biga Cheat-Cs2-Modified.exe"')
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "same-origin")
                if COOKIE_SECURE:
                    self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
                self.end_headers()
                with DOWNLOAD_PATH.open("rb") as file:
                    while chunk := file.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            if not self.is_user_premium(user_id):
                self.send_json({"ok": False, "error": "premium_gerekli"}, 403)
                return
            files = []
            if PAID_CHEATS_DIR.is_dir():
                for f in sorted(PAID_CHEATS_DIR.iterdir(), key=lambda p: p.name.lower()):
                    if f.is_file() and f.name.lower() != "readme.md":
                        files.append(f)
            if not files:
                self.send_json({"ok": False, "error": "icerik_yok"}, 404)
                return
            serial = generate_license_serial()
            zip_data = make_watermarked_zip(row["username"], serial, files)
            with db() as connection:
                connection.execute("INSERT INTO downloads(user_id, filename, serial, ip, created_at) VALUES(?,?,?,?,?)", (user_id, "loader.zip", serial, ip, int(time.time())))
            log_event(f"[LOADER] '{row['username']}' ücretli içeriği loader'dan indirdi (lisans {serial}).")
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(zip_data)))
            self.send_header("Content-Disposition", 'attachment; filename="BigaCheat-Premium.zip"')
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "same-origin")
            if COOKIE_SECURE:
                self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
            self.end_headers()
            self.wfile.write(zip_data)
        elif path == "/logout":
            token = self.session_cookie()
            if token:
                token_h = token_digest(token)
                with db() as connection:
                    connection.execute("DELETE FROM sessions WHERE token_hash=?", (token_h,))
                    connection.commit()
            self.send_response(302)
            self.send_header("Location", "/")
            self.send_header("Set-Cookie", "session_v2=; Max-Age=0; Path=/; HttpOnly; SameSite=Lax")
            self.end_headers()
        elif path == "/projects/upload":
            if not current:
                self.redirect("/login")
                return
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length <= 0 or content_length > MAX_PROJECT_SIZE + 1_000_000:
                self.send_html(page("Proje yükleme", '<section class="auth-card"><h1>Dosya çok büyük</h1><p class="muted">Maksimum 25 MB yükleyebilirsin.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 413)
                return
            body = self.rfile.read(content_length)
            parts = parse_multipart(body, self.headers.get("Content-Type", ""))
            if not self.verify_csrf(parts, is_multipart=True):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            name = str(parts.get("project_name", {}).get("data", b"").decode("utf-8", "replace")).strip()
            file_part = parts.get("project_file", {})
            filename = Path(str(file_part.get("filename", ""))).name
            data = file_part.get("data", b"")
            allowed = (".zip", ".7z", ".rar", ".tar.gz", ".tgz")
            if not name or len(name) > 80 or not filename.lower().endswith(allowed) or not isinstance(data, bytes) or not data or len(data) > MAX_PROJECT_SIZE:
                self.send_html(page("Proje yükleme", '<section class="auth-card"><h1>Geçersiz proje</h1><p class="muted">Yalnızca 25 MB’a kadar ZIP, 7Z, RAR veya TAR.GZ arşivi yükleyebilirsin.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 400)
                return
            PROJECTS_PATH.mkdir(parents=True, exist_ok=True)
            with db() as connection:
                project_id = connection.insert_id("INSERT INTO projects(user_id,name,filename,stored_path,size,created_at) VALUES(?,?,?,?,?,?)", (current[1], name, filename, "", len(data), int(time.time())))
                stored_name = f"{project_id}_{secrets.token_hex(8)}_{filename}"
                stored_path = str(Path("projects") / stored_name)
                connection.execute("UPDATE projects SET stored_path=? WHERE id=?", (stored_path, project_id))
            (DATA_DIR / stored_path).write_bytes(data)
            log_event(f"[PROJE] '{username}' kullanıcısı '{name}' isimli projeyi başarıyla yükledi.")
            self.redirect("/projects")
        elif path == "/payment/submit":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            amount = fields.get("amount", "").strip()
            platform = fields.get("platform", "").strip().upper()
            code = fields.get("code", "").strip().upper()
            if platform not in ("STEAM", "GPLAY"):
                self.redirect("/payment?msg=Gecersiz+platform&msg_type=error")
                return
            if not amount or not code or len(code) > 50:
                self.redirect("/payment?msg=Gecersiz+kod+veya+tutar&msg_type=error")
                return
            if not validate_game_code(platform, code):
                hint = "XXXXX-XXXXX-XXXXX" if platform == "STEAM" else "XXXX-XXXX-XXXX-XXXX"
                self.redirect(f"/payment?msg=Kod+formati+hatali.+Dogru+format:+{hint}&msg_type=error")
                return
            with db() as connection:
                dup = connection.execute("SELECT id FROM payments WHERE LOWER(code)=LOWER(?)", (code,)).fetchone()
                if dup:
                    self.redirect("/payment?msg=Bu+kod+zaten+kullanilmis&msg_type=error")
                    return
                connection.execute("INSERT INTO payments(user_id, platform, code, amount, status, created_at) VALUES(?,?,?,?,?,?)", (current[1], platform, code, amount, "BEKLEMEDE", int(time.time())))
            log_event(f"[ÖDEME] '{username}' kullanıcısı {PLATFORM_NAMES[platform]} {amount} değerinde kod bildirdi: {code[:4]}...{code[-4:] if len(code) > 4 else ''}")
            self.redirect("/payment?msg=Kod+basariyla+gonderildi.+Yonetici+tarafindan+onaylanacaktir.&msg_type=success")
        elif path == "/admin/login":
            if not ADMIN_PASSWORD or fields.get("username", "").strip().lower() != ADMIN_USERNAME.lower() or not hmac.compare_digest(fields.get("password", ""), ADMIN_PASSWORD):
                time.sleep(1.0)
                fields_html = '<label>Yönetici adı<input name="username" autocomplete="username" required></label><label>Yönetici şifresi<input name="password" type="password" autocomplete="current-password" required></label>'
                self.send_html(form_page("Yönetici girişi", "/admin/login", "Panele gir", fields_html, username, "Yönetici bilgileri hatalı veya ayarlanmamış.", message_type="error", is_premium=is_premium, csrf_token=csrf_tok), 401)
                return
            log_event("[YÖNETİCİ] Yönetici başarıyla panele giriş yaptı.")
            self.redirect_admin("/admin", admin_cookie_value())
        elif path == "/admin/payments/approve":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            payment_id = fields.get("payment_id", "")
            if not payment_id:
                self.redirect_admin("/admin?msg=Eksik+bilgi&msg_type=error")
                return
            log_msg = None
            with db() as connection:
                payment = connection.execute("SELECT user_id, status, amount FROM payments WHERE id=?", (payment_id,)).fetchone()
                if payment and payment["status"] == "BEKLEMEDE":
                    connection.execute("UPDATE payments SET status='ONAYLANDI' WHERE id=?", (payment_id,))
                    
                    # Extract numeric value from amount string (e.g. "100 TL" -> 100.0)
                    amount_str = payment["amount"]
                    try:
                        amount_val = float("".join(c for c in amount_str if c.isdigit() or c == "."))
                    except ValueError:
                        amount_val = 0.0
                        
                    connection.execute("UPDATE users SET is_premium=1, balance=balance+? WHERE id=?", (amount_val, payment["user_id"]))
                    user_row = connection.execute("SELECT username FROM users WHERE id=?", (payment["user_id"],)).fetchone()
                    if user_row:
                        log_msg = f"[PREMIUM] '{user_row['username']}' kullanıcısının ödeme talebi onaylandı, premium yapıldı ve {amount_val:.2f} TL bakiye eklendi."
            if log_msg:
                log_event(log_msg)
            self.redirect_admin("/admin?msg=Odeme+onaylandi&msg_type=success")
        elif path == "/admin/payments/reject":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            payment_id = fields.get("payment_id", "")
            if not payment_id:
                self.redirect_admin("/admin?msg=Eksik+bilgi&msg_type=error")
                return
            log_msg = None
            with db() as connection:
                payment = connection.execute("SELECT user_id, status FROM payments WHERE id=?", (payment_id,)).fetchone()
                if payment and payment["status"] == "BEKLEMEDE":
                    connection.execute("UPDATE payments SET status='REDDEDİLDİ' WHERE id=?", (payment_id,))
                    user_row = connection.execute("SELECT username FROM users WHERE id=?", (payment["user_id"],)).fetchone()
                    if user_row:
                        log_msg = f"[RED] '{user_row['username']}' kullanıcısının ödeme talebi reddedildi."
            if log_msg:
                log_event(log_msg)
            self.redirect_admin("/admin?msg=Odeme+reddedildi&msg_type=success")
        elif path == "/admin/updates/create":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            tag = fields.get("tag", "GÜNCELLEME").strip()[:24] or "GÜNCELLEME"
            title = fields.get("title", "").strip()[:100]
            body = fields.get("body", "").strip()[:500]
            if not title or not body:
                self.send_html(page("Duyuru yayınla", '<section class="auth-card"><h1>Eksik bilgi</h1><p class="muted">Başlık ve metin alanlarını doldurmalısın.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 400)
                return
            with db() as connection:
                connection.execute("INSERT INTO updates(title, body, tag, created_at) VALUES(?,?,?,?)", (title, body, tag, int(time.time())))
            log_event(f"[DUYURU] Yeni duyuru yayınlandı: '{title}'")
            self.redirect_admin("/admin")
        elif path == "/admin/users/toggle_premium":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            user_id = fields.get("user_id", "")
            if not user_id:
                self.redirect_admin("/admin?msg=Eksik+bilgi&msg_type=error")
                return
            log_msg = None
            with db() as connection:
                user_row = connection.execute("SELECT username, is_premium, premium_until FROM users WHERE id=?", (user_id,)).fetchone()
                if user_row:
                    is_prem_now = bool(user_row["is_premium"]) or user_row["premium_until"] > int(time.time())
                    new_status = 0 if is_prem_now else 1
                    if new_status:
                        connection.execute("UPDATE users SET is_premium=1, premium_until=0 WHERE id=?", (user_id,))
                    else:
                        connection.execute("UPDATE users SET is_premium=0, premium_until=0 WHERE id=?", (user_id,))
                    status_str = "PREMIUM yapıldı" if new_status else "PREMIUM iptal edildi"
                    log_msg = f"[KULLANICI] '{user_row['username']}' kullanıcısının premium durumu değiştirildi: {status_str}"
            if log_msg:
                log_event(log_msg)
            self.redirect_admin("/admin?msg=Kullanici+premium+durumu+guncellendi&msg_type=success")
        elif path == "/admin/users/add_balance":
            if not is_admin_cookie(self.admin_cookie()):
                self.redirect("/admin/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
            user_id = fields.get("user_id", "")
            amount = fields.get("amount", "")
            if not user_id or not amount:
                self.redirect_admin("/admin?msg=Eksik+bilgi&msg_type=error")
                return
            try:
                amount_val = float(amount)
            except ValueError:
                self.redirect_admin("/admin?msg=Gecersiz+tutar&msg_type=error")
                return
            with db() as connection:
                user_row = connection.execute("SELECT username FROM users WHERE id=?", (user_id,)).fetchone()
                if user_row:
                    connection.execute("UPDATE users SET balance=balance+? WHERE id=?", (amount_val, user_id))
                    log_msg = f"[BAKİYE] Admin '{user_row['username']}' kullanıcısına {amount_val:.2f} TL bakiye ekledi."
            if log_msg:
                log_event(log_msg)
            self.redirect_admin("/admin?msg=Bakiye+guncellendi&msg_type=success")
        elif path == "/daily/claim":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.send_html(page("Hata", '<section class="auth-card"><h1>403 Forbidden</h1><p class="muted">CSRF doğrulaması başarısız oldu.</p></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 403)
                return
                
            def is_today(t: int) -> bool:
                return datetime.date.fromtimestamp(t) == datetime.date.today()
            def is_yesterday(t: int) -> bool:
                return (datetime.date.today() - datetime.date.fromtimestamp(t)).days == 1
                
            with db() as connection:
                row = connection.execute("SELECT daily_streak, last_daily_claim FROM users WHERE id=?", (current[1],)).fetchone()
                if not row:
                    self.redirect("/daily?msg=Kullanici+bulunamadi&msg_type=error")
                    return
                daily_streak = row["daily_streak"]
                last_daily_claim = row["last_daily_claim"]
                
                if is_today(last_daily_claim):
                    self.redirect("/daily?msg=Bugunun+odulunu+zaten+aldiniz&msg_type=error")
                    return
                    
                if is_yesterday(last_daily_claim) or last_daily_claim == 0:
                    new_streak = daily_streak + 1
                else:
                    new_streak = 1
                    
                rewards = {1: 10.0, 2: 15.0, 3: 20.0, 4: 25.0, 5: 30.0, 6: 35.0, 7: 50.0}
                active_day = ((new_streak - 1) % 7) + 1
                reward_amt = rewards[active_day]

                current_time = int(time.time())
                # Atomic guard: only one concurrent request can claim per day.
                today_start = int(datetime.datetime.combine(datetime.date.today(), datetime.time.min).timestamp())
                cur = connection.execute(
                    "UPDATE users SET balance=balance+?, daily_streak=?, last_daily_claim=? WHERE id=? AND last_daily_claim<?",
                    (reward_amt, new_streak, current_time, current[1], today_start),
                )
                if cur.rowcount == 0:
                    connection.rollback()
                    self.redirect("/daily?msg=Bugunun+odulunu+zaten+aldiniz&msg_type=error")
                    return
                
                log_msg = f"[GÜNLÜK] '{username}' kullanıcısı Gün {active_day} günlük ödülünü aldı: {reward_amt:.2f} TL. (Yeni Seri: {new_streak})"
            log_event(log_msg)
            self.redirect(f"/daily?msg=Tebrikler!+{reward_amt:.0f}+TL+bakiye+hesabiniza+eklendi.&msg_type=success")
        elif path == "/wheel/spin":
            if not current:
                self.redirect("/login")
                return
            if not self.verify_csrf(fields):
                self.redirect("/wheel?msg=CSRF+hatasi&msg_type=error")
                return
            wheel_key = fields.get("wheel", "")
            if wheel_key not in WHEELS:
                self.redirect("/wheel?msg=Gecersiz+cark&msg_type=error")
                return
            w = WHEELS[wheel_key]
            with db() as connection:
                urow = connection.execute("SELECT balance, last_free_spin FROM users WHERE id=?", (current[1],)).fetchone()
                if not urow:
                    self.redirect("/wheel?msg=Kullanici+bulunamadi&msg_type=error")
                    return
                balance = float(urow["balance"])
                last_free = urow["last_free_spin"]
                now = int(time.time())

                if wheel_key == "ucretsiz":
                    if (now - last_free) < FREE_SPIN_COOLDOWN:
                        self.redirect("/wheel?msg=Ucretsiz+cark+icin+beklemelisiniz&msg_type=error")
                        return
                else:
                    if balance < w["cost"]:
                        self.redirect("/wheel?msg=Yetersiz+bakiye&msg_type=error")
                        return

                # Weighted random selection
                seg_idx = random.choices(range(8), weights=w["weights"], k=1)[0]
                reward = w["rewards"][seg_idx]

                if wheel_key == "ucretsiz":
                    # Atomic guard prevents a double-claim race on the cooldown.
                    cur = connection.execute(
                        "UPDATE users SET balance=balance+?, last_free_spin=? WHERE id=? AND ?-last_free_spin>=?",
                        (reward, now, current[1], now, FREE_SPIN_COOLDOWN),
                    )
                    if cur.rowcount == 0:
                        connection.rollback()
                        self.redirect("/wheel?msg=Ucretsiz+cark+icin+beklemelisiniz&msg_type=error")
                        return
                else:
                    net = reward - w["cost"]
                    connection.execute("UPDATE users SET balance=balance+? WHERE id=?", (net, current[1]))
                log_msg = f"[ÇARK] '{username}' {w['name']} çevirdi → {reward} TL kazandı (Segment {seg_idx+1})"
            log_event(log_msg)
            self.redirect(f"/wheel?won={reward}&wh={wheel_key}&seg={seg_idx}")
        else:
            self.send_html(page("Bulunamadı", '<section class="auth-card"><h1>404</h1></section>', username, is_premium=is_premium, csrf_token=csrf_tok), 404)


def main() -> None:
    if not APP_SECRET:
        raise RuntimeError("APP_SECRET env var is required")
    if not ADMIN_PASSWORD:
        print("UYARI: ADMIN_PASSWORD ayarlanmamış. Yönetici paneli girişi devre dışı. "
              "Render dashboard'da ADMIN_PASSWORD ortam değişkenini ayarlayın.")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        probe = DATA_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        raise RuntimeError(
            f"Veri dizini yazılabilir değil: {DATA_DIR} ({exc}). "
            "Render'da kalıcı disk bağlanmamış olabilir; kayıtlar kaybolur."
        ) from exc
    with db():
        pass
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    print(f"Biga Cheat site listening on http://127.0.0.1:{port}")
    print(f"Veritabanı: {'PostgreSQL (DATABASE_URL)' if IS_POSTGRES else DB_PATH}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
