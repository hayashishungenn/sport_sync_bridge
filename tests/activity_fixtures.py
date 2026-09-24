from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.device_info_message import DeviceInfoMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.messages.time_in_zone_message import TimeInZoneMessage
from fit_tool.profile.profile_type import FileType, Sport


START = datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)


def create_fit(
    path: Path,
    *,
    start: datetime = START,
    manufacturer: int = 999,
    product: int = 123,
    firmware: float | None = 2.0,
    with_track: bool = True,
    with_position: bool = True,
    with_timestamps: bool = True,
    with_heart_rate: bool = True,
    sport: int = Sport.CYCLING.value,
    average_heart_rate: int | None = None,
    maximum_heart_rate: int | None = None,
    average_power: int | None = None,
    maximum_power: int | None = None,
    normalized_power: int | None = None,
    intensity_factor: float | None = None,
    aerobic_training_effect: float | None = None,
    anaerobic_training_effect: float | None = None,
    training_stress_score: float | None = None,
    time_in_zone: dict[str, int | float | list[int | float]] | None = None,
) -> Path:
    builder = FitFileBuilder(auto_define=True)
    file_id = FileIdMessage()
    _set(file_id, "type", FileType.ACTIVITY.value)
    _set(file_id, "manufacturer", manufacturer)
    _set(file_id, "product", product)
    _set(file_id, "time_created", _timestamp(start))
    builder.add(file_id)

    if firmware is not None:
        device = DeviceInfoMessage()
        _set(device, "manufacturer", manufacturer)
        _set(device, "product", product)
        _set(device, "software_version", firmware)
        builder.add(device)

    if with_track:
        for index in range(2):
            point = RecordMessage()
            if with_timestamps:
                _set(point, "timestamp", _timestamp(start.replace(minute=start.minute + index)))
            if with_position:
                _set(point, "position_lat", 31.2300 + index * 0.001)
                _set(point, "position_long", 121.4700 + index * 0.001)
            _set(point, "altitude", 10.0 + index)
            _set(point, "distance", 100.0 * index)
            _set(point, "speed", 5.0 + index * 0.1)
            if with_heart_rate:
                _set(point, "heart_rate", 150 + index)
            _set(point, "cadence", 80 + index)
            _set(point, "power", 200 + index)
            builder.add(point)

        lap = LapMessage()
        _set(lap, "start_time", _timestamp(start))
        _set(lap, "timestamp", _timestamp(start.replace(minute=start.minute + 1)))
        _set(lap, "total_elapsed_time", 61.0)
        _set(lap, "total_timer_time", 60.0)
        _set(lap, "total_distance", 100.0)
        _set(lap, "total_calories", 30)
        if with_heart_rate:
            _set(lap, "avg_heart_rate", 150)
            _set(lap, "max_heart_rate", 151)
        _set(lap, "sport", sport)
        builder.add(lap)

        session = SessionMessage()
        _set(session, "start_time", _timestamp(start))
        _set(session, "timestamp", _timestamp(start.replace(minute=start.minute + 1)))
        _set(session, "sport", sport)
        _set(session, "total_distance", 100.0)
        _set(session, "total_elapsed_time", 62.0)
        _set(session, "total_timer_time", 59.0)
        _set(session, "avg_speed", 5.0)
        _set(session, "max_speed", 5.1)
        if average_heart_rate is not None:
            _set(session, "avg_heart_rate", average_heart_rate)
        if maximum_heart_rate is not None:
            _set(session, "max_heart_rate", maximum_heart_rate)
        if average_power is not None:
            _set(session, "avg_power", average_power)
        if maximum_power is not None:
            _set(session, "max_power", maximum_power)
        if normalized_power is not None:
            _set(session, "normalized_power", normalized_power)
        if intensity_factor is not None:
            _set(session, "intensity_factor", intensity_factor)
        if aerobic_training_effect is not None:
            _set(session, "total_training_effect", aerobic_training_effect)
        if anaerobic_training_effect is not None:
            _set(session, "total_anaerobic_training_effect", anaerobic_training_effect)
        if training_stress_score is not None:
            _set(session, "training_stress_score", training_stress_score)
        builder.add(session)

    if time_in_zone:
        zones = TimeInZoneMessage()
        for field_name, value in time_in_zone.items():
            field = zones.get_field_by_name(field_name)
            if field is None:
                raise ValueError(f"Unknown FIT time-in-zone field: {field_name}")
            values = value if isinstance(value, list) else [value]
            for index, item in enumerate(values):
                field.set_value(index, item)
        builder.add(zones)

    path.parent.mkdir(parents=True, exist_ok=True)
    builder.build().to_file(str(path))
    return path


