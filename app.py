#!/usr/bin/env python3
from __future__ import annotations

import math
import os
import ssl
import sys
import threading
import time
import uuid
import webbrowser
from collections import defaultdict
from datetime import date, datetime, time as dtime, timedelta
from typing import Any

from dotenv import load_dotenv
from flask import Flask, jsonify, request
import paho.mqtt.client as mqtt


def _exe_dir() -> str:
    """Dossier où chercher les fichiers qui accompagnent l'application.

    Une fois empaqueté en .exe (PyInstaller --onefile), __file__ pointe vers
    un dossier temporaire recréé à chaque lancement ; sys.executable, lui,
    reste stable et pointe vers le vrai .exe.
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


# Identifiants chargés depuis un fichier .env (jamais commité dans git, voir
# .env.example) plutôt qu'écrits en clair dans le code source.
load_dotenv(os.path.join(_exe_dir(), ".env"))


# ============================================================
# CONFIGURATION À MODIFIER
# ============================================================

MQTT_HOST = "808e8e92302944fba38103e4683967ce.s1.eu.hivemq.cloud"
MQTT_PORT = 8883

MQTT_USERNAME = os.environ.get("MQTT_USERNAME", "")
MQTT_PASSWORD = os.environ.get("MQTT_PASSWORD", "")

DEVICE_ID = "esp32-weather-01"

# Au-delà de ce délai, la dernière mesure stockée localement est jugée trop
# vieille et on interroge l'ESP32 pour compléter les données manquantes.
FRESHNESS_SECONDS = 70 * 60

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 5000


def _app_data_dir() -> str:
    """Dossier où stocker les données persistantes.

    Une fois empaqueté en .exe (PyInstaller --onefile), __file__ pointe vers
    un dossier temporaire recréé à chaque lancement : il ne faut donc pas y
    stocker la base SQLite, sous peine de perdre l'historique à chaque
    redémarrage. On utilise alors le dossier de données de l'utilisateur.
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
        data_dir = os.path.join(base, "ClimatInterieur")
        os.makedirs(data_dir, exist_ok=True)
        return data_dir
    return os.path.dirname(os.path.abspath(__file__))


DB_PATH = os.path.join(_app_data_dir(), "climate_history.db")


app = Flask(__name__)


# ============================================================
# STOCKAGE PERSISTANT (SQLite)
# ============================================================
#
# Les mesures passées ne changent plus une fois enregistrées : on les garde
# durablement sur disque et on ne réinterroge l'ESP32 que pour la portion
# manquante (l'historique plus ancien que ce qu'on a déjà, ou les échantillons
# récents pas encore vus), au lieu de retélécharger tout l'historique à
# chaque requête.

import sqlite3

