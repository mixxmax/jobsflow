"""Postcondition checks for destructive fresh actions."""

from __future__ import annotations

from typing import Any, Protocol


class FreshLike(Protocol):
    def row_count(self) -> int: ...
    def digest(self) -> str: ...
    def archive_exists(self, dest: str) -> bool: ...
    def archive_digest(self, dest: str) -> str | None: ...


def unconfirmed_archive_postcondition(
    *,
    before_row_count: int,
    after_row_count: int,
    before_digest: str,
    after_digest: str,
    archive_event_count: int,
) -> list[str]:
    failures = []
    if before_row_count != after_row_count:
        failures.append("fresh_row_count_changed")
    if before_digest != after_digest:
        failures.append("fresh_digest_changed")
    if archive_event_count != 0:
        failures.append("unexpected_archive_event")
    return failures


def confirmed_archive_postcondition(
    store: FreshLike,
    *,
    target: str,
    preview_digest: str,
) -> list[str]:
    failures = []
    if not store.archive_exists(target):
        failures.append("archive_copy_missing")
    elif store.archive_digest(target) != preview_digest:
        failures.append("archive_copy_digest_mismatch")
    if store.row_count() != 0:
        failures.append("fresh_not_header_only")
    return failures


def as_report(failures: list[str]) -> dict[str, Any]:
    return {"ok": not failures, "failures": failures}
