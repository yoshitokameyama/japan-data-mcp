# MLIT representative layer mapping

Updated: 2026-08-22

The unified read-only adapter starts with four representative MLIT layers:

| API number | Endpoint | Use |
| --- | --- | --- |
| 3 | `XPT002` | nearby official/public land-price points |
| 4 | `XKT001` | urban-planning area at the point |
| 5 | `XKT002` | zoning at the point |
| 11 | `XKT010` | nearby medical facilities |

## Input contract

| Argument | Supported value | Notes |
| --- | --- | --- |
| `lat` | valid latitude | required |
| `lon` | valid longitude | required |
| `target_apis` | one to four of 3, 4, 5, 11 | unsupported APIs fail explicitly |
| `distance` | 0–425 m | point layers only |
| `year` | supported source year | currently used by API 3 |

File-writing parameters are deliberately unsupported. The adapter is read-only.

## Output and safety

- Returns the common evidence envelope rather than file paths.
- Includes source and endpoint IDs, retrieval time, query conditions, and one
  status per requested layer.
- Caps returned GeoJSON features at 100 per layer.
- Missing API keys return `not_configured`.
- Invalid coordinates, distance, year, or API number fail before an upstream
  request.
- Partial upstream failure preserves successful layers and returns `partial`.
- API keys are sent only in request headers and are never returned in output.
