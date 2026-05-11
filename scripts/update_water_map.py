#!/usr/bin/env python3
from __future__ import annotations

import base64
import csv
import json
import math
import re
import sys
import time
from collections import defaultdict
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
DOCS_RIVERS = DOCS_DATA_DIR / "river_lines.geojson"
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
        text = second.decode("utf-8")
        return float(text)
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


def parse_iso_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def reading_status(dt: str, level: float | None, fetched_at_dt: datetime) -> str:
    if not dt or level is None:
        return "no_recent_reading"
    reading_dt = parse_iso_datetime(dt)
    if reading_dt and fetched_at_dt - reading_dt > timedelta(days=STALE_AFTER_DAYS):
        return "stale"
    return "current"


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


def normalize_datetime(value: str) -> str:
    if not value:
        return ""
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ).isoformat()
        except ValueError:
            pass
    return value


def record_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]


def write_csv(path: Path, rows: Iterable[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: format_value(row.get(field)) for field in fieldnames})


def format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


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
        merged[(str(row["station_id"]), dt)] = {
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
    stations = {}
    for row in latest_rows:
        sid = str(row["station_id"])
        stations[sid] = {field: row.get(field) for field in STATION_FIELDS}
    return sorted(stations.values(), key=lambda item: (str(item["basin"]), str(item["river"]), str(item["station"])))


def build_river_lines(stations: list[dict[str, object]]) -> dict[str, object]:
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for station in stations:
        if station.get("snap_lng") is None or station.get("snap_lat") is None:
            continue
        groups[(str(station.get("basin") or ""), str(station.get("river") or ""))].append(station)

    features = []
    for (basin, river), points in groups.items():
        if len(points) < 2 or not river:
            continue
        lngs = [float(p["snap_lng"]) for p in points]
        lats = [float(p["snap_lat"]) for p in points]
        if max(lngs) - min(lngs) >= max(lats) - min(lats):
            ordered = sorted(points, key=lambda p: (float(p["snap_lng"]), float(p["snap_lat"])))
        else:
            ordered = sorted(points, key=lambda p: (float(p["snap_lat"]), float(p["snap_lng"])))
        coords = []
        seen = set()
        for point in ordered:
            coord = (round(float(point["snap_lng"]), 6), round(float(point["snap_lat"]), 6))
            if coord not in seen:
                coords.append([coord[0], coord[1]])
                seen.add(coord)
        if len(coords) < 2:
            continue
        features.append(
            {
                "type": "Feature",
                "properties": {"basin": basin, "river": river, "station_count": len(points), "source": "station_sequence"},
                "geometry": {"type": "LineString", "coordinates": coords},
            }
        )
    return {"type": "FeatureCollection", "features": features}


def write_docs(stations: list[dict[str, object]], latest_rows: list[dict[str, object]], history: list[dict[str, object]], fetched_at: str) -> None:
    DOCS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    stations_payload = {
        "generated_at": fetched_at,
        "source": SOURCE_URL,
        "snap_note": "站点使用水利部全国水雨情信息站点坐标；河线为同名河流站点序列线，用于确保站点在可视化中落在对应河流线上。",
        "stations": stations,
    }
    latest_payload = {
        "generated_at": fetched_at,
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
    DOCS_RIVERS.write_text(json.dumps(build_river_lines(stations), ensure_ascii=False), encoding="utf-8")
    (DOCS_DIR / ".nojekyll").write_text("", encoding="utf-8")
    DOCS_HTML.write_text(render_html(), encoding="utf-8")


def render_html() -> str:
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>全国河道站实时水位地图</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css" />
  <link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css" />
  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
  <script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {
      --ink: #172033;
      --muted: #607086;
      --line: #d8e0ea;
      --surface: #ffffff;
      --bg: #f4f7fb;
      --blue: #2563eb;
      --green: #059669;
      --amber: #d97706;
      --red: #dc2626;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
      background: var(--bg);
      color: var(--ink);
    }
    main {
      display: grid;
      grid-template-columns: 330px 1fr;
      min-height: 100vh;
    }
    aside {
      background: var(--surface);
      border-right: 1px solid var(--line);
      padding: 18px 16px;
      overflow: auto;
    }
    h1 {
      margin: 0 0 6px;
      font-size: 24px;
      line-height: 1.16;
      letter-spacing: 0;
    }
    .subhead {
      margin: 0 0 16px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .filters {
      display: grid;
      gap: 10px;
      margin-bottom: 14px;
    }
    label {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
    }
    select, input, button {
      height: 34px;
      border: 1px solid var(--line);
      border-radius: 7px;
      background: #fff;
      color: var(--ink);
      font: inherit;
      font-size: 13px;
      padding: 0 9px;
    }
    button {
      cursor: pointer;
      padding: 0 11px;
    }
    button.active {
      border-color: var(--blue);
      color: var(--blue);
      background: #eff6ff;
      font-weight: 600;
    }
    .metrics {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 8px;
      margin: 12px 0;
    }
    .metric {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 9px 10px;
      background: #fbfdff;
    }
    .metric span {
      display: block;
      color: var(--muted);
      font-size: 12px;
      margin-bottom: 4px;
    }
    .metric strong {
      font-size: 17px;
    }
    .station-panel {
      border-top: 1px solid var(--line);
      padding-top: 14px;
      margin-top: 14px;
    }
    .station-title {
      margin: 0 0 4px;
      font-size: 18px;
    }
    .station-meta {
      margin: 0 0 10px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }
    .range-controls {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      margin-bottom: 8px;
    }
    #years-input { width: 62px; }
    #chart {
      height: 320px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .map-wrap {
      position: relative;
      min-width: 0;
    }
    #map {
      height: 100vh;
      width: 100%;
    }
    .legend {
      position: absolute;
      right: 14px;
      bottom: 20px;
      z-index: 500;
      background: rgba(255,255,255,.94);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 10px 12px;
      color: var(--muted);
      font-size: 12px;
      box-shadow: 0 8px 30px rgba(23,32,51,.12);
    }
    .dot {
      display: inline-block;
      width: 10px;
      height: 10px;
      border-radius: 50%;
      margin-right: 6px;
    }
    .popup-title { font-weight: 700; font-size: 14px; margin-bottom: 4px; }
    .popup-line { color: #3a4556; line-height: 1.45; }
    @media (max-width: 920px) {
      main { grid-template-columns: 1fr; }
      aside { border-right: 0; border-bottom: 1px solid var(--line); max-height: 58vh; }
      #map { height: 62vh; }
      #chart { height: 260px; }
    }
  </style>
</head>
<body>
  <main>
    <aside>
      <h1>全国河道站实时水位</h1>
      <p class="subhead">来源：全国水雨情信息。地图为实时快照，点击站点查看长期累积水位变化。</p>

      <section class="filters" aria-label="筛选">
        <label>流域<select id="basin-filter"><option value="">全部流域</option></select></label>
        <label>行政区划<select id="admin-filter"><option value="">全部行政区划</option></select></label>
        <label>河流<select id="river-filter"><option value="">全部河流</option></select></label>
        <label>站点<input id="station-filter" type="search" placeholder="输入站名关键字" /></label>
      </section>

      <div class="metrics">
        <div class="metric"><span>显示站点</span><strong id="visible-count">--</strong></div>
        <div class="metric"><span>全部河道站</span><strong id="total-count">--</strong></div>
        <div class="metric"><span>最高水位</span><strong id="max-level">--</strong></div>
        <div class="metric"><span>更新时间</span><strong id="updated-at">--</strong></div>
      </div>

      <section class="station-panel">
        <h2 class="station-title" id="station-title">选择一个站点</h2>
        <p class="station-meta" id="station-meta">在地图上点击站点，或用筛选框定位站点。</p>
        <div class="range-controls" aria-label="趋势时间范围">
          <button type="button" data-days="7">1周</button>
          <button type="button" data-days="30">1月</button>
          <button type="button" data-days="90">3个月</button>
          <button type="button" data-days="180">6个月</button>
          <button type="button" data-days="365">1年</button>
          <button type="button" data-all="true">全部</button>
          <input id="years-input" type="number" min="1" max="50" value="2" aria-label="自定义年数" />
          <button type="button" id="apply-years">N年</button>
        </div>
        <div id="chart"></div>
      </section>
    </aside>
    <section class="map-wrap">
      <div id="map"></div>
      <div class="legend">
        <div><span class="dot" style="background:#2563eb"></span>正常</div>
        <div><span class="dot" style="background:#d97706"></span>接近警戒</div>
        <div><span class="dot" style="background:#dc2626"></span>超警戒</div>
        <div><span class="dot" style="background:#64748b"></span>非实时/暂无水位</div>
      </div>
    </section>
  </main>

  <script>
    const state = {
      stations: [],
      latestById: new Map(),
      historyById: new Map(),
      markersById: new Map(),
      selectedId: null,
      selectedRangeDays: 60,
      riversLayer: null,
      cluster: null
    };

    const map = L.map("map", { preferCanvas: true }).setView([34.5, 108.5], 5);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
      maxZoom: 18,
      attribution: "&copy; OpenStreetMap contributors"
    }).addTo(map);
    state.cluster = L.markerClusterGroup({ showCoverageOnHover: false, maxClusterRadius: 46 });
    map.addLayer(state.cluster);

    const hasMeasuredLevel = (record) =>
      record?.water_level_m !== null &&
      record?.water_level_m !== "" &&
      Number.isFinite(Number(record?.water_level_m));
    const isCurrentReading = (record) => record?.reading_status === "current" && hasMeasuredLevel(record);
    const fmtLevel = (value) =>
      value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? `${Number(value).toFixed(2)} m` : "--";
    const fmtStatus = (record) => {
      if (isCurrentReading(record)) return "近三日有效";
      if (hasMeasuredLevel(record)) return "历史读数";
      return "暂无有效水位";
    };
    const uniqueSorted = (items) => [...new Set(items.filter(Boolean))].sort((a, b) => a.localeCompare(b, "zh-CN"));
    const getColor = (record) => {
      if (!isCurrentReading(record)) return "#64748b";
      const level = Number(record?.water_level_m);
      const alert = Number(record?.alert_level_m);
      if (Number.isFinite(alert) && alert > 0 && Number.isFinite(level)) {
        if (level >= alert) return "#dc2626";
        if (level >= alert - 0.5) return "#d97706";
      }
      return "#2563eb";
    };

    function parseCsv(text) {
      const lines = text.trim().split(/\\r?\\n/);
      const headers = lines.shift().split(",");
      return lines.filter(Boolean).map((line) => {
        const cells = line.split(",");
        return Object.fromEntries(headers.map((header, index) => [header, cells[index] ?? ""]));
      });
    }

    async function loadData() {
      const [stationsPayload, latestPayload, historyText, rivers] = await Promise.all([
        fetch("./data/stations.json").then((r) => r.json()),
        fetch("./data/latest.json").then((r) => r.json()),
        fetch("./data/history.csv").then((r) => r.text()),
        fetch("./data/river_lines.geojson").then((r) => r.json())
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
      state.riversLayer = L.geoJSON(rivers, {
        style: { color: "#0ea5e9", weight: 1.4, opacity: 0.38 },
        interactive: false
      }).addTo(map);
      initializeFilters();
      renderMarkers();
      renderEmptyChart();
      document.getElementById("total-count").textContent = state.stations.length;
      document.getElementById("updated-at").textContent = new Intl.DateTimeFormat("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false
      }).format(new Date(latestPayload.generated_at));
    }

    function fillSelect(id, values, label) {
      const select = document.getElementById(id);
      select.innerHTML = `<option value="">${label}</option>` + values.map((value) => `<option value="${value}">${value}</option>`).join("");
    }

    function initializeFilters() {
      fillSelect("basin-filter", uniqueSorted(state.stations.map((item) => item.basin)), "全部流域");
      fillSelect("admin-filter", uniqueSorted(state.stations.map((item) => item.admin_region)), "全部行政区划");
      updateRiverFilter();
      ["basin-filter", "admin-filter", "river-filter"].forEach((id) => {
        document.getElementById(id).addEventListener("change", () => {
          if (id !== "river-filter") updateRiverFilter();
          renderMarkers();
        });
      });
      document.getElementById("station-filter").addEventListener("input", renderMarkers);
    }

    function updateRiverFilter() {
      const selected = document.getElementById("river-filter").value;
      const basin = document.getElementById("basin-filter").value;
      const admin = document.getElementById("admin-filter").value;
      const rows = state.stations.filter((item) =>
        (!basin || item.basin === basin) &&
        (!admin || item.admin_region === admin)
      );
      fillSelect("river-filter", uniqueSorted(rows.map((item) => item.river)), "全部河流");
      if (selected && uniqueSorted(rows.map((item) => item.river)).includes(selected)) {
        document.getElementById("river-filter").value = selected;
      }
    }

    function filteredStations() {
      const basin = document.getElementById("basin-filter").value;
      const admin = document.getElementById("admin-filter").value;
      const river = document.getElementById("river-filter").value;
      const query = document.getElementById("station-filter").value.trim();
      return state.stations.filter((item) =>
        (!basin || item.basin === basin) &&
        (!admin || item.admin_region === admin) &&
        (!river || item.river === river) &&
        (!query || item.station.includes(query))
      );
    }

    function renderMarkers() {
      state.cluster.clearLayers();
      state.markersById.clear();
      const rows = filteredStations();
      let maxRecord = null;
      rows.forEach((station) => {
        const latest = state.latestById.get(String(station.station_id));
        const color = getColor(latest);
        const marker = L.circleMarker([Number(station.snap_lat), Number(station.snap_lng)], {
          radius: 6,
          weight: 1,
          color: "#ffffff",
          fillColor: color,
          fillOpacity: isCurrentReading(latest) ? 0.88 : 0.62
        });
        marker.bindPopup(popupHtml(station, latest));
        marker.on("click", () => selectStation(String(station.station_id), true));
        state.cluster.addLayer(marker);
        state.markersById.set(String(station.station_id), marker);
        if (isCurrentReading(latest) && (!maxRecord || Number(latest.water_level_m) > Number(maxRecord.water_level_m))) maxRecord = latest;
      });
      document.getElementById("visible-count").textContent = rows.length;
      document.getElementById("max-level").textContent = fmtLevel(maxRecord?.water_level_m);
      if (rows.length && rows.length < state.stations.length) {
        const bounds = L.latLngBounds(rows.map((item) => [Number(item.snap_lat), Number(item.snap_lng)]));
        map.fitBounds(bounds.pad(0.18), { maxZoom: 9 });
      }
    }

    function popupHtml(station, latest) {
      return `<div class="popup-title">${station.station}</div>
        <div class="popup-line">流域：${station.basin || "--"}</div>
        <div class="popup-line">河流：${station.river || "--"}</div>
        <div class="popup-line">行政区划：${station.admin_region || "--"}</div>
        <div class="popup-line">状态：${fmtStatus(latest)}</div>
        <div class="popup-line">水位：${fmtLevel(latest?.water_level_m)}</div>
        <div class="popup-line">时间：${latest?.datetime ? latest.datetime.slice(0,16).replace("T"," ") : "--"}</div>`;
    }

    function selectStation(stationId, panToMarker = false) {
      state.selectedId = stationId;
      const station = state.stations.find((item) => String(item.station_id) === stationId);
      const latest = state.latestById.get(stationId);
      document.getElementById("station-title").textContent = station.station;
      document.getElementById("station-meta").textContent =
        `${station.basin} / ${station.river} / ${station.admin_region}，${fmtStatus(latest)}，最新水位 ${fmtLevel(latest?.water_level_m)}`;
      if (panToMarker) {
        map.setView([Number(station.snap_lat), Number(station.snap_lng)], Math.max(map.getZoom(), 8));
      }
      renderChart();
    }

    function rangeRecords(records) {
      if (!records.length || state.selectedRangeDays === Infinity) return records;
      const latest = new Date(records[records.length - 1].datetime);
      const start = new Date(latest.getTime() - state.selectedRangeDays * 86400000);
      return records.filter((record) => new Date(record.datetime) >= start);
    }

    function renderEmptyChart(message = "选择站点后显示趋势") {
      Plotly.newPlot("chart", [], {
        margin: { l: 48, r: 18, t: 20, b: 42 },
        xaxis: { title: "" },
        yaxis: { title: "水位 (m)" },
        annotations: [{ text: message, showarrow: false, x: 0.5, y: 0.5, xref: "paper", yref: "paper" }]
      }, { displaylogo: false, responsive: true });
    }

    function renderChart() {
      const records = rangeRecords(state.historyById.get(state.selectedId) || []);
      const station = state.stations.find((item) => String(item.station_id) === state.selectedId);
      if (!records.length) {
        renderEmptyChart("该站点暂无历史水位数据");
        return;
      }
      Plotly.newPlot("chart", [{
        x: records.map((record) => record.datetime),
        y: records.map((record) => Number(record.water_level_m)),
        mode: "lines+markers",
        line: { color: "#2563eb", width: 2.5 },
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

    document.querySelectorAll("[data-days]").forEach((button) => {
      button.addEventListener("click", () => setRange(Number(button.dataset.days), button));
    });
    document.querySelector("[data-all]").addEventListener("click", (event) => setRange(Infinity, event.currentTarget));
    document.getElementById("apply-years").addEventListener("click", (event) => {
      const years = Math.max(1, Number(document.getElementById("years-input").value) || 1);
      setRange(years * 365, event.currentTarget);
    });

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
    LATEST_JSON.write_text(json.dumps({"generated_at": fetched_at, "records": latest_rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    write_docs(stations, latest_rows, history, fetched_at)

    print(f"Fetched {len(latest_rows)} river records for {len(stations)} stations.")
    print(f"History rows: {len(history)}.")
    print(f"Wrote {DOCS_HTML.relative_to(ROOT)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
