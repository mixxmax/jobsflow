"""The product-line materials pipeline (``materials-vnext-1``).

This is the only materials implementation exposed by the workflow gateway.
The retired modules remain in the repository solely for migration/rollback
inspection; they are not a second authoring path.  Scan, scoring, tracker
entry and portal adapters remain outside this package's boundary.
"""

from .engine import MaterialsEngine

__all__ = ["MaterialsEngine"]
