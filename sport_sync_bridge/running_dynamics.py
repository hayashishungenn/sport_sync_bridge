from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunningDynamicsSummary:
    cadence: float
    stride_length_m: float
    total_steps: int
    vertical_oscillation_cm: float
    vertical_ratio_percent: float
    accelerometer_samples: int
    gps_segments: int


@dataclass(frozen=True, slots=True)
class _BiquadCoefficients:
    b0: float
    b1: float
    b2: float
    a1: float
    a2: float


class _BiquadFilter:
    def __init__(self, coefficients: _BiquadCoefficients) -> None:
        self.coefficients = coefficients
        self._state_1 = 0.0
        self._state_2 = 0.0

    @classmethod
    def low_pass(cls, cutoff_hz: float, sample_rate_hz: float = 50.0) -> _BiquadFilter:
        omega = cutoff_hz * 2 * math.pi / sample_rate_hz
        sine = math.sin(omega)
        cosine = math.cos(omega)
        alpha = sine / 1.414
        divisor = 1 + alpha
        b0 = (1 - cosine) / 2 / divisor
        coefficients = _BiquadCoefficients(
            b0=b0,
            b1=(1 - cosine) / divisor,
            b2=b0,
            a1=-2 * cosine / divisor,
            a2=(1 - alpha) / divisor,
        )
        return cls(coefficients)

    @classmethod
    def high_pass_1hz(cls) -> _BiquadFilter:
        omega = 0.06283185307179587
        sine = math.sin(omega)
        cosine = math.cos(omega)
        alpha = sine / 1.414
        divisor = 1 + alpha
        b0 = (1 + cosine) / 2 / divisor
        coefficients = _BiquadCoefficients(
            b0=b0,
            b1=-(1 + cosine) / divisor,
            b2=b0,
            a1=-2 * cosine / divisor,
            a2=(1 - alpha) / divisor,
        )
        return cls(coefficients)

    def process(self, value: float) -> float:
        coefficients = self.coefficients
        output = coefficients.b0 * value + self._state_1
        self._state_1 = (
            coefficients.b1 * value - coefficients.a1 * output + self._state_2
        )
        self._state_2 = coefficients.b2 * value - coefficients.a2 * output
        return output


class _SlidingWindow:
    def __init__(self, capacity: int) -> None:
        self.capacity = capacity
        self.values: deque[float] = deque()

    def add(self, value: float) -> None:
        self.values.append(value)
        if len(self.values) > self.capacity:
            self.values.popleft()

    @property
    def is_full(self) -> bool:
        return len(self.values) >= self.capacity

    @property
    def mean(self) -> float:
        return sum(self.values) / len(self.values) if self.values else 0.0

    @property
    def standard_deviation(self) -> float:
        if not self.values:
            return 0.0
        mean = self.mean
        return math.sqrt(sum((value - mean) ** 2 for value in self.values) / len(self.values))


class _StepDetector:
    def __init__(self) -> None:
        self._high_pass = _BiquadFilter.high_pass_1hz()
        self._low_pass = _BiquadFilter.low_pass(3.5)
        self._window = _SlidingWindow(50)
        self._above_threshold = False
        self._last_step_time_ms = 0
        self.total_steps = 0

    def process_sample(self, timestamp_ms: int, x: float, y: float, z: float) -> bool:
        magnitude = math.sqrt(x * x + y * y + z * z)
        filtered = self._low_pass.process(self._high_pass.process(magnitude))
        self._window.add(filtered)
        threshold = max(0.8, self._window.mean + 0.6 * self._window.standard_deviation)

        if filtered > threshold:
            if not self._above_threshold and timestamp_ms - self._last_step_time_ms > 230:
                self.total_steps += 1
                self._last_step_time_ms = timestamp_ms
                self._above_threshold = True
                return True
        elif filtered <= threshold * 0.7:
            self._above_threshold = False
        return False


class _PhonePositionDetector:
    def __init__(self) -> None:
        self._filters = [_BiquadFilter.low_pass(1.0) for _ in range(3)]
        self._windows = [_SlidingWindow(100) for _ in range(4)]
        self._active_position = 3
        self._candidate_position = 3
        self._candidate_count = 0

    def process_sample(self, x: float, y: float, z: float) -> bool:
        values = (x, y, z)
        filtered = tuple(
            filter_.process(value) for filter_, value in zip(self._filters, values)
        )
        for window, value in zip(self._windows[:3], filtered):
            window.add(value)
        raw_magnitude = math.sqrt(sum(value * value for value in values))
        filtered_magnitude = math.sqrt(sum(value * value for value in filtered))
        self._windows[3].add(abs(raw_magnitude - filtered_magnitude))

        if self._windows[3].is_full:
            self._detect()
        return self._active_position == 0

    def _detect(self) -> None:
        deviation_sum = sum(window.standard_deviation for window in self._windows[:3])
        magnitude_delta_mean = self._windows[3].mean
        if deviation_sum > 1.5:
            candidate = 0
        elif magnitude_delta_mean > 4.5:
            candidate = 1
        elif magnitude_delta_mean > 1.5:
            candidate = 2
        else:
            candidate = 3

        if candidate != self._candidate_position:
            self._candidate_position = candidate
            self._candidate_count = 0
            return
        self._candidate_count += 1
        if self._candidate_count > 50:
            self._active_position = candidate