def create_gpx(path: Path, *, with_track: bool = True) -> Path:
    points = """<trkpt lat="31.23" lon="121.47"><ele>10</ele><time>2026-01-02T03:04:00Z</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>150</gpxtpx:hr><gpxtpx:cad>80</gpxtpx:cad></gpxtpx:TrackPointExtension><ssb:speed>5</ssb:speed><ssb:distance>0</ssb:distance><ssb:power>200</ssb:power></extensions></trkpt><trkpt lat="31.231" lon="121.471"><ele>11</ele><time>2026-01-02T03:05:00Z</time><extensions><gpxtpx:TrackPointExtension><gpxtpx:hr>151</gpxtpx:hr><gpxtpx:cad>81</gpxtpx:cad></gpxtpx:TrackPointExtension><ssb:speed>5.1</ssb:speed><ssb:distance>100</ssb:distance><ssb:power>201</ssb:power></extensions></trkpt>""" if with_track else ""
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" xmlns:gpxtpx="http://www.garmin.com/xmlschemas/TrackPointExtension/v1" xmlns:ssb="https://sport-sync-bridge.example/xmlschemas/extensions/v1" version="1.1" creator="fixture">
  <trk><name>Evening ride</name><type>cycling</type><trkseg>{points}</trkseg></trk>
</gpx>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def create_tcx(path: Path, *, with_track: bool = True) -> Path:
    points = """<Trackpoint><Time>2026-01-02T03:04:00Z</Time><Position><LatitudeDegrees>31.23</LatitudeDegrees><LongitudeDegrees>121.47</LongitudeDegrees></Position><AltitudeMeters>10</AltitudeMeters><DistanceMeters>0</DistanceMeters><HeartRateBpm><Value>150</Value></HeartRateBpm><Cadence>80</Cadence><Extensions><ns3:TPX><ns3:Speed>5</ns3:Speed><ns3:Watts>200</ns3:Watts></ns3:TPX></Extensions></Trackpoint><Trackpoint><Time>2026-01-02T03:05:00Z</Time><Position><LatitudeDegrees>31.231</LatitudeDegrees><LongitudeDegrees>121.471</LongitudeDegrees></Position><AltitudeMeters>11</AltitudeMeters><DistanceMeters>100</DistanceMeters><HeartRateBpm><Value>151</Value></HeartRateBpm><Cadence>81</Cadence><Extensions><ns3:TPX><ns3:Speed>5.1</ns3:Speed><ns3:Watts>201</ns3:Watts></ns3:TPX></Extensions></Trackpoint>""" if with_track else ""
    payload = f"""<?xml version="1.0" encoding="UTF-8"?>
<TrainingCenterDatabase xmlns="http://www.garmin.com/xmlschemas/TrainingCenterDatabase/v2" xmlns:ns3="http://www.garmin.com/xmlschemas/ActivityExtension/v2">
  <Activities><Activity Sport="Biking"><Id>2026-01-02T03:04:00Z</Id><Notes>Evening ride</Notes><Lap StartTime="2026-01-02T03:04:00Z"><TotalTimeSeconds>60</TotalTimeSeconds><DistanceMeters>100</DistanceMeters><Calories>30</Calories><AverageHeartRateBpm><Value>150</Value></AverageHeartRateBpm><MaximumHeartRateBpm><Value>151</Value></MaximumHeartRateBpm><Intensity>Active</Intensity><TriggerMethod>Manual</TriggerMethod><Track>{points}</Track></Lap></Activity></Activities>
</TrainingCenterDatabase>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path


def _set(message: object, name: str, value: object) -> None:
    field = message.get_field_by_name(name)
    if field is not None:
        field.set_value(0, value)


def _timestamp(value: datetime) -> int:
    return round(value.timestamp() * 1000)
