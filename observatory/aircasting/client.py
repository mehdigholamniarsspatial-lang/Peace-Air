"""AirCasting downloader.

A module version of ``AirCasting_Download_AIRBEAM3_B0B21C7627C4.ipynb``. The request
logic, caching, windowing, region filtering, timestamp handling and CSV columns are
kept as in the notebook; the only structural change is that the notebook's global
configuration cell is now a :class:`DownloadConfig` object, so the Django app can run
one download per registered device (from the web UI, a management command or the
scheduler) without editing code.

Only GET requests are made. No data is fabricated: failed windows are reported in the
manifest rather than treated as empty successes.
"""
from __future__ import annotations

import base64
import csv
import gzip
import hashlib
import json
import logging
import math
import re
import time
import zipfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo

log = logging.getLogger("observatory.aircasting")

COLUMNS = [
    "device_group", "sensor_package_name", "device_query", "session_id", "stream_id", "session_type",
    "sensor_name", "measurement_type", "unit", "raw_time", "source_time", "time_utc", "time_status",
    "value", "latitude", "longitude", "coordinate_source", "source_row_index",
]


def package_variants(device_id: str) -> list[str]:
    """AirCasting stores package names with exact spelling, so try the common forms."""
    model, _, mac = device_id.replace("-", ":").partition(":")
    model = "AirBeam" + model.upper().replace("AIRBEAM", "")
    variants: list[str] = []
    for sep in (":", "-"):
        for m in (mac.lower(), mac.upper()):
            candidate = model + sep + m
            if candidate not in variants:
                variants.append(candidate)
    if device_id not in variants:
        variants.append(device_id)
    return variants


@dataclass
class DownloadConfig:
    device_id: str = ""
    device_packages: list[str] = field(default_factory=list)
    known_session_ids: list[int] = field(default_factory=list)
    project_tags: list[str] = field(default_factory=list)
    discovery_start: str = "2020-01-01"
    download_from: str = "2020-01-01"
    download_until: str | None = None  # exclusive; default = tomorrow
    discovery_window_days: int = 365
    fixed_window_days: int = 7
    region_name: str = "all_locations"
    bbox: tuple[float, float, float, float] | None = None
    region_geojson: str | None = None
    time_convention: str = "unverified"  # "unverified", "local_as_utc" or "utc"
    timezone_name: str = "Europe/Dublin"
    output_root: Path = Path("aircasting_downloads")
    refresh: bool = True
    request_timeout: int = 45
    request_pause: float = 0.2
    base_url: str = "https://aircasting.org"

    def __post_init__(self):
        if self.device_id and not self.device_packages:
            self.device_packages = package_variants(self.device_id)
        if self.download_until is None:
            self.download_until = (datetime.now() + timedelta(days=1)).date().isoformat()
        self.output_root = Path(self.output_root)
        if self.time_convention not in {"unverified", "utc", "local_as_utc"}:
            raise ValueError("Unknown time_convention")
        if self.time_convention == "local_as_utc":
            ZoneInfo(self.timezone_name)


# --------------------------------------------------------------------------- helpers
class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def safe_name(value) -> str:
    text = str(value)
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", text)[:75] + "_" + hashlib.sha256(text.encode()).hexdigest()[:8]


def save_json(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def as_clock(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, timezone.utc).replace(tzinfo=None)
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)


def encoded_ms(value) -> int:
    return int(as_clock(value).replace(tzinfo=timezone.utc).timestamp() * 1000)


def windows(start, stop, days):
    if days <= 0:
        raise ValueError("Window length must be positive")
    start, stop = as_clock(start), as_clock(stop)
    if stop <= start:
        raise ValueError("End date must be later than start date")
    while start < stop:
        end = min(start + timedelta(days=days), stop)
        yield start, end
        start = end


def normalise_package(value) -> str:
    parts = re.split(r"([:\-])", str(value), maxsplit=1)
    return parts[0] + parts[1] + parts[2].lower() if len(parts) == 3 else str(value)


def valid_coordinates(lon, lat) -> bool:
    try:
        return (math.isfinite(float(lon)) and math.isfinite(float(lat))
                and -180 <= float(lon) <= 180 and -90 <= float(lat) <= 90)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- regions
