from __future__ import annotations

import json
import uuid
import re
from datetime import datetime
from typing import Any, Dict

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import HumanMessage, SystemMessage

from app.deps import get_llm
from app.workflows.leave.models import LeaveState
from app.workflows.leave.rules import validate_leave
from app.db.mysql import (
    get_leave_balance,
    insert_leave_request,
    get_leave_request,
    cancel_leave_request,
)

# ========= Prompts =========

SLOT_SYSTEM = (
    "你是企业HR请假助手。"
    "你的任务是从用户请假描述中抽取结构化信息。"
    "只输出JSON，不要解释。"
)

# NOTE: 双大括号避免 .format() KeyError
SLOT_USER = """请从下面文本中抽取字段，输出严格 JSON：
{{
  "leave_type": "annual|sick|personal|other",
  "start_time": "YYYY-MM-DD HH:MM 或 null",
  "end_time": "YYYY-MM-DD HH:MM 或 null",
  "reason": "string 或 null"
}}

要求：
- 如果用户没有明确说开始/结束时间，就输出 null
- 时间必须是 ISO 8601 格式（YYYY-MM-DD HH:MM）
- 不要编造时间
- 只输出 JSON

文本：{text}
"""

TIME_SYSTEM = (
    "你是时间解析器。"
    "请把中文自然语言中的请假时间解析为 ISO 8601 start_time/end_time。"
    "只输出JSON，不要解释。"
)

# NOTE: 双大括号避免 .format() KeyError
TIME_USER = """现在时间是：{now}
用户文本：{text}

请输出严格 JSON：
{{
  "start_time": "YYYY-MM-DD HH:MM 或 null",
  "end_time": "YYYY-MM-DD HH:MM 或 null"
}}

规则：
- 能明确推断出具体日期就填 ISO；否则填 null
- “下周二/明天/后天/本周五”等要结合 now 推断
- “上午/下午/全天/半天”：
  - 全天：09:00-18:00
  - 上午：09:00-12:00
  - 下午：13:00-18:00
  - 半天：若只说半天且无上下文，按上午 09:00-12:00
- 如果文本里已经出现 ISO 时间，直接按其输出
- 不要编造不存在的日期
- 只输出 JSON
"""

# ========= Helpers =========

def _safe_json_load(s: str) -> Dict[str, Any]:
    if not s:
        return {}
    s = s.strip()
    if s.startswith("```"):
        s = s.strip("`")
        if s.lower().startswith("json"):
            s = s[4:].strip()
    try:
        return json.loads(s)
    except Exception:
        return {}

def _safe_iso(s: Any) -> str | None:
    if not s or not isinstance(s, str):
        return None
    s = s.strip()
    try:
        datetime.fromisoformat(s)
        return s
    except Exception:
        return None

def _extract_leave_id(text: str) -> str | None:
    if not text:
        return None
    m = re.search(r"\bLV-[0-9a-fA-F]{6,12}\b", text)
    return m.group(0) if m else None

# ========= Intent Routing =========

def decide_intent(state: LeaveState) -> str:
    """apply / query / cancel"""
    text = (state.get("text") or state.get("question") or "").lower()

    if any(k in text for k in ["取消", "撤销", "作废"]):
        return "cancel"

    if any(k in text for k in ["查询", "查", "状态", "进度", "结果"]):
        if any(k in text for k in ["请假", "年假", "病假", "事假", "休假", "调休", "假期", "申请", "单"]):
            return "query"

    return "apply"

def intent_node(state: LeaveState) -> dict:
    return {}

# ========= Query / Cancel Nodes =========

def query_leave_node(state: LeaveState) -> dict:
    text = state.get("text") or state.get("question") or ""
    leave_id = state.get("leave_id") or _extract_leave_id(text)

    if not leave_id:
        # 如果没有提供id，就到数据库里查找这个用户所有的或者前面几个请假的单子显示出来
        return {"answer": "请提供请假编号（例如 LV-xxxxxxx），我才能帮你查询。"}

    row = get_leave_request(leave_id)  # 到mysql数据库中按照id查询
    if not row:
        return {"answer": f"未找到编号为 {leave_id} 的请假申请。"}

    return {
        "leave_id": leave_id,
        "answer": (
            f"请假单 {leave_id} 当前状态：{row['status']}\n"
            f"类型：{row['leave_type']}\n"
            f"开始：{row['start_time']}\n"
            f"结束：{row['end_time']}\n"
            f"时长：{row['duration_days']} 天\n"
            f"原因：{row.get('reason') or '无'}"
        ),
    }

def cancel_leave_node(state: LeaveState) -> dict:
    text = state.get("text") or state.get("question") or ""
    leave_id = state.get("leave_id") or _extract_leave_id(text)

    if not leave_id:
        return {"answer": "请提供要取消的请假编号（例如 LV-xxxxxxx）。"}

    ok = cancel_leave_request(leave_id)  # 也是mysql里面写好的取消代码
    if not ok:
        return {"answer": "取消失败：未找到该单，或单据不是待审批状态（PENDING）。"}

    return {"leave_id": leave_id, "answer": f"已取消请假申请 {leave_id}。"}

