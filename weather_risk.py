"""Official Day 1 outlooks and active alerts for Tazewell County, Illinois."""

import json
import ssl
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import certifi

COUNTY_ZONE = "ILC179"
FORECAST_ZONE = "ILZ037"
COUNTY_URL = f"https://api.weather.gov/zones/county/{COUNTY_ZONE}"
ALERTS_URL = "https://api.weather.gov/alerts/active"
SPC_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/outlooks/SPC_wx_outlks/MapServer/1/query"
WPC_URL = "https://mapservices.weather.noaa.gov/vector/rest/services/hazards/wpc_precip_hazards/MapServer/0/query"
REFRESH_INTERVAL = timedelta(minutes=30)
CACHE_MAX_AGE = timedelta(hours=2)
SOURCES = ("SPC", "WPC", "NWS")
ALERT_EVENTS = {
    "Tornado Watch", "Tornado Warning", "Severe Thunderstorm Watch",
    "Severe Thunderstorm Warning", "Flood Watch", "Flood Warning",
    "Flash Flood Watch", "Flash Flood Warning",
}
SPC_LEVELS = {2: "General thunderstorms", 3: "Marginal", 4: "Slight", 5: "Enhanced", 6: "Moderate", 8: "High"}
WPC_LEVELS = {0: "None", 1: "Marginal", 2: "Slight", 3: "Moderate", 4: "High"}


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    for date_format in ("%Y%m%d%H%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(value, date_format).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Missing time zone")
    return result.astimezone(timezone.utc)


def county_query_geometry(geometry):
    """Convert NWS GeoJSON rings to Esri rings; NOAA performs the intersection."""
    if geometry["type"] == "Polygon":
        polygons = [geometry["coordinates"]]
    elif geometry["type"] == "MultiPolygon":
        polygons = geometry["coordinates"]
    else:
        raise ValueError("County boundary is not a polygon")
    rings = []
    for polygon in polygons:
        for index, ring in enumerate(polygon):
            if len(ring) < 4 or ring[0] != ring[-1]:
                raise ValueError("Invalid county boundary ring")
            area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(ring, ring[1:]))
            # Esri exterior rings are clockwise; holes are counterclockwise.
            rings.append(list(reversed(ring)) if (area > 0) == (index == 0) else ring)
    if not rings:
        raise ValueError("Empty county boundary")
    return {"rings": rings, "spatialReference": {"wkid": 4326}}


@dataclass(frozen=True)
class SourceResult:
    source: str
    risk: bool
    reason: str
    checked_at: datetime
    valid_until: datetime

    def usable(self, now):
        return timedelta(0) <= now - self.checked_at < CACHE_MAX_AGE and now < self.valid_until


@dataclass(frozen=True)
class WeatherDecision:
    spotting: bool
    reasons: tuple
    unavailable: tuple
    cached: tuple
    checked_at: datetime | None

    @property
    def status(self):
        return (":tornado:", "Available for spotting") if self.spotting else (":large_green_circle:", "Available")


class WeatherState:
    def __init__(self, cache_path=None):
        self.cache_path = cache_path
        self.results = {}
        self.errors = {}
        if cache_path is not None:
            try:
                records = json.loads(cache_path.read_text())
                for source in SOURCES:
                    record = records.get(source)
                    if not isinstance(record, dict) or type(record.get("risk")) is not bool:
                        continue
                    result = SourceResult(source, record["risk"], str(record["reason"]),
                                          parse_time(record["checked_at"]), parse_time(record["valid_until"]))
                    if result.usable(utc_now()):
                        self.results[source] = result
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                self.results = {}

    def accept(self, results, errors):
        self.results.update(results)
        self.errors = errors
        if self.cache_path is not None:
            records = {}
            for source, result in self.results.items():
                record = asdict(result)
                record["checked_at"] = result.checked_at.isoformat()
                record["valid_until"] = result.valid_until.isoformat()
                records[source] = record
            try:
                temporary = self.cache_path.with_suffix(".tmp")
                temporary.write_text(json.dumps(records))
                temporary.replace(self.cache_path)
            except OSError:
                pass

    def decision(self, now=None):
        now = now or utc_now()
        usable = [result for result in self.results.values() if result.usable(now)]
        unavailable = tuple(source for source in SOURCES if not any(r.source == source for r in usable))
        cached = tuple(r.source for r in usable if r.source in self.errors)
        return WeatherDecision(
            any(result.risk for result in usable),
            tuple(result.reason for result in usable if result.risk),
            unavailable, cached, max((r.checked_at for r in usable), default=None),
        )


