#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
DOCS_DATA_DIR = DOCS_DIR / "data"

SOURCE_URL = "http://xxfb.mwr.cn/sq_djdh.html"
MAP_SEARCH_URL = "http://xxfb.mwr.cn/hydroSearch/mapSearch"
TZ = timezone(timedelta(hours=8), "Asia/Shanghai")
STALE_AFTER_DAYS = 3

STATIONS_CSV = DATA_DIR / "stations.csv"
HISTORY_CSV = DATA_DIR / "river_water_levels.csv"
LATEST_JSON = DATA_DIR / "latest_river_water_levels.json"

DOCS_STATIONS = DOCS_DATA_DIR / "stations.json"
DOCS_LATEST = DOCS_DATA_DIR / "latest.json"
DOCS_HISTORY = DOCS_DATA_DIR / "history.csv"
DOCS_HTML = DOCS_DIR / "index.html"

STATION_FIELDS = [
    "station_id",
    "station",
    "basin",
    "river",
    "admin_region",
    "official_lng",
    "official_lat",
    "snap_lng",
    "snap_lat",
    "snap_source",
    "station_type",
    "important_section",
]

HISTORY_FIELDS = [
    "station_id",
    "datetime",
    "date",
    "station",
    "basin",
    "river",
    "admin_region",
    "water_level_m",
    "alert_level_m",
    "source_updated_at",
    "fetched_at",
]