def load_region(filename):
    if not filename:
        return []
    obj = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
    if obj.get("crs"):
        name = json.dumps(obj["crs"]).lower()
        if "4326" not in name and "crs84" not in name:
            raise ValueError("Transform the GeoJSON to WGS84 first")

    def polygons(node):
        kind = node.get("type")
        if kind == "FeatureCollection":
            return [p for f in node["features"] for p in polygons(f)]
        if kind == "Feature":
            return polygons(node["geometry"])
        if kind == "Polygon":
            return [node["coordinates"]]
        if kind == "MultiPolygon":
            return node["coordinates"]
        raise ValueError("Use Polygon or MultiPolygon GeoJSON")

    result = polygons(obj)
    if not result:
        raise ValueError("The region contains no polygons")
    for polygon in result:
        if not polygon:
            raise ValueError("Empty polygon")
        for ring in polygon:
            if len(ring) < 4 or ring[0][:2] != ring[-1][:2]:
                raise ValueError("Each polygon ring must be closed with at least four positions")
            for point in ring:
                x, y = point[:2]
                if not (-180 <= x <= 180 and -90 <= y <= 90):
                    raise ValueError("Invalid longitude/latitude coordinates")
            if any(abs(a[0] - b[0]) > 180 for a, b in zip(ring, ring[1:])):
                raise ValueError("Split regions at the antimeridian before use")
    return result


def ring_position(x, y, ring):
    inside = False
    for a, b in zip(ring, ring[1:]):
        ax, ay = a[:2]
        bx, by = b[:2]
        cross = (x - ax) * (by - ay) - (y - ay) * (bx - ax)
        if (abs(cross) <= 1e-12 and min(ax, bx) - 1e-12 <= x <= max(ax, bx) + 1e-12
                and min(ay, by) - 1e-12 <= y <= max(ay, by) + 1e-12):
            return 2  # boundary
        if (ay > y) != (by > y) and x < (bx - ax) * (y - ay) / (by - ay) + ax:
            inside = not inside
    return 1 if inside else 0


def polygon_covers(x, y, polygon):
    outer = ring_position(x, y, polygon[0])
    if outer == 0:
        return False
    if outer == 2:
        return True
    for hole in polygon[1:]:
        position = ring_position(x, y, hole)
        if position == 2:
            return True
        if position == 1:
            return False
    return True


# --------------------------------------------------------------------------- client
class AirCastingClient:
    ALLOWED_HOSTS = {"aircasting.org", "aircasting.habitatmap.org"}

    def __init__(self, cfg: DownloadConfig):
        parsed = urlsplit(cfg.base_url)
        if parsed.scheme != "https" or parsed.hostname not in self.ALLOWED_HOSTS or parsed.username or parsed.password:
            raise ValueError("Use the documented HTTPS AirCasting host, without credentials in the URL.")
        self.cfg = cfg
        self.root = cfg.output_root
        self.cache = self.root / "raw_responses"
        self.cache.mkdir(parents=True, exist_ok=True)
        self.opener = build_opener(_NoRedirect())
        self.calls: list[dict] = []

    def get(self, path, params=None, token=None, refresh=None):
        if not path.startswith("/api/") or ".." in path:
            raise ValueError("Expected an AirCasting API path")
        params = params or {}
        url = self.cfg.base_url.rstrip("/") + path + ("?" + urlencode(params, doseq=True) if params else "")
        cache_path = self.cache / (hashlib.sha256(url.encode()).hexdigest() + ".json.gz")
        refresh = self.cfg.refresh if refresh is None else refresh
        if not token and not refresh and cache_path.exists():
            with gzip.open(cache_path, "rt", encoding="utf-8") as fh:
                envelope = json.load(fh)
            self.calls.append({"path": path, "cache": str(cache_path), "cached": True, "fetched_at": envelope["fetched_at"]})
            return envelope["data"]
        headers = {"Accept": "application/json", "User-Agent": "AirQualityObservatory/1.0"}
        if token:
            headers["Authorization"] = "Basic " + base64.b64encode((token + ":X").encode()).decode()
        for attempt in range(4):
            try:
                time.sleep(self.cfg.request_pause)
                with self.opener.open(Request(url, headers=headers, method="GET"), timeout=self.cfg.request_timeout) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if "json" not in content_type.lower():
                        raise RuntimeError("The endpoint returned non-JSON content; check deployed API compatibility.")
                    raw = response.read()
                    if response.headers.get("Content-Encoding") == "gzip":
                        raw = gzip.decompress(raw)
                    data = json.loads(raw)
                fetched_at = datetime.now(timezone.utc).isoformat()
                if not token:
                    temporary = cache_path.with_name(cache_path.name + ".tmp")
                    with gzip.open(temporary, "wt", encoding="utf-8") as fh:
                        json.dump({"endpoint": path, "params": params, "fetched_at": fetched_at, "data": data}, fh, ensure_ascii=False)
                    temporary.replace(cache_path)
                self.calls.append({"path": path, "cache": str(cache_path) if not token else None, "cached": False, "fetched_at": fetched_at})
                return data
            except HTTPError as error:
                if error.code not in {429, 500, 502, 503, 504} or attempt == 3:
                    raise RuntimeError(f"HTTP {error.code} at {path}. Check access, identifiers, and deployed API version.") from None
                retry = error.headers.get("Retry-After", "")
                delay = float(retry) if retry.isdigit() else 2 ** attempt
                if delay > 60:
                    raise RuntimeError(f"Server requested a {delay:g}-second pause. Retry later; cached responses are retained.") from None
                time.sleep(delay)
            except (URLError, TimeoutError):
                if attempt == 3:
                    raise RuntimeError(f"Network request failed at {path}; check your connection.") from None
                time.sleep(2 ** attempt)


