"""Pure helpers for evidence-based real-estate research."""

from __future__ import annotations

import statistics
import unicodedata
from typing import Any, Iterable
from urllib.parse import urlsplit

from japan_data_mcp.realestate.models import Transaction


def normalized_text(value: object) -> str:
    """Normalize Japanese search text for conservative substring matching."""
    return "".join(
        unicodedata.normalize("NFKC", str(value or "")).lower().split()
    )


def to_number(value: object) -> float | None:
    """Parse a finite numeric value used by normalized feeds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    cleaned = value.replace(",", "").replace("㎡", "").replace("円", "").strip()
    try:
        parsed = float(cleaned)
    except ValueError:
        return None
    return parsed


def is_land_transaction(transaction: Transaction) -> bool:
    """Return true only for land transactions without a building component."""
    transaction_type = normalized_text(transaction.transaction_type)
    has_land = "土地" in transaction_type or "land" in transaction_type
    has_building = "建物" in transaction_type or "building" in transaction_type
    return has_land and not has_building


def normalize_transaction(transaction: Transaction) -> dict[str, Any]:
    """Return a stable subset of the upstream transaction record."""
    total_price = transaction.trade_price_int
    area = to_number(transaction.area)
    unit_price = to_number(transaction.unit_price)
    if unit_price is None and total_price is not None and area and area > 0:
        unit_price = round(total_price / area)
    return {
        "district_name": transaction.district_name,
        "municipality": transaction.municipality,
        "prefecture": transaction.prefecture,
        "transaction_type": transaction.transaction_type,
        "total_price_yen": total_price,
        "land_area_sqm": area,
        "unit_price_yen_per_sqm": unit_price,
        "period": transaction.period or None,
        "city_planning": transaction.city_planning or None,
        "coverage_ratio": transaction.coverage_ratio or None,
        "floor_area_ratio": transaction.floor_area_ratio or None,
    }


def filter_transactions(
    transactions: Iterable[Transaction],
    *,
    area_name: str,
    land_only: bool,
) -> list[dict[str, Any]]:
    """Filter by district name and normalize without inferring missing facts."""
    query = normalized_text(area_name)
    matches: list[dict[str, Any]] = []
    for transaction in transactions:
        if query and query not in normalized_text(transaction.district_name):
            continue
        if land_only and not is_land_transaction(transaction):
            continue
        matches.append(normalize_transaction(transaction))
    return matches


def calculate_price_stats(
    transactions: Iterable[dict[str, Any]], target_area_sqm: float | None
) -> dict[str, Any]:
    """Calculate transparent descriptive statistics, not an appraisal."""
    items = list(transactions)
    totals = [
        float(item["total_price_yen"])
        for item in items
        if item.get("total_price_yen") is not None
    ]
    units = [
        float(item["unit_price_yen_per_sqm"])
        for item in items
        if item.get("unit_price_yen_per_sqm") is not None
        and float(item["unit_price_yen_per_sqm"]) > 0
    ]
    median_unit = statistics.median(units) if units else None
    return {
        "count": len(items),
        "median_total_price_yen": statistics.median(totals) if totals else None,
        "min_total_price_yen": min(totals) if totals else None,
        "max_total_price_yen": max(totals) if totals else None,
        "median_unit_price_yen_per_sqm": median_unit,
        "estimated_price_for_target_area_yen": (
            round(median_unit * target_area_sqm)
            if median_unit is not None and target_area_sqm is not None
            else None
        ),
        "target_area_sqm": target_area_sqm,
    }


def filter_feed_items(
    items: Iterable[dict[str, Any]],
    *,
    area_name: str,
    min_area_sqm: float | None,
    max_area_sqm: float | None,
) -> list[dict[str, Any]]:
    """Filter a licensed normalized feed by address and land area."""
    query = normalized_text(area_name)
    matches: list[dict[str, Any]] = []
    for item in items:
        address = normalized_text(item.get("address") or item.get("area_name"))
        area = to_number(item.get("land_area_sqm"))
        if query not in address:
            continue
        if min_area_sqm is not None and (area is None or area < min_area_sqm):
            continue
        if max_area_sqm is not None and (area is None or area > max_area_sqm):
            continue
        matches.append(item)
    return matches


def public_source_url(value: object, fallback: str) -> str:
    """Return a public origin/path while stripping query strings and fragments."""
    candidate = str(value or fallback)
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        parsed = urlsplit(fallback)
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