_fetch_lock = threading.Lock()


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema() -> None:
    with _db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS records (
                epoch_utc INTEGER PRIMARY KEY,
                temperature_c REAL NOT NULL,
                humidity_percent REAL NOT NULL
            )
            """
        )


_ensure_schema()


def _store_records(records: list[dict[str, Any]]) -> None:
    if not records:
        return
    with _db() as conn:
        conn.executemany(
            "INSERT OR IGNORE INTO records (epoch_utc, temperature_c, humidity_percent) "
            "VALUES (:epoch_utc, :temperature_c, :humidity_percent)",
            records,
        )


def _stored_bounds() -> tuple[int | None, int | None]:
    with _db() as conn:
        row = conn.execute("SELECT MIN(epoch_utc), MAX(epoch_utc) FROM records").fetchone()
    return (row[0], row[1]) if row else (None, None)


def _query_range(start_epoch: int, end_epoch: int | None = None) -> list[dict[str, Any]]:
    sql = "SELECT epoch_utc, temperature_c, humidity_percent FROM records WHERE epoch_utc >= ?"
    params: list[Any] = [start_epoch]
    if end_epoch is not None:
        sql += " AND epoch_utc < ?"
        params.append(end_epoch)
    sql += " ORDER BY epoch_utc"

    with _db() as conn:
        rows = conn.execute(sql, params).fetchall()

    return [dict(row) for row in rows]


def get_history(days: int) -> list[dict[str, Any]]:
    """Renvoie l'historique demandé, en n'interrogeant l'ESP32 que pour ce
    qui manque réellement en local (données plus anciennes jamais vues, ou
    échantillons récents pas encore mémorisés)."""

    now = int(time.time())
    needed_start = now - days * 86400

    with _fetch_lock:
        oldest_stored, newest_stored = _stored_bounds()

        need_backfill = oldest_stored is None or oldest_stored > needed_start
        need_refresh = newest_stored is None or (now - newest_stored) > FRESHNESS_SECONDS

        if need_backfill:
            # On n'a jamais couvert cette profondeur d'historique : il faut
            # demander la fenêtre complète à l'ESP32. Les mesures déjà
            # connues seront simplement ignorées (INSERT OR IGNORE) à la
            # fusion, seules les nouvelles sont réellement ajoutées.
            _store_records(fetch_history(days))
        elif need_refresh:
            # L'historique ancien est déjà couvert : on ne redemande que le
            # petit créneau récent qui manque encore.
            gap_days = max(1, math.ceil((now - newest_stored) / 86400) + 1)
            _store_records(fetch_history(min(gap_days, days)))
        # Sinon : tout ce qu'il faut est déjà en local, aucun appel MQTT.

    return _query_range(needed_start)


def get_day_records(target_date: date) -> list[dict[str, Any]]:
    start_local = datetime.combine(target_date, dtime.min).astimezone()
    end_local = start_local + timedelta(days=1)
    start_epoch = int(start_local.timestamp())
    end_epoch = int(end_local.timestamp())

    today_local = datetime.now().astimezone().date()
    days_needed = max(1, (today_local - target_date).days + 1)

    get_history(days_needed)  # garantit une couverture locale suffisante

    return _query_range(start_epoch, end_epoch)


# ============================================================
# PAGE HTML
# ============================================================

PAGE_HTML = r"""<!doctype html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Climat intérieur</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.9/dist/chart.umd.min.js"></script>

  <style>
    :root {
      color-scheme: light;
      --page: #f9f9f7;
      --surface: #fcfcfb;
      --surface-2: #f2f1ed;
      --ink: #0b0b0b;
      --ink-2: #52514e;
      --muted: #898781;
      --grid: #e1e0d9;
      --axis: #c3c2b7;
      --border: rgba(11, 11, 11, 0.10);
      --border-strong: rgba(11, 11, 11, 0.16);

      --temp: #eb6834;
      --temp-soft: rgba(235, 104, 52, 0.14);
      --temp-strong: #b8451a;

      --humidity: #2a78d6;
      --humidity-soft: rgba(42, 120, 214, 0.14);
      --humidity-strong: #184f95;

      --good: #0ca30c;
      --critical: #d03b3b;

      --shadow: 0 12px 32px rgba(11, 11, 11, 0.06);
      --radius-xl: 22px;
      --radius-lg: 16px;
      --radius-md: 12px;
    }

    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) {
        color-scheme: dark;
        --page: #0d0d0d;
        --surface: #1a1a19;
        --surface-2: #202020;
        --ink: #ffffff;
        --ink-2: #c3c2b7;
        --muted: #898781;
        --grid: #2c2c2a;
        --axis: #383835;
        --border: rgba(255, 255, 255, 0.10);
        --border-strong: rgba(255, 255, 255, 0.18);

        --temp: #d95926;
        --temp-soft: rgba(217, 89, 38, 0.20);
        --temp-strong: #f0975f;

        --humidity: #3987e5;
        --humidity-soft: rgba(57, 135, 229, 0.20);
        --humidity-strong: #86b6ef;

        --shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
      }
    }

    :root[data-theme="dark"] {
      color-scheme: dark;
      --page: #0d0d0d;
      --surface: #1a1a19;
      --surface-2: #202020;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --muted: #898781;
      --grid: #2c2c2a;
      --axis: #383835;
      --border: rgba(255, 255, 255, 0.10);
      --border-strong: rgba(255, 255, 255, 0.18);

      --temp: #d95926;
      --temp-soft: rgba(217, 89, 38, 0.20);
      --temp-strong: #f0975f;

      --humidity: #3987e5;
      --humidity-soft: rgba(57, 135, 229, 0.20);
      --humidity-strong: #86b6ef;

      --shadow: 0 12px 32px rgba(0, 0, 0, 0.35);
    }

    * { box-sizing: border-box; }

    html, body {
      margin: 0;
      min-height: 100%;
      background: var(--page);
      color: var(--ink);
    }

    body {
      padding: 28px 20px 48px;
      font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    }

    .page {
      width: min(1180px, 100%);
      margin: 0 auto;
    }

    a, button, input { font-family: inherit; }

    /* ---------- header ---------- */

    .hero {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 18px;
      margin-bottom: 20px;
    }

    .eyebrow {
      margin: 0 0 8px;
      font-size: 12px;
      font-weight: 700;
      letter-spacing: .14em;
      text-transform: uppercase;
      color: var(--muted);
    }

    h1 {
      margin: 0;
      font-size: clamp(30px, 4.4vw, 42px);
      line-height: 1.05;
      letter-spacing: -0.03em;
      font-weight: 700;
    }

    .hero-right {
      display: flex;
      align-items: center;
      gap: 10px;
    }

    .status-chip {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 8px 13px;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface);
      font-size: 12px;
      font-weight: 700;
      color: var(--ink-2);
    }

    .status-dot {
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: var(--good);
      box-shadow: 0 0 0 4px rgba(12, 163, 12, 0.15);
    }

    .status-chip.offline .status-dot {
      background: var(--critical);
      box-shadow: 0 0 0 4px rgba(208, 59, 59, 0.15);
    }

    .theme-toggle {
      appearance: none;
      width: 36px;
      height: 36px;
      display: grid;
      place-items: center;
      border-radius: 999px;
      border: 1px solid var(--border);
      background: var(--surface);
      color: var(--ink-2);
      cursor: pointer;
    }

    .theme-toggle:hover { border-color: var(--border-strong); }
    .theme-toggle svg { width: 17px; height: 17px; }
    .theme-toggle .icon-moon { display: none; }
    :root[data-theme="dark"] .theme-toggle .icon-sun { display: none; }
    :root[data-theme="dark"] .theme-toggle .icon-moon { display: block; }

    @media (prefers-color-scheme: dark) {
      :root:not([data-theme="light"]) .theme-toggle .icon-sun { display: none; }
      :root:not([data-theme="light"]) .theme-toggle .icon-moon { display: block; }
    }

    /* ---------- controls ---------- */

    .controls-bar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      margin-bottom: 18px;
      padding: 10px;
      border-radius: var(--radius-xl);
      background: var(--surface);
      border: 1px solid var(--border);
    }

    .pill-group {
      display: inline-flex;
      gap: 4px;
      padding: 3px;
      border-radius: 999px;
      background: var(--surface-2);
      flex-wrap: wrap;
    }

    .pill {
      appearance: none;
      border: none;
      border-radius: 999px;
      background: transparent;
      color: var(--ink-2);
      padding: 8px 14px;
      font-size: 13px;
      font-weight: 700;
      cursor: pointer;
      transition: background .15s ease, color .15s ease;
    }

    .pill:hover { color: var(--ink); }

    .pill.active {
      background: var(--ink);
      color: var(--page);
    }

    .day-picker-field {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 3px 4px 3px 12px;
      border-radius: 999px;
      background: var(--surface-2);
    }

    .day-picker-field label {
      font-size: 13px;
      font-weight: 700;
      color: var(--ink-2);
    }

    .day-picker-field input[type="date"] {
      appearance: none;
      border: none;
      border-radius: 999px;
      background: var(--surface);
      color: var(--ink);
      padding: 6px 10px;
      font-size: 13px;
      font-weight: 600;
    }

    .day-nav-btn {
      appearance: none;
      width: 30px;
      height: 30px;
      display: grid;
      place-items: center;
      border-radius: 50%;
      border: none;
      background: var(--surface);
      color: var(--ink-2);
      cursor: pointer;
      font-size: 15px;
      font-weight: 700;
      line-height: 1;
    }

    .day-nav-btn:hover { color: var(--ink); }
    .day-nav-btn:disabled { opacity: .35; cursor: not-allowed; }

    [hidden] { display: none !important; }

    /* ---------- stat tiles ---------- */

    .stats-row {
      display: grid;
      grid-template-columns: repeat(4, 1fr);
      gap: 12px;
      margin-bottom: 18px;
    }

    .stat-tile {
      padding: 16px 18px;
      border-radius: var(--radius-lg);
      background: var(--surface);
      border: 1px solid var(--border);
    }

    .stat-tile-label {
      font-size: 11px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: var(--muted);
    }

    .stat-tile-value {
      margin-top: 8px;
      font-size: 26px;
      font-weight: 700;
      letter-spacing: -0.02em;
      font-variant-numeric: tabular-nums;
    }

    .stat-tile-value .unit {
      font-size: 14px;
      font-weight: 600;
      color: var(--muted);
      margin-left: 2px;
    }

    .stat-tile.accent-temp .stat-tile-value { color: var(--temp); }
    .stat-tile.accent-humidity .stat-tile-value { color: var(--humidity); }

    /* ---------- chart cards ---------- */

    .charts {
      display: flex;
      flex-direction: column;
      gap: 14px;
    }

    .chart-card {
      padding: 22px;
      border-radius: var(--radius-xl);
      background: var(--surface);
      border: 1px solid var(--border);
      box-shadow: var(--shadow);
    }

    .chart-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      margin-bottom: 14px;
    }

    .chart-title-group { display: flex; align-items: center; gap: 10px; }

    .chart-swatch {
      width: 10px;
      height: 10px;
      border-radius: 3px;
    }

    .chart-swatch.temp { background: var(--temp); }
    .chart-swatch.humidity { background: var(--humidity); }

    .chart-title { font-size: 15px; font-weight: 700; letter-spacing: -0.01em; }

    .chart-meta { font-size: 12px; color: var(--muted); font-weight: 600; }

    .chart-wrap {
      position: relative;
      height: 260px;
    }

    .empty-state,
    .loading,
    .error-box {
      position: absolute;
      inset: 0;
      z-index: 4;
      display: grid;
      place-items: center;
      text-align: center;
      padding: 20px;
      border-radius: var(--radius-md);
      background: color-mix(in srgb, var(--surface) 88%, transparent);
      backdrop-filter: blur(6px);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }

    .error-box { color: var(--critical); }

    .table-toggle {
      margin-top: 12px;
    }

    .table-toggle summary {
      cursor: pointer;
      font-size: 12px;
      font-weight: 700;
      color: var(--ink-2);
      list-style: none;
    }

    .table-toggle summary::-webkit-details-marker { display: none; }
    .table-toggle summary::before { content: "▸ "; }
    .table-toggle[open] summary::before { content: "▾ "; }

    .data-table-wrap {
      margin-top: 10px;
      max-height: 220px;
      overflow: auto;
      border-radius: var(--radius-md);
      border: 1px solid var(--border);
    }

    table.data-table {
      width: 100%;
      border-collapse: collapse;
      font-size: 12px;
      font-variant-numeric: tabular-nums;
    }

    table.data-table th,
    table.data-table td {
      text-align: right;
      padding: 7px 12px;
      border-bottom: 1px solid var(--grid);
      white-space: nowrap;
    }

    table.data-table th:first-child,
    table.data-table td:first-child { text-align: left; }

    table.data-table thead th {
      position: sticky;
      top: 0;
      background: var(--surface);
      color: var(--muted);
      font-weight: 700;
      text-transform: uppercase;
      font-size: 10px;
      letter-spacing: .04em;
    }

    .footer {
      margin-top: 20px;
      padding: 0 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.6;
    }

    @media (max-width: 900px) {
      .stats-row { grid-template-columns: 1fr 1fr; }
    }

    @media (max-width: 640px) {
      body { padding: 16px 12px 32px; }
      .hero { flex-direction: column; }
      .hero-right { align-self: flex-end; }
      .controls-bar { flex-direction: column; align-items: stretch; }
      .pill-group { justify-content: center; }
      .stats-row { grid-template-columns: 1fr 1fr; }
      .chart-card { padding: 16px; }
      .chart-wrap { height: 220px; }
    }
  </style>