class _StrideCalibrator:
    def __init__(self, height_cm: float) -> None:
        self.stride_length_m = height_cm * 0.413 / 100
        self._distance_m = 0.0
        self._step_count = 0

    def add_gps_segment(
        self,
        distance_m: float,
        step_delta: int,
        horizontal_accuracy_m: float,
        speed_mps: float,
    ) -> None:
        if horizontal_accuracy_m > 10 or speed_mps > 15:
            return
        self._distance_m += distance_m
        self._step_count += step_delta
        if self._distance_m <= 50 or self._step_count <= 20:
            return

        measured_stride = self._distance_m / self._step_count
        if 0.4 < measured_stride < 2.5:
            self.stride_length_m = self.stride_length_m * 0.8 + measured_stride * 0.2
        self._distance_m = 0.0
        self._step_count = 0


class _VerticalOscillationIntegrator:
    def __init__(self) -> None:
        self._filter = _BiquadFilter.high_pass_1hz()
        self._last_timestamp_ms: int | None = None
        self._velocity = 0.0
        self._position = 0.0
        self._maximum_position = -math.inf
        self._minimum_position = math.inf
        self.vertical_oscillation_cm = 0.0

    def process(self, timestamp_ms: int, acceleration: float, stepped: bool) -> float:
        if self._last_timestamp_ms is None:
            self._last_timestamp_ms = timestamp_ms
            return self.vertical_oscillation_cm

        interval_s = (timestamp_ms - self._last_timestamp_ms) / 1000
        self._last_timestamp_ms = timestamp_ms
        if interval_s <= 0 or interval_s > 0.1:
            return self.vertical_oscillation_cm

        self._velocity += acceleration * interval_s
        filtered = self._filter.process(acceleration)
        self._velocity = filtered
        self._position += filtered * interval_s
        self._maximum_position = max(self._maximum_position, self._position)
        self._minimum_position = min(self._minimum_position, self._position)
        if stepped:
            return self.vertical_oscillation_cm

        amplitude_cm = (self._maximum_position - self._minimum_position) * 100
        if 2 < amplitude_cm < 25:
            self.vertical_oscillation_cm = amplitude_cm
        self._position = 0.0
        self._maximum_position = -math.inf
        self._minimum_position = math.inf
        return self.vertical_oscillation_cm


