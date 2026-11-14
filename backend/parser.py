import re
from datetime import datetime

STATION = re.compile(r"^[A-Z]{2}\d{2}$")
FLAGS = {"A": "实测", "M": "插补", "B": "可疑"}

# 入库前的清洗方式。keep 保留原旗，to_a / to_m 改旗后放行。
CLEANING_METHODS = {
    "keep": "原旗放行",
    "to_a": "改记实测",
    "to_m": "改记插补",
}

# 当日的策略单只写一种清洗方式。
POLICY_LABELS = {
    "strict": "严格清洗：可疑旗须改为实测或插补",
    "keep_b": "留旗入库：可疑旗可原旗入库，详情保留原旗",
}


def parse_telegram_line(line: str):
    parts = [part.strip() for part in line.strip().split("|")]
    if len(parts) != 4:
        return None, "列数不是四段"
    station, stamp, tide, flag = parts
    if not STATION.match(station):
        return None, "站码应为两位字母加两位数字"
    try:
        observed = datetime.strptime(stamp, "%Y%m%d%H%M")
    except ValueError:
        return None, "时间不是十二位数字"
    if not re.fullmatch(r"-?\d+", tide):
        return None, "潮位不是整数厘米"
    centimeters = int(tide)
    if centimeters < -500 or centimeters > 2000:
        return None, "潮位超出量程"
    if flag not in FLAGS:
        return None, "质量旗不在 A、M、B"
    return {
        "station_code": station,
        "observed_at": observed,
        "tide_cm": centimeters,
        "quality_flag": flag,
        "flag_label": FLAGS[flag],
    }, None


def method_label(method: str, original_flag: str) -> str:
    """待落列表和清洗详情里显示的方式名。"""
    if method == "keep":
        return {"A": "实测原旗", "M": "插补原旗", "B": "可疑留旗"}.get(
            original_flag, "原旗放行"
        )
    return CLEANING_METHODS.get(method, method)


def cleaned_flag(original_flag: str, method: str) -> str:
    if method == "keep":
        return original_flag
    return "A" if method == "to_a" else "M"


def method_allowed(original_flag: str, method: str, policy: str) -> bool:
    """清洗方式必须和当日策略单一致，否则记清洗方式不符。"""
    if original_flag != "B":
        return method == "keep"
    if policy == "strict":
        return method in ("to_a", "to_m")
    if policy == "keep_b":
        return method == "keep"
    return False