</head>
<body>
  <main class="page">
    <header class="hero">
      <div>
        <p class="eyebrow">Capteur intérieur · ESP32</p>
        <h1>Climat intérieur</h1>
      </div>

      <div class="hero-right">
        <div id="statusChip" class="status-chip">
          <span class="status-dot"></span>
          <span id="statusText">Connexion…</span>
        </div>
        <button id="themeToggle" class="theme-toggle" type="button" aria-label="Changer de thème">
          <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>
          <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z"/></svg>
        </button>
      </div>
    </header>

    <section class="controls-bar">
      <div class="pill-group" aria-label="Vue">
        <button class="pill view-button active" data-view="history">Historique</button>
        <button class="pill view-button" data-view="day">Journée · heure par heure</button>
      </div>

      <div class="pill-group" aria-label="Données affichées">
        <button class="pill active metric-button" data-metric="temperature">Température</button>
        <button class="pill metric-button" data-metric="humidity">Humidité</button>
        <button class="pill metric-button" data-metric="both">Les deux</button>
      </div>

      <div id="rangeControls" class="pill-group" aria-label="Fenêtre temporelle">
        <button class="pill range-button" data-days="7">1 semaine</button>
        <button class="pill range-button active" data-days="31">1 mois</button>
        <button class="pill range-button" data-days="92">3 mois</button>
        <button class="pill range-button" data-days="365">1 an</button>
      </div>

      <div id="dayControls" class="day-picker-field" hidden>
        <button id="dayPrev" class="day-nav-btn" type="button" aria-label="Jour précédent">‹</button>
        <label for="dayPicker">Jour</label>
        <input id="dayPicker" type="date">
        <button id="dayNext" class="day-nav-btn" type="button" aria-label="Jour suivant">›</button>
      </div>
    </section>

    <section class="stats-row">
      <div class="stat-tile accent-temp">
        <div class="stat-tile-label">Température actuelle</div>
        <div id="currentTemp" class="stat-tile-value">—<span class="unit">°C</span></div>
      </div>
      <div class="stat-tile accent-humidity">
        <div class="stat-tile-label">Humidité actuelle</div>
        <div id="currentHumidity" class="stat-tile-value">—<span class="unit">%</span></div>
      </div>
      <div class="stat-tile">
        <div id="statLabel3" class="stat-tile-label">Température min/max · 24 h</div>
        <div id="statValue3" class="stat-tile-value">— / —<span class="unit">°C</span></div>
      </div>
      <div class="stat-tile">
        <div id="statLabel4" class="stat-tile-label">Humidité min/max · 24 h</div>
        <div id="statValue4" class="stat-tile-value">— / —<span class="unit">%</span></div>
      </div>
    </section>

    <section class="charts">
      <article id="tempCard" class="chart-card">
        <div class="chart-head">
          <div class="chart-title-group">
            <span class="chart-swatch temp"></span>
            <span id="tempChartTitle" class="chart-title">Température</span>
          </div>
          <span id="tempChartMeta" class="chart-meta"></span>
        </div>
        <div class="chart-wrap">
          <canvas id="tempCanvas"></canvas>
          <div id="tempLoading" class="loading" hidden>Chargement…</div>
          <div id="tempEmpty" class="empty-state" hidden>Aucune mesure pour cette période.</div>
          <div id="tempError" class="error-box" hidden></div>
        </div>
        <details class="table-toggle">
          <summary>Voir les données en tableau</summary>
          <div id="tempTableWrap" class="data-table-wrap"></div>
        </details>
      </article>

      <article id="humidityCard" class="chart-card">
        <div class="chart-head">
          <div class="chart-title-group">
            <span class="chart-swatch humidity"></span>
            <span id="humidityChartTitle" class="chart-title">Humidité</span>
          </div>
          <span id="humidityChartMeta" class="chart-meta"></span>
        </div>
        <div class="chart-wrap">
          <canvas id="humidityCanvas"></canvas>
          <div id="humidityLoading" class="loading" hidden>Chargement…</div>
          <div id="humidityEmpty" class="empty-state" hidden>Aucune mesure pour cette période.</div>
          <div id="humidityError" class="error-box" hidden></div>
        </div>
        <details class="table-toggle">
          <summary>Voir les données en tableau</summary>
          <div id="humidityTableWrap" class="data-table-wrap"></div>
        </details>
      </article>
    </section>

    <div class="footer">
      Vue « Historique » : trait plein = maximum du jour, trait pointillé = minimum du jour.
      Vue « Journée » : chaque point correspond à une mesure horaire de l’ESP32.
      Actualisation automatique toutes les 10 minutes. Les données déjà collectées sont conservées
      durablement ; seules les mesures manquantes sont redemandées à l’ESP32 via HiveMQ.
    </div>
  </main>

  <script>
    const state = {
      view: "history",
      metric: "temperature",
      days: 31,
      date: null,
      tempChart: null,
      humidityChart: null,
      timer: null
    };

    const number1 = new Intl.NumberFormat("fr-FR", { minimumFractionDigits: 1, maximumFractionDigits: 1 });
    const dateTimeFormatter = new Intl.DateTimeFormat("fr-FR", { dateStyle: "medium", timeStyle: "short" });
    const dayFormatter = new Intl.DateTimeFormat("fr-FR", { day: "2-digit", month: "short" });
    const hourFormatter = new Intl.DateTimeFormat("fr-FR", { hour: "2-digit", minute: "2-digit" });
    const dayTitleFormatter = new Intl.DateTimeFormat("fr-FR", { weekday: "long", day: "numeric", month: "long" });

    function format1(value) {
      return value == null ? "—" : number1.format(value);
    }

    function todayIso() {
      const now = new Date();
      const offset = now.getTimezoneOffset();
      return new Date(now.getTime() - offset * 60000).toISOString().slice(0, 10);
    }

    // ---------- thème ----------

    function initTheme() {
      const saved = localStorage.getItem("theme");
      if (saved === "light" || saved === "dark") {
        document.documentElement.setAttribute("data-theme", saved);
      }
      document.getElementById("themeToggle").addEventListener("click", () => {
        const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
        const current = document.documentElement.getAttribute("data-theme") || (prefersDark ? "dark" : "light");
        const next = current === "dark" ? "light" : "dark";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("theme", next);
      });
    }

    // ---------- panneau de contrôle ----------

    function setActiveButtons(selector, value, attr) {
      document.querySelectorAll(selector).forEach(btn => {
        btn.classList.toggle("active", btn.dataset[attr] === String(value));
      });
    }

    function applyMetricVisibility() {
      const showTemp = state.metric === "temperature" || state.metric === "both";
      const showHumidity = state.metric === "humidity" || state.metric === "both";
      document.getElementById("tempCard").hidden = !showTemp;
      document.getElementById("humidityCard").hidden = !showHumidity;
    }

    function applyViewVisibility() {
      const isDay = state.view === "day";
      document.getElementById("rangeControls").hidden = isDay;
      document.getElementById("dayControls").hidden = !isDay;

      const statLabel3 = document.getElementById("statLabel3");
      const statLabel4 = document.getElementById("statLabel4");
      statLabel3.textContent = isDay ? "Température min/max · jour" : "Température min/max · 24 h";
      statLabel4.textContent = isDay ? "Humidité min/max · jour" : "Humidité min/max · 24 h";
    }

    document.querySelectorAll(".view-button").forEach(button => {
      button.addEventListener("click", () => {
        state.view = button.dataset.view;
        setActiveButtons(".view-button", state.view, "view");
        applyViewVisibility();
        loadCurrentView();
      });
    });

    document.querySelectorAll(".metric-button").forEach(button => {
      button.addEventListener("click", () => {
        state.metric = button.dataset.metric;
        setActiveButtons(".metric-button", state.metric, "metric");
        applyMetricVisibility();
        loadCurrentView();
      });
    });

    document.querySelectorAll(".range-button").forEach(button => {
      button.addEventListener("click", () => {
        state.days = Number(button.dataset.days);
        setActiveButtons(".range-button", state.days, "days");
        loadHistory();
      });
    });

    const dayPicker = document.getElementById("dayPicker");
    dayPicker.max = todayIso();
    dayPicker.addEventListener("change", () => {
      state.date = dayPicker.value || todayIso();
      loadDay();
    });

    document.getElementById("dayPrev").addEventListener("click", () => shiftDay(-1));
    document.getElementById("dayNext").addEventListener("click", () => shiftDay(1));

    function shiftDay(delta) {
      const current = new Date(`${state.date}T12:00:00`);
      current.setDate(current.getDate() + delta);
      const iso = current.toISOString().slice(0, 10);
      if (iso > todayIso()) return;
      state.date = iso;
      dayPicker.value = iso;
      loadDay();
    }

    function loadCurrentView() {
      if (state.view === "day") {
        loadDay();
      } else {
        loadHistory();
      }
    }

    // ---------- statut / stats communes ----------

    function setOnline(isOnline) {
      const chip = document.getElementById("statusChip");
      chip.classList.toggle("offline", !isOnline);
      document.getElementById("statusText").textContent = isOnline ? "En ligne" : "Hors ligne";
    }

    function updateCurrentStats(current) {
      const tempEl = document.getElementById("currentTemp");
      const humidityEl = document.getElementById("currentHumidity");

      if (!current) {
        tempEl.innerHTML = `—<span class="unit">°C</span>`;
        humidityEl.innerHTML = `—<span class="unit">%</span>`;
        return;
      }

      tempEl.innerHTML = `${format1(current.temperature_c)}<span class="unit">°C</span>`;
      humidityEl.innerHTML = `${format1(current.humidity_percent)}<span class="unit">%</span>`;
    }

    function updateMinMaxStats(tempStats, humidityStats) {
      document.getElementById("statValue3").innerHTML =
        `${format1(tempStats.min)} / ${format1(tempStats.max)}<span class="unit">°C</span>`;
      document.getElementById("statValue4").innerHTML =
        `${format1(humidityStats.min)} / ${format1(humidityStats.max)}<span class="unit">%</span>`;
    }

    // ---------- rendu graphique générique ----------

    function setChartState(prefix, mode, message) {
      document.getElementById(`${prefix}Loading`).hidden = mode !== "loading";
      document.getElementById(`${prefix}Empty`).hidden = mode !== "empty";
      const errorBox = document.getElementById(`${prefix}Error`);
      errorBox.hidden = mode !== "error";
      if (mode === "error") errorBox.textContent = message || "Erreur inconnue.";
    }

    function destroyChart(key) {
      if (state[key]) {
        state[key].destroy();
        state[key] = null;
      }
    }

    function renderLineChart(canvasId, key, labels, mainSeries, secondarySeries, color, colorStrong, unit) {
      destroyChart(key);

      const datasets = [{
        label: mainSeries.label,
        data: mainSeries.data,
        borderColor: color,
        backgroundColor: color + "26",
        borderWidth: 2.2,
        pointRadius: labels.length > 60 ? 0 : 3,
        pointHoverRadius: 5,
        tension: .25,
        fill: true
      }];

      if (secondarySeries) {
        datasets.push({
          label: secondarySeries.label,
          data: secondarySeries.data,
          borderColor: colorStrong,
          backgroundColor: "transparent",
          borderDash: [5, 4],
          borderWidth: 1.8,
          pointRadius: labels.length > 60 ? 0 : 2,
          pointHoverRadius: 4,
          tension: .25,
          fill: false
        });
      }

      const styles = getComputedStyle(document.documentElement);
      const gridColor = styles.getPropertyValue("--grid").trim();
      const mutedColor = styles.getPropertyValue("--muted").trim();

      state[key] = new Chart(document.getElementById(canvasId), {
        type: "line",
        data: { labels, datasets },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: datasets.length > 1
              ? { position: "bottom", labels: { usePointStyle: true, boxWidth: 8, padding: 14, color: mutedColor, font: { weight: 700, size: 11 } } }
              : { display: false },
            tooltip: {
              callbacks: {
                label(context) {
                  return `${context.dataset.label} : ${number1.format(context.parsed.y)} ${unit}`;
                }
              }
            }
          },
          scales: {
            x: {
              grid: { display: false },
              ticks: { color: mutedColor, maxTicksLimit: 10, maxRotation: 0 },
              border: { display: false }
            },
            y: {
              position: "left",
              grace: "10%",
              grid: { color: gridColor },
              border: { display: false },
              ticks: { color: mutedColor, callback: v => `${v}${unit}` }
            }
          }
        }
      });
    }

    function renderTable(containerId, headers, rows) {
      const container = document.getElementById(containerId);
      if (!rows.length) {
        container.innerHTML = "";
        return;
      }
      const head = `<thead><tr>${headers.map(h => `<th>${h}</th>`).join("")}</tr></thead>`;
      const body = `<tbody>${rows.map(r => `<tr>${r.map(c => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody>`;
      container.innerHTML = `<table class="data-table">${head}${body}</table>`;
    }

    // ---------- vue historique ----------

    async function loadHistory() {
      applyMetricVisibility();
      setChartState("temp", "loading");
      setChartState("humidity", "loading");

      try {
        const response = await fetch(`/api/climate?days=${state.days}`, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Impossible de récupérer les mesures.");

        setOnline(true);
        updateCurrentStats(data.current);
        updateMinMaxStats(data.temperature, data.humidity);

        const labels = data.daily.map(item => dayFormatter.format(new Date(`${item.date}T12:00:00`)));
        const rangeLabel = { 7: "7 derniers jours", 31: "30 derniers jours", 92: "3 derniers mois", 365: "12 derniers mois" }[state.days];

        document.getElementById("tempChartTitle").textContent = "Température";
        document.getElementById("tempChartMeta").textContent = rangeLabel;
        document.getElementById("humidityChartTitle").textContent = "Humidité";
        document.getElementById("humidityChartMeta").textContent = rangeLabel;

        if (!data.daily.length) {
          setChartState("temp", "empty");
          setChartState("humidity", "empty");
          renderTable("tempTableWrap", [], []);
          renderTable("humidityTableWrap", [], []);
          return;
        }

        setChartState("temp", "ok");
        setChartState("humidity", "ok");

        renderLineChart(
          "tempCanvas", "tempChart", labels,
          { label: "Max", data: data.daily.map(d => d.temperature.max) },
          { label: "Min", data: data.daily.map(d => d.temperature.min) },
          getComputedStyle(document.documentElement).getPropertyValue("--temp").trim(),
          getComputedStyle(document.documentElement).getPropertyValue("--temp-strong").trim(),
          "°C"
        );

        renderLineChart(
          "humidityCanvas", "humidityChart", labels,
          { label: "Max", data: data.daily.map(d => d.humidity.max) },
          { label: "Min", data: data.daily.map(d => d.humidity.min) },
          getComputedStyle(document.documentElement).getPropertyValue("--humidity").trim(),
          getComputedStyle(document.documentElement).getPropertyValue("--humidity-strong").trim(),
          "%"
        );

        renderTable(
          "tempTableWrap", ["Date", "Min (°C)", "Max (°C)"],
          data.daily.map(d => [dayFormatter.format(new Date(`${d.date}T12:00:00`)), format1(d.temperature.min), format1(d.temperature.max)])
        );
        renderTable(
          "humidityTableWrap", ["Date", "Min (%)", "Max (%)"],
          data.daily.map(d => [dayFormatter.format(new Date(`${d.date}T12:00:00`)), format1(d.humidity.min), format1(d.humidity.max)])
        );
      } catch (error) {
        setOnline(false);
        setChartState("temp", "error", error.message);
        setChartState("humidity", "error", error.message);
      }
    }

    // ---------- vue journée (heure par heure) ----------

    async function loadDay() {
      applyMetricVisibility();
      setChartState("temp", "loading");
      setChartState("humidity", "loading");

      try {
        const response = await fetch(`/api/climate/day?date=${state.date}`, { cache: "no-store" });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Impossible de récupérer les mesures.");

        setOnline(true);
        updateMinMaxStats(
          { min: data.temperature.min, max: data.temperature.max },
          { min: data.humidity.min, max: data.humidity.max }
        );

        const dayTitle = dayTitleFormatter.format(new Date(`${data.date}T12:00:00`));
        document.getElementById("tempChartTitle").textContent = "Température";
        document.getElementById("tempChartMeta").textContent = dayTitle;
        document.getElementById("humidityChartTitle").textContent = "Humidité";
        document.getElementById("humidityChartMeta").textContent = dayTitle;

        if (data.hourly.length) {
          updateCurrentStats(data.hourly[data.hourly.length - 1]);
        } else {
          updateCurrentStats(null);
        }

        if (!data.hourly.length) {
          setChartState("temp", "empty");
          setChartState("humidity", "empty");
          renderTable("tempTableWrap", [], []);
          renderTable("humidityTableWrap", [], []);
          return;
        }

        setChartState("temp", "ok");
        setChartState("humidity", "ok");

        const labels = data.hourly.map(item => hourFormatter.format(new Date(item.epoch_utc * 1000)));

        renderLineChart(
          "tempCanvas", "tempChart", labels,
          { label: "Température", data: data.hourly.map(d => d.temperature_c) },
          null,
          getComputedStyle(document.documentElement).getPropertyValue("--temp").trim(),
          getComputedStyle(document.documentElement).getPropertyValue("--temp-strong").trim(),
          "°C"
        );

        renderLineChart(
          "humidityCanvas", "humidityChart", labels,
          { label: "Humidité", data: data.hourly.map(d => d.humidity_percent) },
          null,
          getComputedStyle(document.documentElement).getPropertyValue("--humidity").trim(),
          getComputedStyle(document.documentElement).getPropertyValue("--humidity-strong").trim(),
          "%"
        );

        renderTable(
          "tempTableWrap", ["Heure", "Température (°C)"],
          data.hourly.map(d => [hourFormatter.format(new Date(d.epoch_utc * 1000)), format1(d.temperature_c)])
        );
        renderTable(
          "humidityTableWrap", ["Heure", "Humidité (%)"],
          data.hourly.map(d => [hourFormatter.format(new Date(d.epoch_utc * 1000)), format1(d.humidity_percent)])
        );
      } catch (error) {
        setOnline(false);
        setChartState("temp", "error", error.message);
        setChartState("humidity", "error", error.message);
      }
    }

    // ---------- démarrage ----------

    initTheme();
    applyViewVisibility();
    applyMetricVisibility();
    state.date = todayIso();
    dayPicker.value = state.date;

    loadHistory();
    state.timer = window.setInterval(loadCurrentView, 10 * 60 * 1000);
  </script>
</body>
</html>
"""


def fetch_history(days: int, timeout: int | None = None) -> list[dict[str, Any]]:
    if timeout is None:
        timeout = max(45, min(180, 30 + days // 3))

    request_id = uuid.uuid4().hex[:16]
    request_topic = f"weather/{DEVICE_ID}/request"
    response_topic = f"weather/{DEVICE_ID}/response/{request_id}"

    records: list[dict[str, Any]] = []
    completed = threading.Event()
    connected = threading.Event()
    error_holder: list[str] = []

    def on_connect(
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        if reason_code.is_failure:
            error_holder.append(f"Connexion MQTT refusée : {reason_code}")
            completed.set()
            return
        client.subscribe(response_topic, qos=0)
        connected.set()

    def on_subscribe(
        client: mqtt.Client,
        userdata: Any,
        mid: int,
        reason_code_list: list[mqtt.ReasonCode],
        properties: mqtt.Properties | None,
    ) -> None:
        client.publish(
            request_topic,
            payload=f"{request_id},{days}",
            qos=0,
            retain=False,
        )

    def on_message(
        client: mqtt.Client,
        userdata: Any,
        message: mqtt.MQTTMessage,
    ) -> None:
        text = message.payload.decode("utf-8", errors="replace").strip()
        parts = text.split(",")

        try:
            if parts[0] == "DATA" and len(parts) == 4:
                records.append(
                    {
                        "epoch_utc": int(parts[1]),
                        "temperature_c": float(parts[2]),
                        "humidity_percent": float(parts[3]),
                    }
                )
            elif parts[0] == "END":
                completed.set()
            elif parts[0] == "ERROR":
                error_holder.append(",".join(parts[1:]) or "Erreur ESP32")
                completed.set()
        except (ValueError, IndexError) as exc:
            error_holder.append(f"Réponse ESP32 invalide : {exc}")
            completed.set()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"climate-dashboard-{request_id}",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.tls_set(
        ca_certs=None,
        certfile=None,
        keyfile=None,
        cert_reqs=ssl.CERT_REQUIRED,
        tls_version=ssl.PROTOCOL_TLS_CLIENT,
    )

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()

        if not connected.wait(timeout=20):
            raise TimeoutError("Délai dépassé pendant la connexion à HiveMQ.")

        if not completed.wait(timeout=timeout):
            raise TimeoutError(
                "L’ESP32 n’a pas terminé l’envoi de l’historique dans le délai prévu."
            )

        if error_holder:
            raise RuntimeError(error_holder[0])
    finally:
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.loop_stop()
        except Exception:
            pass

    by_epoch = {record["epoch_utc"]: record for record in records}
    return [by_epoch[key] for key in sorted(by_epoch)]


def build_dashboard_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {
            "current": None,
            "temperature": {"min_24h": None, "max_24h": None},
            "humidity": {"min_24h": None, "max_24h": None},
            "daily": [],
            "sample_count": 0,
            "stale": True,
        }

    now = int(time.time())
    current = records[-1]

    last_24h = [record for record in records if record["epoch_utc"] >= now - 86400]

    def min_or_none(values: list[float]) -> float | None:
        return round(min(values), 1) if values else None

    def max_or_none(values: list[float]) -> float | None:
        return round(max(values), 1) if values else None

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)

    for record in records:
        local_date = (
            datetime.fromtimestamp(record["epoch_utc"]).astimezone().date().isoformat()
        )
        grouped[local_date].append(record)

    daily = []
    for day, values in sorted(grouped.items()):
        temperatures = [item["temperature_c"] for item in values]
        humidities = [item["humidity_percent"] for item in values]
        daily.append(
            {
                "date": day,
                "temperature": {
                    "min": round(min(temperatures), 1),
                    "max": round(max(temperatures), 1),
                },
                "humidity": {
                    "min": round(min(humidities), 1),
                    "max": round(max(humidities), 1),
                },
            }
        )

    return {
        "current": {
            "epoch_utc": current["epoch_utc"],
            "temperature_c": round(current["temperature_c"], 1),
            "humidity_percent": round(current["humidity_percent"], 1),
        },
        "temperature": {
            "min_24h": min_or_none([r["temperature_c"] for r in last_24h]),
            "max_24h": max_or_none([r["temperature_c"] for r in last_24h]),
        },
        "humidity": {
            "min_24h": min_or_none([r["humidity_percent"] for r in last_24h]),
            "max_24h": max_or_none([r["humidity_percent"] for r in last_24h]),
        },
        "daily": daily,
        "sample_count": len(records),
        "stale": now - current["epoch_utc"] > 90 * 60,
    }


def build_day_payload(target_date: date, records: list[dict[str, Any]]) -> dict[str, Any]:
    hourly = [
        {
            "epoch_utc": r["epoch_utc"],
            "temperature_c": round(r["temperature_c"], 1),
            "humidity_percent": round(r["humidity_percent"], 1),
        }
        for r in records
    ]

    def stats(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"min": None, "max": None, "avg": None}
        return {
            "min": round(min(values), 1),
            "max": round(max(values), 1),
            "avg": round(sum(values) / len(values), 1),
        }

    return {
        "date": target_date.isoformat(),
        "hourly": hourly,
        "temperature": stats([r["temperature_c"] for r in records]),
        "humidity": stats([r["humidity_percent"] for r in records]),
        "sample_count": len(records),
    }


@app.get("/")
def index() -> str:
    return PAGE_HTML


@app.get("/api/climate")
def climate_api():
    allowed_ranges = {7, 31, 92, 365}

    try:
        days = int(request.args.get("days", "31"))
    except ValueError:
        return jsonify({"error": "Fenêtre temporelle invalide."}), 400

    if days not in allowed_ranges:
        return jsonify({"error": "Fenêtre temporelle non autorisée."}), 400

    try:
        records = get_history(days)
        return jsonify(build_dashboard_payload(records))
    except Exception as exc:
        app.logger.exception("Impossible de récupérer les mesures")
        return jsonify({"error": str(exc)}), 503


@app.get("/api/climate/day")
def climate_day_api():
    date_str = request.args.get("date", "")

    try:
        target_date = date.fromisoformat(date_str)
    except ValueError:
        return jsonify({"error": "Date invalide (format attendu : AAAA-MM-JJ)."}), 400

    today_local = datetime.now().astimezone().date()
    if target_date > today_local:
        return jsonify({"error": "Impossible d’afficher une date future."}), 400

    try:
        records = get_day_records(target_date)
        return jsonify(build_day_payload(target_date, records))
    except Exception as exc:
        app.logger.exception("Impossible de récupérer les mesures horaires")
        return jsonify({"error": str(exc)}), 503


if __name__ == "__main__":
    if not MQTT_USERNAME or not MQTT_PASSWORD:
        raise SystemExit(
            "MQTT_USERNAME / MQTT_PASSWORD manquants. Crée un fichier .env à côté "
            "de app.py (voir .env.example) avec tes identifiants HiveMQ."
        )

    url = f"http://{LOCAL_HOST}:{LOCAL_PORT}"

    print()
    print(f"Tableau de bord : {url}")
    print("Arrêt : Ctrl+C")
    print()

    # Ouvre le navigateur automatiquement une fois le serveur prêt ; si la
    # fenêtre est fermée, l'adresse ci-dessus reste joignable manuellement.
    threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    app.run(
        host=LOCAL_HOST,
        port=LOCAL_PORT,
        debug=False,
        threaded=True,
    )
