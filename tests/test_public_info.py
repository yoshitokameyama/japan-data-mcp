"""Tests for the high-level Japan public-information evidence tools."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from japan_data_mcp.public_info.evidence import build_evidence_envelope
from japan_data_mcp.public_info.real_estate import (
    calculate_price_stats,
    filter_feed_items,
    public_source_url,
)
from japan_data_mcp.public_info.project_links import parse_project_links_listings
from japan_data_mcp.realestate.models import Transaction
from japan_data_mcp.server import (
    get_connector_status,
    research_real_estate_area,
    search_plateau_datasets,
    search_government_open_data,
    search_real_estate_transactions,
)


def _ctx(*, transactions: list[Transaction] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.fastmcp = MagicMock()
    ctx.info = AsyncMock()
    if transactions is None:
        ctx.fastmcp._realestate_client = None
    else:
        client = MagicMock()
        client.get_transactions = AsyncMock(return_value=transactions)
        ctx.fastmcp._realestate_client = client
    ctx.fastmcp._public_http_client = MagicMock()
    return ctx


def _transactions() -> list[Transaction]:
    return [
        Transaction(
            transaction_type="宅地(土地)",
            trade_price="50000000",
            area="100",
            unit_price="500000",
            municipality_code="13106",
            prefecture="東京都",
            municipality="台東区",
            district_name="谷中",
            period="2024年第1四半期",
        ),
        Transaction(
            transaction_type="宅地(土地と建物)",
            trade_price="80000000",
            area="100",
            municipality_code="13106",
            prefecture="東京都",
            municipality="台東区",
            district_name="谷中",
        ),
        Transaction(
            transaction_type="宅地(土地)",
            trade_price="30000000",
            area="60",
            unit_price="500000",
            municipality_code="13106",
            prefecture="東京都",
            municipality="台東区",
            district_name="上野桜木",
        ),
    ]


def test_evidence_envelope_has_required_fields():
    envelope = build_evidence_envelope(
        status="ok",
        data_as_of="2024",
        precision="dataset",
        confidence="high",
        sources=[{"name": "source", "url": "https://example.test/data"}],
        limitations=[],
        data={"value": 1},
    )

    assert set(envelope) == {
        "status",
        "data_as_of",
        "retrieved_at",
        "precision",
        "confidence",
        "sources",
        "limitations",
        "data",
    }
    assert envelope["retrieved_at"].endswith("+00:00")


def test_price_stats_are_transparent_and_target_specific():
    stats = calculate_price_stats(
        [
            {
                "total_price_yen": 30_000_000,
                "unit_price_yen_per_sqm": 300_000,
            },
            {
                "total_price_yen": 50_000_000,
                "unit_price_yen_per_sqm": 500_000,
            },
        ],
        100,
    )

    assert stats["median_total_price_yen"] == 40_000_000
    assert stats["median_unit_price_yen_per_sqm"] == 400_000
    assert stats["estimated_price_for_target_area_yen"] == 40_000_000


def test_feed_filter_and_public_url_do_not_leak_query_secrets():
    matches = filter_feed_items(
        [
            {"address": "東京都台東区谷中", "land_area_sqm": 100},
            {"address": "東京都台東区上野", "land_area_sqm": 100},
        ],
        area_name="谷中",
        min_area_sqm=80,
        max_area_sqm=120,
    )

    assert len(matches) == 1
    assert (
        public_source_url(
            "https://example.test/feed?token=secret#fragment",
            "https://fallback.test/",
        )
        == "https://example.test/feed"
    )


async def test_transaction_search_returns_only_land_and_evidence_fields():
    result = json.loads(
        await search_real_estate_transactions(
            "谷中", "13106", _ctx(transactions=_transactions()), years=[2024]
        )
    )

    assert result["status"] == "ok"
    assert result["precision"] == "transaction"
    assert result["data"]["total_matches"] == 1
    assert result["data"]["transactions"][0]["transaction_type"] == "宅地(土地)"
    assert "売り出されている物件ではありません" in result["limitations"][0]
    assert result["sources"][0]["url"].startswith("https://www.reinfolib.mlit.go.jp/")


async def test_integrated_research_keeps_evidence_layers_separate(monkeypatch):
    monkeypatch.delenv("LISTINGS_JSON_URL", raising=False)
    monkeypatch.delenv("VACANT_CANDIDATES_URL", raising=False)

    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.content = (
        "PROPERTY_NUMBER_ID,PROPERTY_CATEGORY,PREFECTURE,CITY,AMOUNT/RENT,SIZE_OF_LOT\n"
        "1,売買土地,東京都,台東区,30000000,100\n"
    ).encode()
    ctx = _ctx(transactions=_transactions())
    ctx.fastmcp._public_http_client.get = AsyncMock(return_value=response)

    result = json.loads(
        await research_real_estate_area(
            "谷中", "13106", ctx,
            target_area_sqm=100,
            years=[2024],
        )
    )

    assert result["status"] == "partial"
    assert result["precision"] == "area"
    assert result["data"]["transactions"]["status"] == "ok"
    assert result["data"]["current_listings"]["status"] == "ok"
    assert result["data"]["current_listings"]["precision"] == "municipality"
    assert result["data"]["vacant_candidates"]["status"] == "not_configured"
    assert "2025年3月31日以降の掲載継続状況" in result["data"]["unknowns"]
    assert "所有者" in result["data"]["unknowns"]


async def test_integrated_research_reports_all_missing_connectors_as_not_configured(
    monkeypatch,
):
    monkeypatch.delenv("LISTINGS_JSON_URL", raising=False)
    monkeypatch.delenv("VACANT_CANDIDATES_URL", raising=False)

    response = MagicMock()
    response.raise_for_status = MagicMock(side_effect=Exception("unavailable"))
    ctx = _ctx(transactions=None)
    ctx.fastmcp._public_http_client.get = AsyncMock(side_effect=ValueError("unavailable"))
    result = json.loads(
        await research_real_estate_area(
            "谷中", "13106", ctx,
            target_area_sqm=100,
        )
    )

    assert result["status"] == "upstream_error"
    assert {
        result["data"]["transactions"]["status"],
        result["data"]["current_listings"]["status"],
        result["data"]["vacant_candidates"]["status"],
    } == {"not_configured", "upstream_error"}


async def test_invalid_city_code_returns_safe_error_envelope():
    result = json.loads(
        await search_real_estate_transactions(
            "谷中", "invalid", _ctx(transactions=_transactions()), years=[2024]
        )
    )

    assert result["status"] == "upstream_error"
    assert result["data"]["transactions"] == []
    assert "5桁" in result["limitations"][0]


async def test_connector_status_never_returns_secret_values(monkeypatch):
    monkeypatch.setenv("REALESTATE_API_KEY", "do-not-return-this")
    monkeypatch.setenv("LISTINGS_JSON_URL", "https://example.test/feed")

    result_text = await get_connector_status(_ctx())
    result = json.loads(result_text)

    assert result["data"]["connectors"]["transaction_prices"]["configured"] is True
    assert result["data"]["connectors"]["current_listings"]["configured"] is True
    assert "do-not-return-this" not in result_text


def test_project_links_parser_filters_municipality_land_and_area():
    content = (
        "PROPERTY_NUMBER_ID,PROPERTY_CATEGORY,PREFECTURE,CITY,AMOUNT/RENT,"
        "SIZE_OF_LOT,OCCUPATION_AREA,STRONG_POINTS\n"
        "1,売買土地,東京都,台東区,30000000,100,,候補A\n"
        "2,売買居住用,東京都,台東区,50000000,100,80,候補B\n"
        "3,売買土地,東京都,文京区,40000000,100,,候補C\n"
        "4,売買土地,東京都,台東区,20000000,200,,候補D\n"
    ).encode()

    matches = parse_project_links_listings(
        content, municipality="台東区", target_area_sqm=100
    )

    assert [item["property_number"] for item in matches] == ["1"]
    assert matches[0]["land_area_sqm"] == 100


async def test_government_open_data_search_returns_compact_official_metadata():
    ctx = _ctx()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = {
        "success": True,
        "result": {
            "count": 1,
            "results": [
                {
                    "name": "sample",
                    "title": "自治体広報サンプル",
                    "publisher": "デジタル庁",
                    "notes": "説明",
                    "metadata_modified": "2026-08-01T00:00:00",
                    "landingPage": "https://example.go.jp/data",
                    "resources": [
                        {
                            "name": "CSV",
                            "format": "CSV",
                            "url": "https://example.go.jp/data.csv",
                        }
                    ],
                }
            ],
        },
    }
    ctx.fastmcp._public_http_client.get = AsyncMock(return_value=response)

    result = json.loads(await search_government_open_data("自治体 広報", ctx, 3))

    assert result["status"] == "ok"
    assert result["data"]["total_matches"] == 1
    assert result["data"]["datasets"][0]["dataset_id"] == "sample"


async def test_plateau_search_filters_official_catalog():
    ctx = _ctx()
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = [
        {"city_code": "13106", "type_en": "bldg", "year": 2023},
        {"city_code": "13101", "type_en": "bldg", "year": 2023},
    ]
    ctx.fastmcp._public_http_client.get = AsyncMock(return_value=response)

    result = json.loads(
        await search_plateau_datasets(
            ctx, city_code="13106", dataset_type="bldg", year=2023
        )
    )

    assert result["status"] == "ok"
    assert result["data"]["count"] == 1
    assert result["data"]["datasets"][0]["city_code"] == "13106"
