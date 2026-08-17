"""Deterministic Google Sheets tracker presentation contracts.

The tracker is a projection, but its user-facing status controls are part of
the product contract.  Keep this module independent from the legacy writer so
the unified workflow can initialize and maintain the same format without
opening a second push path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MATERIAL_STATUS_COLUMN = "材料状态"
MATERIAL_STATUS_OPTIONS = ("未做", "已定制", "已投递", "面试中", "已结束", "已录用")
TRACKER_FORMAT_VERSION = 1
MATERIAL_STATUS_GREEN = {"red": 0.20, "green": 0.65, "blue": 0.20}


def _column_letter(index: int) -> str:
    """Convert a zero-based column index to its Sheets letter."""
    result = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


def _sheet_metadata(metadata: Mapping[str, Any] | None, sheet_id: int) -> Mapping[str, Any] | None:
    if not isinstance(metadata, Mapping):
        return None
    for sheet in metadata.get("sheets") or []:
        if not isinstance(sheet, Mapping):
            continue
        properties = sheet.get("properties") or {}
        if str(properties.get("sheetId")) == str(sheet_id):
            return sheet
    return None


def _has_green_status_rule(sheet: Mapping[str, Any] | None, formula: str) -> bool:
    """Avoid accumulating duplicate conditional-format rules on each push."""
    if not isinstance(sheet, Mapping):
        return False
    for rule in sheet.get("conditionalFormats") or []:
        if not isinstance(rule, Mapping):
            continue
        boolean_rule = rule.get("booleanRule") or {}
        condition = boolean_rule.get("condition") or {}
        if condition.get("type") != "CUSTOM_FORMULA":
            continue
        values = condition.get("values") or []
        if any(str(item.get("userEnteredValue") or "") == formula for item in values if isinstance(item, Mapping)):
            return True
    return False


def build_material_status_format_requests(
    *,
    sheet_id: int,
    headers: list[str],
    total_rows: int = 0,
    min_rows: int = 100,
    metadata: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Build idempotent Sheets API requests for the V-column status contract.

    Data validation starts at row 2 and extends beyond the current rows so a
    later append inherits the dropdown.  The green rule is row-wide and uses
    a relative row reference, so selecting ``已投递`` in column V colors the
    whole row without any model or script choosing a color.
    """
    if MATERIAL_STATUS_COLUMN not in headers:
        return []
    status_column = headers.index(MATERIAL_STATUS_COLUMN)
    status_letter = _column_letter(status_column)
    end_row = max(int(total_rows) + 1, int(min_rows))
    formula = f'=${status_letter}2="已投递"'
    sheet = _sheet_metadata(metadata, sheet_id)
    requests: list[dict[str, Any]] = [
        {
            "setDataValidation": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 1,
                    "endRowIndex": end_row,
                    "startColumnIndex": status_column,
                    "endColumnIndex": status_column + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [
                            {"userEnteredValue": value}
                            for value in MATERIAL_STATUS_OPTIONS
                        ],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }
    ]
    if not _has_green_status_rule(sheet, formula):
        requests.append(
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "endRowIndex": end_row,
                                "startColumnIndex": 0,
                                "endColumnIndex": len(headers),
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": formula}],
                            },
                            "format": {"backgroundColor": MATERIAL_STATUS_GREEN},
                        },
                    },
                    "index": 0,
                }
            }
        )
    return requests


def apply_material_status_formats(
    spreadsheet: Any,
    worksheet: Any,
    headers: list[str],
    *,
    total_rows: int = 0,
    min_rows: int = 100,
) -> dict[str, Any]:
    """Apply the status contract, tolerating metadata endpoints unavailable in fixtures."""
    sheet_id = getattr(worksheet, "id", None)
    if spreadsheet is None or sheet_id is None or not hasattr(spreadsheet, "batch_update"):
        return {"applied": False, "request_count": 0, "version": TRACKER_FORMAT_VERSION}
    metadata = None
    fetch_metadata = getattr(spreadsheet, "fetch_sheet_metadata", None)
    if callable(fetch_metadata):
        try:
            metadata = fetch_metadata({"includeGridData": False})
        except Exception:
            # Formatting remains best-effort when a restricted fixture or a
            # transient metadata read cannot be completed.  The write itself
            # still receives the full contract below.
            metadata = None
    requests = build_material_status_format_requests(
        sheet_id=int(sheet_id),
        headers=list(headers),
        total_rows=total_rows,
        min_rows=min_rows,
        metadata=metadata,
    )
    if requests:
        spreadsheet.batch_update({"requests": requests})
    return {
        "applied": bool(requests),
        "request_count": len(requests),
        "version": TRACKER_FORMAT_VERSION,
        "column": MATERIAL_STATUS_COLUMN,
        "column_letter": _column_letter(headers.index(MATERIAL_STATUS_COLUMN))
        if MATERIAL_STATUS_COLUMN in headers
        else "",
    }


__all__ = [
    "MATERIAL_STATUS_COLUMN",
    "MATERIAL_STATUS_GREEN",
    "MATERIAL_STATUS_OPTIONS",
    "TRACKER_FORMAT_VERSION",
    "apply_material_status_formats",
    "build_material_status_format_requests",
]
