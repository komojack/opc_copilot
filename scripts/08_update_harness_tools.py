"""
Phase 2 / 3 / 5 · 用 allowedTools 逐阶段放开工具
--------------------------------------------------------
MCP 服务从第一次部署起就带着三个工具，但对模型可见的是哪些，由 Harness
侧的 allowedTools 控制：

    --phase 2   kb_search                    只有规则检索
    --phase 3   + query_business_data        加上业务事实查询
    --phase 5   + record_decision            加上决策登记（Policy 同时生效）
运行：
    python scripts/08_update_harness_tools.py --phase 2
    python scripts/08_update_harness_tools.py --phase 3
    python scripts/08_update_harness_tools.py --phase 5
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import gateway_lib as gw
from common import PROJECT_ROOT, load_harness_state, load_json, save_json, HARNESS_STATE_FILE

GATEWAY_STATE_FILE = os.path.join(PROJECT_ROOT, ".gateway_state.json")

# Harness 侧给这个网关工具起的名字，allowedTools 里 @ 后面用它
GATEWAY_TOOL_NAME = "opcgw"

# 每个 Phase 放开的 MCP 工具（不含 target 前缀）
TOOLS_BY_PHASE = {
    2: ["kb_search"],
    3: ["kb_search", "query_business_data"],
    5: ["kb_search", "query_business_data", "record_decision"],
}

CAPABILITIES_BY_PHASE = {
    2: {"tools": True, "knowledge_base": True},
    3: {"tools": True, "knowledge_base": True, "business_data": True},
    5: {"tools": True, "knowledge_base": True, "business_data": True, "policy": True},
}


def build_allowed_tools(tool_names: list[str]) -> list[str]:
    """构造 allowedTools 模式。

    网关暴露给模型的工具名带 target 前缀，形如 opctools___kb_search。
    这里用 @server/glob 形式的通配（@opcgw/*kb_search），不写死前缀——
    前缀由 target 名决定，写死的话 target 一改名就全部失配，
    而失配的表现是"工具静默消失"，不报错，很难查。
    """
    return [f"@{GATEWAY_TOOL_NAME}/*{name}" for name in tool_names]


def main() -> int:
    parser = argparse.ArgumentParser(description="按 Phase 放开 Harness 可见的工具")
    parser.add_argument("--phase", type=int, required=True, choices=sorted(TOOLS_BY_PHASE))
    args = parser.parse_args()
    phase = args.phase

    harness_state = load_harness_state()
    region = harness_state["region"]
    harness_id = harness_state["harness_id"]

    if not os.path.exists(GATEWAY_STATE_FILE):
        raise SystemExit("缺少 .gateway_state.json，请先运行 scripts/07_setup_gateway.py")
    gateway_state = load_json(GATEWAY_STATE_FILE)

    tool_names = TOOLS_BY_PHASE[phase]
    allowed = build_allowed_tools(tool_names)

    print("=" * 60)
    print(f"Phase {phase} · 更新 Harness 工具配置")
    print("=" * 60)
    print(f"\nHarness      {harness_id}")
    print(f"Gateway      {gateway_state['gateway_id']}")
    print(f"工具名前缀   {gateway_state['target_name']}___")
    print(f"\n本阶段放开 {len(tool_names)} 个工具：")
    for n in tool_names:
        print(f"  · {n}")
    print(f"\nallowedTools = {allowed}")

    if phase >= 5 and not gateway_state.get("forbid_policy_ids"):
        print(
            "\n⚠ record_decision 已放开，但网关上还没有 forbid 策略。\n"
            "  此时只剩业务层兜底，Gateway 不会拦截任何越权调用。\n"
            "  请接着运行：python scripts/10_setup_policy.py"
        )

    control = boto3.client("bedrock-agentcore-control", region_name=region)

    tools = [{
        "type": "agentcore_gateway",
        "name": GATEWAY_TOOL_NAME,
        "config": {"agentCoreGateway": {
            "gatewayArn": gateway_state["gateway_arn"],
            # SigV4：用 Harness 执行角色的凭证调网关，
            # 需要角色具备 bedrock-agentcore:InvokeGateway（template-opc.yaml 已给）
            "outboundAuth": {"awsIam": {}},
        }},
    }]

    print("\n提交 UpdateHarness ...")
    control.update_harness(
        harnessId=harness_id,
        tools=tools,
        allowedTools=allowed,
    )
    print("✓ 已更新")

    caps = dict(harness_state.get("capabilities", {}))
    caps.update(CAPABILITIES_BY_PHASE[phase])
    harness_state["capabilities"] = caps
    harness_state["phase"] = phase
    harness_state["allowed_tools"] = allowed
    save_json(HARNESS_STATE_FILE, harness_state)

    print(f"✓ 状态已更新 {HARNESS_STATE_FILE}（phase={phase}）")
    print("\n下一步：")
    print("  python scripts/03_chat.py           手动验证工具确实被调用")
    print("  python scripts/04_run_evalset.py    跑评测，看与上一 Phase 的分数差")
    print("\n若模型报告看不到工具，多半是 allowedTools 通配没匹配上实际工具名。")
    print("在对话里让它列出可用工具，再据此调整 build_allowed_tools()。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
