"""Helpers for MLIT Project LINKS vacant-house and vacant-land data."""

from __future__ import annotations

import csv
import io
from typing import Any

from japan_data_mcp.public_info.real_estate import normalized_text, to_number

PROJECT_LINKS_LISTINGS_CSV_URL = (
    "https://www.geospatial.jp/ckan/dataset/"
    "da1b7c8d-164f-4fdd-977b-3c49c7396c08/resource/"
    "d1cbba16-4972-4bab-bcf5-e275b26a18de/download/01_tourokubukken.csv"
)
PROJECT_LINKS_DATASET_URL = "https://www.mlit.go.jp/links/open-data.html"
PROJECT_LINKS_DATA_AS_OF = "2025-03-31"


def parse_project_links_listings(
    content: bytes,
    *,
    municipality: str,
    land_only: bool = True,
    target_area_sqm: float | None = None,
) -> list[dict[str, Any]]:
    """Parse and conservatively filter the official Project LINKS CSV snapshot.

    The source contains municipality but not a town-level address.  Callers must
    therefore report municipality-level precision and must not imply a match to
    a smaller area such as a district or chome.
    """
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    city_query = normalized_text(municipality)
    min_area = target_area_sqm * 0.8 if target_area_sqm else None
    max_area = target_area_sqm * 1.2 if target_area_sqm else None
    matches: list[dict[str, Any]] = []

    for row in reader:
        if normalized_text(row.get("CITY")) != city_query:
            continue
        category = str(row.get("PROPERTY_CATEGORY") or "")
        if land_only and "売買土地" not in category:
            continue
        land_area = to_number(row.get("SIZE_OF_LOT"))
        if min_area is not None and (land_area is None or land_area < min_area):
            continue
        if max_area is not None and (land_area is None or land_area > max_area):
            continue
        matches.append(
            {
                "property_number": row.get("PROPERTY_NUMBER_ID") or None,
                "property_category": category or None,
                "prefecture": row.get("PREFECTURE") or None,
                "municipality": row.get("CITY") or None,
                "amount_or_rent_yen": to_number(row.get("AMOUNT/RENT")),
                "land_area_sqm": land_area,
                "building_area_sqm": to_number(row.get("OCCUPATION_AREA")),
                "construction_date": row.get("DATE_OF_CONSTRUCTION") or None,
                "construction": row.get("CONSTRUCTION") or None,
                "land_category": row.get("LAND_CATEGORY") or None,
                "city_planning_area": row.get("CITY_PLANNING_AREA") or None,
                "use_district": row.get("USE_DISTRICT") or None,
                "land_ownership": row.get("LAND_OWNERSHIP") or None,
                "private_road": row.get("PRIVATE_ROAD") or None,
                "setback": row.get("SETBACK") or None,
                "strong_points": row.get("STRONG_POINTS") or None,
                "floor_area_ratio": to_number(row.get("FLOOR_AREA_RATIO")),
                "building_coverage_ratio": to_number(
                    row.get("BUILDING_COVERAGE_RATIO")
                ),
            }
        )
    return matches
