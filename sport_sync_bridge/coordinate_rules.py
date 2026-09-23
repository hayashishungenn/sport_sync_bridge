from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any


COORDINATE_MODES = {"none", "gcj02_to_wgs84"}


@dataclass(frozen=True, slots=True)
class CoordinateRule:
    manufacturer_id: int
    product_id: int | None
    firmware_min: float | None
    firmware_max: float | None
    coordinate_mode: str

    def matches(
        self,
        manufacturer_id: int | None,
        product_id: int | None,
        firmware_version: float | None,
    ) -> bool:
        if manufacturer_id != self.manufacturer_id:
            return False
        if self.product_id is not None and product_id != self.product_id:
            return False
        if firmware_version is None:
            return self.firmware_min is None and self.firmware_max is None
        if self.firmware_min is not None and firmware_version < self.firmware_min:
            return False
        if self.firmware_max is not None and firmware_version > self.firmware_max:
            return False
        return True


def load_coordinate_rules(path: Path) -> tuple[CoordinateRule, ...]:
    if not path.exists():
        return ()

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read coordinate rules from {path}: {exc}") from exc

    if not isinstance(payload, list):
        raise ValueError(f"Coordinate rules in {path} must be a JSON array")

    rules = tuple(_parse_rule(item, index, path) for index, item in enumerate(payload))
    _validate_no_overlaps(rules, path)
    return rules


def _parse_rule(value: Any, index: int, path: Path) -> CoordinateRule:
    label = f"Coordinate rule #{index + 1} in {path}"
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")

    allowed = {
        "manufacturer_id",
        "product_id",
        "firmware_min",
        "firmware_max",
        "coordinate_mode",
    }
    unexpected = set(value) - allowed
    missing = {"manufacturer_id", "coordinate_mode"} - set(value)
    if unexpected:
        raise ValueError(f"{label} has unsupported keys: {', '.join(sorted(unexpected))}")
    if missing:
        raise ValueError(f"{label} is missing keys: {', '.join(sorted(missing))}")

    manufacturer_id = _parse_id(value["manufacturer_id"], f"{label}.manufacturer_id")
    product_id = value.get("product_id")
    if product_id is not None:
        product_id = _parse_id(product_id, f"{label}.product_id")

    firmware_min = _parse_firmware(value.get("firmware_min"), f"{label}.firmware_min")
    firmware_max = _parse_firmware(value.get("firmware_max"), f"{label}.firmware_max")
    if firmware_min is not None and firmware_max is not None and firmware_min > firmware_max:
        raise ValueError(f"{label}.firmware_min must not exceed firmware_max")

    coordinate_mode = value["coordinate_mode"]
    if not isinstance(coordinate_mode, str) or coordinate_mode not in COORDINATE_MODES:
        supported = ", ".join(sorted(COORDINATE_MODES))
        raise ValueError(f"{label}.coordinate_mode must be one of: {supported}")

    return CoordinateRule(
        manufacturer_id=manufacturer_id,
        product_id=product_id,
        firmware_min=firmware_min,
        firmware_max=firmware_max,
        coordinate_mode=coordinate_mode,
    )


def _parse_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _parse_firmware(value: Any, label: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number or null")
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise ValueError(f"{label} must be a finite non-negative number")
    return parsed


def _validate_no_overlaps(rules: tuple[CoordinateRule, ...], path: Path) -> None:
    for index, rule in enumerate(rules):
        for other in rules[index + 1 :]:
            manufacturer_matches = rule.manufacturer_id == other.manufacturer_id
            product_matches = (
                rule.product_id is None
                or other.product_id is None
                or rule.product_id == other.product_id
            )
            if not manufacturer_matches or not product_matches:
                continue

            minimum = max(
                rule.firmware_min if rule.firmware_min is not None else -math.inf,
                other.firmware_min if other.firmware_min is not None else -math.inf,
            )
            maximum = min(
                rule.firmware_max if rule.firmware_max is not None else math.inf,
                other.firmware_max if other.firmware_max is not None else math.inf,
            )
            if minimum <= maximum:
                raise ValueError(
                    f"Overlapping coordinate rules in {path} for manufacturer "
                    f"{rule.manufacturer_id} and product "
                    f"{_overlap_product_label(rule.product_id, other.product_id)}"
                )


def _overlap_product_label(first: int | None, second: int | None) -> str:
    if first is None or second is None:
        return "*"
    return str(first)
