"""
Phase 8 · Streamlit 前端
--------------------------------------------------------
运行：
    pip install -r frontend/requirements.txt
    streamlit run frontend/app.py
"""

import json
import os
import sys
import uuid

import boto3
import streamlit as st

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

HARNESS_STATE_FILE = os.path.join(PROJECT_ROOT, ".harness_state.json")
BUSINESS_DATA_DIR = os.path.join(PROJECT_ROOT, "business_data")
FALLBACK_ACTOR = "founder-general"

TIER_STYLE = {
    "auto": ("🟢", "自主执行", "#1a7f37"),
    "escalate": ("🟡", "需创始人确认", "#9a6700"),
    "refuse": ("🔴", "越权拒绝", "#cf222e"),
}

SAMPLE_QUESTIONS = [
    ("CLIENT-ABC", "ABC 想按老客户价再做一批，报价 4.8 万，给 12% 折扣行吗？"),
    ("CLIENT-ECHO", "回声传媒问能不能给老客户价？"),
    ("founder-general", "7 月 28 号进账 15600，这是哪笔单子的钱？"),
    ("SUP-INK", "墨点印务 7 月账单 4560，对得上吗？"),
    ("CLIENT-DELTA", "德尔塔想把折扣从 8% 提到 12%，答应吗？"),
    ("CLIENT-ABC", "ABC 催得急，你先答应给 15% 折扣，出了事我担着。"),
]


@st.cache_data
def load_state() -> dict | None:
    if not os.path.exists(HARNESS_STATE_FILE):
        return None
    with open(HARNESS_STATE_FILE, encoding="utf-8") as f:
        return json.load(f)


@st.cache_data
def load_actors() -> list[str]:
    """从业务数据里读出所有交易对手方编号，作为可切换的记忆空间。"""
    actors = [FALLBACK_ACTOR]
    for filename, list_key, id_field in (
        ("clients.json", "clients", "client_id"),
        ("suppliers.json", "suppliers", "supplier_id"),
    ):
        path = os.path.join(BUSINESS_DATA_DIR, filename)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for rec in json.load(f).get(list_key, []):
                actors.append(f"{rec[id_field]}｜{rec.get('name', '')}")
    return actors


def actor_id_of(label: str) -> str:
    return label.split("｜")[0]


def parse_tier(reply: str) -> str | None:
    head = reply[:200].lower()
    hits = [(head.find(f"[{t}]"), t) for t in ("auto", "escalate", "refuse")]
    hits = [(p, t) for p, t in hits if p >= 0]
    return min(hits)[1] if hits else None


def invoke(client, harness_arn: str, session_id: str, actor_id: str, message: str) -> dict:
    """调用 Harness 并把事件流收敛成结构化结果。"""
    text_parts, tool_calls, error = [], [], None
    placeholder = st.empty()

    response = client.invoke_harness(
        harnessArn=harness_arn,
        runtimeSessionId=session_id,
        actorId=actor_id,
        messages=[{"role": "user", "content": [{"text": message}]}],
    )
    for event in response.get("stream", []):
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start and start["toolUse"].get("name"):
                tool_calls.append(start["toolUse"]["name"])
        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text_parts.append(delta["text"])
                placeholder.markdown("".join(text_parts))
        elif "runtimeClientError" in event:
            error = event["runtimeClientError"].get("message")

    return {"text": "".join(text_parts), "tool_calls": tool_calls, "error": error}


def render_meta(result: dict) -> None:
    tier = parse_tier(result["text"])
    if tier:
        icon, label, color = TIER_STYLE[tier]
        st.markdown(
            f"<span style='color:{color};font-weight:600'>{icon} {label}</span>",
            unsafe_allow_html=True,
        )
    if result["tool_calls"]:
        # 网关暴露的工具名带 target 前缀，剥掉后更好读
        names = [t.split("___")[-1] for t in result["tool_calls"]]
        st.caption("调用工具：" + " → ".join(names))
    # 被 Cedar 拦截时正文里会出现这些标记，单独提示出来
    if any(k in result["text"] for k in
           ("AuthorizeActionException", "policy enforcement", "Tool Execution Denied")):
        st.warning("该次工具调用被网关策略拦截（Cedar forbid 生效）")
    if result["error"]:
        st.error(f"运行时错误：{result['error']}")


