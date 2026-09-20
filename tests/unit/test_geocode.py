"""Tests for reverse geocoding of photo GPS coordinates."""

import json

import pytest

import geocode


@pytest.fixture(autouse=True)
def _isolate_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_GEOCODE_CACHE", str(tmp_path / "geocode.json"))
    monkeypatch.setattr(geocode, "_MIN_INTERVAL_S", 0.0)
    yield


def _enable(monkeypatch):
    monkeypatch.setenv("LLMLIBRARIAN_REVERSE_GEOCODE", "1")


class TestOptIn:
    def test_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("LLMLIBRARIAN_REVERSE_GEOCODE", raising=False)
        assert geocode.reverse_geocode_enabled() is False

    def test_disabled_makes_no_network_call(self, monkeypatch):
        monkeypatch.delenv("LLMLIBRARIAN_REVERSE_GEOCODE", raising=False)

        def explode(*args, **kwargs):
            raise AssertionError("must not call the network when disabled")

        monkeypatch.setattr(geocode, "_fetch_json", explode)
        assert geocode.reverse_geocode(38.95, -104.72) == {}

    @pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
    def test_truthy_values_enable(self, monkeypatch, value):
        monkeypatch.setenv("LLMLIBRARIAN_REVERSE_GEOCODE", value)
        assert geocode.reverse_geocode_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "", "no"])
    def test_falsey_values_stay_disabled(self, monkeypatch, value):
        monkeypatch.setenv("LLMLIBRARIAN_REVERSE_GEOCODE", value)
        assert geocode.reverse_geocode_enabled() is False


