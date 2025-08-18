#!/usr/bin/env python3
# tapify.py — Notcoin-style Telegram Mini App in a single file
# Requirements:
#   pip install flask python-telegram-bot psycopg[binary] python-dotenv
#
# Start:
#   python tapify.py
#
# Environment (.env):
#   BOT_TOKEN=123:ABC...
#   ADMIN_ID=123456789
#   WEBAPP_URL=https://your-service.onrender.com/app
#   DATABASE_URL=postgres://user:pass@host:5432/dbname   # optional; if absent -> SQLite
#   AI_BOOST_LINK=...
#   DAILY_TASK_LINK=...
#   GROUP_LINK=...
#   SITE_LINK=...

import os
import sys
import json
import hmac
import base64
import hashlib
import logging
import threading
import time
import secrets
import typing as t
from urllib.parse import urlparse, parse_qsl, quote_plus

from datetime import datetime, timedelta, timezone, date
from collections import deque, defaultdict

from flask import Flask, request, jsonify, Response

# Optional dotenv
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

# --- Config & Globals ---------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip()
PORT = int(os.getenv("PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

AI_BOOST_LINK = os.getenv("AI_BOOST_LINK", "#")
DAILY_TASK_LINK = os.getenv("DAILY_TASK_LINK", "#")
GROUP_LINK = os.getenv("GROUP_LINK", "#")
SITE_LINK = os.getenv("SITE_LINK", "#")

if not BOT_TOKEN:
    print("ERROR: BOT_TOKEN is required in environment (.env).", file=sys.stderr)

# Logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("tapify")

# --- Database Layer (psycopg v3 if DATABASE_URL else SQLite) ------------------

USE_POSTGRES = False
psycopg = None
conn = None  # type: ignore

import sqlite3

def _connect_sqlite():
    db = sqlite3.connect("tapify.db", check_same_thread=False, isolation_level=None)
    db.row_factory = sqlite3.Row
    return db

def _connect_postgres(url: str):
    global psycopg
    try:
        import psycopg  # type: ignore
        from psycopg.rows import dict_row  # noqa
    except Exception as e:
        log.error("psycopg (v3) is required for Postgres: pip install 'psycopg[binary]'\n%s", e)
        raise
    # Append sslmode=require if not present
    if "sslmode=" not in url:
        if "?" in url:
            url += "&sslmode=require"
        else:
            url += "?sslmode=require"
    return psycopg.connect(url)

def db_execute(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
    else:
        conn.execute(query, params)

def db_fetchone(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if not row:
                return None
            # psycopg default returns tuple; convert with column names
            cols = [desc[0] for desc in cur.description]
            return dict(zip(cols, row))
    else:
        cur = conn.execute(query, params)
        row = cur.fetchone()
        return dict(row) if row else None

def db_fetchall(query: str, params: t.Tuple = ()):
    if USE_POSTGRES:
        with conn.cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
            cols = [desc[0] for desc in cur.description]
            return [dict(zip(cols, r)) for r in rows]
    else:
        cur = conn.execute(query, params)
        rows = cur.fetchall()
        return [dict(r) for r in rows]

def db_init():
    # Core "users" table (compatible with existing setups).
    # We won't drop or overwrite existing columns; we create minimum needed.
    if USE_POSTGRES:
        db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id BIGINT PRIMARY KEY,
            username TEXT,
            payment_status TEXT DEFAULT NULL,
            invites INTEGER DEFAULT 0
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_users (
            chat_id BIGINT PRIMARY KEY,
            coins BIGINT DEFAULT 0,
            energy INT DEFAULT 500,
            max_energy INT DEFAULT 500,
            energy_updated_at TIMESTAMP,
            multitap_until TIMESTAMP,
            autotap_until TIMESTAMP,
            regen_rate_seconds INT DEFAULT 3,
            last_tap_at TIMESTAMP,
            daily_streak INT DEFAULT 0,
            last_streak_at DATE
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_taps (
            id BIGSERIAL PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            ts TIMESTAMP NOT NULL,
            delta INT NOT NULL,
            nonce TEXT NOT NULL
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_referrals (
            referrer BIGINT NOT NULL,
            referee BIGINT NOT NULL,
            created_at TIMESTAMP NOT NULL,
            PRIMARY KEY (referrer, referee)
        );
        """)
    else:
        db_execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            payment_status TEXT DEFAULT NULL,
            invites INTEGER DEFAULT 0
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_users (
            chat_id INTEGER PRIMARY KEY,
            coins INTEGER DEFAULT 0,
            energy INTEGER DEFAULT 500,
            max_energy INTEGER DEFAULT 500,
            energy_updated_at TEXT,
            multitap_until TEXT,
            autotap_until TEXT,
            regen_rate_seconds INTEGER DEFAULT 3,
            last_tap_at TEXT,
            daily_streak INTEGER DEFAULT 0,
            last_streak_at TEXT
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_taps (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            ts TEXT NOT NULL,
            delta INTEGER NOT NULL,
            nonce TEXT NOT NULL
        );
        """)
        db_execute("""
        CREATE TABLE IF NOT EXISTS game_referrals (
            referrer INTEGER NOT NULL,
            referee INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (referrer, referee)
        );
        """)

def db_now() -> datetime:
    # Store UTC
    return datetime.now(timezone.utc)

def db_date_utc() -> date:
    return db_now().date()

def upsert_user_if_missing(chat_id: int, username: str | None):
    existing = db_fetchone("SELECT chat_id FROM users WHERE chat_id = ?", (chat_id,)) if not USE_POSTGRES \
        else db_fetchone("SELECT chat_id FROM users WHERE chat_id = %s", (chat_id,))
    if not existing:
        if USE_POSTGRES:
            db_execute("INSERT INTO users (chat_id, username, payment_status, invites) VALUES (%s,%s,%s,%s)",
                       (chat_id, username, None, 0))
        else:
            db_execute("INSERT INTO users (chat_id, username, payment_status, invites) VALUES (?,?,?,?)",
                       (chat_id, username, None, 0))
    # Also ensure game_users exists
    existing_g = db_fetchone("SELECT chat_id FROM game_users WHERE chat_id = ?", (chat_id,)) if not USE_POSTGRES \
        else db_fetchone("SELECT chat_id FROM game_users WHERE chat_id = %s", (chat_id,))
    if not existing_g:
        now = db_now()
        if USE_POSTGRES:
            db_execute("""INSERT INTO game_users
                (chat_id, coins, energy, max_energy, energy_updated_at, regen_rate_seconds, daily_streak)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                (chat_id, 0, 500, 500, now, 3, 0))
        else:
            db_execute("""INSERT INTO game_users
                (chat_id, coins, energy, max_energy, energy_updated_at, regen_rate_seconds, daily_streak)
                VALUES (?,?,?,?,?,?,?)""",
                (chat_id, 0, 500, 500, now.isoformat(), 3, 0))

def is_registered(chat_id: int) -> bool:
    q = "SELECT payment_status FROM users WHERE chat_id = ?" if not USE_POSTGRES \
        else "SELECT payment_status FROM users WHERE chat_id = %s"
    row = db_fetchone(q, (chat_id,))
    if not row:
        return False
    return (row.get("payment_status") or "").lower() == "registered"

def add_referral_if_absent(referrer: int, referee: int):
    if referrer == referee or referee <= 0:
        return
    q = "SELECT 1 FROM game_referrals WHERE referrer=? AND referee=?" if not USE_POSTGRES \
        else "SELECT 1 FROM game_referrals WHERE referrer=%s AND referee=%s"
    r = db_fetchone(q, (referrer, referee))
    if r:
        return
    now = db_now()
    if USE_POSTGRES:
        db_execute("INSERT INTO game_referrals (referrer, referee, created_at) VALUES (%s,%s,%s)",
                   (referrer, referee, now))
        # optional bookkeeping on user invites
        db_execute("UPDATE users SET invites=COALESCE(invites,0)+1 WHERE chat_id=%s", (referrer,))
    else:
        db_execute("INSERT INTO game_referrals (referrer, referee, created_at) VALUES (?,?,?)",
                   (referrer, referee, now.isoformat()))
        db_execute("UPDATE users SET invites=COALESCE(invites,0)+1 WHERE chat_id=?", (referrer,))

def get_game_user(chat_id: int) -> dict:
    q = "SELECT * FROM game_users WHERE chat_id = ?" if not USE_POSTGRES \
        else "SELECT * FROM game_users WHERE chat_id = %s"
    row = db_fetchone(q, (chat_id,))
    if not row:
        upsert_user_if_missing(chat_id, None)
        row = db_fetchone(q, (chat_id,))
    return row or {}

def update_game_user_fields(chat_id: int, fields: dict):
    # Build dynamic update
    keys = list(fields.keys())
    if not keys:
        return
    if USE_POSTGRES:
        set_clause = ", ".join(f"{k}=%s" for k in keys)
        params = tuple(fields[k] for k in keys) + (chat_id,)
        db_execute(f"UPDATE game_users SET {set_clause} WHERE chat_id=%s", params)
    else:
        set_clause = ", ".join(f"{k}=?" for k in keys)
        params = tuple(fields[k] for k in keys) + (chat_id,)
        db_execute(f"UPDATE game_users SET {set_clause} WHERE chat_id=?", params)

def add_tap(chat_id: int, delta: int, nonce: str):
    now = db_now()
    if USE_POSTGRES:
        db_execute("INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (%s,%s,%s,%s)",
                   (chat_id, now, delta, nonce))
        db_execute("UPDATE game_users SET coins=COALESCE(coins,0)+%s, last_tap_at=%s WHERE chat_id=%s",
                   (delta, now, chat_id))
    else:
        db_execute("INSERT INTO game_taps (chat_id, ts, delta, nonce) VALUES (?,?,?,?)",
                   (chat_id, now.isoformat(), delta, nonce))
        db_execute("UPDATE game_users SET coins=COALESCE(coins,0)+?, last_tap_at=? WHERE chat_id=?",
                   (delta, now.isoformat(), chat_id))

def leaderboard(range_: str = "all", limit: int = 50):
    if range_ == "all":
        q = "SELECT u.username, g.chat_id, g.coins AS score FROM game_users g LEFT JOIN users u ON u.chat_id=g.chat_id ORDER BY score DESC LIMIT "
        q += "%s" if USE_POSTGRES else "?"
        return db_fetchall(q, (limit,))
    else:
        # compute from taps in period
        now = db_now()
        if range_ == "day":
            since = now - timedelta(days=1)
        else:
            since = now - timedelta(days=7)
        if USE_POSTGRES:
            return db_fetchall("""
                SELECT u.username, t.chat_id, COALESCE(SUM(t.delta),0) AS score
                FROM game_taps t
                LEFT JOIN users u ON u.chat_id=t.chat_id
                WHERE t.ts >= %s
                GROUP BY t.chat_id, u.username
                ORDER BY score DESC
                LIMIT %s
            """, (since, limit))
        else:
            return db_fetchall("""
                SELECT u.username, t.chat_id, COALESCE(SUM(t.delta),0) AS score
                FROM game_taps t
                LEFT JOIN users u ON u.chat_id=t.chat_id
                WHERE t.ts >= ?
                GROUP BY t.chat_id, u.username
                ORDER BY score DESC
                LIMIT ?
            """, (since.isoformat(), limit))

# --- Auth: Telegram WebApp initData verification ------------------------------

def _hmac_sha256(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()

def verify_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Per Telegram docs:
      secret_key = HMAC_SHA256(key=bot_token, data=b"WebAppData")
      data_check_string = "\n".join(sorted(["key=value", ...] excluding 'hash'))
      calc_hash = hex(HMAC_SHA256(key=secret_key, data=data_check_string.encode()))
    """
    try:
        items = dict(parse_qsl(init_data, strict_parsing=True))
        provided_hash = items.pop("hash", "")
        # Build data_check_string
        pairs = []
        for k in sorted(items.keys()):
            pairs.append(f"{k}={items[k]}")
        data_check_string = "\n".join(pairs).encode("utf-8")
        secret_key = _hmac_sha256(bot_token.encode("utf-8"), b"WebAppData")
        calc_hash = hmac.new(secret_key, data_check_string, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc_hash, provided_hash):
            return None
        # Parse "user" JSON if present
        user_payload = {}
        if "user" in items:
            user_payload = json.loads(items["user"])
        return {
            "ok": True,
            "user": user_payload,
            "query": items
        }
    except Exception as e:
        log.warning("verify_init_data error: %s", e)
        return None

# --- Game Mechanics: energy, rate-limits, boosts, streaks ---------------------

MAX_TAPS_PER_SEC = 20  # anti-spam
RATE_WINDOW_SEC = 1.0

_rate_windows: dict[int, deque[float]] = defaultdict(lambda: deque(maxlen=MAX_TAPS_PER_SEC * 3))
_recent_nonces: dict[int, set[str]] = defaultdict(set)

def _clean_old_nonces(chat_id: int):
    # lightweight: keep only up to 200 recent nonces in memory
    s = _recent_nonces[chat_id]
    if len(s) > 200:
        # reset to keep memory bound
        _recent_nonces[chat_id] = set(list(s)[-100:])

def can_tap_now(chat_id: int) -> bool:
    now = time.monotonic()
    dq = _rate_windows[chat_id]
    # Remove entries older than 1s
    while dq and now - dq[0] > RATE_WINDOW_SEC:
        dq.popleft()
    if len(dq) >= MAX_TAPS_PER_SEC:
        return False
    dq.append(now)
    return True

def compute_energy(user_row: dict) -> tuple[int, datetime]:
    """
    Returns (available_energy, new_energy_updated_at)
    """
    max_energy = int(user_row.get("max_energy") or 500)
    regen_rate_seconds = int(user_row.get("regen_rate_seconds") or 3)

    raw = user_row.get("energy_updated_at")
    if isinstance(raw, str):
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            last = db_now()
    else:
        last = raw or db_now()
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)

    stored_energy = int(user_row.get("energy") or 0)
    now = db_now()
    elapsed = int((now - last).total_seconds())
    regen = elapsed // max(1, regen_rate_seconds)
    energy = min(max_energy, stored_energy + regen)
    # If regenerated, move the timestamp forward by regen*regen_rate_seconds
    if regen > 0:
        last = last + timedelta(seconds=regen * regen_rate_seconds)
    return energy, last

def streak_update(gu: dict, tapped_today: bool) -> tuple[int, date]:
    today = db_date_utc()
    last_str = gu.get("last_streak_at")
    last_date: date | None = None
    if isinstance(last_str, str) and last_str:
        try:
            last_date = datetime.fromisoformat(last_str).date()
        except Exception:
            last_date = None
    elif isinstance(last_str, datetime):
        last_date = last_str.date()
    elif isinstance(last_str, date):
        last_date = last_str

    streak = int(gu.get("daily_streak") or 0)
    if not tapped_today:
        return streak, last_date or today

    if last_date == today - timedelta(days=1):
        streak += 1
    elif last_date == today:
        pass  # already accounted today
    else:
        streak = 1
    return streak, today

def boost_multiplier(gu: dict) -> int:
    mult = 1
    # MultiTap
    mt = gu.get("multitap_until")
    at = gu.get("autotap_until")
    now = db_now()
    if isinstance(mt, str) and mt:
        try: mt = datetime.fromisoformat(mt)
        except: mt = None
    if isinstance(at, str) and at:
        try: at = datetime.fromisoformat(at)
        except: at = None
    if isinstance(mt, datetime) and mt.replace(tzinfo=timezone.utc) > now:
        mult = max(mult, 2)
    if isinstance(at, datetime) and at.replace(tzinfo=timezone.utc) > now:
        mult = max(mult, 2)
    return mult

def activate_boost(chat_id: int, boost: str) -> tuple[bool, str]:
    gu = get_game_user(chat_id)
    coins = int(gu.get("coins") or 0)
    now = db_now()
    cost = 0
    field = None
    duration = timedelta(minutes=15)

    if boost == "multitap":
        cost = 1000
        field = "multitap_until"
        duration = timedelta(minutes=15)
    elif boost == "autotap":
        cost = 3000
        field = "autotap_until"
        duration = timedelta(minutes=10)
    elif boost == "maxenergy":
        cost = 2500
        # one-time upgrade
        if coins < cost:
            return False, "Not enough coins"
        update_game_user_fields(chat_id, {
            "coins": coins - cost,
            "max_energy": int(gu.get("max_energy") or 500) + 100
        })
        return True, "Max energy increased by +100!"
    else:
        return False, "Unknown boost"

    if coins < cost:
        return False, "Not enough coins"
    until = now + duration
    update_game_user_fields(chat_id, {
        "coins": coins - cost,
        field: until
    })
    return True, f"{boost} activated!"

# --- Flask App ----------------------------------------------------------------

flask_app = Flask(__name__)

INDEX_HEALTH = "Tapify is alive!"

WEBAPP_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Tapify</title>
  <script src="https://telegram.org/js/telegram-web-app.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background: radial-gradient(1200px 600px at 50% -100px, rgba(255,215,0,0.12), transparent 70%), #0b0f14; }
    .coin {
      width: 220px; height: 220px; border-radius: 50%;
      background: radial-gradient(circle at 30% 30%, #ffd700, #b8860b);
      box-shadow: 0 0 30px rgba(255,215,0,0.4), inset 0 0 20px rgba(255,255,255,0.2);
      transition: transform .07s ease, box-shadow .2s ease;
    }
    .coin:active { transform: scale(0.96); box-shadow: 0 0 10px rgba(255,215,0,0.6), inset 0 0 30px rgba(255,255,255,0.3); }
    .glow { filter: drop-shadow(0 0 15px rgba(255,215,0,0.4)); }
    .tab { opacity: .5; }
    .tab.active { opacity: 1; border-bottom: 2px solid #ffd700; }
    .lock { filter: grayscale(0.6); }
  </style>
</head>
<body class="text-white">
  <div id="root" class="max-w-sm mx-auto px-4 pt-6 pb-24">
    <div class="flex items-center justify-between">
      <div class="text-xl font-semibold">Tapify</div>
      <div id="streak" class="text-sm opacity-80">🔥 Streak: 0</div>
    </div>

    <div id="locked" class="hidden mt-10 text-center">
      <div class="text-3xl font-bold mb-3">Access Locked</div>
      <div class="opacity-80 mb-6">Complete registration in the bot to start playing.</div>
      <button id="btnCheck" class="px-4 py-2 rounded-lg bg-yellow-500 text-black font-semibold">Check again</button>
      <div class="mt-6 text-xs opacity-60">If this persists, close and reopen the webapp.</div>
    </div>

    <div id="game" class="mt-8">
      <div class="flex items-center justify-center">
        <div id="energyRing" class="relative glow">
          <div id="tapBtn" class="coin select-none flex items-center justify-center text-3xl font-extrabold">TAP</div>
        </div>
      </div>
      <div class="mt-6 text-center">
        <div id="balance" class="text-4xl font-bold">0</div>
        <div id="energy" class="mt-1 text-sm opacity-80">⚡ 0 / 0</div>
      </div>

      <div class="mt-8 grid grid-cols-4 gap-2 text-center text-sm">
        <button class="tab active py-2" data-tab="play">Play</button>
        <button class="tab py-2" data-tab="boosts">Boosts</button>
        <button class="tab py-2" data-tab="board">Leaderboard</button>
        <button class="tab py-2" data-tab="refer">Refer</button>
      </div>

      <div id="panelPlay" class="mt-6"></div>

      <div id="panelBoosts" class="hidden mt-6 space-y-3">
        <div class="bg-white/5 p-4 rounded-xl">
          <div class="font-semibold">MultiTap x2 (15m)</div>
          <div class="text-xs opacity-70 mb-2">Cost: 1000</div>
          <button data-boost="multitap" class="boostBtn px-3 py-2 rounded-lg bg-yellow-500 text-black">Activate</button>
        </div>
        <div class="bg-white/5 p-4 rounded-xl">
          <div class="font-semibold">AutoTap (10m)</div>
          <div class="text-xs opacity-70 mb-2">Cost: 3000</div>
          <button data-boost="autotap" class="boostBtn px-3 py-2 rounded-lg bg-yellow-500 text-black">Activate</button>
        </div>
        <div class="bg-white/5 p-4 rounded-xl">
          <div class="font-semibold">Increase Max Energy +100</div>
          <div class="text-xs opacity-70 mb-2">Cost: 2500</div>
          <button data-boost="maxenergy" class="boostBtn px-3 py-2 rounded-lg bg-yellow-500 text-black">Upgrade</button>
        </div>
      </div>

      <div id="panelBoard" class="hidden mt-6">
        <div class="flex gap-2 text-sm">
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="day">Today</button>
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="week">This Week</button>
          <button class="lbBtn px-3 py-1 rounded bg-white/10" data-range="all">All Time</button>
        </div>
        <ol id="lbList" class="mt-4 space-y-2"></ol>
      </div>

      <div id="panelRefer" class="hidden mt-6">
        <div class="bg-white/5 p-4 rounded-xl">
          <div class="font-semibold mb-1">Invite friends</div>
          <div class="text-xs opacity-70 mb-2">Share your link to earn bonuses.</div>
          <input id="refLink" class="w-full px-3 py-2 rounded bg-black/30 border border-white/10" readonly />
          <button id="copyRef" class="mt-2 px-3 py-2 rounded bg-yellow-500 text-black">Copy link</button>
        </div>
        <div class="mt-4 grid grid-cols-2 gap-3 text-sm">
          <a href="#" id="aiLink" class="text-center bg-white/5 p-3 rounded-lg">AI Boost Task</a>
          <a href="#" id="dailyLink" class="text-center bg-white/5 p-3 rounded-lg">Daily Task</a>
          <a href="#" id="groupLink" class="text-center bg-white/5 p-3 rounded-lg">Join Group</a>
          <a href="#" id="siteLink" class="text-center bg-white/5 p-3 rounded-lg">Visit Site</a>
        </div>
      </div>
    </div>
  </div>

<script>
const tg = window.Telegram?.WebApp;
if (tg) tg.expand();

const $ = (q) => document.querySelector(q);
const $$ = (q) => Array.from(document.querySelectorAll(q));

function setTab(name) {
  $$(".tab").forEach(b=>{
    b.classList.toggle("active", b.dataset.tab===name);
  });
  $("#panelPlay").classList.toggle("hidden", name!=="play");
  $("#panelBoosts").classList.toggle("hidden", name!=="boosts");
  $("#panelBoard").classList.toggle("hidden", name!=="board");
  $("#panelRefer").classList.toggle("hidden", name!=="refer");
}
$$(".tab").forEach(b => b.addEventListener("click", ()=> setTab(b.dataset.tab)));

const haptics = (type="light") => {
  try { tg?.HapticFeedback?.impactOccurred(type); } catch(e){}
};

let USER = null;
let LOCKED = false;
let RANGE = "all";

async function api(path, body) {
  const initData = tg?.initData || "";
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type":"application/json" },
    body: JSON.stringify(Object.assign({initData}, body||{}))
  });
  return await res.json();
}

async function resolveAuth() {
  const out = await api("/api/auth/resolve");
  if (!out.ok) {
    $("#locked").classList.remove("hidden");
    $("#game").classList.add("lock");
    LOCKED = true;
    return;
  }
  USER = out.user;
  LOCKED = !out.allowed;
  $("#locked").classList.toggle("hidden", out.allowed);
  $("#game").classList.toggle("lock", !out.allowed);
  $("#refLink").value = out.refLink;
  $("#aiLink").href = out.aiLink;
  $("#dailyLink").href = out.dailyLink;
  $("#groupLink").href = out.groupLink;
  $("#siteLink").href = out.siteLink;
  if (out.allowed) await refreshState();
}

$("#btnCheck").addEventListener("click", resolveAuth);

async function refreshState() {
  const out = await api("/api/state");
  if (!out.ok) return;
  $("#balance").textContent = out.coins;
  $("#energy").textContent = `⚡ ${out.energy} / ${out.max_energy}`;
  $("#streak").textContent = `🔥 Streak: ${out.daily_streak||0}`;
}

async function doTap() {
  if (LOCKED) return;
  const nonce = btoa(String.fromCharCode(...crypto.getRandomValues(new Uint8Array(12))));
  const out = await api("/api/tap", { nonce });
  if (!out.ok) {
    if (out.error) console.log(out.error);
    return;
  }
  haptics("light");
  $("#balance").textContent = out.coins;
  $("#energy").textContent = `⚡ ${out.energy} / ${out.max_energy}`;
}

$("#tapBtn").addEventListener("click", doTap);

$$(".boostBtn").forEach(b=>{
  b.addEventListener("click", async ()=>{
    const out = await api("/api/boost", { name: b.dataset.boost });
    if (out.ok) { await refreshState(); haptics("medium"); }
    else if (out.error) alert(out.error);
  });
});

$$(".lbBtn").forEach(b=>{
  b.addEventListener("click", async ()=>{
    RANGE = b.dataset.range;
    const q = await fetch(`/api/leaderboard?range=${RANGE}`);
    const data = await q.json();
    const list = $("#lbList"); list.innerHTML = "";
    (data.items || []).forEach((r, i)=>{
      const li = document.createElement("li");
      li.className = "flex justify-between bg-white/5 px-3 py-2 rounded-lg";
      li.innerHTML = `<div>#${i+1} @${r.username||r.chat_id}</div><div>${r.score}</div>`;
      list.appendChild(li);
    });
  });
});

$("#copyRef").addEventListener("click", ()=>{
  navigator.clipboard.writeText($("#refLink").value);
  haptics("light");
});

setTab("play");
resolveAuth();
setInterval(refreshState, 4000);
</script>
</body>
</html>
"""

@flask_app.get("/")
def health():
    return Response(INDEX_HEALTH, mimetype="text/plain")

@flask_app.get("/app")
def app_page():
    return Response(WEBAPP_HTML, mimetype="text/html")

def _resolve_user_from_init(init_data: str) -> tuple[bool, dict | None, str]:
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return False, None, "Invalid auth"
    tg_user = auth["user"]
    chat_id = int(tg_user.get("id"))
    username = tg_user.get("username")
    upsert_user_if_missing(chat_id, username)
    return True, {"chat_id": chat_id, "username": username}, ""

@flask_app.post("/api/auth/resolve")
def api_auth_resolve():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    ok, user, err = _resolve_user_from_init(init_data)
    if not ok:
        return jsonify({"ok": False, "error": err})
    chat_id = user["chat_id"]
    allowed = is_registered(chat_id)
    # Build referral link
    # NOTE: client will embed in UI
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}" if BOT_USERNAME else ""
    return jsonify({
        "ok": True,
        "user": user,
        "allowed": allowed,
        "refLink": ref_link,
        "aiLink": AI_BOOST_LINK,
        "dailyLink": DAILY_TASK_LINK,
        "groupLink": GROUP_LINK,
        "siteLink": SITE_LINK,
    })

@flask_app.post("/api/state")
def api_state():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    gu = get_game_user(chat_id)
    energy, energy_ts = compute_energy(gu)
    out = {
        "ok": True,
        "coins": int(gu.get("coins") or 0),
        "energy": energy,
        "max_energy": int(gu.get("max_energy") or 500),
        "daily_streak": int(gu.get("daily_streak") or 0),
    }
    # persist recomputed energy timestamp/amount
    update_game_user_fields(chat_id, {"energy": energy, "energy_updated_at": energy_ts})
    return jsonify(out)

@flask_app.post("/api/boost")
def api_boost():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    name = data.get("name", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    ok, msg = activate_boost(chat_id, name)
    return jsonify({"ok": ok, "error": None if ok else msg})

@flask_app.post("/api/tap")
def api_tap():
    data = request.get_json(silent=True) or {}
    init_data = data.get("initData", "")
    nonce = data.get("nonce", "")
    auth = verify_init_data(init_data, BOT_TOKEN)
    if not auth or not auth.get("user"):
        return jsonify({"ok": False, "error": "Invalid auth"})
    chat_id = int(auth["user"]["id"])
    if not is_registered(chat_id):
        return jsonify({"ok": False, "error": "Not registered"})
    if not can_tap_now(chat_id):
        return jsonify({"ok": False, "error": "Rate limited"})

    if not nonce or len(nonce) > 200:
        return jsonify({"ok": False, "error": "Bad nonce"})
    if nonce in _recent_nonces[chat_id]:
        return jsonify({"ok": False, "error": "Replay blocked"})
    _recent_nonces[chat_id].add(nonce)
    _clean_old_nonces(chat_id)

    gu = get_game_user(chat_id)
    energy, energy_ts = compute_energy(gu)
    if energy < 1:
        return jsonify({"ok": False, "error": "No energy", "coins": int(gu.get("coins") or 0),
                        "energy": energy, "max_energy": int(gu.get("max_energy") or 500)})
    mult = boost_multiplier(gu)
    delta = 1 * mult
    add_tap(chat_id, delta, nonce)
    # consume 1 energy
    update_game_user_fields(chat_id, {"energy": energy - 1, "energy_updated_at": energy_ts})
    # streak handling
    new_streak, streak_date = streak_update(gu, tapped_today=True)
    update_game_user_fields(chat_id, {"daily_streak": new_streak, "last_streak_at": streak_date})
    gu2 = get_game_user(chat_id)
    energy2, _ = compute_energy(gu2)
    return jsonify({
        "ok": True,
        "coins": int(gu2.get("coins") or 0),
        "energy": energy2,
        "max_energy": int(gu2.get("max_energy") or 500),
    })

@flask_app.get("/api/leaderboard")
def api_leaderboard():
    rng = request.args.get("range", "all")
    if rng not in ("day", "week", "all"):
        rng = "all"
    items = leaderboard(rng, 50)
    return jsonify({"ok": True, "items": items})

# --- Telegram Bot -------------------------------------------------------------

from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup, WebAppInfo,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
)

BOT_USERNAME = ""  # populated on startup

def deep_link_ref(chat_id: int) -> str:
    if not BOT_USERNAME:
        return ""
    return f"https://t.me/{BOT_USERNAME}?start=ref_{chat_id}"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat_id = user.id
    username = user.username or ""
    upsert_user_if_missing(chat_id, username)

    # Handle referral code
    args = context.args or []
    if args and len(args) >= 1 and args[0].startswith("ref_"):
        try:
            referrer = int(args[0][4:])
            add_referral_if_absent(referrer, chat_id)
        except Exception:
            pass

    kb = [[KeyboardButton(text="Open Game", web_app=WebAppInfo(url=WEBAPP_URL))]]
    text = (
        "Welcome to <b>Tapify</b>!\n\n"
        "Tap to earn coins, activate boosts, climb the leaderboard.\n"
        "If you haven't completed registration yet, please do so in the bot.\n\n"
        f"<i>WebApp:</i> {WEBAPP_URL}"
    )
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True)
    )

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_user.id
    gu = get_game_user(chat_id)
    energy, _ = compute_energy(gu)
    text = (
        f"👤 <b>You</b>\n"
        f"Coins: <b>{int(gu.get('coins') or 0)}</b>\n"
        f"Energy: <b>{energy}/{int(gu.get('max_energy') or 500)}</b>\n"
        f"Streak: <b>{int(gu.get('daily_streak') or 0)}</b>\n"
        f"Referral link: {deep_link_ref(chat_id)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

def _is_admin(update: Update) -> bool:
    return update.effective_user and update.effective_user.id == ADMIN_ID

async def cmd_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return
    msg = " ".join(context.args)
    # get all players who have a game_users row
    rows = db_fetchall("SELECT chat_id FROM game_users", ())
    sent = 0
    for r in rows:
        try:
            await context.bot.send_message(chat_id=r["chat_id"], text=msg)
            sent += 1
        except Exception:
            pass
    await update.message.reply_text(f"Broadcast sent to {sent} players.")

async def cmd_setcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setcoins <chat_id> <amount>")
        return
    try:
        cid = int(context.args[0]); amount = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid numbers.")
        return
    update_game_user_fields(cid, {"coins": amount})
    await update.message.reply_text("OK")

async def cmd_addcoins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /addcoins <chat_id> <delta>")
        return
    try:
        cid = int(context.args[0]); delta = int(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid numbers.")
        return
    gu = get_game_user(cid)
    coins = int(gu.get("coins") or 0) + delta
    update_game_user_fields(cid, {"coins": coins})
    await update.message.reply_text("OK")

async def cmd_leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    top = leaderboard("all", 10)
    lines = []
    for i, r in enumerate(top, start=1):
        lines.append(f"{i}. @{r.get('username') or r.get('chat_id')} — {r.get('score')}")
    await update.message.reply_text("🏆 Top 10 (all time)\n" + "\n".join(lines))

async def fallback_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Provide a quick open link
    await update.message.reply_text(f"Open the game here:\n{WEBAPP_URL}")

# --- Startup orchestration ----------------------------------------------------

def start_flask():
    log.info("Starting Flask on 0.0.0.0:%s", PORT)
    flask_app.run(host="0.0.0.0", port=PORT, use_reloader=False, threaded=True)

async def start_bot():
    global BOT_USERNAME
    application = Application.builder().token(BOT_TOKEN).build()
    me = await application.bot.get_me()
    BOT_USERNAME = me.username
    log.info("Bot username: @%s", BOT_USERNAME)

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("me", cmd_me))
    application.add_handler(CommandHandler("broadcast", cmd_broadcast))
    application.add_handler(CommandHandler("setcoins", cmd_setcoins))
    application.add_handler(CommandHandler("addcoins", cmd_addcoins))
    application.add_handler(CommandHandler("leaderboard", cmd_leaderboard))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, fallback_text))

    log.info("Starting bot polling...")
    await application.initialize()
    await application.start()
    await application.updater.start_polling()
    # Keep running until process exit
    await application.updater.wait()

def print_checklist():
    print("=== Tapify Startup Checklist ===")
    print(f"BOT_TOKEN: {'OK' if BOT_TOKEN else 'MISSING'}")
    print(f"ADMIN_ID:  {ADMIN_ID if ADMIN_ID else 'MISSING'}")
    print(f"DB:        {'Postgres' if USE_POSTGRES else 'SQLite'}")
    print(f"WEBAPP:    {WEBAPP_URL or '(derive from host)'}")
    print("================================")

def main():
    global conn, USE_POSTGRES
    # DB connect
    try:
        if DATABASE_URL:
            conn_pg = _connect_postgres(DATABASE_URL)
            conn_pg.autocommit = True
            conn = conn_pg
            USE_POSTGRES = True
            log.info("Connected to Postgres")
        else:
            conn = _connect_sqlite()
            log.info("Connected to SQLite")
    except Exception as e:
        log.error("Database connection failed: %s", e)
        sys.exit(1)

    # Create tables
    db_init()

    print_checklist()

    # Start Flask in a thread (Render health checks hit this)
    th = threading.Thread(target=start_flask, daemon=True)
    th.start()

    # Start Telegram bot (async)
    import asyncio
    try:
        asyncio.run(start_bot())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()