class WeatherService:
    def __init__(self, fetch_json=None):
        self.fetch_json = fetch_json or self._fetch_json
        self.county_geometry = None

    @staticmethod
    def _fetch_json(url, params=None):
        if params:
            url += "?" + urlencode(params)
        request = Request(url, headers={
            "User-Agent": "JasonSlackStatus/1.0 (personal status app; Tazewell County IL)",
            "Accept": "application/geo+json, application/json",
        })
        with urlopen(request, timeout=15, context=ssl.create_default_context(cafile=certifi.where())) as response:
            data = json.load(response)
        if not isinstance(data, dict) or "error" in data:
            raise ValueError("Weather service returned an error")
        return data

    def fetch(self):
        results, errors = {}, {}
        try:
            if self.county_geometry is None:
                county = self.fetch_json(COUNTY_URL)
                if county["properties"]["name"] != "Tazewell":
                    raise ValueError("Unexpected county boundary")
                self.county_geometry = county_query_geometry(county["geometry"])
        except Exception as error:
            errors.update({"SPC": str(error), "WPC": str(error)})
        jobs = {"NWS": self.fetch_alerts}
        if self.county_geometry is not None:
            jobs.update({"SPC": lambda: self.fetch_outlook("SPC"), "WPC": lambda: self.fetch_outlook("WPC")})
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = {source: pool.submit(job) for source, job in jobs.items()}
            for source, future in futures.items():
                try:
                    results[source] = future.result()
                except Exception as error:
                    errors[source] = str(error)
        return results, errors

    def query_features(self, url, **params):
        data = self.fetch_json(url, {"where": "1=1", "outFields": "*", "returnGeometry": "false", "f": "json", **params})
        if data.get("exceededTransferLimit") or not isinstance(data.get("features"), list):
            raise ValueError("Incomplete outlook response")
        return [feature["attributes"] for feature in data["features"]]

    def fetch_outlook(self, source):
        url = SPC_URL if source == "SPC" else WPC_URL
        start_field, end_field = ("valid", "expire") if source == "SPC" else ("start_time", "end_time")
        levels = SPC_LEVELS if source == "SPC" else WPC_LEVELS
        threshold = 3 if source == "SPC" else 1
        now = utc_now()
        # A county query can be empty on a quiet day. Check the national product's
        # validity too, so an empty/stale service is not mistaken for no risk.
        national = self.query_features(url)
        valid = [row for row in national if parse_time(row[start_field]) <= now < parse_time(row[end_field])]
        if not valid:
            raise ValueError("No current Day 1 outlook available")
        county = self.query_features(url, geometry=json.dumps(self.county_geometry),
                                     geometryType="esriGeometryPolygon", inSR="4326", spatialRel="esriSpatialRelIntersects")
        active = [row for row in county if parse_time(row[start_field]) <= now < parse_time(row[end_field])]
        ranks = [int(row["dn"]) for row in active]
        if any(rank not in levels for rank in ranks):
            raise ValueError("Unrecognized outlook risk category")
        rank = max(ranks, default=0)
        risk = rank >= threshold
        reason = f"{source}: {levels[rank]}" if risk else f"{source}: below Marginal"
        return SourceResult(source, risk, reason, now, min(parse_time(row[end_field]) for row in valid))

    def fetch_alerts(self):
        now = utc_now()
        data = self.fetch_json(ALERTS_URL, {"zone": f"{COUNTY_ZONE},{FORECAST_ZONE}", "status": "actual"})
        if not isinstance(data.get("features"), list) or data.get("pagination", {}).get("next"):
            raise ValueError("Incomplete alerts response")
        active = []
        for feature in data["features"]:
            properties = feature["properties"]
            if properties.get("status") != "Actual" or properties.get("messageType") == "Cancel":
                continue
            if properties.get("event") not in ALERT_EVENTS:
                continue
            zones = set(properties.get("geocode", {}).get("UGC", []))
            zones.update(zone.rsplit("/", 1)[-1] for zone in properties.get("affectedZones", []))
            if not zones.intersection({COUNTY_ZONE, FORECAST_ZONE}):
                continue
            ends = properties.get("ends") or properties["expires"]
            expiry = parse_time(ends)
            if expiry > now:
                active.append((properties["event"], expiry))
        reasons = sorted({event for event, _ in active})
        return SourceResult("NWS", bool(active), "NWS: " + (", ".join(reasons) if reasons else "no qualifying alerts"),
                            now, min((expiry for _, expiry in active), default=now + CACHE_MAX_AGE))
