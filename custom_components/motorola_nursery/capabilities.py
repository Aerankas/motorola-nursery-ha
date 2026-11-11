"""Known control semantics combined with camera-advertised access and bounds."""

from .telemetry import WRITABLE_FIELDS, TelemetryData

OPTION_VALUES = {
    "night_vision": {"off": 0, "on": 1, "auto": 2},
    "video_bitrate": {
        "160_kbit_s": 0,
        "480_kbit_s": 1,
        "640_kbit_s": 2,
        "1000_kbit_s": 3,
    },
    "video_light_frequency": {"50_hz": 0, "60_hz": 1},
}
SWITCH_FIELDS = frozenset(
    {"ceiling_mount", "lowtemp_switch", "hightemp_switch", "video[0].motion_zone"}
    | {f"video[0].motion.zone[{i}].enable" for i in range(4)}
)


def writable(data: TelemetryData | None, key: str, kind: str = "int") -> bool:
    cap = data.capabilities.get(key) if data else None
    return bool(cap and cap.writable and cap.kind == kind)


def options(data: TelemetryData | None, key: str) -> list[str]:
    cap = data.capabilities.get(key) if data else None
    if not writable(data, key):
        return []
    return [
        name
        for name, value in OPTION_VALUES[key].items()
        if cap.minimum <= value <= cap.maximum
    ]


def setting_supported(data: TelemetryData | None, key: str) -> bool:
    if key not in WRITABLE_FIELDS or not writable(data, key):
        return False
    cap = data.capabilities[key]
    if key in OPTION_VALUES:
        return len(options(data, key)) > 1
    if key in SWITCH_FIELDS:
        return cap.minimum <= 0 and cap.maximum >= 1
    return cap.minimum < cap.maximum


def playlist_supported(data: TelemetryData | None) -> bool:
    return bool(
        data and {"existsongs", "playing", "audio_control"} <= data.capabilities.keys()
    )


def ptz_supported(data: TelemetryData | None, axis: str) -> bool:
    if axis == "ptz":
        return writable(data, "ptz", "int") and (
            data.capabilities["ptz"].minimum <= 0 <= data.capabilities["ptz"].maximum
        )
    if not writable(data, axis, "str"):
        return False
    cap = data.capabilities[axis]
    lengths = {"pan": (4, 5), "tilt": (2, 4)}[axis]
    return cap.minimum <= lengths[0] and cap.maximum >= lengths[1]
