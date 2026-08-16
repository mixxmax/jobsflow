"""Schema loading and validation for Quality Control data contracts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"


class SchemaValidationError(Exception):
    """Raised when data fails schema validation."""
    def __init__(self, message: str, errors: Optional[List[str]] = None):
        super().__init__(message)
        self.errors = errors or [message]


def load_schema(schema_name: str) -> Dict[str, Any]:
    """Load JSON schema by name."""
    if not schema_name.endswith(".json"):
        schema_name = f"{schema_name}.json"
    schema_path = SCHEMAS_DIR / schema_name
    if not schema_path.is_file():
        raise FileNotFoundError(f"Schema file not found: {schema_path}")
    with open(schema_path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_schema(data: Any, schema_name: str) -> Tuple[bool, List[str]]:
    """Validate data dictionary against a named schema.

    Returns (is_valid, list_of_error_strings).
    Fail-closed: Any deviation is reported as an error.
    """
    try:
        schema = load_schema(schema_name)
    except Exception as e:
        return False, [f"Failed to load schema {schema_name}: {str(e)}"]

    errors: List[str] = []
    _validate_value(data, schema, path="#", errors=errors)
    return len(errors) == 0, errors


def assert_valid_schema(data: Any, schema_name: str) -> None:
    """Validate and raise SchemaValidationError on failure."""
    is_valid, errors = validate_schema(data, schema_name)
    if not is_valid:
        raise SchemaValidationError(
            f"Validation failed for schema '{schema_name}': {'; '.join(errors)}",
            errors=errors,
        )


def _validate_value(value: Any, schema: Dict[str, Any], path: str, errors: List[str]) -> None:
    expected_type = schema.get("type")

    if expected_type:
        if expected_type == "object":
            if not isinstance(value, dict):
                errors.append(f"{path}: expected object, got {type(value).__name__}")
                return
            required_fields = schema.get("required", [])
            for field in required_fields:
                if field not in value or value[field] is None:
                    errors.append(f"{path}.{field}: missing required property '{field}'")

            properties = schema.get("properties", {})
            if schema.get("additionalProperties") is False:
                for prop_name in value:
                    if prop_name not in properties:
                        errors.append(f"{path}.{prop_name}: additional property is not allowed")
            for prop_name, prop_val in value.items():
                if prop_name in properties:
                    _validate_value(prop_val, properties[prop_name], f"{path}.{prop_name}", errors)

        elif expected_type == "array":
            if not isinstance(value, (list, tuple)):
                errors.append(f"{path}: expected array, got {type(value).__name__}")
                return
            item_schema = schema.get("items")
            if item_schema and isinstance(item_schema, dict):
                for idx, item in enumerate(value):
                    _validate_value(item, item_schema, f"{path}[{idx}]", errors)
            if "minItems" in schema and len(value) < int(schema["minItems"]):
                errors.append(f"{path}: expected at least {schema['minItems']} items")
            if "maxItems" in schema and len(value) > int(schema["maxItems"]):
                errors.append(f"{path}: expected at most {schema['maxItems']} items")

        elif expected_type == "string":
            if not isinstance(value, str):
                errors.append(f"{path}: expected string, got {type(value).__name__}")
                return
            enum_values = schema.get("enum")
            if enum_values and value not in enum_values:
                errors.append(f"{path}: value '{value}' not in allowed enum {enum_values}")
            if "minLength" in schema and len(value) < int(schema["minLength"]):
                errors.append(f"{path}: string shorter than minLength {schema['minLength']}")
            if "maxLength" in schema and len(value) > int(schema["maxLength"]):
                errors.append(f"{path}: string longer than maxLength {schema['maxLength']}")
            if schema.get("pattern"):
                try:
                    if re.search(str(schema["pattern"]), value) is None:
                        errors.append(f"{path}: value does not match pattern")
                except re.error:
                    errors.append(f"{path}: invalid schema pattern")

        elif expected_type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{path}: expected number, got {type(value).__name__}")
                return
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: value {value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: value {value} > maximum {schema['maximum']}")

        elif expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{path}: expected integer, got {type(value).__name__}")
                return
            if "minimum" in schema and value < schema["minimum"]:
                errors.append(f"{path}: value {value} < minimum {schema['minimum']}")
            if "maximum" in schema and value > schema["maximum"]:
                errors.append(f"{path}: value {value} > maximum {schema['maximum']}")

        elif expected_type == "boolean":
            if not isinstance(value, bool):
                errors.append(f"{path}: expected boolean, got {type(value).__name__}")
                return