# ========= Apply-flow Nodes =========

def parse_time_node(state: LeaveState) -> dict:
    req = state.get("req") or {}
    if _safe_iso(req.get("start_time")) and _safe_iso(req.get("end_time")):
        return {}

    llm = get_llm()
    text = state.get("text", "") or state.get("question", "") or ""
    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    messages = [
        SystemMessage(content=TIME_SYSTEM),
        HumanMessage(content=TIME_USER.format(now=now, text=text)),
    ]
    raw = llm.invoke(messages).content
    # 代码调试的时候，这里一定打断点看看raw返回的是不是你要的结果
    data = _safe_json_load(raw)

    start = _safe_iso(data.get("start_time"))
    end = _safe_iso(data.get("end_time"))

    if start or end:
        req.update({
            "start_time": start or req.get("start_time"),
            "end_time": end or req.get("end_time"),
        })
        return {"req": req}
    return {}

def extract_slots_node(state: LeaveState) -> dict:
    llm = get_llm()
    text = state.get("text", "") or state.get("question", "") or ""

    messages = [
        SystemMessage(content=SLOT_SYSTEM),
        HumanMessage(content=SLOT_USER.format(text=text)),
    ]
    raw = llm.invoke(messages).content
    data = _safe_json_load(raw)

    req = state.get("req") or {}
    req.update({
        "leave_type": data.get("leave_type") or req.get("leave_type"),
        "start_time": _safe_iso(data.get("start_time")) or req.get("start_time"),
        "end_time": _safe_iso(data.get("end_time")) or req.get("end_time"),
        "reason": data.get("reason") or req.get("reason"),
    })
    req["requester"] = state.get("requester", "anonymous")
    return {"req": req}

def validate_node(state: LeaveState) -> dict:
    req = state.get("req") or {}
    requester = req.get("requester") or state.get("requester", "anonymous")

    bal = get_leave_balance(requester) or {}  #
    annual_balance = float(bal.get("annual_days", 0))

    missing, violations = validate_leave(req, balance_days=annual_balance)
    return {"missing_fields": missing, "violations": violations, "req": req}

def decide_next(state: LeaveState) -> str:
    if state.get("missing_fields") or state.get("violations"):
        return "need_info"
    return "confirm"

def need_info_node(state: LeaveState) -> dict:
    missing = state.get("missing_fields") or []
    violations = state.get("violations") or []
    tips = []
    if missing:
        tips.append("缺少信息：" + "、".join(missing))
    if violations:
        tips.append("规则问题：" + "；".join(violations))
    return {"answer": "；".join(tips) + "。请补充/修正后再说一次。"}

def confirm_node(state: LeaveState) -> dict:
    req = state.get("req") or {}
    ans = (
        "请确认你的请假信息：\n"
        f"- 类型：{req.get('leave_type')}\n"
        f"- 开始：{req.get('start_time')}\n"
        f"- 结束：{req.get('end_time')}\n"
        f"- 时长：{req.get('duration_days')} 天\n"
        f"- 原因：{req.get('reason') or '无'}\n"
        "回复“确认”提交，或直接回复修改后的信息。"
    )
    return {"answer": ans}

def decide_confirm(state: LeaveState) -> str:
    text = (state.get("text") or "").strip().lower()
    if text in {"确认", "确定", "yes", "ok", "submit"}:
        return "create"
    return "end"

def create_leave_node(state: LeaveState) -> dict:
    req = state.get("req") or {}
    leave_id = "LV-" + uuid.uuid4().hex[:8]
    req_to_save = {
        "leave_id": leave_id,
        "requester": req["requester"],
        "leave_type": req["leave_type"],
        "start_time": req["start_time"],
        "end_time": req["end_time"],
        "duration_days": req["duration_days"],
        "reason": req.get("reason"),
    }
    insert_leave_request(req_to_save)
    return {"leave_id": leave_id, "answer": f"已提交请假申请，编号 {leave_id}，等待审批。"}

# ========= Build Graph =========

def build_leave_graph():
    g = StateGraph(LeaveState)

    # intent routing
    g.add_node("intent", intent_node)
    g.add_node("query", query_leave_node)
    g.add_node("cancel", cancel_leave_node)

    # apply-flow
    g.add_node("parse_time", parse_time_node)
    g.add_node("extract", extract_slots_node)
    g.add_node("validate", validate_node)
    g.add_node("need_info", need_info_node)
    g.add_node("confirm", confirm_node)
    g.add_node("create", create_leave_node)

    g.add_edge(START, "intent")

    g.add_conditional_edges(
        "intent",
        decide_intent,
        {"apply": "parse_time", "query": "query", "cancel": "cancel"},
    )

    g.add_edge("parse_time", "extract")
    g.add_edge("extract", "validate")

    g.add_conditional_edges(
        "validate",
        decide_next,
        {"need_info": "need_info", "confirm": "confirm"},
    )

    g.add_conditional_edges(
        "confirm",
        decide_confirm,
        {"create": "create", "end": END},
    )

    g.add_edge("query", END)
    g.add_edge("cancel", END)
    g.add_edge("need_info", END)
    g.add_edge("create", END)

    return g.compile()
