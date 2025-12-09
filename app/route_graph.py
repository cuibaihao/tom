from __future__ import annotations
from typing import TypedDict, Any
from langgraph.graph import StateGraph, START, END

from app.rag.qa_graph import build_qa_graph
from app.workflows.leave.leave_graph import build_leave_graph


class RouterState(TypedDict, total=False):
    question: str
    text: str          # 兼容 /chat 的字段
    user_role: str
    mode: str          # "qa"/"rag"/"kb"/"leave" 可选提示
    requester: str
    active_route: str

    # --- fields produced by subgraphs that we want to keep across turns ---
    req: dict
    missing_fields: list[str]
    violations: list[str]

    answer: str
    docs: list[Any]
    leave_id: str


def decide_route(state: RouterState) -> str:
    mode = (state.get("mode") or "").lower().strip()

    active = (state.get("active_route") or "").lower().strip()
    if active == "leave" and mode not in {"qa", "rag", "kb"}:
        return "leave"

    # 显式 mode 优先
    if mode in {"qa", "rag", "kb"}:
        return "qa"
    if mode in {"leave", "hr"}:
        return "leave"

    # 关键词路由
    text = (state.get("text") or state.get("question") or "").lower()
    if any(k in text for k in ["请假", "年假", "病假", "事假", "休假", "调休", "假期", "请一天假", "请半天假"]):
        return "leave"

    return "qa"


def route_node(state: RouterState) -> dict:
    """Router node runnable. Must return dict updates."""
    return {"active_route": decide_route(state)}


def build_router_graph():
    qa_graph = build_qa_graph()
    leave_graph = build_leave_graph()

    g = StateGraph(RouterState)

    g.add_node("route", route_node)
    g.add_node("qa", qa_graph)
    g.add_node("leave", leave_graph)

    g.add_edge(START, "route")

    g.add_conditional_edges(
        "route",
        decide_route,
        {"qa": "qa", "leave": "leave"},
    )

    g.add_edge("qa", END)
    g.add_edge("leave", END)

    return g.compile()


router_graph = build_router_graph()

# 之后测试
# 启动服务uvicorn app.main:app --reload --port 8002后
# peter@Peters-MacBook-Air enterprise-kb-assistant % curl -X POST http://127.0.0.1:8002/chat \
#   -H "Content-Type: application/json" \
#   -d '{"text":"我下周二想请一天年假","user_role":"public","requester":"peter"}'
# 我这边可以得到结果
# {"answer":"缺少信息：start_time、end_time。请补充/修正后再说一次。"}%