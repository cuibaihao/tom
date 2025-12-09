from typing import List, Tuple, Dict, Any
from datetime import datetime, timedelta

def validate_leave(req: Dict[str, Any], balance_days: float = 5.0) -> Tuple[List[str], List[str]]:

    missing = []
    violations = []

    for f in ["leave_type", "start_time", "end_time"]:
        if not req.get(f):
            missing.append(f)

    if missing:
        return missing, violations

    # parse time
    try:
        # 所有前台拿来的内容全都是str类型，即便是数字，也是str类型
        start = datetime.fromisoformat(req["start_time"])
        end = datetime.fromisoformat(req["end_time"])
    except Exception:
        violations.append("start_time/end_time 格式应为 ISO（YYYY-MM-DD HH:MM）")
        return missing, violations

    if end <= start:
        violations.append("结束时间必须晚于开始时间")

    duration = (end - start).total_seconds() / 3600.0 / 8.0
    if duration < 0.5:
        violations.append("最小请假单位为 0.5 天")

    leave_type = req.get("leave_type")
    if leave_type == "annual":
        if duration > balance_days:
            violations.append(f"年假余额不足（剩余 {balance_days} 天）")
        # 提前 1 个工作日（简单版：提前 24h）
        if start < datetime.now() + timedelta(days=1):
            violations.append("年假需至少提前 1 个工作日提交")

    if leave_type == "sick":
        if duration >= 1 and not req.get("reason"):
            violations.append("病假超过 1 天需提供病假原因/证明说明")

    req["duration_days"] = round(duration, 2)
    return missing, violations