class RunningDynamicsAnalyzer:
    def __init__(self, height_cm: float = 175.0) -> None:
        self._height_cm = _positive_finite(height_cm, "Height")
        self._phone_position = _PhonePositionDetector()
        self._step_detector = _StepDetector()
        self._stride_calibrator = _StrideCalibrator(self._height_cm)
        self._vo_integrator = _VerticalOscillationIntegrator()
        self._step_timestamps_ms: list[int] = []
        self._last_sample_timestamp_ms: int | None = None
        self._cadence = 0.0
        self._accelerometer_samples = 0
        self._gps_segments = 0

    def add_accelerometer_sample(
        self,
        timestamp_ms: int,
        x_mps2: float,
        y_mps2: float,
        z_mps2: float,
    ) -> None:
        timestamp = _timestamp_ms(timestamp_ms)
        if self._last_sample_timestamp_ms is not None and timestamp < self._last_sample_timestamp_ms:
            raise ValueError("Event timestamps must be in nondecreasing order")
        x = _finite_number(x_mps2, "x_mps2")
        y = _finite_number(y_mps2, "y_mps2")
        z = _finite_number(z_mps2, "z_mps2")

        self._last_sample_timestamp_ms = timestamp
        self._accelerometer_samples += 1
        uses_magnitude = self._phone_position.process_sample(x, y, z)
        self._check_cadence_timeout(timestamp)
        stepped = self._step_detector.process_sample(timestamp, x, y, z)

        if uses_magnitude:
            vertical_acceleration = math.sqrt(x * x + y * y + z * z) - 9.8
        else:
            absolute_axes = (abs(x), abs(y), abs(z))
            if absolute_axes[0] > absolute_axes[1] and absolute_axes[0] > absolute_axes[2]:
                vertical_acceleration = absolute_axes[0] - 9.8
            elif absolute_axes[1] > absolute_axes[0] and absolute_axes[1] > absolute_axes[2]:
                vertical_acceleration = absolute_axes[1] - 9.8
            else:
                vertical_acceleration = absolute_axes[2] - 9.8

        self._vo_integrator.process(timestamp, vertical_acceleration, stepped)
        if stepped:
            self._on_step(timestamp, self._step_detector.total_steps)

    def add_gps_segment(
        self,
        distance_m: float,
        step_delta: int,
        horizontal_accuracy_m: float,
        speed_mps: float,
    ) -> None:
        distance = _nonnegative_finite(distance_m, "distance_m")
        accuracy = _nonnegative_finite(horizontal_accuracy_m, "horizontal_accuracy_m")
        speed = _nonnegative_finite(speed_mps, "speed_mps")
        if isinstance(step_delta, bool) or not isinstance(step_delta, int) or step_delta < 0:
            raise ValueError("step_delta must be a nonnegative integer")
        self._stride_calibrator.add_gps_segment(distance, step_delta, accuracy, speed)
        self._gps_segments += 1

    def summary(self) -> RunningDynamicsSummary:
        stride_length = self._stride_calibrator.stride_length_m
        vertical_oscillation = self._vo_integrator.vertical_oscillation_cm
        vertical_ratio = (
            vertical_oscillation / (stride_length * 100) * 100 if stride_length > 0 else 0.0
        )
        return RunningDynamicsSummary(
            cadence=self._cadence,
            stride_length_m=stride_length,
            total_steps=self._step_detector.total_steps,
            vertical_oscillation_cm=vertical_oscillation,
            vertical_ratio_percent=vertical_ratio,
            accelerometer_samples=self._accelerometer_samples,
            gps_segments=self._gps_segments,
        )

    def _check_cadence_timeout(self, timestamp_ms: int) -> None:
        if self._cadence > 0 and self._step_timestamps_ms:
            if timestamp_ms - self._step_timestamps_ms[-1] > 2000:
                self._step_timestamps_ms.clear()
                self._cadence = 0.0

    def _on_step(self, timestamp_ms: int, total_steps: int) -> None:
        self._step_timestamps_ms.append(timestamp_ms)
        self._step_timestamps_ms = [
            recorded_at
            for recorded_at in self._step_timestamps_ms
            if timestamp_ms - recorded_at <= 10000
        ]
        if len(self._step_timestamps_ms) < 2:
            self._cadence = 0.0
        else:
            elapsed_s = (self._step_timestamps_ms[-1] - self._step_timestamps_ms[0]) / 1000
            cadence = (len(self._step_timestamps_ms) - 1) / elapsed_s * 60 / 2
            self._cadence = cadence if cadence >= 15 else 0.0
        self._step_detector.total_steps = total_steps


def analyze_running_dynamics_file(
    input_path: str | Path,
    *,
    height_cm: float = 175.0,
) -> RunningDynamicsSummary:
    path = Path(input_path)
    analyzer = RunningDynamicsAnalyzer(height_cm)
    previous_timestamp: int | None = None
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, start=1):
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number}: {exc.msg}") from exc
            if not isinstance(event, dict):
                raise ValueError(f"Event on line {line_number} must be a JSON object")
            timestamp = _timestamp_ms(event.get("timestamp_ms"))
            if previous_timestamp is not None and timestamp < previous_timestamp:
                raise ValueError(f"Event timestamps are out of order on line {line_number}")
            previous_timestamp = timestamp
            event_type = event.get("type")
            if event_type == "accelerometer":
                analyzer.add_accelerometer_sample(
                    timestamp,
                    event.get("x_mps2"),
                    event.get("y_mps2"),
                    event.get("z_mps2"),
                )
            elif event_type == "gps_segment":
                analyzer.add_gps_segment(
                    event.get("distance_m"),
                    event.get("step_delta"),
                    event.get("horizontal_accuracy_m"),
                    event.get("speed_mps"),
                )
            else:
                raise ValueError(f"Unsupported event type on line {line_number}: {event_type!r}")

    if analyzer.summary().accelerometer_samples == 0:
        raise ValueError("Input contains no accelerometer samples")
    return analyzer.summary()


def format_running_dynamics(summary: RunningDynamicsSummary, output_format: str) -> str:
    if output_format == "json":
        return json.dumps(asdict(summary), ensure_ascii=False, indent=2) + "\n"
    if output_format != "txt":
        raise ValueError(f"Unsupported running dynamics report format: {output_format}")
    return "\n".join(
        (
            f"步频：{summary.cadence:.2f}",
            f"步幅：{summary.stride_length_m:.3f} 米",
            f"总步数：{summary.total_steps}",
            f"垂直振幅：{summary.vertical_oscillation_cm:.2f} 厘米",
            f"垂直步幅比：{summary.vertical_ratio_percent:.2f}%",
            f"加速度样本：{summary.accelerometer_samples}",
            f"GPS 分段：{summary.gps_segments}",
        )
    ) + "\n"


def _timestamp_ms(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("timestamp_ms must be a nonnegative integer")
    return value


def _positive_finite(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return number


def _nonnegative_finite(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise ValueError(f"{label} must be a nonnegative finite number")
    return number


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number
