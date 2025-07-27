from typing import Union

from datetime import timedelta


def format_bandwidth(bytes_value: int, as_bit: bool = True) -> str:
    if as_bit:
        value = bytes_value * 8  # 转为比特
        kbps = value / 1_000
        if kbps < 1_000:
            return f"{kbps:.1f} Kbps"
        mbps = kbps / 1_000
        if mbps < 1_000:
            return f"{mbps:.1f} Mbps"
        gbps = mbps / 1_000
        return f"{gbps:.1f} Gbps"
    else:
        value = bytes_value
        kb = value / 1024
        if kb < 1024:
            return f"{kb:.1f} KB/s"
        mb = kb / 1024
        if mb < 1024:
            return f"{mb:.1f} MB/s"
        gb = mb / 1024
        return f"{gb:.1f} GB/s"


def format_delta_time(second_value: Union[int, float, timedelta]) -> str:

    if isinstance(second_value, timedelta):
        seconds = second_value.total_seconds()
    else:
        seconds = float(second_value)
    if seconds < 60.0:
        return f"{seconds:.1f} s"
    minutes = seconds / 60.0
    if minutes < 60.0:
        return f"{minutes:.1f} min"
    hours = minutes / 60.0
    return f"{hours:.1f} hr"