def fetch_text(url: str, data: dict[str, str] | None = None, retries: int = 3) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": SOURCE_URL,
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/javascript, */*; q=0.01",
    }
    encoded = None
    if data is not None:
        encoded = urlencode(data).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = Request(url, data=encoded, headers=headers, method="POST" if data is not None else "GET")
            with urlopen(request, timeout=60) as response:
                return response.read().decode("utf-8")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def decode_coord(value: str | None) -> float | None:
    if not value:
        return None
    try:
        first = base64.b64decode(value)
        second = base64.b64decode(first)
        return float(second.decode("utf-8"))
    except Exception:
        return None


def to_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        match = re.search(r"[+-]?\d+(?:\.\d+)?", str(value))
        return float(match.group(0)) if match else None


def clean_text(value: object) -> str:
    return "" if value is None else re.sub(r"\s+", " ", str(value)).strip()


def normalize_datetime(value: str) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ).isoformat()
        except ValueError:
            pass
    return value


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def record_date(value: str) -> str:
    parsed = parse_iso_datetime(value)
    return parsed.date().isoformat() if parsed else value[:10]


def reading_status(dt: str, level: float | None, fetched_at_dt: datetime) -> str:
    if not dt or level is None:
        return "no_recent_reading"
    reading_dt = parse_iso_datetime(dt)
    if reading_dt and fetched_at_dt - reading_dt > timedelta(days=STALE_AFTER_DAYS):
        return "stale"
    return "current"


def latest_data_timestamp(latest_rows: list[dict[str, object]], fallback: str) -> str:
    values: list[datetime] = []
    for row in latest_rows:
        parsed = parse_iso_datetime(str(row.get("datetime") or ""))
        if parsed:
            values.append(parsed)
    return max(values).isoformat(timespec="seconds") if values else fallback


def fetch_river_rows(fetched_at_dt: datetime) -> list[dict[str, object]]:
    payload = json.loads(fetch_text(MAP_SEARCH_URL, {"name": ""}))
    if payload.get("returncode") != 0:
        raise RuntimeError(f"Unexpected source response: {payload!r}")

    rows: list[dict[str, object]] = []
    for row in payload.get("result") or []:
        if row.get("stType") != "river":
            continue
        lng = decode_coord(row.get("lgtd"))
        lat = decode_coord(row.get("lttd"))
        if lng is None or lat is None:
            continue
        if not (70 <= lng <= 140 and 15 <= lat <= 55):
            continue
        station_id = clean_text(row.get("idNo"))
        if not station_id:
            continue
        dt = normalize_datetime(clean_text(row.get("tm")))
        level = to_float(row.get("z"))
        alert_level = to_float(row.get("alertValue"))
        status = reading_status(dt, level, fetched_at_dt)
        has_reading = status != "no_recent_reading"
        rows.append(
            {
                "station_id": station_id,
                "station": clean_text(row.get("stnm")),
                "basin": clean_text(row.get("bsnm")),
                "river": clean_text(row.get("rvnm")),
                "admin_region": clean_text(row.get("addvnm")),
                "official_lng": lng,
                "official_lat": lat,
                "snap_lng": lng,
                "snap_lat": lat,
                "snap_source": "official_coordinate",
                "station_type": "river",
                "important_section": clean_text(row.get("importantSection")),
                "datetime": dt,
                "water_level_m": level if has_reading else None,
                "alert_level_m": alert_level if has_reading else None,
                "reading_status": status,
                "source_updated_at": clean_text(row.get("createTime")),
            }
        )
    rows.sort(key=lambda item: (str(item["basin"]), str(item["river"]), str(item["station"]), str(item["station_id"])))
    return rows


def format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def write_csv(path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: format_value(row.get(field)) for field in fieldnames})


def load_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def merge_history(existing: list[dict[str, str]], latest_rows: list[dict[str, object]], fetched_at: str) -> list[dict[str, object]]:
    merged: dict[tuple[str, str], dict[str, object]] = {}
    for row in existing:
        sid = row.get("station_id", "")
        dt = row.get("datetime", "")
        if sid and dt:
            merged[(sid, dt)] = row
    for row in latest_rows:
        dt = str(row["datetime"])
        if not dt or row.get("water_level_m") is None:
            continue
        key = (str(row["station_id"]), dt)
        if key in merged:
            continue
        merged[key] = {
            "station_id": row["station_id"],
            "datetime": dt,
            "date": record_date(dt),
            "station": row["station"],
            "basin": row["basin"],
            "river": row["river"],
            "admin_region": row["admin_region"],
            "water_level_m": row["water_level_m"],
            "alert_level_m": row["alert_level_m"],
            "source_updated_at": row["source_updated_at"],
            "fetched_at": fetched_at,
        }
    return sorted(merged.values(), key=lambda item: (str(item["datetime"]), str(item["station_id"])))


def station_rows(latest_rows: list[dict[str, object]]) -> list[dict[str, object]]:
    stations: dict[str, dict[str, object]] = {}
    for row in latest_rows:
        stations[str(row["station_id"])] = {field: row.get(field) for field in STATION_FIELDS}
    return sorted(stations.values(), key=lambda item: (str(item["basin"]), str(item["river"]), str(item["station"])))


def write_docs(stations: list[dict[str, object]], latest_rows: list[dict[str, object]], history: list[dict[str, object]], fetched_at: str) -> None:
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    data_updated_at = latest_data_timestamp(latest_rows, fetched_at)

    stations_payload = {
        "generated_at": data_updated_at,
        "source": SOURCE_URL,
        "coordinate_note": "站点使用水利部全国水雨情信息站点坐标；页面不绘制站点连线。",
        "stations": stations,
    }
    latest_payload = {
        "generated_at": data_updated_at,
        "source": SOURCE_URL,
        "records": [
            {
                "station_id": row["station_id"],
                "datetime": row["datetime"],
                "station": row["station"],
                "basin": row["basin"],
                "river": row["river"],
                "admin_region": row["admin_region"],
                "water_level_m": row["water_level_m"],
                "alert_level_m": row["alert_level_m"],
                "reading_status": row["reading_status"],
            }
            for row in latest_rows
        ],
    }
    DOCS_STATIONS.write_text(json.dumps(stations_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    DOCS_LATEST.write_text(json.dumps(latest_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(DOCS_HISTORY, history, HISTORY_FIELDS)
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    DOCS_HTML.write_text(render_html(), encoding="utf-8")


def render_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>全国河道站实时水位地图</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
      --ink: #142033;
      --muted: #617184;
      --line: #d8e1ea;
      --surface: rgba(255, 255, 255, .94);
      --blue: #2563eb;
      --red: #ef4444;
      --green: #16a34a;
      --gray: #64748b;
      --shadow: 0 14px 36px rgba(15, 23, 42, .18);
      --safe-top: env(safe-area-inset-top, 0px);
      --safe-bottom: env(safe-area-inset-bottom, 0px);
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0;
      overflow: hidden;
      color: var(--ink);
      background: #eef3f8;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
    }
    button, input, select { font: inherit; color: var(--ink); }
    button {
      cursor: pointer;
      height: 38px;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 0 12px;
      background: rgba(255, 255, 255, .96);
      white-space: nowrap;
    }
    button.primary { border-color: var(--blue); background: var(--blue); color: #fff; }
    button.icon {
      width: 40px;
      padding: 0;
      display: inline-grid;
      place-items: center;
      font-size: 18px;
      line-height: 1;
    }
    #map {
      position: fixed;
      inset: 0;
      z-index: 1;
      width: 100%;
      height: 100%;
    }
    .topbar {
      position: fixed;
      top: calc(10px + var(--safe-top));
      left: 12px;
      right: 12px;
      z-index: 520;
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
      pointer-events: none;
    }
    .brand,
    .toolbar,
    .filter-panel,
    .station-sheet,
    .sheet-peek,
    .legend {
      background: var(--surface);
      border: 1px solid rgba(216, 225, 234, .95);
      box-shadow: var(--shadow);
      backdrop-filter: blur(12px);
    }
    .brand {
      min-width: 0;
      border-radius: 10px;
      padding: 9px 12px;
      pointer-events: auto;
    }
    .brand-title {
      display: block;
      font-size: 17px;
      line-height: 1.15;
      font-weight: 700;
      white-space: nowrap;
    }
    .brand-meta {
      display: block;
      max-width: min(58vw, 470px);
      margin-top: 4px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.25;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .toolbar {
      display: flex;
      gap: 7px;
      border-radius: 10px;
      padding: 6px;
      pointer-events: auto;
    }
    .filter-panel {
      position: fixed;
      top: calc(74px + var(--safe-top));
      left: 12px;
      z-index: 530;
      width: min(342px, calc(100vw - 24px));
      max-height: calc(100vh - 104px - var(--safe-top));
      overflow: auto;
      border-radius: 10px;
      padding: 14px;
      opacity: 0;
      transform: translateX(calc(-100% - 20px));
      transition: transform .2s ease, opacity .2s ease;
    }
    .filter-panel.open { opacity: 1; transform: translateX(0); }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
      margin-bottom: 12px;
    }
    .panel-head h2 { margin: 0; font-size: 17px; }
    .filters { display: grid; gap: 10px; }
    label { display: grid; gap: 5px; color: var(--muted); font-size: 12px; }
    select, input {
      width: 100%;
      height: 36px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
      padding: 0 10px;
      font-size: 14px;
    }
    .filter-actions {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .stats {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 12px;
    }
    .stat {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px 9px;
      background: #fbfdff;
    }
    .stat span { display: block; margin-bottom: 3px; color: var(--muted); font-size: 12px; }
    .stat strong { font-size: 16px; }
    .legend {
      position: fixed;
      top: calc(82px + var(--safe-top));
      right: 12px;
      z-index: 500;
      border-radius: 10px;
      padding: 9px 11px;
      color: #415166;
      font-size: 12px;
      line-height: 1.7;
    }
    .dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 6px;
    }
    .leaflet-control-zoom { margin-top: 82px !important; }
    .cluster-toggle-floating {
      position: fixed;
      left: 64px;
      top: calc(92px + var(--safe-top));
      z-index: 500;
    }
    .cluster-toggle-button {
      width: 42px;
      height: 34px;
      border: 1px solid rgba(216, 225, 234, .95);
      border-radius: 8px;
      background: rgba(255, 255, 255, .96);
      box-shadow: var(--shadow);
      color: var(--ink);
      font-size: 13px;
      font-weight: 700;
      padding: 0;
    }
    .cluster-toggle-button.expanded {
      color: var(--blue);
      background: #eff6ff;
    }
    .station-sheet {
      position: fixed;
      right: 12px;
      bottom: calc(18px + var(--safe-bottom));
      z-index: 540;
      width: min(450px, calc(100vw - 24px));
      max-height: min(68vh, 620px);
      overflow: hidden;
      border-radius: 12px;
      pointer-events: none;
      transform: translateY(calc(100% + 24px));
      transition: transform .24s ease;
    }
    .station-sheet.open {
      pointer-events: auto;
      transform: translateY(0);
    }
    .sheet-peek {
      position: fixed;
      right: 12px;
      bottom: calc(18px + var(--safe-bottom));
      z-index: 545;
      width: 48px;
      height: 48px;
      display: none;
      place-items: center;
      border-radius: 12px;
      padding: 0;
      color: var(--blue);
      font-size: 20px;
      font-weight: 700;
    }
    .sheet-peek.show {
      display: grid;
    }
    .sheet-head {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 8px;
      align-items: center;
      min-height: 58px;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }
    .station-title { margin: 0; font-size: 17px; line-height: 1.2; }
    .station-meta { margin: 4px 0 0; color: var(--muted); font-size: 12px; line-height: 1.35; }
    .sheet-body {
      max-height: calc(min(68vh, 620px) - 58px);
      overflow: auto;
      padding: 12px;
      background: rgba(255,255,255,.98);
    }
    .reading-row {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 10px;
    }
    .reading-card {
      min-width: 0;
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 8px;
      background: #fbfdff;
    }
    .reading-card span { display: block; margin-bottom: 4px; color: var(--muted); font-size: 11px; }
    .reading-card strong { font-size: 15px; white-space: nowrap; }
    .trend-chip {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      height: 24px;
      min-width: 0;
      border-radius: 999px;
      padding: 0 8px;
      color: #fff;
      font-size: 12px;
      white-space: nowrap;
    }
    .range-controls {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 4px 0 10px;
    }
    .range-controls button { height: 32px; padding: 0 9px; font-size: 13px; }
    .range-controls button.active { border-color: var(--blue); background: #eff6ff; color: var(--blue); font-weight: 600; }
    #years-input { width: 70px; height: 32px; font-size: 13px; }
    #chart {
      height: 310px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .popup-title { margin-bottom: 5px; font-size: 14px; font-weight: 700; }
    .popup-line { color: #374151; line-height: 1.45; }
    .leaflet-control-layers,
    .leaflet-control-zoom {
      border: 1px solid rgba(216, 225, 234, .95) !important;
      border-radius: 8px !important;
      box-shadow: var(--shadow) !important;
      overflow: hidden;
    }
    @media (max-width: 760px) {
      .topbar { top: calc(8px + var(--safe-top)); left: 8px; right: 8px; }
      .brand { padding: 8px 10px; }
      .brand-title { font-size: 15px; }
      .brand-meta { max-width: 50vw; font-size: 11px; }
      .toolbar { gap: 5px; padding: 5px; }
      button { height: 36px; border-radius: 9px; }
      button.icon { width: 38px; }
      .filter-panel {
        top: calc(64px + var(--safe-top));
        left: 8px;
        width: calc(100vw - 16px);
        max-height: min(66vh, 520px);
        padding: 12px;
      }
      .legend { display: none; }
      .station-sheet {
        left: 8px;
        right: 8px;
        bottom: calc(8px + var(--safe-bottom));
        width: auto;
        max-height: 72vh;
        border-radius: 14px;
      }
      .sheet-body { max-height: calc(72vh - 58px); }
      .reading-row { grid-template-columns: 1fr 1fr; }
      .reading-card:nth-child(3) { grid-column: 1 / -1; }
      #chart { height: 260px; }
      .leaflet-control-zoom { margin-top: 72px !important; }
      .cluster-toggle-floating {
        left: 62px;
        top: calc(80px + var(--safe-top));
      }
      .sheet-peek {
        right: 8px;
        bottom: calc(8px + var(--safe-bottom));
        width: 44px;
        height: 44px;
      }
    }
  </style>
</head>
<body>
  <div id="map"></div>
  <button type="button" class="cluster-toggle-button cluster-toggle-floating" id="cluster-toggle" aria-label="切换站点聚合">聚合</button>
  <header class="topbar">
    <div class="brand">
      <span class="brand-title">全国河道站实时水位</span>
      <span class="brand-meta" id="updated-at">正在加载数据</span>
    </div>
    <div class="toolbar">
      <button type="button" class="icon" id="locate-btn" title="定位到附近水域" aria-label="定位到附近水域">◎</button>
      <button type="button" id="filter-toggle">筛选</button>
    </div>
  </header>

  <aside class="filter-panel" id="filter-panel" aria-label="筛选">
    <div class="panel-head">
      <h2>筛选站点</h2>
      <button type="button" class="icon" id="filter-close" aria-label="收起筛选">×</button>
    </div>
    <section class="filters">
      <label>流域<select id="basin-filter"><option value="">全部流域</option></select></label>
      <label>行政区划<select id="admin-filter"><option value="">全部行政区划</option></select></label>
      <label>河流<select id="river-filter"><option value="">全部河流</option></select></label>
      <label>站点<input id="station-filter" type="search" placeholder="输入站名关键词" /></label>
    </section>
    <div class="filter-actions">
      <button type="button" id="clear-filters">清空</button>
      <button type="button" class="primary" id="apply-filters">查看</button>
    </div>
    <div class="stats">
      <div class="stat"><span>显示站点</span><strong id="visible-count">--</strong></div>
      <div class="stat"><span>全部站点</span><strong id="total-count">--</strong></div>
    </div>
  </aside>

  <div class="legend">
    <div><span class="dot" style="background:#ef4444"></span>上涨 ≥5cm</div>
    <div><span class="dot" style="background:#2563eb"></span>下降 ≥5cm</div>
    <div><span class="dot" style="background:#16a34a"></span>稳定 &lt;5cm</div>
    <div><span class="dot" style="background:#64748b"></span>非实时/暂无</div>
  </div>

  <section class="station-sheet" id="station-sheet" aria-label="站点水位趋势">
    <div class="sheet-head">
      <div>
        <h2 class="station-title" id="station-title">点击站点查看水位</h2>
        <p class="station-meta" id="station-meta">可定位到附近水域，也可打开筛选查找站点。</p>
      </div>
      <button type="button" class="icon" id="sheet-close" aria-label="收起站点详情">⌄</button>
    </div>
    <div class="sheet-body">
      <div class="reading-row">
        <div class="reading-card"><span>最新水位</span><strong id="latest-level">--</strong></div>
        <div class="reading-card"><span>变化趋势</span><strong id="trend-label">--</strong></div>
        <div class="reading-card"><span>更新时间</span><strong id="latest-time">--</strong></div>
      </div>
      <div class="range-controls" aria-label="趋势时间范围">
        <button type="button" data-days="7">1周</button>
        <button type="button" data-days="30" class="active">1月</button>
        <button type="button" data-days="90">3个月</button>
        <button type="button" data-days="180">6个月</button>
        <button type="button" data-days="365">1年</button>
        <button type="button" data-all="true">全部</button>
        <input id="years-input" type="number" min="1" max="50" value="2" aria-label="自定义年数" />
        <button type="button" id="apply-years">应用</button>
      </div>
      <div id="chart"></div>
    </div>
  </section>
  <button type="button" class="sheet-peek" id="sheet-peek" aria-label="打开站点详情">⌃</button>

  <script>
    const state = {
      stations: [],
      latestById: new Map(),
      historyById: new Map(),
      markersById: new Map(),
      selectedId: null,
      selectedRangeDays: 30,
      clusterEnabled: true,
      cluster: null,
      markerLayer: null,
      clusterToggleButton: null,
      userLayer: null
    };

    const trendStyles = {
      rising: { label: "上涨", color: "#ef4444" },
      falling: { label: "下降", color: "#2563eb" },
      stable: { label: "稳定", color: "#16a34a" },
      unavailable: { label: "暂无", color: "#64748b" }
    };

    const map = L.map("map", { preferCanvas: true, zoomControl: true }).setView([34.5, 108.5], 5);
    const gaodeOptions = { subdomains: "1234", maxZoom: 18, attribution: "&copy; 高德地图" };
    const baseLayers = {
      "高德地图": L.tileLayer("https://webrd0{s}.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", gaodeOptions),
      "高德卫星": L.layerGroup([
        L.tileLayer("https://webst0{s}.is.autonavi.com/appmaptile?style=6&x={x}&y={y}&z={z}", gaodeOptions),
        L.tileLayer("https://webst0{s}.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}", gaodeOptions)
      ]),
      "OpenStreetMap": L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
        maxZoom: 18,
        attribution: "&copy; OpenStreetMap contributors"
      })
    };
    baseLayers["高德地图"].addTo(map);
    L.control.layers(baseLayers, null, { position: "bottomleft", collapsed: true }).addTo(map);
    state.cluster = L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 42 });
    state.markerLayer = L.layerGroup();
    map.addLayer(state.cluster);
    state.clusterToggleButton = document.getElementById("cluster-toggle");
    updateClusterToggleLabel();

    const escapeHtml = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[ch]));
    const uniqueSorted = (items) => [...new Set(items.filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    const hasMeasuredLevel = (record) =>
      record?.water_level_m !== null &&
      record?.water_level_m !== "" &&
      Number.isFinite(Number(record?.water_level_m));
    const isCurrentReading = (record) => record?.reading_status === "current" && hasMeasuredLevel(record);
    const fmtLevel = (value) =>
      value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} m` : "--";
    const fmtTime = (value) => value ? value.slice(5, 16).replace("T", " ") : "--";
    const fmtStatus = (record) => {
      if (isCurrentReading(record)) return "近三日有效";
      if (hasMeasuredLevel(record)) return "历史读数";
      return "暂无有效水位";
    };

    function parseCsv(text) {
      const rows = [];
      let row = [];
      let cell = "";
      let quoted = false;
      for (let i = 0; i < text.length; i += 1) {
        const ch = text[i];
        const next = text[i + 1];
        if (quoted) {
          if (ch === '"' && next === '"') {
            cell += '"';
            i += 1;
          } else if (ch === '"') {
            quoted = false;
          } else {
            cell += ch;
          }
        } else if (ch === '"') {
          quoted = true;
        } else if (ch === ",") {
          row.push(cell);
          cell = "";
        } else if (ch === "\\n") {
          row.push(cell);
          rows.push(row);
          row = [];
          cell = "";
        } else if (ch !== "\\r") {
          cell += ch;
        }
      }
      if (cell || row.length) {
        row.push(cell);
        rows.push(row);
      }
      const headers = rows.shift() || [];
      return rows.filter((line) => line.length && line.some(Boolean)).map((line) =>
        Object.fromEntries(headers.map((header, index) => [header, line[index] ?? ""]))
      );
    }

    function historyRecords(stationId) {
      return (state.historyById.get(String(stationId)) || [])
        .filter((record) => record.datetime && Number.isFinite(Number(record.water_level_m)))
        .sort((a, b) => new Date(a.datetime) - new Date(b.datetime));
    }

    function trendInfo(stationId, latest) {
      if (!isCurrentReading(latest)) {
        return { ...trendStyles.unavailable, delta: null, text: fmtStatus(latest) };
      }
      const records = historyRecords(stationId);
      if (records.length < 2) {
        return { ...trendStyles.unavailable, delta: null, text: "暂无对比" };
      }
      const current = records[records.length - 1];
      const previous = records[records.length - 2];
      const delta = Number(current.water_level_m) - Number(previous.water_level_m);
      if (delta >= 0.05) return { ...trendStyles.rising, delta, text: `上涨 ${delta.toFixed(2)} m` };
      if (delta <= -0.05) return { ...trendStyles.falling, delta, text: `下降 ${Math.abs(delta).toFixed(2)} m` };
      return { ...trendStyles.stable, delta, text: `稳定 ${delta >= 0 ? "+" : ""}${delta.toFixed(2)} m` };
    }

    function fillSelect(id, values, label) {
      const select = document.getElementById(id);
      select.innerHTML = `<option value="">${escapeHtml(label)}</option>` +
        values.map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("");
    }

    function selectedFilters() {
      return {
        basin: document.getElementById("basin-filter").value,
        admin: document.getElementById("admin-filter").value,
        river: document.getElementById("river-filter").value,
        query: document.getElementById("station-filter").value.trim()
      };
    }

    function hasActiveFilters() {
      const filters = selectedFilters();
      return Boolean(filters.basin || filters.admin || filters.river || filters.query);
    }

    function filteredStations() {
      const filters = selectedFilters();
      return state.stations.filter((item) =>
        (!filters.basin || item.basin === filters.basin) &&
        (!filters.admin || item.admin_region === filters.admin) &&
        (!filters.river || item.river === filters.river) &&
        (!filters.query || item.station.includes(filters.query) || item.river.includes(filters.query))
      );
    }

    function updateRiverFilter() {
      const selected = document.getElementById("river-filter").value;
      const { basin, admin } = selectedFilters();
      const rows = state.stations.filter((item) =>
        (!basin || item.basin === basin) &&
        (!admin || item.admin_region === admin)
      );
      const rivers = uniqueSorted(rows.map((item) => item.river));
      fillSelect("river-filter", rivers, "全部河流");
      if (selected && rivers.includes(selected)) document.getElementById("river-filter").value = selected;
    }

    function initializeFilters() {
      fillSelect("basin-filter", uniqueSorted(state.stations.map((item) => item.basin)), "全部流域");
      fillSelect("admin-filter", uniqueSorted(state.stations.map((item) => item.admin_region)), "全部行政区划");
      updateRiverFilter();
      ["basin-filter", "admin-filter"].forEach((id) => {
        document.getElementById(id).addEventListener("change", () => {
          updateRiverFilter();
          renderMarkers(true);
        });
      });
      document.getElementById("river-filter").addEventListener("change", () => renderMarkers(true));
      document.getElementById("station-filter").addEventListener("input", () => renderMarkers(false));
      document.getElementById("apply-filters").addEventListener("click", () => {
        renderMarkers(true);
        closeFilters();
      });
      document.getElementById("clear-filters").addEventListener("click", () => {
        document.getElementById("basin-filter").value = "";
        document.getElementById("admin-filter").value = "";
        document.getElementById("station-filter").value = "";
        updateRiverFilter();
        document.getElementById("river-filter").value = "";
        renderMarkers(false);
      });
    }

    function popupHtml(station, latest) {
      const trend = trendInfo(station.station_id, latest);
      return `<div class="popup-title">${escapeHtml(station.station)}</div>
        <div class="popup-line">河流：${escapeHtml(station.river || "--")}</div>
        <div class="popup-line">水位：${fmtLevel(latest?.water_level_m)}</div>
        <div class="popup-line">趋势：<span style="color:${trend.color};font-weight:700">${escapeHtml(trend.text)}</span></div>
        <div class="popup-line">时间：${fmtTime(latest?.datetime)}</div>`;
    }

    function updateClusterToggleLabel() {
      if (!state.clusterToggleButton) return;
      state.clusterToggleButton.textContent = state.clusterEnabled ? "聚合" : "展开";
      state.clusterToggleButton.classList.toggle("expanded", !state.clusterEnabled);
    }

    function setClusterEnabled(enabled) {
      state.clusterEnabled = enabled;
      if (map.hasLayer(state.cluster)) map.removeLayer(state.cluster);
      if (map.hasLayer(state.markerLayer)) map.removeLayer(state.markerLayer);
      map.addLayer(state.clusterEnabled ? state.cluster : state.markerLayer);
      updateClusterToggleLabel();
      renderMarkers(false);
    }

    function renderMarkers(fitFiltered = false) {
      state.cluster.clearLayers();
      state.markerLayer.clearLayers();
      state.markersById.clear();
      const rows = filteredStations();
      const targetLayer = state.clusterEnabled ? state.cluster : state.markerLayer;
      rows.forEach((station) => {
        const latest = state.latestById.get(String(station.station_id));
        const trend = trendInfo(station.station_id, latest);
        const marker = L.circleMarker([Number(station.snap_lat), Number(station.snap_lng)], {
          radius: window.innerWidth <= 760 ? 7 : 6,
          weight: 1.4,
          color: "#ffffff",
          fillColor: trend.color,
          fillOpacity: isCurrentReading(latest) ? 0.9 : 0.62
        });
        marker.bindPopup(popupHtml(station, latest));
        marker.on("click", () => selectStation(String(station.station_id), true));
        targetLayer.addLayer(marker);
        state.markersById.set(String(station.station_id), marker);
      });
      document.getElementById("visible-count").textContent = rows.length;
      if (fitFiltered && hasActiveFilters() && rows.length) {
        const bounds = L.latLngBounds(rows.map((item) => [Number(item.snap_lat), Number(item.snap_lng)]));
        map.fitBounds(bounds.pad(0.18), { maxZoom: 10 });
      }
    }

    function openFilters() { document.getElementById("filter-panel").classList.add("open"); }
    function closeFilters() { document.getElementById("filter-panel").classList.remove("open"); }
    function openSheet() {
      document.getElementById("station-sheet").classList.add("open");
      document.getElementById("sheet-peek").classList.remove("show");
      setTimeout(() => Plotly.Plots.resize(document.getElementById("chart")), 160);
    }
    function closeSheet() {
      document.getElementById("station-sheet").classList.remove("open");
      document.getElementById("sheet-peek").classList.toggle("show", Boolean(state.selectedId));
    }

    function selectStation(stationId, panToMarker = false) {
      state.selectedId = stationId;
      const station = state.stations.find((item) => String(item.station_id) === stationId);
      const latest = state.latestById.get(stationId);
      const trend = trendInfo(stationId, latest);
      document.getElementById("station-title").textContent = station.station;
      document.getElementById("station-meta").textContent = `${station.river || station.basin || ""} · ${station.admin_region || ""}`;
      document.getElementById("latest-level").textContent = fmtLevel(latest?.water_level_m);
      document.getElementById("latest-time").textContent = fmtTime(latest?.datetime);
      document.getElementById("trend-label").innerHTML =
        `<span class="trend-chip" style="background:${trend.color}">${escapeHtml(trend.text)}</span>`;
      if (panToMarker) {
        map.setView([Number(station.snap_lat), Number(station.snap_lng)], Math.max(map.getZoom(), 9));
      }
      renderChart();
      openSheet();
    }

    function rangeRecords(records) {
      if (!records.length || state.selectedRangeDays === Infinity) return records;
      const latest = new Date(records[records.length - 1].datetime);
      const start = new Date(latest.getTime() - state.selectedRangeDays * 86400000);
      return records.filter((record) => new Date(record.datetime) >= start);
    }

    function renderEmptyChart(message = "选择站点后显示趋势") {
      Plotly.newPlot("chart", [], {
        margin: { l: 48, r: 18, t: 18, b: 40 },
        xaxis: { title: "" },
        yaxis: { title: "水位 (m)" },
        annotations: [{ text: message, showarrow: false, x: 0.5, y: 0.5, xref: "paper", yref: "paper" }]
      }, { displaylogo: false, responsive: true });
    }

    function renderChart() {
      const records = rangeRecords(historyRecords(state.selectedId));
      const station = state.stations.find((item) => String(item.station_id) === state.selectedId);
      if (!records.length) {
        renderEmptyChart("该站点暂无历史水位数据");
        return;
      }
      Plotly.newPlot("chart", [{
        x: records.map((record) => record.datetime),
        y: records.map((record) => Number(record.water_level_m)),
        mode: "lines+markers",
        line: { color: "#2563eb", width: 2.4 },
        marker: { color: "#2563eb", size: 7 },
        hovertemplate: "%{x|%Y-%m-%d %H:%M}<br>水位 %{y:.2f} m<extra></extra>"
      }], {
        margin: { l: 48, r: 18, t: 24, b: 42 },
        title: { text: station.station, font: { size: 14 } },
        xaxis: { type: "date", rangeslider: { visible: true, thickness: 0.08 }, gridcolor: "#edf2f7" },
        yaxis: { title: "水位 (m)", gridcolor: "#edf2f7", zeroline: false },
        paper_bgcolor: "#ffffff",
        plot_bgcolor: "#ffffff"
      }, { displaylogo: false, responsive: true, scrollZoom: true });
    }

    function setRange(days, button) {
      state.selectedRangeDays = days;
      document.querySelectorAll(".range-controls button").forEach((item) => item.classList.remove("active"));
      if (button) button.classList.add("active");
      if (state.selectedId) renderChart();
    }

    function distanceKm(aLat, aLng, bLat, bLng) {
      const toRad = (value) => value * Math.PI / 180;
      const earth = 6371;
      const dLat = toRad(bLat - aLat);
      const dLng = toRad(bLng - aLng);
      const s1 = Math.sin(dLat / 2) ** 2;
      const s2 = Math.cos(toRad(aLat)) * Math.cos(toRad(bLat)) * Math.sin(dLng / 2) ** 2;
      return 2 * earth * Math.asin(Math.sqrt(s1 + s2));
    }

    function nearestStation(lat, lng) {
      let nearest = null;
      let nearestDistance = Infinity;
      state.stations.forEach((station) => {
        const distance = distanceKm(lat, lng, Number(station.snap_lat), Number(station.snap_lng));
        if (distance < nearestDistance) {
          nearest = station;
          nearestDistance = distance;
        }
      });
      return nearest ? { station: nearest, distance: nearestDistance } : null;
    }

    function locateUser(auto = false) {
      if (!navigator.geolocation) return;
      navigator.geolocation.getCurrentPosition((position) => {
        const lat = position.coords.latitude;
        const lng = position.coords.longitude;
        const accuracy = position.coords.accuracy || 0;
        if (state.userLayer) map.removeLayer(state.userLayer);
        state.userLayer = L.layerGroup([
          L.circle([lat, lng], { radius: accuracy, color: "#2563eb", weight: 1, fillOpacity: 0.08 }),
          L.circleMarker([lat, lng], { radius: 7, color: "#fff", weight: 2, fillColor: "#2563eb", fillOpacity: 1 })
        ]).addTo(map);
        map.setView([lat, lng], 10);
        const nearest = nearestStation(lat, lng);
        if (nearest) {
          document.getElementById("updated-at").textContent =
            `附近站点：${nearest.station.station}，约 ${nearest.distance.toFixed(0)} km`;
          if (!auto && nearest.distance <= 120) selectStation(String(nearest.station.station_id), false);
        }
      }, () => {}, { enableHighAccuracy: true, timeout: 8000, maximumAge: 600000 });
    }

    async function loadData() {
      const [stationsPayload, latestPayload, historyText] = await Promise.all([
        fetch("./data/stations.json").then((r) => r.json()),
        fetch("./data/latest.json").then((r) => r.json()),
        fetch("./data/history.csv").then((r) => r.text())
      ]);
      state.stations = stationsPayload.stations;
      latestPayload.records.forEach((record) => state.latestById.set(String(record.station_id), record));
      parseCsv(historyText).forEach((record) => {
        const id = String(record.station_id);
        if (!state.historyById.has(id)) state.historyById.set(id, []);
        state.historyById.get(id).push(record);
      });
      for (const records of state.historyById.values()) {
        records.sort((a, b) => new Date(a.datetime) - new Date(b.datetime));
      }
      initializeFilters();
      renderMarkers(false);
      renderEmptyChart();
      document.getElementById("total-count").textContent = state.stations.length;
      document.getElementById("updated-at").textContent = new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
      }).format(new Date(latestPayload.generated_at));
      if (window.matchMedia("(max-width: 760px)").matches) locateUser(true);
    }

    document.getElementById("filter-toggle").addEventListener("click", openFilters);
    document.getElementById("filter-close").addEventListener("click", closeFilters);
    document.getElementById("sheet-close").addEventListener("click", closeSheet);
    document.getElementById("sheet-peek").addEventListener("click", openSheet);
    document.getElementById("locate-btn").addEventListener("click", () => locateUser(false));
    document.getElementById("cluster-toggle").addEventListener("click", () => setClusterEnabled(!state.clusterEnabled));
    document.querySelectorAll("[data-days]").forEach((button) => {
      button.addEventListener("click", () => setRange(Number(button.dataset.days), button));
    });
    document.querySelector("[data-all]").addEventListener("click", (event) => setRange(Infinity, event.currentTarget));
    document.getElementById("apply-years").addEventListener("click", (event) => {
      const years = Math.max(1, Number(document.getElementById("years-input").value) || 1);
      setRange(years * 365, event.currentTarget);
    });
    new ResizeObserver(() => Plotly.Plots.resize(document.getElementById("chart"))).observe(document.getElementById("station-sheet"));

    loadData().catch((error) => {
      console.error(error);
      alert("数据加载失败，请稍后重试。");
    });
  </script>
</body>
</html>
"""


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fetched_at_dt = datetime.now(TZ)
    fetched_at = fetched_at_dt.isoformat(timespec="seconds")
    latest_rows = fetch_river_rows(fetched_at_dt)
    if not latest_rows:
        print("No river station records fetched.", file=sys.stderr)
        return 1

    stations = station_rows(latest_rows)
    history = merge_history(load_history(HISTORY_CSV), latest_rows, fetched_at)

    write_csv(STATIONS_CSV, stations, STATION_FIELDS)
    write_csv(HISTORY_CSV, history, HISTORY_FIELDS)
    data_updated_at = latest_data_timestamp(latest_rows, fetched_at)
    LATEST_JSON.write_text(json.dumps({"generated_at": data_updated_at, "records": latest_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_docs(stations, latest_rows, history, fetched_at)

    print(f"Fetched {len(latest_rows)} river records for {len(stations)} stations.")
    print(f"History rows: {len(history)}.")
    print(f"Wrote {DOCS_HTML.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