class TestCoordinateValidation:
    @pytest.mark.parametrize(
        "lat,lon",
        [(91.0, 0.0), (-91.0, 0.0), (0.0, 181.0), (0.0, -181.0)],
    )
    def test_out_of_range_rejected(self, monkeypatch, lat, lon):
        _enable(monkeypatch)
        monkeypatch.setattr(
            geocode, "_fetch_json", lambda *a, **k: pytest.fail("no lookup expected")
        )
        assert geocode.reverse_geocode(lat, lon) == {}

    def test_non_numeric_rejected(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setattr(
            geocode, "_fetch_json", lambda *a, **k: pytest.fail("no lookup expected")
        )
        assert geocode.reverse_geocode("nope", None) == {}


class TestNearbyOverridesContainingFeature:
    """The coordinate usually lands on a parking lot, not the venue."""

    def test_named_venue_within_50m_wins(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "restaurant")

        reverse_payload = {
            "type": "parking",
            "category": "amenity",
            "address": {
                "retail": "Powers Center Point",
                "city": "Colorado Springs",
                "state": "Colorado",
                "postcode": "80924",
            },
        }
        search_payload = [
            {"name": "Happy Time Korean BBQ", "lat": "38.9544607", "lon": "-104.7282350", "type": "restaurant"},
            {"name": "Turmeric Indian Cuisine", "lat": "38.9546", "lon": "-104.7285", "type": "restaurant"},
        ]

        def fake_fetch(url, params):
            return reverse_payload if url == geocode.NOMINATIM_URL else search_payload

        monkeypatch.setattr(geocode, "_fetch_json", fake_fetch)

        result = geocode.reverse_geocode(38.9544111, -104.7283778)
        assert result["place_name"] == "Happy Time Korean BBQ"
        assert result["place_category"] == "restaurant"
        assert "Powers Center Point" in result["place_address"]
        # Alternatives stay on record so a wrong pick is visible.
        assert "Turmeric Indian Cuisine" in result["nearby_places"]

    def test_distant_venue_does_not_override(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "restaurant")
        # ~111 m north: listed as nearby, but too far to name as the location.
        search_payload = [
            {"name": "Far Cafe", "lat": "38.9554111", "lon": "-104.7283778", "type": "restaurant"}
        ]

        def fake_fetch(url, params):
            return {"type": "parking", "address": {"city": "Colorado Springs"}} if url == geocode.NOMINATIM_URL else search_payload

        monkeypatch.setattr(geocode, "_fetch_json", fake_fetch)
        result = geocode.reverse_geocode(38.9544111, -104.7283778)
        assert "place_name" not in result
        assert "Far Cafe" in result["nearby_places"]

    def test_results_beyond_radius_dropped(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "restaurant")
        # ~1.1 km away -- outside the 150 m box, so not "nearby" at all.
        search_payload = [
            {"name": "Elsewhere", "lat": "38.9644111", "lon": "-104.7283778", "type": "restaurant"}
        ]
        monkeypatch.setattr(
            geocode,
            "_fetch_json",
            lambda url, params: {} if url == geocode.NOMINATIM_URL else search_payload,
        )
        assert "nearby_places" not in geocode.reverse_geocode(38.9544111, -104.7283778)


class TestResilience:
    def test_network_failure_returns_empty(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setattr(geocode, "_fetch_json", lambda url, params: None)
        assert geocode.reverse_geocode(38.9544111, -104.7283778) == {}

    def test_error_payload_returns_empty(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        monkeypatch.setattr(
            geocode, "_fetch_json", lambda url, params: {"error": "Unable to geocode"}
        )
        assert geocode.reverse_geocode(38.9544111, -104.7283778) == {}

    def test_unnamed_results_skipped(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "restaurant")
        monkeypatch.setattr(
            geocode,
            "_fetch_json",
            lambda url, params: {} if url == geocode.NOMINATIM_URL else [{"lat": "38.95441", "lon": "-104.72838"}],
        )
        assert geocode.reverse_geocode(38.9544111, -104.7283778) == {}


class TestCaching:
    def test_second_lookup_uses_cache(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        calls = []

        def fake_fetch(url, params):
            calls.append(url)
            return {"name": "Happy Time Korean BBQ", "type": "restaurant", "address": {"city": "Colorado Springs"}}

        monkeypatch.setattr(geocode, "_fetch_json", fake_fetch)
        first = geocode.reverse_geocode(38.9544111, -104.7283778)
        second = geocode.reverse_geocode(38.9544111, -104.7283778)
        assert first == second
        assert len(calls) == 1

    def test_nearby_coordinates_share_a_cache_entry(self, monkeypatch):
        """Rounded to ~1 m; two shots of the same table are one lookup."""
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        calls = []
        monkeypatch.setattr(
            geocode,
            "_fetch_json",
            lambda url, params: (calls.append(url), {"name": "Venue", "address": {}})[1],
        )
        geocode.reverse_geocode(38.95441114, -104.72837781)
        geocode.reverse_geocode(38.95441112, -104.72837783)
        assert len(calls) == 1

    def test_empty_result_is_cached(self, monkeypatch):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        calls = []
        monkeypatch.setattr(
            geocode,
            "_fetch_json",
            lambda url, params: (calls.append(url), {"error": "nope"})[1],
        )
        geocode.reverse_geocode(0.0, 0.0)
        geocode.reverse_geocode(0.0, 0.0)
        assert len(calls) == 1

    def test_corrupt_cache_file_is_survivable(self, monkeypatch, tmp_path):
        _enable(monkeypatch)
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        cache_file = tmp_path / "geocode.json"
        cache_file.write_text("{not json", encoding="utf-8")
        monkeypatch.setenv("LLMLIBRARIAN_GEOCODE_CACHE", str(cache_file))
        monkeypatch.setattr(
            geocode, "_fetch_json", lambda url, params: {"name": "Venue", "address": {}}
        )
        assert geocode.reverse_geocode(38.95, -104.72)["place_name"] == "Venue"
        assert json.loads(cache_file.read_text(encoding="utf-8"))


class TestNearbyCategories:
    def test_default_categories(self, monkeypatch):
        monkeypatch.delenv("LLMLIBRARIAN_NEARBY_CATEGORIES", raising=False)
        assert geocode._nearby_categories() == geocode._DEFAULT_NEARBY_CATEGORIES

    def test_empty_disables_nearby_pass(self, monkeypatch):
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "")
        assert geocode._nearby_categories() == ()

    def test_custom_list_parsed(self, monkeypatch):
        monkeypatch.setenv("LLMLIBRARIAN_NEARBY_CATEGORIES", "museum, park ,")
        assert geocode._nearby_categories() == ("museum", "park")
