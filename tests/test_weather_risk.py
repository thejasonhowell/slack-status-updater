import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from weather_risk import (
    ALERTS_URL, SOURCES, SPC_URL, WPC_URL, SourceResult, WeatherService,
    WeatherState, county_query_geometry,
)

NOW = datetime(2026, 10, 21, 18, tzinfo=timezone.utc)


def result(source, risk=False, age=0, expires=6):
    return SourceResult(source, risk, f"{source}: Marginal" if risk else f"{source}: below threshold",
                        NOW - timedelta(minutes=age), NOW + timedelta(hours=expires))


class WeatherStateTests(unittest.TestCase):
    def test_quiet_day_and_either_positive_source(self):
        for positive in (None, *SOURCES):
            state = WeatherState()
            state.accept({source: result(source, source == positive) for source in SOURCES}, {})
            decision = state.decision(NOW)
            self.assertEqual(decision.spotting, positive is not None)
            self.assertFalse(decision.unavailable)
            self.assertEqual(decision.status[1], "Available for spotting" if positive else "Available")

    def test_recent_cache_survives_failure_but_expires(self):
        state = WeatherState()
        state.accept({source: result(source, source == "SPC", age=90) for source in SOURCES}, {})
        state.accept({}, {source: "Timeout" for source in SOURCES})
        self.assertTrue(state.decision(NOW).spotting)
        self.assertEqual(set(state.decision(NOW).cached), set(SOURCES))
        decision = state.decision(NOW + timedelta(minutes=31))
        self.assertFalse(decision.spotting)
        self.assertEqual(set(decision.unavailable), set(SOURCES))

    def test_product_and_alert_expiry_override_cache_age(self):
        state = WeatherState()
        state.accept({"SPC": result("SPC", True, expires=-1)}, {"SPC": "Offline"})
        self.assertFalse(state.decision(NOW).spotting)
        self.assertIn("SPC", state.decision(NOW).unavailable)

    def test_partial_outage_does_not_hide_positive(self):
        state = WeatherState()
        state.accept({"WPC": result("WPC", True)}, {"SPC": "Offline", "NWS": "Offline"})
        decision = state.decision(NOW)
        self.assertTrue(decision.spotting)
        self.assertEqual(set(decision.unavailable), {"SPC", "NWS"})

    def test_empty_state_is_unknown_not_a_clear_forecast(self):
        decision = WeatherState().decision(NOW)
        self.assertFalse(decision.spotting)
        self.assertEqual(set(decision.unavailable), set(SOURCES))
        self.assertIsNone(decision.checked_at)

    def test_cache_round_trip_and_corrupt_cache(self):
        with tempfile.TemporaryDirectory() as directory, patch("weather_risk.utc_now", return_value=NOW):
            path = Path(directory) / "weather.json"
            state = WeatherState(path)
            state.accept({"NWS": result("NWS", True)}, {})
            self.assertTrue(WeatherState(path).decision(NOW).spotting)
            path.write_text("invalid json")
            self.assertFalse(WeatherState(path).results)


class WeatherServiceTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch("weather_risk.utc_now", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def outlook_service(self, source, rank, current=True):
        if source == "SPC":
            row = {"dn": rank, "valid": "202610211200", "expire": "202610221200" if current else "202610211300"}
        else:
            row = {"dn": rank, "start_time": "2026-10-21 12:00:00", "end_time": "2026-10-22 12:00:00" if current else "2026-10-21 13:00:00"}
        requests = []

        def fetch(url, params):
            requests.append((url, params))
            return {"features": [{"attributes": row}]}

        service = WeatherService(fetch)
        service.county_geometry = {"rings": [[[0, 0], [0, 1], [1, 1], [0, 0]]], "spatialReference": {"wkid": 4326}}
        return service, requests

    def test_severe_threshold_excludes_general_thunderstorms(self):
        for rank, expected in ((2, False), (3, True), (4, True), (5, True), (6, True), (8, True)):
            service, requests = self.outlook_service("SPC", rank)
            self.assertEqual(service.fetch_outlook("SPC").risk, expected)
            self.assertEqual(requests[-1][0], SPC_URL)
            self.assertEqual(requests[-1][1]["spatialRel"], "esriSpatialRelIntersects")
            self.assertEqual(json.loads(requests[-1][1]["geometry"]), service.county_geometry)

    def test_flooding_threshold_includes_marginal(self):
        for rank, expected in ((0, False), (1, True), (2, True), (3, True), (4, True)):
            service, requests = self.outlook_service("WPC", rank)
            self.assertEqual(service.fetch_outlook("WPC").risk, expected)
            self.assertEqual(requests[-1][0], WPC_URL)

    def test_empty_county_is_clear_only_with_current_national_product(self):
        for source in ("SPC", "WPC"):
            service, _ = self.outlook_service(source, 3)
            original = service.fetch_json
            service.fetch_json = lambda url, params: {"features": []} if "geometry" in params else original(url, params)
            self.assertFalse(service.fetch_outlook(source).risk)
            service.fetch_json = lambda url, params: {"features": []}
            with self.assertRaisesRegex(ValueError, "No current"):
                service.fetch_outlook(source)

    def test_stale_unknown_and_truncated_outlooks_are_errors(self):
        for source in ("SPC", "WPC"):
            service, _ = self.outlook_service(source, 3, current=False)
            with self.assertRaises(ValueError):
                service.fetch_outlook(source)
            service, _ = self.outlook_service(source, 99)
            with self.assertRaisesRegex(ValueError, "Unrecognized"):
                service.fetch_outlook(source)
            service.fetch_json = lambda url, params: {"features": [], "exceededTransferLimit": True}
            with self.assertRaisesRegex(ValueError, "Incomplete"):
                service.fetch_outlook(source)

    def test_alerts_filter_county_event_and_expiration(self):
        def alert(event="Flood Watch", zone="ILZ037", expires="2026-10-22T12:00:00Z", **extra):
            return {"properties": {"status": "Actual", "event": event, "geocode": {"UGC": [zone]},
                                   "expires": expires, **extra}}

        for features, expected in (
            ([alert()], True),
            ([alert(event="Tornado Warning", zone="ILC179")], True),
            ([alert(event="Flash Flood Warning")], True),
            ([alert(event="Flood Warning")], True),
            ([alert(event="Severe Thunderstorm Watch")], True),
            ([alert(event="Heat Advisory")], False),
            ([alert(zone="ILC143")], False),
            ([alert(expires="2026-10-20T12:00:00Z")], False),
            ([alert(status="Test")], False),
            ([alert(messageType="Cancel")], False),
        ):
            def fetch(url, params):
                self.assertEqual(url, ALERTS_URL)
                self.assertEqual(params["zone"], "ILC179,ILZ037")
                return {"features": features}
            self.assertEqual(WeatherService(fetch).fetch_alerts().risk, expected)

    def test_county_failure_still_allows_alerts(self):
        def fetch(url, params=None):
            if url == ALERTS_URL:
                return {"features": []}
            raise OSError("Unavailable")
        results, errors = WeatherService(fetch).fetch()
        self.assertEqual(set(results), {"NWS"})
        self.assertEqual(set(errors), {"SPC", "WPC"})

    def test_esri_boundary_uses_whole_polygon_including_holes(self):
        ring = [[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]
        hole = [[0.5, 0.5], [0.5, 1], [1, 1], [0.5, 0.5]]
        geometry = county_query_geometry({"type": "Polygon", "coordinates": [ring, hole]})
        self.assertEqual(geometry["rings"][0], list(reversed(ring)))
        self.assertEqual(geometry["rings"][1], list(reversed(hole)))


if __name__ == "__main__":
    unittest.main()
