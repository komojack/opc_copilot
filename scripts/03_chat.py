"""
Phase 1 · 交互对话
--------------------------------------------------------
连上 Harness 聊天，验证 baseline 通不通。

关于 actorId：Phase 1 的 Memory 是关闭的，传了也不起作用，但这里已经把
参数通路留好了——Phase 4 打开 Memory 后，同一份代码就能体现按交易对手方
隔离记忆。用 :actor 命令切换当前对手方。

runtimeSessionId 每次启动生成一个新的；同一次运行内复用，多轮对话才连得上。

运行：
    /usr/bin/python3.11 opc_copilot/scripts/03_chat.py
    /usr/bin/python3.11 opc_copilot/scripts/03_chat.py CLIENT-ABC     指定初始 actor
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import (
    FALLBACK_ACTOR,
    consume_stream,
    load_harness_state,
    new_session_id,
    parse_tier,
    with_skill_context,
)

BANNER = """
命令：
  :actor <ID>   切换交易对手方（如 :actor CLIENT-ABC / :actor SUP-INK）
  :skill        查看可指定的作业流程
  :skill <名字> 指定流程（后续消息头注入"请按该流程处理"，强引导）
  :skill off    取消指定，回到模型自动选择
  :new          开一个新会话（清空上下文；会话不换则 :skill 注入不刷新）
  :quit         退出
"""

# 用户消息里前缀式注入当前 actorId。
#
# 为什么需要这条：actorId 只作为 Harness Memory 的隔离维度传给后端，
# 不进 messages —— 模型收不到、不知道"现在在跟谁谈"，于是碰到"这家客户
# 有什么约定"这类含指代的问句只能反问客户编号。把当前对手方明示在消息头，
# 模型才可能据此主动调 query_business_data(entity_id=当前 actor)。
#
# 放消息头而非改 Harness systemPrompt：Harness 的 systemPrompt 是建实例时
# 固化的（02_create_harness.py），调用层 invoke_harness 不覆盖它；而 actor
# 在一次会话里会随 :actor 切换，必须逐条动态注入，故选消息前缀。
def with_actor_context(text: str, actor_id: str) -> str:
    if actor_id == FALLBACK_ACTOR:
        # 无具体对手方时不必注入，保持 baseline 行为，避免噪声
        return text
    return f"[当前对手方：{actor_id}]\n{text}"


def main() -> int:
    state = load_harness_state()
    region = state["region"]
    harness_arn = state["harness_arn"]

    actor_id = sys.argv[1] if len(sys.argv) > 1 else FALLBACK_ACTOR
    session_id = new_session_id()
    # 人工指定的作业流程（理解 B）：None = 模型自动选择
    pinned_skill: str | None = None
    available_skills = (state.get("skills") or {}).get("names", [])

    client = boto3.client("bedrock-agentcore", region_name=region)

    caps = state.get("capabilities", {})
    enabled = [k for k, v in caps.items() if v] or ["（无，baseline）"]

    print("=" * 60)
    print(f"OPC Ops Copilot · Phase {state.get('phase', '?')}")
    print("=" * 60)
    print(f"模型      {state.get('model_id')}")
    print(f"已启用    {', '.join(enabled)}")
    print(f"对手方    {actor_id}")
    print(f"会话      {session_id}")
    if not caps.get("memory"):
        print("\n注意：当前 Memory 未启用，actorId 不产生隔离效果（Phase 4 起生效）")
    if not caps.get("skills"):
        print("注意：当前未挂载 skills，:skill 指定无效（Phase 6 起生效）")
    print(BANNER)

    while True:
        try:
            user_input = input("\n你 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见")
            return 0

        if not user_input:
            continue
        if user_input == ":quit":
            print("再见")
            return 0
        if user_input == ":new":
            session_id = new_session_id()
            print(f"已开新会话：{session_id}")
            continue
        if user_input.startswith(":skill"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 1:
                if available_skills:
                    print("可指定的作业流程：")
                    for name in available_skills:
                        mark = " ← 已指定" if name == pinned_skill else ""
                        print(f"  - {name}{mark}")
                else:
                    print("（当前未挂载任何 skill）")
            elif parts[1].strip().lower() == "off":
                pinned_skill = None
                print("已取消指定，回到模型自动选择")
            else:
                name = parts[1].strip()
                if name not in available_skills:
                    print(f"✗ 未知流程：{name}")
                    if available_skills:
                        print(f"  可选：{', '.join(available_skills)}")
                else:
                    pinned_skill = name
                    print(f"已指定流程：{name}（后续消息头注入，:skill off 取消）")
            continue
        if user_input.startswith(":actor"):
            parts = user_input.split(maxsplit=1)
            if len(parts) == 2:
                actor_id = parts[1].strip()
                # 换对手方等于换记忆空间，会话也一并重开，避免上下文串味
                session_id = new_session_id()
                print(f"对手方切换为 {actor_id}，已开新会话")
            else:
                print(f"当前对手方：{actor_id}")
            continue

        print("\n助理 > ", end="", flush=True)
        try:
            # 两层注入同构：actor（Memory 隔离维度）与 skill（人工指定的流程），
            # 都是 systemPrompt 固化后只能在调用层动态拼进消息头的会话状态
            text = with_actor_context(user_input, actor_id)
            text = with_skill_context(text, pinned_skill)
            response = client.invoke_harness(
                harnessArn=harness_arn,
                runtimeSessionId=session_id,
                actorId=actor_id,
                messages=[{"role": "user", "content": [{"text": text}]}],
            )
            result = consume_stream(response, echo=True)
        except Exception as e:
            print(f"\n[调用失败] {type(e).__name__}: {e}")
            continue

        print()
        tier = parse_tier(result["text"])
        meta = []
        if tier:
            meta.append(f"档位={tier}")
        if result["skills_loaded"]:
            meta.append(f"已加载skill={', '.join(result['skills_loaded'])}")
        if result["tool_calls"]:
            # 已加载的 skill 会以 'skills' 出现在 tool_calls 里，
            # 这里把它从工具列表里隐去，避免和上面的"已加载skill"重复展示
            others = [t for t in result["tool_calls"] if t != "skills"]
            if others:
                meta.append(f"工具={', '.join(others)}")
        if result["stop_reason"] and result["stop_reason"] != "end_turn":
            meta.append(f"stop={result['stop_reason']}")
        if result["error"]:
            meta.append(f"错误={result['error']}")
        if meta:
            print(f"  [{' | '.join(meta)}]")


if __name__ == "__main__":
    sys.exit(main())