# --------------------------------------------------------------------------- downloader
class AirCastingDownloader:
    """Discovery -> retrieval -> CSV export, exactly as sections 2b–7 of the notebook."""

    def __init__(self, cfg: DownloadConfig, client: AirCastingClient | None = None):
        self.cfg = cfg
        self.client = client or AirCastingClient(cfg)
        self.region_polygons = load_region(cfg.region_geojson)
        self.region_matches(0, 0)  # validate rectangle configuration

    # ---- section 2b
    def test_access(self, days=365):
        end = datetime.now() + timedelta(days=1)
        start = end - timedelta(days=days)
        results = []
        for package in self.cfg.device_packages:
            params = {"start_datetime": start.replace(microsecond=0).isoformat(),
                      "end_datetime": end.replace(microsecond=0).isoformat(),
                      "sensor_package_name": package}
            try:
                response = self.client.get("/api/v3/sessions", params, refresh=True)
                items = response.get("sessions", []) if isinstance(response, dict) else []
                results.append({"query": package, "ok": True, "result": f"{len(items)} sessions",
                                "detail": ", ".join(str(s.get("id")) for s in items[:10])})
            except Exception as error:  # noqa: BLE001 - report every failure to the UI
                results.append({"query": package, "ok": False, "result": "Error", "detail": str(error)})
        for sid in self.cfg.known_session_ids:
            try:
                info = self.client.get(f"/api/fixed/sessions/{int(sid)}/streams.json", {"measurements_limit": 0}, refresh=True)
                streams = info.get("streams", [])
                results.append({"query": f"session {sid}", "ok": True,
                                "result": f"'{info.get('title', '')}', {len(streams)} streams",
                                "detail": ", ".join(f"{s.get('sensor_name')}={s.get('stream_id')}" for s in streams)})
            except Exception as error:  # noqa: BLE001
                results.append({"query": f"session {sid}", "ok": False, "result": "Error", "detail": str(error)})
        return results

    # ---- section 4
    def discover_sessions(self):
        cfg = self.cfg
        if not cfg.device_packages and not cfg.project_tags:
            return [], []
        sessions, errors = {}, []
        for package in cfg.device_packages or [None]:
            for start, stop in windows(cfg.discovery_start, cfg.download_until, cfg.discovery_window_days):
                params = {"start_datetime": start.isoformat(), "end_datetime": stop.isoformat()}
                if package:
                    params["sensor_package_name"] = package
                if cfg.project_tags:
                    params["tags[]"] = cfg.project_tags
                try:
                    response = self.client.get("/api/v3/sessions", params)
                    items = response.get("sessions") if isinstance(response, dict) else None
                    if not isinstance(items, list):
                        raise RuntimeError("Unexpected v3 session response")
                    for item in items:
                        sid = int(item["id"])
                        record = sessions.setdefault(sid, {**item, "streams": [], "device_queries": []})
                        if package and package not in record["device_queries"]:
                            record["device_queries"].append(package)
                        known = {int(s["id"]) for s in record["streams"]}
                        for stream in item["streams"]:
                            if int(stream["id"]) not in known:
                                record["streams"].append(stream)
                                known.add(int(stream["id"]))
                except Exception as error:  # noqa: BLE001
                    errors.append({"device_query": package, "start": start.isoformat(), "stop": stop.isoformat(), "error": str(error)})
        return sorted(sessions.values(), key=lambda x: x["id"]), errors

    def add_known_sessions(self, found, errors):
        have = {int(s["id"]) for s in found}
        for sid in self.cfg.known_session_ids:
            if int(sid) in have:
                continue
            try:
                info = self.client.get(f"/api/fixed/sessions/{int(sid)}/streams.json", {"measurements_limit": 0})
                start = info.get("start_datetime") or info.get("start_time_local") or info.get("start_time") or self.cfg.discovery_start
                found.append({"id": int(sid), "type": "FixedSession", "title": info.get("title", ""),
                              "start_datetime": start, "device_queries": [],
                              "streams": [{"id": int(s["stream_id"]), "sensor_name": s.get("sensor_name")} for s in info.get("streams", [])]})
            except Exception as error:  # noqa: BLE001
                errors.append({"device_query": f"known_session:{sid}", "error": str(error)})
        return sorted(found, key=lambda x: int(x["id"])), errors

    # ---- section 5
    def region_matches(self, lon, lat):
        bbox = self.cfg.bbox
        if bbox is None and not self.region_polygons:
            return True
        if not valid_coordinates(lon, lat):
            return False
        x, y = float(lon), float(lat)
        if bbox is not None:
            west, south, east, north = bbox
            if not (-180 <= west < east <= 180 and -90 <= south < north <= 90):
                raise ValueError("BBOX must be (west, south, east, north), without antimeridian crossing")
            if not (west <= x <= east and south <= y <= north):
                return False
        return not self.region_polygons or any(polygon_covers(x, y, p) for p in self.region_polygons)

    def timestamp_fields(self, raw):
        cfg = self.cfg
        try:
            clock = as_clock(raw)
        except (ValueError, TypeError, OverflowError, OSError):
            return "", "", "invalid_source_time"
        if cfg.time_convention == "unverified":
            return clock.isoformat(), "", "source_clock_unverified"
        if cfg.time_convention == "utc":
            instant = clock.replace(tzinfo=timezone.utc) if isinstance(raw, (int, float)) else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if instant.tzinfo is None:
                instant = instant.replace(tzinfo=timezone.utc)
            return clock.isoformat(), instant.astimezone(timezone.utc).isoformat(), "UTC_assumption_confirmed_by_user"
        zone = ZoneInfo(cfg.timezone_name)
        candidates = set()
        for fold in (0, 1):
            utc = clock.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if utc.astimezone(zone).replace(tzinfo=None) == clock:
                candidates.add(utc.isoformat())
        if len(candidates) != 1:
            return clock.isoformat(), "", "DST_ambiguous" if candidates else "DST_nonexistent"
        return clock.isoformat(), next(iter(candidates)), "local_clock_zone_confirmed_by_user"

    # ---- section 6
    def measurement_batches(self, session):
        cfg, client = self.cfg, self.client
        sid = int(session["id"])
        if session["type"] == "MobileSession":
            for stream in session["streams"]:
                stream_id = int(stream["id"])
                try:
                    result = client.get(f"/api/mobile/sessions2/{sid}.json", {"sensor_name": stream["sensor_name"]})
                    candidates = result.get("streams", {})
                    matches = [s for s in candidates.values() if int(s["id"]) == stream_id]
                    if len(matches) != 1:
                        raise RuntimeError("Returned stream ID differs from discovery; ambiguous channel mapping")
                    metadata = matches[0]
                    package = metadata.get("sensor_package_name")
                    queries = session.get("device_queries", [])
                    if queries and (not package or normalise_package(package) not in {normalise_package(q) for q in queries}):
                        yield stream_id, {}, [], {"status": "excluded_other_device", "session_id": sid, "stream_id": stream_id}
                        continue
                    records = metadata.get("measurements")
                    if not isinstance(records, list):
                        raise RuntimeError("Missing mobile measurement array")
                    expected = metadata.get("measurements_count")
                    status = ("count_matches" if expected is not None and int(expected) == len(records)
                              else "count_unavailable" if expected is None else "count_mismatch")
                    yield stream_id, metadata, records, {"status": status, "session_id": sid, "stream_id": stream_id,
                                                         "source_count": expected, "returned_count": len(records)}
                except Exception as error:  # noqa: BLE001
                    yield stream_id, {}, [], {"status": "failed", "session_id": sid, "stream_id": stream_id, "error": str(error)}
        elif session["type"] == "FixedSession":
            try:
                info = client.get(f"/api/fixed/sessions/{sid}/streams.json", {"measurements_limit": 0})
                metadata_by_id = {int(s["stream_id"]): s for s in info["streams"]}
            except Exception as error:  # noqa: BLE001
                yield None, {}, [], {"status": "failed", "session_id": sid, "error": str(error)}
                return
            for stream in session["streams"]:
                stream_id = int(stream["id"])
                if stream_id not in metadata_by_id:
                    yield stream_id, {}, [], {"status": "failed", "session_id": sid, "stream_id": stream_id, "error": "Fixed stream metadata missing"}
                    continue
                metadata = {**metadata_by_id[stream_id],
                            "latitude": None if info.get("is_indoor") else info.get("latitude"),
                            "longitude": None if info.get("is_indoor") else info.get("longitude"),
                            "fixed": True, "is_indoor": info.get("is_indoor", False),
                            "session_title": info.get("title", "")}
                begin = max(as_clock(cfg.download_from), as_clock(session["start_datetime"]))
                stop = as_clock(cfg.download_until)
                if begin >= stop:
                    continue
                for start, end in windows(begin, stop, cfg.fixed_window_days):
                    report = {"session_id": sid, "stream_id": stream_id, "start": start.isoformat(), "stop_exclusive": end.isoformat()}
                    try:
                        records = client.get("/api/v3/fixed_measurements", {"stream_id": str(stream_id), "start_time": encoded_ms(start), "end_time": encoded_ms(end) - 1})
                        if not isinstance(records, list):
                            raise RuntimeError("Missing fixed measurement array")
                        if any(not (start <= as_clock(m["time"]) < end) for m in records):
                            raise RuntimeError("Fixed endpoint returned a record outside its requested window")
                        yield stream_id, metadata, records, {**report, "status": "retrieved_count_unverified", "returned_count": len(records)}
                    except Exception as error:  # noqa: BLE001
                        yield stream_id, {}, [], {**report, "status": "failed", "error": str(error)}
        else:
            yield None, {}, [], {"status": "failed", "session_id": sid, "error": "Unsupported session type: " + str(session["type"])}

    # ---- section 7
    def export_recordings(self, session_list, errors):
        cfg, client = self.cfg, self.client
        run = cfg.output_root / ("export_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ"))
        run.mkdir(parents=True)
        (run / "by_device").mkdir()
        reports, counts, device_files = [], Counter(), {}
        start, stop = as_clock(cfg.download_from), as_clock(cfg.download_until)
        if stop <= start:
            raise ValueError("download_until must follow download_from")
        source_indices = Counter()
        call_start = len(client.calls)
        try:
            with (run / "all_recordings.csv").open("w", newline="", encoding="utf-8-sig") as all_f, \
                    (run / "selected_region.csv").open("w", newline="", encoding="utf-8-sig") as selected_f:
                all_writer, selected_writer = [csv.DictWriter(f, fieldnames=COLUMNS) for f in (all_f, selected_f)]
                all_writer.writeheader()
                selected_writer.writeheader()
                for position, session in enumerate(session_list, 1):
                    log.info("Session %s/%s: %s", position, len(session_list), session["id"])
                    for stream_id, metadata, records, report in self.measurement_batches(session):
                        reports.append(report)
                        for reading in records:
                            key = (session["id"], stream_id)
                            source_indices[key] += 1
                            counts["source_records_returned"] += 1
                            source_time, utc_time, time_status = self.timestamp_fields(reading.get("time"))
                            if not source_time:
                                counts["invalid_timestamp_records"] += 1
                                continue
                            if not (start <= as_clock(source_time) < stop):
                                counts["outside_date_range"] += 1
                                continue
                            package = metadata.get("sensor_package_name", "")
                            queries = ";".join(session.get("device_queries", []))
                            group = package or ("query:" + queries if queries else "session:" + str(session["id"]))
                            fixed = metadata.get("fixed", False)
                            lat = metadata.get("latitude") if fixed else reading.get("latitude")
                            lon = metadata.get("longitude") if fixed else reading.get("longitude")
                            coordinate_source = ("withheld_indoor" if metadata.get("is_indoor")
                                                 else "fixed_session" if fixed else "measurement")
                            row = {"device_group": group, "sensor_package_name": package, "device_query": queries,
                                   "session_id": session["id"], "stream_id": stream_id, "session_type": session["type"],
                                   "sensor_name": metadata.get("sensor_name", ""), "measurement_type": metadata.get("measurement_type", ""),
                                   "unit": metadata.get("unit_symbol", metadata.get("sensor_unit", "")),
                                   "raw_time": reading.get("time"), "source_time": source_time, "time_utc": utc_time, "time_status": time_status,
                                   "value": reading.get("value"), "latitude": lat, "longitude": lon,
                                   "coordinate_source": coordinate_source, "source_row_index": source_indices[key]}
                            all_writer.writerow(row)
                            counts["all_recordings_rows"] += 1
                            if not valid_coordinates(lon, lat):
                                counts["missing_or_invalid_coordinates"] += 1
                            if self.region_matches(lon, lat):
                                selected_writer.writerow(row)
                                counts["selected_region_rows"] += 1
                                if group not in device_files:
                                    fh = (run / "by_device" / (safe_name(group) + ".csv")).open("w", newline="", encoding="utf-8-sig")
                                    writer = csv.DictWriter(fh, fieldnames=COLUMNS)
                                    writer.writeheader()
                                    device_files[group] = (fh, writer)
                                device_files[group][1].writerow(row)
        finally:
            for fh, _writer in device_files.values():
                fh.close()
        problem = bool(errors) or counts["invalid_timestamp_records"] > 0 or any(r["status"] in {"failed", "count_mismatch"} for r in reports)
        manifest = {
            "export_time_utc": datetime.now(timezone.utc).isoformat(), "source": cfg.base_url,
            "status": "partial_or_inconsistent" if problem else "requests_completed_upstream_completeness_unverified",
            "scope": {"device_packages": cfg.device_packages, "project_tags": cfg.project_tags,
                      "discovery_start": cfg.discovery_start, "download_from": cfg.download_from,
                      "download_until_exclusive": cfg.download_until, "region_name": cfg.region_name,
                      "bbox": cfg.bbox, "geojson_polygons": self.region_polygons,
                      "time_convention": cfg.time_convention, "timezone": cfg.timezone_name},
            "counts": dict(counts), "sessions_discovered": len(session_list), "discovery_errors": errors,
            "retrieval_reports": reports, "requests": client.calls[call_start:], "refresh": cfg.refresh,
            "notes": ["No measurement IDs or fixed-series total counts are exposed by these endpoints.",
                      "Device queries are search associations, not independently observed fixed hardware identifiers.",
                      "Raw responses and metadata are retained separately under raw_responses."],
        }
        save_json(run / "manifest.json", manifest)
        save_json(run / "sessions.json", session_list)
        archive = run / "recordings.zip"
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            for path in sorted(run.rglob("*")):
                if path.is_file() and path != archive:
                    zipped.write(path, path.relative_to(run))
        return run, manifest

    def run(self):
        """Full pipeline. Returns (export_dir or None, manifest-like dict)."""
        sessions, errors = self.discover_sessions()
        sessions, errors = self.add_known_sessions(sessions, errors)
        save_json(self.cfg.output_root / "discovered_sessions.json", sessions)
        save_json(self.cfg.output_root / "discovery_errors.json", errors)
        if not sessions:
            return None, {"status": "no_sessions", "discovery_errors": errors, "counts": {}}
        return self.export_recordings(sessions, errors)
