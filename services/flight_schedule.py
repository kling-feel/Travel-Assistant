"""Deterministic fictional flight times for the full-network demo import.

These values are presentation estimates, not airline schedules or route advice.
"""

from __future__ import annotations

from datetime import timedelta
from math import asin, ceil, cos, isfinite, radians, sin, sqrt
from typing import Mapping


FLIGHT_SCHEDULE_RULE = "fictional-flight-time-v1"
EARTH_DIAMETER_KM = 12742
GROUND_MINUTES = 35
DETOUR_FACTOR = 1.05
CRUISE_KM_PER_HOUR = 820
MIN_MINUTES = 45
MAX_MINUTES = 24 * 60


def great_circle_km(origin: Mapping, destination: Mapping) -> float:
    """Airport-coordinate distance; reject missing or invalid positions."""
    values = (origin.get("latitude"), origin.get("longitude"),
              destination.get("latitude"), destination.get("longitude"))
    if any(value is None or not isfinite(value) for value in values):
        raise ValueError("Flight route lacks usable airport coordinates")
    lat1, lon1, lat2, lon2 = values
    if not (-90 <= lat1 <= 90 and -180 <= lon1 <= 180
            and -90 <= lat2 <= 90 and -180 <= lon2 <= 180):
        raise ValueError("Flight route has invalid airport coordinates")
    latitude_delta = radians(lat2 - lat1)
    longitude_delta = radians(lon2 - lon1)
    a = (sin(latitude_delta / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2))
         * sin(longitude_delta / 2) ** 2)
    return EARTH_DIAMETER_KM * asin(min(1.0, sqrt(a)))


def fictional_duration(distance_km: float) -> timedelta:
    """35 minutes overhead + distance at 820 km/h with 5% route allowance.

    Round up to five minutes and clamp to 45 minutes through 24 hours.
    """
    if not isfinite(distance_km) or distance_km < 0:
        raise ValueError("Invalid flight route distance")
    raw_minutes = GROUND_MINUTES + distance_km * DETOUR_FACTOR * 60 / CRUISE_KM_PER_HOUR
    rounded = ceil(raw_minutes / 5) * 5
    return timedelta(minutes=max(MIN_MINUTES, min(MAX_MINUTES, rounded)))
