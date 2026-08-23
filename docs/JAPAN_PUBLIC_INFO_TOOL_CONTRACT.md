# 日本公共情報MCP tool contract（Phase 1 implemented baseline）

更新日: 2026-08-22

## 目的

AIクライアントへ大量の重複toolをそのまま公開せず、用途が明確な高水準toolと、
互換性維持のための低水準toolを分ける。一次情報の種類、データ時点、限界を
機械可読な形で必ず返す。

## 公開レベル

- **default**: 一般的なAI質問で自動選択してよいtool。
- **advanced**: 詳細な統計表IDや国交省API番号を扱う利用者向けtool。
- **disabled**: 契約・ETL・個人情報管理が整うまで選択させないtool。
- **legacy**: 移行期間中だけ互換性のために残すtool。

通常のAI接続では `MCP_TOOL_PROFILE=curated` を使用し、default toolと、
それらが必要とする統計検索の補助toolに、公式データカタログ検索を加えた13 toolsを公開する。
`full`は移行・管理用とし、実装を削除せず公開面だけを切り替える。

## Phase 1 target tools

| Tool | Level | Primary source | Notes |
| --- | --- | --- | --- |
| `get_population` | default | e-Stat | 地域名から人口を取得 |
| `get_regional_profile` | default | e-Stat | 人口・経済・労働の要約 |
| `search_statistics` | advanced | e-Stat | 統計表検索 |
| `get_regional_data` | advanced | e-Stat | 統計表ID指定 |
| `compare_regions` | advanced | e-Stat | 複数地域比較 |
| `search_corporations` | default | 国税庁法人番号 | 法人名検索 |
| `get_corporation` | advanced | 国税庁法人番号 | 法人番号指定 |
| `search_invoice_by_name` | default | 法人番号・インボイス | 会社名から確認する標準経路 |
| `search_government_open_data` | default | e-Govデータポータル | 政府機関の公式公開データをメタデータ検索 |
| `check_invoice_registration` | advanced | インボイス | 登録番号が既知の場合 |
| `validate_invoice_on_date` | advanced | インボイス | 指定日時点の有効性 |
| `research_real_estate_area` | default | 国交省＋optional feeds | 土地調査の統合入口 |
| `search_real_estate_transactions` | advanced | 不動産情報ライブラリ | 過去成約のみ。売出ではない |
| `get_mlit_geospatial_layers` | advanced | 不動産情報ライブラリ | 既存約30 APIの低水準互換入口 |
| `search_plateau_datasets` | advanced | PLATEAU catalog | データセットの発見のみ |
| `get_connector_status` | advanced | service configuration | 利用可能・未接続の監査 |
| `search_current_listings` | internal | Project LINKS / 利用許諾済み売出feed | 公式スナップショットを既定値とし、契約feed設定時はそちらを優先 |
| `find_vacant_land_candidates` | disabled | parcel/building/land-use ETL | 高水準tool内の内部adapter。候補であり売出ではない |

## Existing implementation mapping

| Existing implementation | Current tool | Target |
| --- | --- | --- |
| `japan-data-mcp` | `get_real_estate_transactions` | `search_real_estate_transactions`へ名称・出力を整理。移行中はaliasを検討 |
| `mlit-geospatial-mcp-poc` | `get_multi_api` | `get_mlit_geospatial_layers`としてadapter化。入力互換testを用意 |
| Node MVP | `research_real_estate_area` | Pythonへ移植 |
| Node MVP | `search_current_listings` | 内部のdisabled adapterとして移植済み |
| Node MVP | `find_vacant_land_candidates` | 内部のdisabled adapterとして移植済み |
| Node MVP | `search_plateau_datasets` | Pythonへ移植済み |
| Node MVP | `get_connector_status` | Pythonへ移植済み |

MLITの代表レイヤーは [MLIT_LAYER_MAPPING.md](MLIT_LAYER_MAPPING.md) を参照。

## Evidence envelope

高水準toolは次のJSON objectを返す。tool固有データは`data`に入れる。

```json
{
  "status": "ok | partial | not_configured | no_results | upstream_error",
  "data_as_of": "source data date or null",
  "retrieved_at": "ISO-8601 timestamp",
  "precision": "dataset | area | transaction | listing | candidate | registry",
  "confidence": "high | medium | low | not_applicable",
  "sources": [
    {
      "name": "official source name",
      "url": "https://official.example/",
      "document_id": "optional",
      "query": "non-secret query summary"
    }
  ],
  "limitations": ["human-readable limitation"],
  "data": {}
}
```

## Required semantics

1. `data_as_of`と`retrieved_at`を混同しない。
2. `sources[].url`は可能な限り公式原文または公式datasetへ直接リンクする。
3. 0件は成功と失敗を区別し、`no_results`を使う。
4. API keyやBearer tokenを`query`、error、logへ含めない。
5. 一部sourceだけ成功した場合は`partial`とし、成功分と失敗分を分ける。
6. 現在の売出を過去成約から推測しない。
7. 空き地候補から所有者・売却意思・建築可能性を推測しない。
8. 所有者情報はPhase 1のtool contractに含めない。

## `research_real_estate_area` output sections

- `transactions`: 過去成約。件数、中央値、単価、対象面積の参考値。
- `current_listings`: 正規feed接続時のみ。未接続なら`not_configured`。
- `vacant_candidates`: 空間ETL接続時のみ。未接続なら`not_configured`。
- `unknowns`: 現在確認できないことを明示する。
- `next_actions`: 仲介確認、現地確認、用途地域・接道・権利確認等。

## 実装状況（2026-08-22）

- 共通証拠エンベロープを `public_info/evidence.py` に実装。
- `research_real_estate_area` と出典付き `search_real_estate_transactions` を実装。
- 売出フィードと空間ETLは、環境変数がない限り内部adapterが
  `not_configured` を返す。公開toolとしては登録しない。
- PLATEAUカタログ検索とconnector状態確認を実装。
- 既存テストを含む全テストを継続して実行する。
- `get_mlit_geospatial_layers`でAPI 3/4/5/11を段階移植し、旧toolとの
  代表入力互換・上流失敗・Secret非露出をテストで固定。

## Compatibility gate

- [ ] 既存`get_real_estate_transactions`の代表queryを新toolでも再現できる。
- [x] 既存`get_multi_api`の代表API番号について、入力と主要fieldを対応表にする。
- [ ] aliasを残す場合は廃止予定をresponseまたはdocumentationに明記する。
- [x] AIクライアントのdefault tool一覧は15個前後を上限の目安とする（curated 13 tools）。
- [ ] disabled toolは未設定でも起動を妨げない。