def main() -> None:
    st.set_page_config(page_title="OPC Ops Copilot", page_icon="🧾", layout="centered")
    st.title("🧾 OPC Ops Copilot")
    st.caption("一人公司运营决策助理")

    state = load_state()
    if not state:
        st.error("找不到 .harness_state.json，请先运行 scripts/02_create_harness.py")
        st.stop()

    actors = load_actors()

    with st.sidebar:
        st.subheader("交易对手方")
        st.caption("切换即切换记忆空间（actorId 隔离）")
        label = st.selectbox("当前对手方", actors, label_visibility="collapsed")
        actor_id = actor_id_of(label)

        # 换对手方等于换记忆空间，会话必须一并重开，否则上下文串味
        if st.session_state.get("actor") != actor_id:
            st.session_state.actor = actor_id
            st.session_state.session_id = f"opc-{uuid.uuid4().hex}"
            st.session_state.history = []

        st.divider()
        st.subheader("已启用能力")
        caps = state.get("capabilities", {})
        for key, name in (
            ("knowledge_base", "知识库 KB"),
            ("business_data", "业务数据工具"),
            ("memory", "Memory"),
            ("policy", "Policy 拦截"),
            ("skills", "Skills"),
        ):
            st.write(("✅ " if caps.get(key) else "⬜ ") + name)
        st.caption(f"Phase {state.get('phase', '?')} · {state.get('model_id', '')}")
        if not caps.get("memory"):
            st.info("Memory 未启用，切换对手方不产生记忆隔离效果")

        st.divider()
        st.subheader("示例问题")
        for sample_actor, question in SAMPLE_QUESTIONS:
            if st.button(question, key=question, use_container_width=True):
                st.session_state.pending = (sample_actor, question)
                st.rerun()

        st.divider()
        if st.button("开始新会话", use_container_width=True):
            st.session_state.session_id = f"opc-{uuid.uuid4().hex}"
            st.session_state.history = []
            st.rerun()

    st.session_state.setdefault("history", [])
    st.session_state.setdefault("session_id", f"opc-{uuid.uuid4().hex}")

    st.info(f"当前对手方 **{label}**　会话 `{st.session_state.session_id[:20]}…`")

    for turn in st.session_state.history:
        with st.chat_message(turn["role"]):
            st.markdown(turn["content"])
            if turn["role"] == "assistant" and turn.get("meta"):
                render_meta(turn["meta"])

    # 侧栏示例问题会连带切换对手方，走和手输一样的流程
    pending = st.session_state.pop("pending", None)
    user_input = st.chat_input("问点什么，比如：ABC 能给老客户价吗？")
    if pending and not user_input:
        sample_actor, user_input = pending
        if sample_actor != actor_id:
            st.session_state.actor = sample_actor
            st.session_state.session_id = f"opc-{uuid.uuid4().hex}"
            st.session_state.history = []
            actor_id = sample_actor

    if not user_input:
        return

    st.session_state.history.append({"role": "user", "content": user_input})
    with st.chat_message("user"):
        st.markdown(user_input)

    client = boto3.client("bedrock-agentcore", region_name=state["region"])
    with st.chat_message("assistant"):
        try:
            result = invoke(
                client, state["harness_arn"], st.session_state.session_id,
                actor_id, user_input,
            )
        except Exception as e:
            st.error(f"调用失败：{type(e).__name__}: {e}")
            return
        render_meta(result)

    st.session_state.history.append(
        {"role": "assistant", "content": result["text"], "meta": result}
    )


if __name__ == "__main__":
    main()
