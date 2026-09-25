from __future__ import annotations


_GUIDES = {
    "en": (
        "BLE sensor permissions",
        "Bluetooth sensors can provide heart rate, power, and cadence during activities. "
        "Enable Bluetooth and allow access for the operating system environment running this CLI. "
        "If access was denied, review the system's Bluetooth permissions in Settings. "
        "This command only displays guidance; it cannot open an Android permission prompt or change system settings.",
        "Skip this setup if you do not use BLE sensors.",
    ),
    "zh": (
        "蓝牙传感器权限",
        "蓝牙传感器可在运动中提供心率、功率和踏频数据。请在运行此 CLI 的操作系统中开启蓝牙并允许当前环境访问。"
        "如果曾拒绝访问，请到系统设置检查蓝牙权限。本命令只显示说明，不能打开 Android 授权弹窗或修改系统设置。",
        "不使用蓝牙传感器时可以跳过此设置。",
    ),
}


def format_ble_permission_guide(locale: str) -> str:
    try:
        title, description, skip_note = _GUIDES[locale]
    except KeyError as exc:
        raise ValueError(f"Unsupported BLE guide locale: {locale}") from exc

    return f"{title}\n{description}\n{skip_note}"
