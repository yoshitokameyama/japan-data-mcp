"""Tests for the representative MLIT geospatial adapter."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

from japan_data_mcp.public_info.mlit import (
    filter_geojson_features,
    latlon_to_tile_fraction,
    surrounding_tiles,
)
from japan_data_mcp.server import get_mlit_geospatial_layers


def _ctx(responses: list[MagicMock] | None = None) -> MagicMock:
    ctx = MagicMock()
    ctx.fastmcp = MagicMock()
    ctx.info = AsyncMock()
    ctx.fastmcp._public_http_client = MagicMock()
    if responses is not None:
        ctx.fastmcp._public_http_client.get = AsyncMock(side_effect=responses)
    return ctx


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.raise_for_status = MagicMock()
    response.json.return_value = payload
    return response


def test_tile_conversion_and_surrounding_tiles_are_bounded():
    x, y, x_fraction, y_fraction = latlon_to_tile_fraction(35.681236, 139.767125)
    tiles = surrounding_tiles(x, y, x_fraction, y_fraction)

    assert len(tiles) == 4
    assert len(set(tiles)) == 4
    assert tiles[0] == (x, y)


def test_point_and_polygon_filtering():
    point_features = [
        {"geometry": {"type": "Point", "coordinates": [139.767125, 35.681236]}},
        {"geometry": {"type": "Point", "coordinates": [140.0, 36.0]}},
    ]
    polygon_features = [
        {
            "geometry": {
                "type": "Polygon",
                "coordinates": [[
                    [139.7, 35.6],
                    [139.8, 35.6],
                    [139.8, 35.7],
                    [139.7, 35.7],
                    [139.7, 35.6],
                ]],
            }
        }
    ]

    assert len(
        filter_geojson_features(
            point_features,
            geometry_mode="point",
            lat=35.681236,
            lon=139.767125,
            distance_m=425,
        )
    ) == 1
    assert len(
        filter_geojson_features(
            polygon_features,
            geometry_mode="polygon",
            lat=35.681236,
            lon=139.767125,
            distance_m=425,
        )
    ) == 1


async def test_mlit_adapter_reports_not_configured(monkeypatch):
    monkeypatch.delenv("REALESTATE_API_KEY", raising=False)

    result = json.loads(
        await get_mlit_geospatial_layers(
            35.681236, 139.767125, [3, 5], _ctx(), year=2024
        )
    )

    assert result["status"] == "not_configured"
    assert result["data"]["layers"] == []
    assert result["sources"][0]["url"].startswith("https://www.reinfolib.mlit.go.jp/")


async def test_mlit_adapter_keeps_old_representative_inputs(monkeypatch):
    monkeypatch.setenv("REALESTATE_API_KEY", "hidden-key")
    point_payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [139.767125, 35.681236]},
                "properties": {"price": 1},
            }
        ],
    }
    polygon_payload = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[
                        [139.7, 35.6],
                        [139.8, 35.6],
                        [139.8, 35.7],
                        [139.7, 35.7],
                        [139.7, 35.6],
                    ]],
                },
                "properties": {"zone": "test"},
            }
        ],
    }
    ctx = _ctx(
        responses=[
            _response(point_payload),
            _response({"type": "FeatureCollection", "features": []}),
            _response({"type": "FeatureCollection", "features": []}),
            _response({"type": "FeatureCollection", "features": []}),
            _response(polygon_payload),
        ]
    )

    result_text = await get_mlit_geospatial_layers(
        35.681236,
        139.767125,
        [3, 5],
        ctx,
        distance=425,
        year=2024,
    )
    result = json.loads(result_text)

    assert result["status"] == "ok"
    assert result["data"]["query"] == {
        "lat": 35.681236,
        "lon": 139.767125,
        "target_apis": [3, 5],
        "distance": 425,
        "year": 2024,
    }
    assert [layer["api_number"] for layer in result["data"]["layers"]] == [3, 5]
    assert all(layer["count"] == 1 for layer in result["data"]["layers"])
    assert "hidden-key" not in result_text

    calls = ctx.fastmcp._public_http_client.get.call_args_list
    assert calls[0].kwargs["params"]["year"] == 2024
    assert calls[-1].kwargs["params"].get("year") is None


async def test_mlit_adapter_rejects_unsupported_api_without_request(monkeypatch):
    monkeypatch.setenv("REALESTATE_API_KEY", "hidden-key")
    ctx = _ctx()

    result = json.loads(
        await get_mlit_geospatial_layers(35.0, 139.0, [30], ctx)
    )

    assert result["status"] == "upstream_error"
    assert "未対応" in result["limitations"][0]
    ctx.fastmcp._public_http_client.get.assert_not_called()
