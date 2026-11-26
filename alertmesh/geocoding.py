"""Replaceable geocoding adapters for AlertMesh Place lookup."""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol


CONUS_STATE_CODES = frozenset({
    "US-AL", "US-AZ", "US-AR", "US-CA", "US-CO", "US-CT", "US-DE", "US-FL",
    "US-GA", "US-ID", "US-IL", "US-IN", "US-IA", "US-KS", "US-KY", "US-LA",
    "US-ME", "US-MD", "US-MA", "US-MI", "US-MN", "US-MS", "US-MO", "US-MT",
    "US-NE", "US-NV", "US-NH", "US-NJ", "US-NM", "US-NY", "US-NC", "US-ND",
    "US-OH", "US-OK", "US-OR", "US-PA", "US-RI", "US-SC", "US-SD", "US-TN",
    "US-TX", "US-UT", "US-VT", "US-VA", "US-WA", "US-WV", "US-WI", "US-WY",
    "US-DC",
})
RADIUS_PRESETS_KM = (5, 10, 25, 50, 100)
OSM_ATTRIBUTION = "© OpenStreetMap contributors"


@dataclass(frozen=True)
class GeocodedPlace:
    name: str
    latitude: float
    longitude: float
    state_code: str

    @property
    def is_contiguous_us(self) -> bool:
        return self.state_code in CONUS_STATE_CODES


class Geocoder(Protocol):
    def search(self, query: str) -> list[GeocodedPlace]: ...

    def reverse(self, latitude: float, longitude: float) -> GeocodedPlace | None: ...


class NominatimGeocoder:
    """OpenStreetMap Nominatim adapter. Calls should be user initiated, not autocomplete."""

    endpoint = "https://nominatim.openstreetmap.org"

    def __init__(self, *, user_agent: str | None = None, timeout: float = 8.0) -> None:
        self.user_agent = user_agent or os.environ.get(
            "ALERTMESH_GEOCODER_USER_AGENT", "AlertMesh/1.0"
        )
        self.timeout = timeout
        self._request_lock = threading.Lock()
        self._last_request = 0.0

    def search(self, query: str) -> list[GeocodedPlace]:
        payload = self._request("/search", {
            "q": query, "countrycodes": "us", "format": "jsonv2",
            "addressdetails": "1", "limit": "8",
        })
        return [place for item in payload if (place := self._parse(item)) is not None and place.is_contiguous_us]

    def reverse(self, latitude: float, longitude: float) -> GeocodedPlace | None:
        payload = self._request("/reverse", {
            "lat": str(latitude), "lon": str(longitude), "format": "jsonv2", "addressdetails": "1",
        })
        place = self._parse(payload) if isinstance(payload, dict) else None
        return place if place and place.is_contiguous_us else None

    def _request(self, path: str, params: dict[str, str]):
        url = f"{self.endpoint}{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"User-Agent": self.user_agent, "Accept": "application/json"})
        with self._request_lock:
            delay = 1.0 - (time.monotonic() - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))

    @staticmethod
    def _parse(item: dict) -> GeocodedPlace | None:
        address = item.get("address") or {}
        if address.get("country_code", "").lower() != "us":
            return None
        state_code = address.get("ISO3166-2-lvl4", "")
        if not state_code:
            return None
        try:
            return GeocodedPlace(
                name=str(item["display_name"]), latitude=float(item["lat"]),
                longitude=float(item["lon"]), state_code=state_code,
            )
        except (KeyError, TypeError, ValueError):
            return None
