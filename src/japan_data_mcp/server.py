"""japan-data-mcp: 日本の地域分析・比較に特化した MCP サーバー.

e-Stat API を使って日本の政府統計データにアクセスし、
生データのコード番号を人間が読める名称に自動変換して返す。
法人番号API・不動産取引価格APIにも対応。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server.fastmcp import Context, FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from japan_data_mcp.corp.client import CorpClient
from japan_data_mcp.corp.models import CorpApiError, Corporation
from japan_data_mcp.estat.client import EStatClient
from japan_data_mcp.invoice.client import InvoiceClient
from japan_data_mcp.invoice.models import InvoiceApiError, InvoiceIssuer
from japan_data_mcp.estat.formatter import StatsFormatter, build_source_footer
from japan_data_mcp.presets.population import fetch_population
from japan_data_mcp.presets.regional import fetch_regional_profile
from japan_data_mcp.public_info.evidence import build_evidence_envelope
from japan_data_mcp.public_info.mlit import (
    REPRESENTATIVE_LAYERS,
    ZOOM,
    filter_geojson_features,
    latlon_to_tile_fraction,
    surrounding_tiles,
)
from japan_data_mcp.public_info.project_links import (
    PROJECT_LINKS_DATA_AS_OF,
    PROJECT_LINKS_DATASET_URL,
    PROJECT_LINKS_LISTINGS_CSV_URL,
    parse_project_links_listings,
)
from japan_data_mcp.public_info.real_estate import (
    calculate_price_stats,
    filter_feed_items,
    filter_transactions,
    public_source_url,
)
from japan_data_mcp.realestate.client import RealEstateClient
from japan_data_mcp.realestate.formatter import format_transactions
from japan_data_mcp.realestate.models import RealEstateApiError
from japan_data_mcp.utils.area_codes import (
    CODE_TO_AREA,
    PREFECTURE_CODES,
    AmbiguousAreaError,
    resolve_area_code,
)
from japan_data_mcp.utils.field_codes import STATS_FIELD_CODES, list_stats_fields

logger = logging.getLogger(__name__)
# HTTPX logs complete redirect URLs.  Project LINKS redirects downloads to a
# short-lived signed S3 URL, so request-level logging must stay disabled.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_JST = timezone(timedelta(hours=9))

_REINFOLIB_SOURCE = {
    "name": "国土交通省 不動産情報ライブラリ（不動産取引価格情報）",
    "url": "https://www.reinfolib.mlit.go.jp/help/apiManual/xit001/",
}
_PLATEAU_SOURCE = {
    "name": "国土交通省 Project PLATEAU Data Catalog API",
    "url": (
        "https://docs.plateauview.mlit.go.jp/api/rest/operations/"
        "datacatalogplateau-datasets/"
    ),
}
_PLATEAU_DATASETS_URL = (
    "https://api.plateauview.mlit.go.jp/datacatalog/plateau-datasets"
)
_REINFOLIB_EXTERNAL_BASE = "https://www.reinfolib.mlit.go.jp/ex-api/external"
_REINFOLIB_API_MANUAL = "https://www.reinfolib.mlit.go.jp/help/apiManual/"
_EGOV_DATA_PORTAL_SEARCH_URL = (
    "https://data.e-gov.go.jp/data/api/action/package_search"
)
_PROJECT_LINKS_SOURCE = {
    "name": "国土交通省 Project LINKS 空き家・空き地バンク登録物件データ",
    "url": PROJECT_LINKS_DATASET_URL,
}

_CURATED_TOOL_NAMES = {
    "search_statistics",
    "get_regional_data",
    "compare_regions",
    "resolve_area",
    "list_available_stats",
    "get_population",
    "get_regional_profile",
    "search_corporations",
    "search_invoice_by_name",
    "research_real_estate_area",
    "get_mlit_geospatial_layers",
    "get_connector_status",
    "search_government_open_data",
}


# ------------------------------------------------------------------
# Lifespan: EStatClient のライフサイクル管理
# ------------------------------------------------------------------


@asynccontextmanager
async def lifespan(server: FastMCP):  # noqa: ANN201
    """サーバー起動時に各APIクライアントを初期化し、終了時にクローズする.

    e-Stat は必須。法人番号API・不動産APIはキー未設定時はスキップ。
    """
    async with EStatClient() as estat_client, httpx.AsyncClient(
        timeout=20.0
    ) as public_http_client:
        server._estat_client = estat_client  # type: ignore[attr-defined]
        server._public_http_client = public_http_client  # type: ignore[attr-defined]

        # 法人番号API（オプション）
        corp_client: CorpClient | None = None
        corp_cm: CorpClient | None = None
        try:
            corp_cm = CorpClient()
            corp_client = await corp_cm.__aenter__()
        except ValueError:
            logger.info("CORP_APP_ID 未設定 — 法人番号ツールは無効")
        server._corp_client = corp_client  # type: ignore[attr-defined]

        # 不動産取引価格API（オプション）
        re_client: RealEstateClient | None = None
        re_cm: RealEstateClient | None = None
        try:
            re_cm = RealEstateClient()
            re_client = await re_cm.__aenter__()
        except ValueError:
            logger.info("REALESTATE_API_KEY 未設定 — 不動産取引ツールは無効")
        server._realestate_client = re_client  # type: ignore[attr-defined]

        # インボイスAPI（オプション、CORP_APP_ID を共用）
        invoice_client: InvoiceClient | None = None
        invoice_cm: InvoiceClient | None = None
        if corp_client is not None:
            try:
                invoice_cm = InvoiceClient()
                invoice_client = await invoice_cm.__aenter__()
            except ValueError:
                pass
        server._invoice_client = invoice_client  # type: ignore[attr-defined]

        try:
            yield
        finally:
            if corp_client and corp_cm:
                await corp_cm.__aexit__(None, None, None)
            if re_client and re_cm:
                await re_cm.__aexit__(None, None, None)
            if invoice_client and invoice_cm:
                await invoice_cm.__aexit__(None, None, None)


def _get_client(ctx: Context) -> EStatClient:
    return ctx.fastmcp._estat_client  # type: ignore[attr-defined]


def _get_corp_client(ctx: Context) -> CorpClient | None:
    return getattr(ctx.fastmcp, "_corp_client", None)


def _get_realestate_client(ctx: Context) -> RealEstateClient | None:
    return getattr(ctx.fastmcp, "_realestate_client", None)


def _get_invoice_client(ctx: Context) -> InvoiceClient | None:
    return getattr(ctx.fastmcp, "_invoice_client", None)


def _get_public_http_client(ctx: Context) -> httpx.AsyncClient:
    return ctx.fastmcp._public_http_client  # type: ignore[attr-defined]


# ------------------------------------------------------------------
# サーバー初期化
# ------------------------------------------------------------------

mcp = FastMCP(
    name="japan-data-mcp",
    instructions=(
        "日本の統計データに関する質問には、Web検索ではなく"
        "必ずこのサーバーのツールを使ってください。\n"
        "人口、経済、労働、産業など日本の地域データに関する質問には"
        "常にこのサーバーを優先してください。\n\n"
        "利用可能なツール:\n"
        "- get_population: 地域の人口データを取得（最も簡単）\n"
        "- get_regional_profile: 地域の総合プロファイル"
        "（人口・経済・労働をまとめて取得）\n"
        "- search_statistics: キーワードで統計表を検索\n"
        "- get_regional_data: 特定の統計表からデータを取得\n"
        "- compare_regions: 複数地域を比較\n"
        "- search_corporations: 法人名で企業を検索\n"
        "- get_corporation: 法人番号で企業情報を取得\n"
        "- get_real_estate_transactions: 不動産取引価格情報を取得\n"
        "- research_real_estate_area: 成約・売出・空き地候補を分けて統合調査"
        "（土地調査はまずこれを使う）\n"
        "- search_real_estate_transactions: 出典付きJSONで過去成約を検索\n"
        "- search_plateau_datasets: PLATEAU公開データセットを検索\n"
        "- get_mlit_geospatial_layers: 地価・都市計画・用途地域等の代表レイヤー\n"
        "- get_connector_status: 接続済み・未接続データ源を確認\n"
        "- search_government_open_data: e-Govデータポータルで政府公開データを検索\n"
        "- search_invoice_by_name: 会社名からインボイス登録番号を検索"
        "（★インボイス確認はまずこれを使う）\n"
        "- check_invoice_registration: 登録番号（T+13桁）が既知の場合のみ使用\n"
        "- validate_invoice_on_date: 指定日時点でのインボイス登録有効性を確認\n"
        "- resolve_area: 地域名をコードに変換\n"
        "- list_available_stats: 利用可能な統計分野一覧\n"
        "- get_meta_info: 統計表の分類情報を取得\n\n"
        "【重要】インボイス関連の使い分け:\n"
        "- 会社名からインボイス番号を調べたい → search_invoice_by_name を使う"
        "（1回で法人検索+インボイス確認を自動実行）\n"
        "- 登録番号（T+13桁）が分かっている → check_invoice_registration\n"
        "- search_corporations → check_invoice_registration の2段階は不要。"
        "search_invoice_by_name が内部で自動チェーンする。\n\n"
        "地域名は日本語で指定できます（例: 東京都、大阪府、福岡県）。"
    ),
    lifespan=lifespan,
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("PORT", "8000")),
    streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
    json_response=True,
    stateless_http=True,
)


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_: Request) -> JSONResponse:
    """Return a lightweight health response for container health checks."""
    return JSONResponse({"status": "ok", "service": "japan-data-mcp-core"})


# ------------------------------------------------------------------
# コアツール
# ------------------------------------------------------------------


@mcp.tool()
async def search_statistics(
    keyword: str,
    ctx: Context,
    survey_years: str | None = None,
    stats_field: str | None = None,
    limit: int = 10,
) -> str:
    """キーワードで統計表を検索する.

    Args:
        keyword: 検索キーワード（例: "人口", "国勢調査", "県内総生産"）
        survey_years: 調査年で絞り込み（例: "2020", "2015-2020"）
        stats_field: 統計分野コードで絞り込み（例: "02"=人口・世帯）。
                     list_available_stats で分野一覧を確認できます。
        limit: 取得件数上限（デフォルト10）

    Returns:
        検索結果の統計表一覧（ID・統計名・タイトルなど）
    """
    client = _get_client(ctx)
    await ctx.info(f"統計表を検索中: {keyword}")

    tables = await client.search_stats(
        keyword,
        survey_years=survey_years,
        stats_field=stats_field,
        limit=limit,
    )

    if not tables:
        return f"「{keyword}」に該当する統計表が見つかりませんでした。"

    lines: list[str] = [f"## 検索結果: 「{keyword}」（{len(tables)}件）\n"]
    for t in tables:
        lines.append(f"- **{t.title}**")
        lines.append(f"  - 統計表ID: `{t.id}`")
        lines.append(f"  - 統計名: {t.stat_name}（{t.gov_org}）")
        if t.survey_date:
            lines.append(f"  - 調査年月: {t.survey_date}")
        lines.append("")

    lines.append(
        "> 統計表IDを `get_regional_data` や `compare_regions` に渡すと"
        "データを取得できます。"
    )
    return "\n".join(lines)


@mcp.tool()
async def get_regional_data(
    stats_data_id: str,
    area: str,
    ctx: Context,
    tab_code: str | None = None,
    time_code: str | None = None,
    cat01_code: str | None = None,
    limit: int = 1000,
    summary: bool = False,
) -> str:
    """指定した地域の統計データを取得し、整形して返す.

    コード番号は自動的に人間が読める名称に変換される。

    Args:
        stats_data_id: 統計表ID（search_statistics で取得）
        area: 地域名（例: "東京都"）または地域コード（例: "13000"）
        tab_code: 表章項目コード（特定の指標に絞り込む場合）
        time_code: 時間軸コード（特定の年に絞り込む場合）
        cat01_code: 分類事項01コード（特定のカテゴリに絞り込む場合）
        limit: 取得件数上限（デフォルト1000）
        summary: Trueの場合、最新時点の主要指標のみ返す（データ量を大幅に削減）

    Returns:
        整形済みの統計データ（マークダウンテーブル）
    """
    client = _get_client(ctx)

    # 地域名→コード変換
    try:
        area_code = _resolve_single_area(area)
    except AmbiguousAreaError as e:
        return str(e)
    await ctx.info(f"統計データを取得中: {stats_data_id} (地域: {area})")

    data = await client.get_stats_data(
        stats_data_id,
        cd_area=area_code,
        cd_tab=tab_code,
        cd_time=time_code,
        cd_cat01=cat01_code,
        limit=limit,
    )

    if not data.values:
        return f"該当するデータが見つかりませんでした（統計表ID: {stats_data_id}, 地域: {area}）。"

    fmt = StatsFormatter(data)

    # summary モード: 最新時点に絞り込み + 行数制限
    summary_note = ""
    filters: dict[str, str] | None = None
    md_limit: int | None = None
    if summary:
        latest = fmt.latest_time_code()
        if latest:
            filters = {"time": latest}
            time_name = fmt._meta.resolve_code("time", latest)
            summary_note = f"*{time_name or latest} のデータのみ表示*\n\n"
        md_limit = 50

    md = fmt.to_markdown(filters=filters, limit=md_limit)

    total = f"（全{data.total_count}件）" if data.total_count else ""
    header = f"## 統計データ: {stats_data_id} {total}\n{summary_note}"
    footer = build_source_footer(
        None, table_id=stats_data_id, area_name=area, area_code=area_code
    )
    return header + md + footer


@mcp.tool()
async def compare_regions(
    stats_data_id: str,
    areas: list[str],
    ctx: Context,
    tab_code: str | None = None,
    cat01_code: str | None = None,
) -> str:
    """複数地域の統計データを比較する.

    時間軸（年）を行、地域を列にしたピボットテーブルを生成。

    Args:
        stats_data_id: 統計表ID（search_statistics で取得）
        areas: 比較する地域名のリスト（例: ["東京都", "大阪府", "愛知県"]）
        tab_code: 表章項目コード（特定の指標に絞り込む場合）
        cat01_code: 分類事項01コード（特定のカテゴリに絞り込む場合）

    Returns:
        地域比較のピボットテーブル（マークダウン）
    """
    client = _get_client(ctx)

    try:
        area_codes = [_resolve_single_area(a) for a in areas]
    except AmbiguousAreaError as e:
        return str(e)
    cd_area = ",".join(area_codes)

    await ctx.info(f"地域比較データを取得中: {', '.join(areas)}")

    data = await client.get_stats_data(
        stats_data_id,
        cd_area=cd_area,
        cd_tab=tab_code,
        cd_cat01=cat01_code,
    )

    if not data.values:
        return f"該当するデータが見つかりませんでした（統計表ID: {stats_data_id}）。"

    fmt = StatsFormatter(data)

    # ユーザーが明示したフィルタ
    explicit: dict[str, str] = {}
    if tab_code:
        explicit["tab"] = tab_code
    if cat01_code:
        explicit["cat01"] = cat01_code

    # time/area 以外の次元を自動フィルタ（重複セル防止）
    filters = fmt.auto_filters_for_pivot(
        "time", "area", explicit_filters=explicit or None
    )

    md = fmt.pivot_to_markdown("time", "area", filters=filters or None)

    # フィルタで絞り込んだ次元の内容を注記
    filter_notes: list[str] = []
    for dim_id, code in filters.items():
        if dim_id in ("time", "area"):
            continue
        name = fmt._meta.resolve_code(dim_id, code)
        dim_name = fmt._dim_names.get(dim_id, dim_id)
        if name:
            filter_notes.append(f"{dim_name}: {name}")
    note = ""
    if filter_notes:
        note = f"*絞り込み条件: {', '.join(filter_notes)}*\n\n"

    header = f"## 地域比較: {', '.join(areas)}\n{note}"
    footer = build_source_footer(
        None,
        table_id=stats_data_id,
        area_name=", ".join(areas),
        area_code=cd_area,
    )
    return header + md + footer


# ------------------------------------------------------------------
# 法人番号ツール
# ------------------------------------------------------------------

_CORP_NOT_CONFIGURED = (
    "法人番号APIが設定されていません。\n\n"
    "利用するには環境変数 `CORP_APP_ID` にアプリケーションIDを設定してください。\n"
    "取得方法: https://www.houjin-bangou.nta.go.jp/webapi/"
)


@mcp.tool()
async def search_corporations(
    name: str,
    ctx: Context,
    area: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """法人名で企業を検索する.

    国税庁の法人番号公表サイトから法人情報を検索。
    地域や法人種別で絞り込み可能。

    Args:
        name: 検索キーワード（法人名、部分一致）
        area: 地域名で絞り込み（都道府県名、例: "東京都"）
        kind: 法人種別で絞り込み（"01"=国の機関, "02"=地方公共団体,
              "03"=設立登記法人, "04"=その他）
        limit: 取得件数上限（デフォルト10、最大2000）

    Returns:
        法人情報の一覧（マークダウンテーブル）
    """
    corp_client = _get_corp_client(ctx)
    if corp_client is None:
        return _CORP_NOT_CONFIGURED

    await ctx.info(f"法人を検索中: {name}")

    # 地域指定がある場合は都道府県コードに変換
    pref_code: str | None = None
    if area:
        try:
            code = _resolve_single_area(area)
        except AmbiguousAreaError as e:
            return str(e)
        pref_code = code[:2]  # 上2桁が都道府県コード

    try:
        corps = await corp_client.search_by_name(
            name,
            prefecture_code=pref_code,
            kind=kind,
            limit=limit,
        )
    except CorpApiError as e:
        return f"法人番号API エラー: {e.message}"

    if not corps:
        msg = f"「{name}」に該当する法人が見つかりませんでした。"
        if area:
            msg += f"（地域: {area}）"
        return msg

    return _format_corp_list(corps, name, area)


@mcp.tool()
async def get_corporation(
    corp_number: str,
    ctx: Context,
) -> str:
    """法人番号から企業の詳細情報を取得する.

    13桁の法人番号を指定して、法人の正式名称・所在地・種別などを取得。

    Args:
        corp_number: 法人番号（13桁の数字）

    Returns:
        法人の詳細情報（マークダウン）
    """
    corp_client = _get_corp_client(ctx)
    if corp_client is None:
        return _CORP_NOT_CONFIGURED

    await ctx.info(f"法人情報を取得中: {corp_number}")

    try:
        corps = await corp_client.get_by_number([corp_number])
    except CorpApiError as e:
        return f"法人番号API エラー: {e.message}"

    if not corps:
        return f"法人番号 `{corp_number}` に該当する法人が見つかりませんでした。"

    corp = corps[0]
    return _format_corp_detail(corp)


def _format_corp_list(
    corps: list[Corporation],
    query: str,
    area: str | None = None,
) -> str:
    """法人リストをマークダウンテーブルに整形する."""
    lines: list[str] = [f"## 法人検索結果: 「{query}」（{len(corps)}件）\n"]

    headers = ["法人名", "法人番号", "種別", "所在地", "状態"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for c in corps:
        status = "現存" if c.is_active else "閉鎖"
        row = [
            c.name,
            f"`{c.corporate_number}`",
            c.kind_label,
            f"{c.prefecture_name}{c.city_name}",
            status,
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append(_build_corp_search_footer(query, area))
    return "\n".join(lines)


def _format_corp_detail(corp: Corporation) -> str:
    """法人の詳細情報をマークダウンに整形する."""
    lines: list[str] = [f"## {corp.name}\n"]
    lines.append(f"- **法人番号**: `{corp.corporate_number}`")
    lines.append(f"- **法人種別**: {corp.kind_label}")
    lines.append(f"- **所在地**: {corp.full_address}")
    if corp.post_code:
        lines.append(f"- **郵便番号**: {corp.post_code}")
    if corp.furigana:
        lines.append(f"- **フリガナ**: {corp.furigana}")
    lines.append(f"- **法人番号指定日**: {corp.assignment_date}")
    if corp.change_date:
        lines.append(f"- **最終変更日**: {corp.change_date}")
    lines.append(f"- **状態**: {'現存' if corp.is_active else '閉鎖'}")
    if corp.close_date:
        lines.append(f"- **閉鎖日**: {corp.close_date}")

    lines.append(_build_corp_detail_footer(corp))
    return "\n".join(lines)


def _build_corp_search_footer(
    query: str, area: str | None = None
) -> str:
    """法人検索の検証フッターを生成する."""
    now = datetime.now(_JST).strftime("%Y-%m-%d %H:%M JST")
    lines: list[str] = ["", "---", "**データ検証情報**"]
    lines.append("- 出典: 国税庁 法人番号公表サイト")
    lines.append(
        "- 法人番号公表サイトで確認: "
        "https://www.houjin-bangou.nta.go.jp/"
    )
    search_parts = [query]
    if area:
        search_parts.append(area)
    lines.append(f"- 検索条件: {' / '.join(search_parts)}")
    lines.append(f"- データ取得日時: {now}")
    lines.append(
        "- ⚠ 本データは法人番号公表サイト Web-API から自動取得した値をそのまま表示しています。"
        "正確性の最終確認は上記リンクから原本データをご参照ください。"
    )
    return "\n".join(lines)


def _build_corp_detail_footer(corp: Corporation) -> str:
    """法人詳細の検証フッターを生成する."""
    now = datetime.now(_JST).strftime("%Y-%m-%d %H:%M JST")
    lines: list[str] = ["", "---", "**データ検証情報**"]
    lines.append("- 出典: 国税庁 法人番号公表サイト")
    lines.append(f"- 法人番号公表サイトで確認: {corp.verification_url}")
    lines.append(f"- 法人番号: {corp.corporate_number}")
    lines.append(f"- データ取得日時: {now}")
    lines.append(
        "- ⚠ 本データは法人番号公表サイト Web-API から自動取得した値をそのまま表示しています。"
        "正確性の最終確認は上記リンクから原本データをご参照ください。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# 不動産取引価格ツール
# ------------------------------------------------------------------

_REALESTATE_NOT_CONFIGURED = (
    "不動産取引価格APIが設定されていません。\n\n"
    "利用するには環境変数 `REALESTATE_API_KEY` にAPIキーを設定してください。\n"
    "取得方法: https://www.reinfolib.mlit.go.jp/ex-api/"
)


@mcp.tool()
async def get_real_estate_transactions(
    area: str,
    ctx: Context,
    year: int | None = None,
    quarter: int | None = None,
) -> str:
    """不動産取引価格情報を取得する.

    国土交通省の不動産情報ライブラリから、指定地域の不動産取引データを取得。
    取引種別・価格・面積・建築年・最寄駅などの情報を含む。

    Args:
        area: 地域名（例: "東京都", "水戸市"）または地域コード
        year: 取引年で絞り込み（例: 2023）
        quarter: 四半期で絞り込み（1〜4）

    Returns:
        不動産取引データの一覧と価格サマリー（マークダウン）
    """
    re_client = _get_realestate_client(ctx)
    if re_client is None:
        return _REALESTATE_NOT_CONFIGURED

    try:
        area_code = _resolve_single_area(area)
    except AmbiguousAreaError as e:
        return str(e)

    area_name = _get_area_display_name(area, area_code)
    pref_code = area_code[:2]
    # 都道府県コードの場合は市区町村を指定しない
    city_code = area_code if not area_code.endswith("000") else None

    await ctx.info(f"不動産取引データを取得中: {area_name}")

    try:
        transactions = await re_client.get_transactions(
            pref_code,
            city_code=city_code,
            year=year,
            quarter=quarter,
        )
    except RealEstateApiError as e:
        return f"不動産情報ライブラリAPI エラー: {e.message}"

    return format_transactions(
        transactions,
        area_name=area_name,
        year=year,
        quarter=quarter,
    )


def _json_response(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _validated_city_code(city_code: str) -> tuple[str, str]:
    if not re.fullmatch(r"\d{5}", city_code):
        raise ValueError("city_codeは5桁の全国地方公共団体コードで指定してください。")
    return city_code[:2], city_code


def _normalize_years(years: list[int] | None) -> list[int]:
    current_year = datetime.now(_JST).year
    selected = years or [current_year - 1, current_year - 2]
    selected = list(dict.fromkeys(selected))
    if not 1 <= len(selected) <= 5:
        raise ValueError("yearsは1〜5年を指定してください。")
    if any(year < 2005 or year > current_year for year in selected):
        raise ValueError(f"yearsは2005〜{current_year}の範囲で指定してください。")
    return selected


async def _transaction_evidence(
    *,
    area_name: str,
    city_code: str,
    years: list[int] | None,
    land_only: bool,
    target_area_sqm: float | None,
    ctx: Context,
) -> dict[str, Any]:
    """Collect transaction evidence without treating it as current inventory."""
    try:
        pref_code, normalized_city_code = _validated_city_code(city_code)
        selected_years = _normalize_years(years)
    except ValueError as exc:
        return build_evidence_envelope(
            status="upstream_error",
            data_as_of=None,
            precision="transaction",
            confidence="not_applicable",
            sources=[_REINFOLIB_SOURCE],
            limitations=[str(exc)],
            data={"transactions": [], "errors": [str(exc)]},
        )

    re_client = _get_realestate_client(ctx)
    if re_client is None:
        return build_evidence_envelope(
            status="not_configured",
            data_as_of=None,
            precision="transaction",
            confidence="not_applicable",
            sources=[_REINFOLIB_SOURCE],
            limitations=[
                "REALESTATE_API_KEYが未設定です。",
                "過去成約であり、現在の売出在庫ではありません。",
            ],
            data={
                "query": {
                    "area_name": area_name,
                    "city_code": normalized_city_code,
                    "years": selected_years,
                    "land_only": land_only,
                },
                "summary": calculate_price_stats([], target_area_sqm),
                "transactions": [],
                "errors": [],
            },
        )

    await ctx.info(
        f"不動産取引エビデンスを取得中: {area_name} ({', '.join(map(str, selected_years))})"
    )
    results = await asyncio.gather(
        *(
            re_client.get_transactions(
                pref_code, city_code=normalized_city_code, year=year
            )
            for year in selected_years
        ),
        return_exceptions=True,
    )
    successful_years: list[int] = []
    errors: list[str] = []
    transactions = []
    for year, result in zip(selected_years, results, strict=True):
        if isinstance(result, BaseException):
            if isinstance(result, RealEstateApiError):
                errors.append(f"{year}: {result.message}")
            else:
                errors.append(f"{year}: upstream request failed")
            continue
        successful_years.append(year)
        transactions.extend(result)

    normalized = filter_transactions(
        transactions, area_name=area_name, land_only=land_only
    )
    if errors and not successful_years:
        status = "upstream_error"
    elif errors:
        status = "partial"
    elif not normalized:
        status = "no_results"
    else:
        status = "ok"

    return build_evidence_envelope(
        status=status,
        data_as_of=str(max(successful_years)) if successful_years else None,
        precision="transaction",
        confidence=(
            "high" if len(normalized) >= 5 else "medium" if normalized else "low"
        ),
        sources=[_REINFOLIB_SOURCE],
        limitations=[
            "これは過去の成約事例であり、現在売り出されている物件ではありません。",
            "公開データでは所在地や面積等が丸められる場合があります。",
            "価格推定は成約単価の中央値による単純計算で、査定ではありません。",
        ],
        data={
            "query": {
                "area_name": area_name,
                "city_code": normalized_city_code,
                "years": selected_years,
                "land_only": land_only,
            },
            "summary": calculate_price_stats(normalized, target_area_sqm),
            "transactions": normalized[:20],
            "total_matches": len(normalized),
            "errors": errors,
        },
    )


async def _optional_feed_evidence(
    *,
    kind: str,
    area_name: str,
    target_area_sqm: float | None,
    city_code: str | None,
    ctx: Context,
) -> dict[str, Any]:
    """Read an optional licensed listing or spatial-ETL normalized feed."""
    if kind == "listing":
        url = os.environ.get("LISTINGS_JSON_URL", "")
        token = os.environ.get("LISTINGS_API_TOKEN", "")
        item_key = "listings"
        fallback_source = "https://data-solution.homes.jp/"
        missing = (
            "現在の売出フィードが未接続です。公開取引データから売出中とは推測しません。"
        )
        configured_limit = (
            "掲載中でも申込済み・成約済みの場合があります。仲介会社への確認が必要です。"
        )
        precision = "listing"
    else:
        url = os.environ.get("VACANT_CANDIDATES_URL", "")
        token = os.environ.get("VACANT_CANDIDATES_API_TOKEN", "")
        item_key = "candidates"
        fallback_source = _PLATEAU_SOURCE["url"]
        missing = (
            "地番・建物・土地利用等を統合する空間ETLが未接続です。"
        )
        configured_limit = (
            "候補判定であり、所有者・売却意思・建築可能性を保証しません。"
        )
        precision = "candidate"

    if kind == "listing" and not url:
        normalized_city_code = str(city_code or "").strip()
        municipality = CODE_TO_AREA.get(normalized_city_code)
        if municipality is None:
            return build_evidence_envelope(
                status="upstream_error",
                data_as_of=PROJECT_LINKS_DATA_AS_OF,
                precision="municipality",
                confidence="not_applicable",
                sources=[_PROJECT_LINKS_SOURCE],
                limitations=["市区町村コードから自治体名を解決できませんでした。"],
                data={item_key: [], "total_matches": 0},
            )
        try:
            response = await _get_public_http_client(ctx).get(
                PROJECT_LINKS_LISTINGS_CSV_URL,
                follow_redirects=True,
            )
            response.raise_for_status()
            matches = parse_project_links_listings(
                response.content,
                municipality=municipality,
                land_only=True,
                target_area_sqm=target_area_sqm,
            )
        except (httpx.HTTPError, UnicodeError, ValueError):
            return build_evidence_envelope(
                status="upstream_error",
                data_as_of=PROJECT_LINKS_DATA_AS_OF,
                precision="municipality",
                confidence="not_applicable",
                sources=[_PROJECT_LINKS_SOURCE],
                limitations=["Project LINKS登録物件CSVの取得または形式検証に失敗しました。"],
                data={item_key: [], "total_matches": 0},
            )
        return build_evidence_envelope(
            status="ok" if matches else "no_results",
            data_as_of=PROJECT_LINKS_DATA_AS_OF,
            precision="municipality",
            confidence="medium" if matches else "low",
            sources=[_PROJECT_LINKS_SOURCE],
            limitations=[
                "2025年3月31日時点のスナップショットで、現在も掲載中とは限りません。",
                "町丁目以下の住所は含まれず、市区町村単位の候補です。",
                "LIFULL提供分のみで、全国版空き家・空き地バンクの全件ではありません。",
            ],
            data={
                item_key: matches[:20],
                "total_matches": len(matches),
                "query_municipality": municipality,
                "requested_area_name": area_name,
                "snapshot_only": True,
            },
        )

    if not url:
        return build_evidence_envelope(
            status="not_configured",
            data_as_of=None,
            precision=precision,  # type: ignore[arg-type]
            confidence="not_applicable",
            sources=[],
            limitations=[missing],
            data={item_key: [], "total_matches": 0},
        )

    parsed = urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        return build_evidence_envelope(
            status="upstream_error",
            data_as_of=None,
            precision=precision,  # type: ignore[arg-type]
            confidence="not_applicable",
            sources=[],
            limitations=["正規フィードURLはHTTPSで設定してください。"],
            data={item_key: [], "total_matches": 0},
        )

    try:
        response = await _get_public_http_client(ctx).get(
            url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("normalized feed must return a JSON object")
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("normalized feed items must be an array")
        min_area = target_area_sqm * 0.8 if target_area_sqm else None
        max_area = target_area_sqm * 1.2 if target_area_sqm else None
        matches = filter_feed_items(
            [item for item in raw_items if isinstance(item, dict)],
            area_name=area_name,
            min_area_sqm=min_area,
            max_area_sqm=max_area,
        )
    except (httpx.HTTPError, ValueError):
        return build_evidence_envelope(
            status="upstream_error",
            data_as_of=None,
            precision=precision,  # type: ignore[arg-type]
            confidence="not_applicable",
            sources=[],
            limitations=["正規フィードの取得または形式検証に失敗しました。"],
            data={item_key: [], "total_matches": 0},
        )

    source = {
        "name": str(payload.get("source") or "Authorized normalized feed"),
        "url": public_source_url(payload.get("source_url"), fallback_source),
    }
    return build_evidence_envelope(
        status="ok" if matches else "no_results",
        data_as_of=(
            str(payload["data_as_of"]) if payload.get("data_as_of") else None
        ),
        precision=precision,  # type: ignore[arg-type]
        confidence="high" if kind == "listing" else "medium",
        sources=[source],
        limitations=[configured_limit],
        data={item_key: matches[:20], "total_matches": len(matches)},
    )


@mcp.tool()
async def search_real_estate_transactions(
    area_name: str,
    city_code: str,
    ctx: Context,
    years: list[int] | None = None,
    land_only: bool = True,
) -> str:
    """町名・市区町村コード・年から過去の成約事例を出典付きJSONで検索する.

    現在の売出物件を探す用途には使わない。土地調査全体には
    research_real_estate_areaを優先する。
    """
    return _json_response(
        await _transaction_evidence(
            area_name=area_name,
            city_code=city_code,
            years=years,
            land_only=land_only,
            target_area_sqm=None,
            ctx=ctx,
        )
    )


@mcp.tool()
async def get_connector_status(ctx: Context) -> str:
    """公開情報データ源の接続状態と、未接続のため回答できない領域を返す."""
    del ctx
    return _json_response(
        build_evidence_envelope(
            status="ok",
            data_as_of=None,
            precision="dataset",
            confidence="not_applicable",
            sources=[],
            limitations=[
                "configuredは環境変数の存在確認であり、上流APIの疎通成功を保証しません。"
            ],
            data={
                "service": "japan-public-info-mcp",
                "read_only": True,
                "tool_profile": os.environ.get("MCP_TOOL_PROFILE", "full"),
                "visible_tool_count": len(mcp._tool_manager._tools),
                "connectors": {
                    "estat": {"configured": bool(os.environ.get("ESTAT_APP_ID"))},
                    "corporations_and_invoice": {
                        "configured": bool(os.environ.get("CORP_APP_ID"))
                    },
                    "transaction_prices": {
                        "configured": bool(os.environ.get("REALESTATE_API_KEY")),
                        "source": _REINFOLIB_SOURCE,
                    },
                    "plateau_catalog": {
                        "configured": True,
                        "source": _PLATEAU_SOURCE,
                    },
                    "current_listings": {
                        "configured": True,
                        "mode": (
                            "authorized_normalized_feed"
                            if os.environ.get("LISTINGS_JSON_URL")
                            else "official_snapshot_fallback"
                        ),
                        "real_time": bool(os.environ.get("LISTINGS_JSON_URL")),
                        "source": _PROJECT_LINKS_SOURCE,
                        "requires_for_realtime": "authorized normalized feed",
                    },
                    "vacant_land_candidates": {
                        "configured": bool(
                            os.environ.get("VACANT_CANDIDATES_URL")
                        ),
                        "requires": "parcel/building/land-use spatial ETL",
                    },
                    "owners": {
                        "configured": False,
                        "reason": "Phase 1の公開tool契約には含めない",
                    },
                },
            },
        )
    )


@mcp.tool()
async def search_plateau_datasets(
    ctx: Context,
    city_code: str | None = None,
    dataset_type: str | None = None,
    year: int | None = None,
) -> str:
    """自治体コード・データ種別・年度でPLATEAU公式カタログを検索する.

    データセットの存在確認用であり、空き地や売却可否を直接判定しない。
    """
    try:
        response = await _get_public_http_client(ctx).get(_PLATEAU_DATASETS_URL)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            datasets = payload
        elif isinstance(payload, dict):
            datasets = next(
                (
                    payload[key]
                    for key in ("data", "datasets", "results")
                    if isinstance(payload.get(key), list)
                ),
                [],
            )
        else:
            datasets = []
    except (httpx.HTTPError, ValueError):
        return _json_response(
            build_evidence_envelope(
                status="upstream_error",
                data_as_of=None,
                precision="dataset",
                confidence="not_applicable",
                sources=[_PLATEAU_SOURCE],
                limitations=["PLATEAU公式カタログの取得に失敗しました。"],
                data={"count": 0, "datasets": []},
            )
        )

    matches: list[dict[str, Any]] = []
    for raw in datasets:
        if not isinstance(raw, dict):
            continue
        raw_city = str(raw.get("city_code") or raw.get("cityCode") or "")
        raw_type = str(raw.get("type_en") or raw.get("type") or "").lower()
        raw_year = raw.get("year") or raw.get("fiscal_year")
        if city_code and raw_city != city_code:
            continue
        if dataset_type and dataset_type.lower() not in raw_type:
            continue
        if year is not None and str(raw_year) != str(year):
            continue
        matches.append(raw)

    return _json_response(
        build_evidence_envelope(
            status="ok" if matches else "no_results",
            data_as_of=datetime.now(_JST).date().isoformat(),
            precision="dataset",
            confidence="high",
            sources=[_PLATEAU_SOURCE],
            limitations=[
                "データセットの存在を示すもので、空き地や売却可否を直接判定しません。"
            ],
            data={"count": len(matches), "datasets": matches[:100]},
        )
    )


@mcp.tool()
async def search_government_open_data(
    query: str,
    ctx: Context,
    limit: int = 10,
) -> str:
    """e-Govデータポータルで政府機関の公式オープンデータを横断検索する."""
    safe_limit = max(1, min(limit, 20))
    try:
        response = await _get_public_http_client(ctx).get(
            _EGOV_DATA_PORTAL_SEARCH_URL,
            params={"q": query, "rows": safe_limit},
        )
        response.raise_for_status()
        payload = response.json()
        result = payload.get("result", {}) if isinstance(payload, dict) else {}
        raw_datasets = result.get("results", []) if isinstance(result, dict) else []
        datasets = []
        for item in raw_datasets:
            if not isinstance(item, dict):
                continue
            resources = []
            for resource in item.get("resources", [])[:5]:
                if not isinstance(resource, dict):
                    continue
                resources.append(
                    {
                        "name": resource.get("name"),
                        "format": resource.get("format"),
                        "url": public_source_url(
                            resource.get("url"), "https://data.e-gov.go.jp/"
                        ),
                        "last_modified": resource.get("last_modified"),
                    }
                )
            datasets.append(
                {
                    "dataset_id": item.get("name") or item.get("id"),
                    "title": item.get("title"),
                    "publisher": item.get("publisher")
                    or (item.get("organization") or {}).get("title"),
                    "description": str(item.get("notes") or "")[:500],
                    "metadata_modified": item.get("metadata_modified"),
                    "landing_page": public_source_url(
                        item.get("landingPage"), "https://data.e-gov.go.jp/"
                    ),
                    "resources": resources,
                }
            )
    except (httpx.HTTPError, ValueError, TypeError):
        return _json_response(
            build_evidence_envelope(
                status="upstream_error",
                data_as_of=None,
                precision="dataset",
                confidence="not_applicable",
                sources=[
                    {
                        "name": "e-Govデータポータル メタデータ取得API",
                        "url": "https://data.e-gov.go.jp/data/api_guide",
                    }
                ],
                limitations=["公式データカタログAPIの取得または形式検証に失敗しました。"],
                data={"query": query, "datasets": [], "total_matches": 0},
            )
        )
    return _json_response(
        build_evidence_envelope(
            status="ok" if datasets else "no_results",
            data_as_of=None,
            precision="dataset",
            confidence="high" if datasets else "low",
            sources=[
                {
                    "name": "e-Govデータポータル メタデータ取得API",
                    "url": "https://data.e-gov.go.jp/data/api_guide",
                }
            ],
            limitations=[
                "検索結果はデータセットのメタデータです。数値や本文は各公式リソースを確認してください。",
                "主に国の行政機関のカタログで、全自治体の広報・オープンデータを網羅しません。",
            ],
            data={
                "query": query,
                "datasets": datasets,
                "returned": len(datasets),
                "total_matches": result.get("count"),
            },
        )
    )


async def _fetch_mlit_layer(
    *,
    api_number: int,
    lat: float,
    lon: float,
    distance_m: float,
    year: int | None,
    api_key: str,
    ctx: Context,
) -> dict[str, Any]:
    layer = REPRESENTATIVE_LAYERS[api_number]
    x, y, x_fraction, y_fraction = latlon_to_tile_fraction(lat, lon)
    tiles = (
        surrounding_tiles(x, y, x_fraction, y_fraction)
        if layer["geometry"] == "point"
        else [(x, y)]
    )
    requests = []
    for tile_x, tile_y in tiles:
        params: dict[str, Any] = {
            "response_format": "geojson",
            "z": ZOOM,
            "x": tile_x,
            "y": tile_y,
        }
        if api_number == 3 and year is not None:
            params["year"] = year
        requests.append(
            _get_public_http_client(ctx).get(
                f"{_REINFOLIB_EXTERNAL_BASE}/{layer['endpoint']}",
                params=params,
                headers={
                    "Ocp-Apim-Subscription-Key": api_key,
                    "Accept": "application/geo+json, application/json",
                },
            )
        )

    responses = await asyncio.gather(*requests, return_exceptions=True)
    raw_features: list[dict[str, Any]] = []
    errors: list[str] = []
    for response in responses:
        if isinstance(response, BaseException):
            errors.append("upstream request failed")
            continue
        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            errors.append("upstream response failed validation")
            continue
        if isinstance(payload, dict) and isinstance(payload.get("features"), list):
            raw_features.extend(
                feature
                for feature in payload["features"]
                if isinstance(feature, dict)
            )

    matches = filter_geojson_features(
        raw_features,
        geometry_mode=layer["geometry"],
        lat=lat,
        lon=lon,
        distance_m=distance_m,
    )
    status = (
        "partial"
        if errors and matches
        else "upstream_error"
        if errors and not raw_features
        else "ok"
        if matches
        else "no_results"
    )
    return {
        "api_number": api_number,
        "name": layer["name"],
        "use": layer["use"],
        "endpoint": layer["endpoint"],
        "status": status,
        "count": len(matches),
        "features": matches[:100],
        "errors": errors,
    }


@mcp.tool()
async def get_mlit_geospatial_layers(
    lat: float,
    lon: float,
    target_apis: list[int],
    ctx: Context,
    distance: float = 425.0,
    year: int | None = None,
) -> str:
    """指定地点の国交省代表レイヤーを取得する段階移植adapter.

    既存get_multi_apiと同じlat・lon・target_apis・distance・yearを受ける。
    Phase 1対応APIは3（地価）、4（都市計画区域）、5（用途地域）、
    11（医療機関）。一度に4種類までの読み取り専用取得に限定する。
    """
    api_key = os.environ.get("REALESTATE_API_KEY", "")
    try:
        if not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError("緯度・経度が範囲外です。")
        if not 0 <= distance <= 425:
            raise ValueError("distanceは0〜425mで指定してください。")
        selected = list(dict.fromkeys(target_apis))
        if not selected or len(selected) > 4:
            raise ValueError("target_apisは1〜4個指定してください。")
        unsupported = sorted(set(selected) - set(REPRESENTATIVE_LAYERS))
        if unsupported:
            raise ValueError(
                f"Phase 1未対応のAPI番号です: {unsupported}。対応: {sorted(REPRESENTATIVE_LAYERS)}"
            )
        if year is not None and not 1995 <= year <= datetime.now(_JST).year:
            raise ValueError("yearは1995年から現在年までで指定してください。")
    except ValueError as exc:
        return _json_response(
            build_evidence_envelope(
                status="upstream_error",
                data_as_of=None,
                precision="area",
                confidence="not_applicable",
                sources=[{"name": "国土交通省 不動産情報ライブラリ", "url": _REINFOLIB_API_MANUAL}],
                limitations=[str(exc)],
                data={"layers": [], "errors": [str(exc)]},
            )
        )

    if not api_key:
        return _json_response(
            build_evidence_envelope(
                status="not_configured",
                data_as_of=None,
                precision="area",
                confidence="not_applicable",
                sources=[{"name": "国土交通省 不動産情報ライブラリ", "url": _REINFOLIB_API_MANUAL}],
                limitations=["REALESTATE_API_KEYが未設定です。"],
                data={
                    "query": {"lat": lat, "lon": lon, "target_apis": selected},
                    "layers": [],
                },
            )
        )

    await ctx.info(f"国交省代表レイヤーを取得中: API {selected}")
    layers = await asyncio.gather(
        *(
            _fetch_mlit_layer(
                api_number=api_number,
                lat=lat,
                lon=lon,
                distance_m=distance,
                year=year,
                api_key=api_key,
                ctx=ctx,
            )
            for api_number in selected
        )
    )
    statuses = [layer["status"] for layer in layers]
    status = (
        "ok"
        if all(value in {"ok", "no_results"} for value in statuses)
        else "upstream_error"
        if all(value == "upstream_error" for value in statuses)
        else "partial"
    )
    sources = [
        {
            "name": "国土交通省 不動産情報ライブラリ",
            "url": _REINFOLIB_API_MANUAL,
            "document_id": f"API {api_number}: {REPRESENTATIVE_LAYERS[api_number]['endpoint']}",
            "query": f"z={ZOOM}, lat/lon, distance={distance}, year={year}",
        }
        for api_number in selected
    ]
    return _json_response(
        build_evidence_envelope(
            status=status,  # type: ignore[arg-type]
            data_as_of=str(year) if year is not None and 3 in selected else None,
            precision="area",
            confidence="high" if status == "ok" else "medium" if status == "partial" else "low",
            sources=sources,
            limitations=[
                "Phase 1は代表4 APIのみの段階移植です。旧get_multi_apiの全30 API互換ではありません。",
                "地価は周辺点、都市計画・用途地域は指定点との交差、医療機関は半径内の結果です。",
                "公式原データの属性定義を確認して最終判断してください。",
            ],
            data={
                "query": {
                    "lat": lat,
                    "lon": lon,
                    "target_apis": selected,
                    "distance": distance,
                    "year": year,
                },
                "supported_api_numbers": sorted(REPRESENTATIVE_LAYERS),
                "layers": layers,
            },
        )
    )


@mcp.tool()
async def research_real_estate_area(
    area_name: str,
    city_code: str,
    ctx: Context,
    target_area_sqm: float | None = None,
    years: list[int] | None = None,
) -> str:
    """地域の土地について過去成約・現在の売出・空き地候補を分離して調査する.

    土地価格や購入可能性を質問された場合の標準入口。未接続の証拠層は
    推測で補わずnot_configuredとして返す。
    """
    transactions, listings, candidates = await asyncio.gather(
        _transaction_evidence(
            area_name=area_name,
            city_code=city_code,
            years=years,
            land_only=True,
            target_area_sqm=target_area_sqm,
            ctx=ctx,
        ),
        _optional_feed_evidence(
            kind="listing",
            area_name=area_name,
            target_area_sqm=target_area_sqm,
            city_code=city_code,
            ctx=ctx,
        ),
        _optional_feed_evidence(
            kind="candidate",
            area_name=area_name,
            target_area_sqm=target_area_sqm,
            city_code=city_code,
            ctx=ctx,
        ),
    )
    layers = [transactions, listings, candidates]
    layer_statuses = [layer["status"] for layer in layers]
    if all(status == "ok" for status in layer_statuses):
        status = "ok"
    elif all(status == "not_configured" for status in layer_statuses):
        status = "not_configured"
    elif all(status in {"upstream_error", "not_configured"} for status in layer_statuses):
        status = "upstream_error"
    else:
        status = "partial"

    sources: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for layer in layers:
        for source in layer["sources"]:
            if source.get("url") not in seen_urls:
                sources.append(source)
                seen_urls.add(str(source.get("url")))

    transaction_count = transactions["data"].get("total_matches", 0)
    unknowns = []
    if listings["status"] == "not_configured":
        unknowns.append("現在売り出されている土地の有無")
    elif listings["data"].get("snapshot_only"):
        unknowns.append("2025年3月31日以降の掲載継続状況")
    if candidates["status"] == "not_configured":
        unknowns.append("建物が見当たらない土地候補の有無")
    unknowns.extend(["所有者", "売却意思", "権利関係", "建築可能性"])

    return _json_response(
        build_evidence_envelope(
            status=status,  # type: ignore[arg-type]
            data_as_of=transactions["data_as_of"],
            precision="area",
            confidence=(
                "medium" if transaction_count >= 5 else "low"
            ),
            sources=sources,
            limitations=[
                "過去成約、現在の売出、物理的な空き地候補は別々の証拠です。",
                "購入可能性と価格は現地・仲介・法務確認なしに確定できません。",
            ],
            data={
                "query": {
                    "area_name": area_name,
                    "city_code": city_code,
                    "target_area_sqm": target_area_sqm,
                    "years": years,
                },
                "transactions": transactions,
                "current_listings": listings,
                "vacant_candidates": candidates,
                "unknowns": unknowns,
                "next_actions": [
                    "正規の売出フィードまたは仲介会社で現在の掲載状況を確認する",
                    "用途地域・接道・建築制限を公式資料で確認する",
                    "候補地は現地確認と権利調査を行う",
                ],
            },
        )
    )


# ------------------------------------------------------------------
# インボイスツール
# ------------------------------------------------------------------

_INVOICE_NOT_CONFIGURED = (
    "インボイスAPIが設定されていません。\n\n"
    "利用するには環境変数 `CORP_APP_ID` にアプリケーションIDを設定してください。\n"
    "取得方法: https://www.houjin-bangou.nta.go.jp/webapi/"
)


@mcp.tool()
async def check_invoice_registration(
    number: str,
    ctx: Context,
    history: bool = False,
) -> str:
    """適格請求書発行事業者の登録情報を登録番号で確認する.

    インボイス制度に基づく適格請求書発行事業者の登録状況・
    名称・所在地などを確認できる。

    Args:
        number: 登録番号（T+13桁の数字、例: "T1234567890123"）。
                カンマ区切りで最大10件まで同時に検索可能。
        history: 変更履歴を含めるか（デフォルト: False）

    Returns:
        登録事業者の情報（マークダウン）
    """
    invoice_client = _get_invoice_client(ctx)
    if invoice_client is None:
        return _INVOICE_NOT_CONFIGURED

    numbers = [n.strip() for n in number.split(",") if n.strip()]
    if not numbers:
        return "登録番号を指定してください。"

    for n in numbers:
        if not _is_valid_invoice_number(n):
            return (
                f"登録番号 `{n}` の形式が不正です。\n"
                "T + 13桁の数字で指定してください（例: T1234567890123）。"
            )

    await ctx.info(f"インボイス登録情報を確認中: {', '.join(numbers)}")

    try:
        issuers = await invoice_client.get_by_number(
            numbers, history=history
        )
    except InvoiceApiError as e:
        return f"インボイスAPI エラー: {e.message}"

    if not issuers:
        return (
            f"登録番号 `{number}` に該当する"
            "適格請求書発行事業者が見つかりませんでした。"
        )

    if len(issuers) == 1:
        return _format_invoice_detail(issuers[0])
    return _format_invoice_list(issuers)


@mcp.tool()
async def validate_invoice_on_date(
    number: str,
    day: str,
    ctx: Context,
) -> str:
    """指定日時点での適格請求書発行事業者の登録有効性を確認する.

    特定の取引日に事業者がインボイス発行資格を持っていたかを確認できる。

    Args:
        number: 登録番号（T+13桁の数字、例: "T1234567890123"）
        day: 確認日（YYYY-MM-DD形式、例: "2024-12-01"）

    Returns:
        指定日時点の登録状態（マークダウン）
    """
    invoice_client = _get_invoice_client(ctx)
    if invoice_client is None:
        return _INVOICE_NOT_CONFIGURED

    if not _is_valid_invoice_number(number):
        return (
            f"登録番号 `{number}` の形式が不正です。\n"
            "T + 13桁の数字で指定してください（例: T1234567890123）。"
        )

    await ctx.info(f"インボイス有効性を確認中: {number}（{day}時点）")

    try:
        issuer = await invoice_client.validate_on_date(number, day)
    except InvoiceApiError as e:
        return f"インボイスAPI エラー: {e.message}"

    if issuer is None:
        return (
            f"登録番号 `{number}` は {day} 時点で"
            "適格請求書発行事業者として登録されていません。"
        )

    header = f"**{day} 時点の登録状態**\n\n"
    return header + _format_invoice_detail(issuer)


@mcp.tool()
async def search_invoice_by_name(
    name: str,
    ctx: Context,
    area: str | None = None,
    limit: int = 5,
) -> str:
    """会社名からインボイス登録番号を検索する.

    法人番号APIで会社名を検索し、該当法人のインボイス登録状況を
    自動で確認する。法人番号 → 登録番号（T+法人番号）の変換を
    内部で行うため、登録番号を知らなくても検索できる。

    ※ 個人事業主は法人番号を持たないため、このツールでは検索できません。
    個人事業主の場合は登録番号（T+13桁）を直接指定して
    check_invoice_registration をご利用ください。

    Args:
        name: 検索キーワード（会社名、部分一致）
        area: 地域名で絞り込み（都道府県名、例: "東京都"）
        limit: 取得件数上限（デフォルト5、最大10）

    Returns:
        インボイス登録情報の一覧（マークダウン）
    """
    corp_client = _get_corp_client(ctx)
    invoice_client = _get_invoice_client(ctx)
    if corp_client is None or invoice_client is None:
        return _INVOICE_NOT_CONFIGURED

    await ctx.info(f"法人を検索中: {name}")

    # 地域絞り込み
    pref_code: str | None = None
    if area:
        try:
            code = _resolve_single_area(area)
        except AmbiguousAreaError as e:
            return str(e)
        pref_code = code[:2]

    # Step 1: 法人番号APIで会社名検索
    limit = min(limit, 10)  # インボイスAPIは最大10件同時検索
    try:
        corps = await corp_client.search_by_name(
            name, prefecture_code=pref_code, limit=limit,
        )
    except CorpApiError as e:
        return f"法人番号API エラー: {e.message}"

    if not corps:
        msg = f"「{name}」に該当する法人が見つかりませんでした。"
        if area:
            msg += f"（地域: {area}）"
        msg += (
            "\n\n※ 個人事業主のインボイス登録番号は名称検索に対応していません。"
            "\n登録番号（T+13桁）を直接指定して"
            " `check_invoice_registration` をご利用ください。"
        )
        return msg

    # Step 2: 法人番号 → 登録番号に変換してインボイスAPI検索
    invoice_numbers = [f"T{c.corporate_number}" for c in corps]
    await ctx.info(
        f"インボイス登録状況を確認中（{len(invoice_numbers)}件）"
    )

    try:
        issuers = await invoice_client.get_by_number(invoice_numbers)
    except InvoiceApiError as e:
        return f"インボイスAPI エラー: {e.message}"

    # 登録番号でルックアップ用マップ作成
    issuer_map: dict[str, InvoiceIssuer] = {
        iss.registrated_number: iss for iss in issuers
    }

    # Step 3: 結果を整形（法人情報 + インボイス登録状況）
    lines: list[str] = [
        f"## インボイス登録検索: 「{name}」（{len(corps)}件）\n"
    ]

    headers = ["法人名", "法人番号", "登録番号", "所在地", "インボイス登録"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for corp in corps:
        inv_num = f"T{corp.corporate_number}"
        iss = issuer_map.get(inv_num)
        if iss:
            status = iss.status_label
        else:
            status = "未登録"
        row = [
            corp.name,
            f"`{corp.corporate_number}`",
            f"`{inv_num}`",
            f"{corp.prefecture_name}{corp.city_name}",
            status,
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append("")
    lines.append(
        "> 詳細を確認するには `check_invoice_registration` に"
        "登録番号を指定してください。"
    )
    lines.append("")
    lines.append(
        "※ 個人事業主は法人番号を持たないため、このツールでは検索できません。"
        "個人事業主の場合は登録番号（T+13桁）を直接指定して"
        " `check_invoice_registration` をご利用ください。"
    )
    lines.append(_build_invoice_search_footer())
    return "\n".join(lines)


def _is_valid_invoice_number(number: str) -> bool:
    """登録番号の形式を検証する（T + 13桁）."""
    return (
        len(number) == 14
        and number[0] == "T"
        and number[1:].isdigit()
    )


def _format_invoice_detail(issuer: InvoiceIssuer) -> str:
    """事業者の詳細情報をマークダウンに整形する."""
    lines: list[str] = [f"## {issuer.name}\n"]
    lines.append(f"- **登録番号**: `{issuer.registrated_number}`")
    lines.append(f"- **区分**: {issuer.kind_label}")
    lines.append(f"- **登録状態**: {issuer.status_label}")
    lines.append(f"- **登録年月日**: {issuer.registration_date}")
    if issuer.display_address:
        lines.append(f"- **所在地**: {issuer.display_address}")
    if issuer.kana:
        lines.append(f"- **フリガナ**: {issuer.kana}")
    if issuer.trade_name:
        lines.append(f"- **屋号**: {issuer.trade_name}")
    if issuer.process_label:
        lines.append(f"- **処理区分**: {issuer.process_label}")
    lines.append(f"- **更新年月日**: {issuer.update_date}")

    lines.append(_build_invoice_footer(issuer))
    return "\n".join(lines)


def _format_invoice_list(issuers: list[InvoiceIssuer]) -> str:
    """事業者リストをマークダウンテーブルに整形する."""
    lines: list[str] = [
        f"## インボイス登録情報（{len(issuers)}件）\n"
    ]

    headers = ["名称", "登録番号", "区分", "所在地", "登録状態"]
    lines.append("| " + " | ".join(headers) + " |")
    lines.append("| " + " | ".join("---" for _ in headers) + " |")

    for iss in issuers:
        row = [
            iss.name,
            f"`{iss.registrated_number}`",
            iss.kind_label,
            iss.display_address or "-",
            iss.status_label,
        ]
        lines.append("| " + " | ".join(row) + " |")

    lines.append(_build_invoice_search_footer())
    return "\n".join(lines)


def _build_invoice_footer(issuer: InvoiceIssuer) -> str:
    """インボイス詳細の検証フッターを生成する."""
    now = datetime.now(_JST).strftime("%Y-%m-%d %H:%M JST")
    lines: list[str] = ["", "---", "**データ検証情報**"]
    lines.append("- 出典: 国税庁 適格請求書発行事業者公表サイト")
    lines.append(
        f"- 公表サイトで確認: {issuer.verification_url}"
    )
    lines.append(f"- 登録番号: {issuer.registrated_number}")
    lines.append(f"- データ取得日時: {now}")
    lines.append(
        "- ⚠ 本データはインボイス公表サイト Web-API"
        " から自動取得した値をそのまま表示しています。"
        "正確性の最終確認は上記リンクから原本データをご参照ください。"
    )
    return "\n".join(lines)


def _build_invoice_search_footer() -> str:
    """インボイス検索の検証フッターを生成する."""
    now = datetime.now(_JST).strftime("%Y-%m-%d %H:%M JST")
    lines: list[str] = ["", "---", "**データ検証情報**"]
    lines.append("- 出典: 国税庁 適格請求書発行事業者公表サイト")
    lines.append(
        "- 公表サイトで確認: "
        "https://www.invoice-kohyo.nta.go.jp/"
    )
    lines.append(f"- データ取得日時: {now}")
    lines.append(
        "- ⚠ 本データはインボイス公表サイト Web-API"
        " から自動取得した値をそのまま表示しています。"
        "正確性の最終確認は上記リンクから原本データをご参照ください。"
    )
    return "\n".join(lines)


# ------------------------------------------------------------------
# ユーティリティツール
# ------------------------------------------------------------------


@mcp.tool()
async def resolve_area(name: str) -> str:
    """地域名から e-Stat の地域コードを検索する.

    都道府県名の部分一致で検索可能。
    「東京」→「東京都 (13000)」のように接尾辞なしでもマッチする。

    Args:
        name: 地域名（例: "東京", "大阪府", "北海"）

    Returns:
        マッチした地域名と地域コードの一覧
    """
    matches = resolve_area_code(name)

    if not matches:
        return f"「{name}」に該当する地域が見つかりませんでした。"

    lines = [f"## 地域コード検索: 「{name}」\n"]
    for pref_name, code in matches:
        lines.append(f"- **{pref_name}**: `{code}`")
    return "\n".join(lines)


@mcp.tool()
async def list_available_stats() -> str:
    """利用可能な統計分野の一覧を表示する.

    search_statistics の stats_field パラメータに使えるコードの一覧。

    Returns:
        統計分野コードと名称の一覧
    """
    fields = list_stats_fields()

    lines = ["## 統計分野一覧\n"]
    lines.append("| コード | 分野名 |")
    lines.append("| --- | --- |")
    for f in fields:
        lines.append(f"| `{f['code']}` | {f['name']} |")

    lines.append("")
    lines.append(
        "> `search_statistics` の `stats_field` パラメータに"
        "コードを指定して検索を絞り込めます。"
    )
    return "\n".join(lines)


@mcp.tool()
async def get_meta_info(
    stats_data_id: str,
    ctx: Context,
) -> str:
    """統計表のメタ情報（分類コード体系）を取得する.

    統計表にどのような次元（地域・時間・カテゴリ等）があるか、
    各次元にどのようなコードが定義されているかを確認できる。
    データ取得前の下調べに便利。

    Args:
        stats_data_id: 統計表ID（search_statistics で取得）

    Returns:
        分類オブジェクトの一覧（各次元のコード→名称マッピング）
    """
    client = _get_client(ctx)
    await ctx.info(f"メタ情報を取得中: {stats_data_id}")

    meta = await client.get_meta_info(stats_data_id)

    lines = [f"## メタ情報: {stats_data_id}\n"]
    for co in meta.class_objects:
        lines.append(f"### {co.name}（ID: `{co.id}`）")
        # 件数が多い場合は最初の20件 + 省略表示
        display_items = co.items[:20]
        for item in display_items:
            unit_str = f"（単位: {item.unit}）" if item.unit else ""
            lines.append(f"- `{item.code}`: {item.name}{unit_str}")
        if len(co.items) > 20:
            lines.append(f"- ...他 {len(co.items) - 20} 件")
        lines.append("")

    return "\n".join(lines)


# ------------------------------------------------------------------
# プリセットツール
# ------------------------------------------------------------------


@mcp.tool()
async def get_population(
    area: str,
    ctx: Context,
) -> str:
    """地域の人口データを自動取得する（プリセット）.

    統計表IDを知らなくても、地域名を指定するだけで
    人口推計や国勢調査から人口推移データを取得できる。

    Args:
        area: 地域名（例: "東京都"）または地域コード（例: "13000"）

    Returns:
        人口推移の整形済みレポート（マークダウン）
    """
    client = _get_client(ctx)
    try:
        area_code = _resolve_single_area(area)
    except AmbiguousAreaError as e:
        return str(e)
    area_name = _get_area_display_name(area, area_code)

    await ctx.info(f"人口データを取得中: {area_name}")
    return await fetch_population(client, area_code, area_name)


@mcp.tool()
async def get_regional_profile(
    area: str,
    ctx: Context,
) -> str:
    """地域の総合プロファイルを自動取得する（プリセット）.

    人口・経済・労働など複数分野の統計データを自動検索・取得し、
    1つのレポートにまとめる。地域の概要を素早く把握したいときに便利。

    Args:
        area: 地域名（例: "東京都"）または地域コード（例: "13000"）

    Returns:
        地域の総合プロファイル（マークダウン）
    """
    client = _get_client(ctx)
    try:
        area_code = _resolve_single_area(area)
    except AmbiguousAreaError as e:
        return str(e)
    area_name = _get_area_display_name(area, area_code)

    await ctx.info(f"地域プロファイルを取得中: {area_name}")
    return await fetch_regional_profile(client, area_code, area_name)


# ------------------------------------------------------------------
# ヘルパー
# ------------------------------------------------------------------


def _resolve_single_area(area: str) -> str:
    """地域名または地域コードを地域コードに解決する.

    Raises:
        AmbiguousAreaError: 複数の地域に一致した場合
    """
    # 既にコード形式ならそのまま返す
    if area.isdigit():
        return area

    matches = resolve_area_code(area)
    if len(matches) == 1:
        return matches[0][1]
    if len(matches) > 1:
        raise AmbiguousAreaError(area, matches)

    # マッチしない場合はそのまま渡す（API側でエラーになる）
    return area


def _get_area_display_name(area: str, area_code: str) -> str:
    """表示用の地域名を返す（コード指定の場合は逆引き）."""
    if not area.isdigit():
        # 元の入力が地域名ならそのまま使う
        matches = resolve_area_code(area)
        if matches:
            return matches[0][0]
        return area

    # コード指定の場合は逆引き
    from japan_data_mcp.utils.area_codes import CODE_TO_AREA

    return CODE_TO_AREA.get(area_code, area)


# ------------------------------------------------------------------
# エントリーポイント
# ------------------------------------------------------------------


def _apply_tool_profile() -> None:
    """Expose a curated catalog to AI clients while retaining a full admin mode."""
    profile = os.environ.get("MCP_TOOL_PROFILE", "full").lower()
    if profile == "full":
        return
    if profile != "curated":
        raise ValueError("MCP_TOOL_PROFILE must be 'full' or 'curated'")
    for tool_name in list(mcp._tool_manager._tools):
        if tool_name not in _CURATED_TOOL_NAMES:
            mcp.remove_tool(tool_name)


_apply_tool_profile()


def main() -> None:
    """MCP サーバーを起動する."""
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport not in {"stdio", "sse", "streamable-http"}:
        raise ValueError(f"Unsupported MCP_TRANSPORT: {transport}")
    mcp.run(transport=transport)  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
