from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from .activity_analysis import validate_ai_language_code


MAX_FORCE_VECTOR_NODES = 12
FORCE_VECTOR_FOCUSES = ("comprehensive", "stability", "peak_power")
FORCE_VECTOR_DETAILS = ("brief", "normal", "detailed")


@dataclass(frozen=True, slots=True)
class ForceVectorSnapshot:
    left_foot_nodes: tuple[float, ...] = ()
    right_foot_nodes: tuple[float, ...] = ()
    left_torque_effectiveness_percent: float | None = None
    right_torque_effectiveness_percent: float | None = None
    left_pedal_smoothness_percent: float | None = None
    right_pedal_smoothness_percent: float | None = None

    @property
    def has_data(self) -> bool:
        return bool(
            self.left_foot_nodes
            or self.right_foot_nodes
            or self.left_torque_effectiveness_percent is not None
            or self.right_torque_effectiveness_percent is not None
            or self.left_pedal_smoothness_percent is not None
            or self.right_pedal_smoothness_percent is not None
        )


def load_force_vector_snapshot(path: Path) -> ForceVectorSnapshot:
    input_path = path.expanduser().resolve()
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("Force-vector JSON must use UTF-8 encoding") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid force-vector JSON: {input_path}") from exc
    except OSError as exc:
        raise ValueError(f"Cannot read force-vector JSON: {input_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Force-vector JSON must contain an object")
    allowed_fields = {
        "left_foot_nodes",
        "right_foot_nodes",
        "left_torque_effectiveness_percent",
        "right_torque_effectiveness_percent",
        "left_pedal_smoothness_percent",
        "right_pedal_smoothness_percent",
    }
    unexpected_fields = sorted(set(payload) - allowed_fields)
    if unexpected_fields:
        raise ValueError(f"Unsupported force-vector fields: {', '.join(unexpected_fields)}")

    return ForceVectorSnapshot(
        left_foot_nodes=_node_values(payload.get("left_foot_nodes"), "left_foot_nodes"),
        right_foot_nodes=_node_values(payload.get("right_foot_nodes"), "right_foot_nodes"),
        left_torque_effectiveness_percent=_optional_percent(
            payload.get("left_torque_effectiveness_percent"),
            "left_torque_effectiveness_percent",
        ),
        right_torque_effectiveness_percent=_optional_percent(
            payload.get("right_torque_effectiveness_percent"),
            "right_torque_effectiveness_percent",
        ),
        left_pedal_smoothness_percent=_optional_percent(
            payload.get("left_pedal_smoothness_percent"),
            "left_pedal_smoothness_percent",
        ),
        right_pedal_smoothness_percent=_optional_percent(
            payload.get("right_pedal_smoothness_percent"),
            "right_pedal_smoothness_percent",
        ),
    )


def build_force_vector_analysis_prompt(
    snapshot: ForceVectorSnapshot,
    *,
    language: str = "zh-CN",
    detail: str = "normal",
    focus: str = "comprehensive",
    question: str | None = None,
) -> str:
    language = validate_ai_language_code(language)
    if detail not in FORCE_VECTOR_DETAILS:
        raise ValueError(f"Unsupported force-vector analysis detail: {detail}")
    if focus not in FORCE_VECTOR_FOCUSES:
        raise ValueError(f"Unsupported force-vector analysis focus: {focus}")

    lines = ["你是一位顶级的职业骑行教练，精通功率计数据分析和生物力学踩踏技术。"]
    if snapshot.has_data:
        lines.append("请基于下方的【骑行功率矢量 (Power Vector)】快照数据，为运动员提供客观、专业的分析。")
    else:
        lines.append(
            "当前尚未获取到该运动员的具体实时数据。请先提供自行车踩踏力学的通用技术指导，"
            "并解释功率矢量图（极坐标系）对提升骑行效率的重要性。"
        )

    lines.extend(
        [
            "",
            "### 安全准则 (CRITICAL):",
            f'1. Respond in Language Code: "{language}".',
            "2. 只根据提供的快照数据分析，不推断缺失数据；数据不足时明确说明。",
            "3. 这是运动技术分析，不提供医疗建议或医学诊断。",
            "",
            "### [Force Vector Snapshot]",
        ]
    )

    metrics = [
        ("TE (Left)", snapshot.left_torque_effectiveness_percent),
        ("TE (Right)", snapshot.right_torque_effectiveness_percent),
        ("PS (Left)", snapshot.left_pedal_smoothness_percent),
        ("PS (Right)", snapshot.right_pedal_smoothness_percent),
    ]
    present_metrics = [
        f"{name}: {_format_number(value)}%"
        for name, value in metrics
        if value is not None
    ]
    if present_metrics:
        lines.append("- Metrics: " + ", ".join(present_metrics))
    if snapshot.left_foot_nodes:
        lines.append(f"- Left Foot Nodes (30° intervals): {_format_nodes(snapshot.left_foot_nodes)}")
    if snapshot.right_foot_nodes:
        lines.append(f"- Right Foot Nodes (30° intervals): {_format_nodes(snapshot.right_foot_nodes)}")
    if not present_metrics and not snapshot.left_foot_nodes and not snapshot.right_foot_nodes:
        lines.append("- No force-vector measurements were provided.")

    lines.extend(["", "### 分析指令:"])
    if detail == "brief":
        lines.append("- 请用简短有力的几句话指出最核心的一两个技术观察。")
    else:
        lines.extend(
            [
                "- 请分析力矩分布的形状特征（如圆形、花生型、梨形）。",
                "- 针对具体角度（钟点位置）给出动作修正建议。",
            ]
        )

    if focus == "stability":
        lines.append("- 侧重于踩踏稳定性，分析力量波动或不必要的左右代偿。")
    elif focus == "peak_power":
        lines.append("- 侧重于峰值功率转换，分析下压阶段（30°–120°）的发力表现。")
    else:
        lines.append("- 侧重于技术全面性，兼顾发力效率和提拉阶段的表现。")
    if question and question.strip():
        lines.extend(["", "### 用户补充问题:", question.strip()])
    return "\n".join(lines)


def _node_values(value: object, label: str) -> tuple[float, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a JSON array")
    if len(value) > MAX_FORCE_VECTOR_NODES:
        raise ValueError(
            f"{label} cannot contain more than {MAX_FORCE_VECTOR_NODES} 30-degree nodes"
        )
    return tuple(_finite_number(item, f"{label} value") for item in value)


def _optional_percent(value: object, label: str) -> float | None:
    if value is None:
        return None
    number = _finite_number(value, label)
    if not 0.0 <= number <= 100.0:
        raise ValueError(f"{label} must be between 0 and 100")
    return number


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _format_number(value: float) -> str:
    return f"{value:.1f}"


def _format_nodes(values: tuple[float, ...]) -> str:
    return "[" + ", ".join(_format_number(value) for value in values) + "]"
