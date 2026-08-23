"""
Phase 2 · 建 Gateway，把 MCP 工具服务包装成受管控的工具面
--------------------------------------------------------
运行前提：06_deploy_mcp_runtime.py 已完成。

运行：
    python scripts/07_setup_gateway.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import gateway_lib as gw
from common import PROJECT_ROOT, load_env_state, load_json, save_json

MCP_STATE_FILE = os.path.join(PROJECT_ROOT, ".mcp_state.json")
GATEWAY_STATE_FILE = os.path.join(PROJECT_ROOT, ".gateway_state.json")

# 放行基线：三个工具都允许调用。真正的限制由 Phase 5 的 forbid 规则施加。
#
# ⚠️ 必须用英文、principal 必须明确、工具名照 schema 原文写——
# NL2Cedar 按 "plain English" 设计和验证，中文 / 主语含糊 / 工具名带中文修饰
# 都会让生成的 Cedar 在 create_policy 时 CREATE_FAILED（实测）。
# 这是官方 Common pitfalls 的前两条：Vague Principals、非英文。
PERMIT_BASELINE_TEXT = (
    "Allow all users to call the kb_search tool. "
    "Allow all users to call the query_business_data tool. "
    "Allow all users to call the record_decision tool."
)


# Gateway 执行角色的候选名，按优先级排。
# 注意这是与 Harness 执行角色不同的角色：网关用它调后端 MCP Runtime。
#
# 候选名必须同时覆盖两种环境的写法：
#   - template-global.yaml 把逻辑资源 AgentCoreGatewayExecutionRole 的
#     物理名改成了 "opc-gateway-execution-role"，名字里根本不含逻辑名，
#     按逻辑名子串匹配会漏掉它（这就是首次跑 07 直接 RuntimeError 的原因）
#   - 有些环境没改名，角色物理名仍是 AgentCoreGatewayExecutionRole-xxx
GATEWAY_ROLE_CANDIDATES = [
    "opc-gateway-execution-role",        # template-global.yaml 的物理名
    "AgentCoreGatewayExecutionRole",     # 逻辑资源名（未改名环境）
    "gateway-execution-role",            # 更松的兜底
]


def find_gateway_role(iam, account_id: str) -> str:
    """找 Gateway 执行角色（template-global.yaml 建）。

    与 01_check_env.py 找 Harness 执行角色同款路子：list_roles 后按候选名
    子串匹配，第一命中即用。找不到时把账号下的角色名列出来，方便人工定位。
    """
    all_names: list[str] = []
    for page in iam.get_paginator("list_roles").paginate():
        all_names.extend(r["RoleName"] for r in page["Roles"])

    for candidate in GATEWAY_ROLE_CANDIDATES:
        match = next((n for n in all_names if candidate in n), None)
        if match:
            return f"arn:aws:iam::{account_id}:role/{match}"

    sample = ", ".join(sorted(all_names)[:30])
    raise RuntimeError(
        f"未找到 Gateway 执行角色（候选：{' / '.join(GATEWAY_ROLE_CANDIDATES)}）。\n"
        f"现有角色名都不匹配。可在 .env_state.json 手工填 gateway_role_arn 字段，\n"
        f"或到 CloudFormation 控制台看 template-global 栈的 AgentCoreGatewayExecutionRoleArn 输出。\n"
        f"账号下的角色（前 30 个）：{sample}"
    )


def main() -> int:
    env = load_env_state()
    region, account_id = env["region"], env["account_id"]

    if not os.path.exists(MCP_STATE_FILE):
        raise SystemExit("缺少 .mcp_state.json，请先运行 scripts/06_deploy_mcp_runtime.py")
    mcp_state = load_json(MCP_STATE_FILE)

    print("=" * 60)
    print("Phase 2 · Gateway + Policy Engine")
    print("=" * 60)

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    iam = boto3.client("iam")

    # 支持三种来源，优先级从高到低：环境变量 > env_state 手填 > list_roles 匹配。
    # list_roles 匹配靠候选名子串，环境改名就漏——后两条是兜底。
    gateway_role_arn = (
        os.environ.get("AGENTCORE_GATEWAY_ROLE_ARN")
        or env.get("gateway_role_arn")
        or find_gateway_role(iam, account_id)
    )
    print(f"\n网关执行角色  {gateway_role_arn}")

    print("\n[1/4] Policy Engine")
    engine_id, engine_arn = gw.get_or_create_policy_engine(control)

    print("[2/4] Gateway")
    gateway = gw.get_or_create_gateway(control, gateway_role_arn, engine_arn)
    gateway_id = gateway["gatewayId"]
    gateway_arn = gateway["gatewayArn"]
    gateway_url = gateway.get("gatewayUrl", "")

    print("[3/4] 注册 MCP Runtime 为 target")
    mcp_endpoint = gw.build_runtime_mcp_url(region, mcp_state["runtime_arn"])
    target_id = gw.get_or_create_target(control, gateway_id, mcp_endpoint, region=region)

    print("[4/4] 放行基线策略")
    # baseline 是无条件放行三个工具，会被 Policy Engine 判 Overly Permissive，
    # 用 IGNORE_ALL_FINDINGS 绕过——真正的限制由 Phase 5 的 forbid 施加。
    permit_ids = gw.create_policy_from_text(
        control, engine_id, gateway_arn, "permit_baseline", PERMIT_BASELINE_TEXT,
        validation_mode="IGNORE_ALL_FINDINGS",
    )
    if not permit_ids:
        raise RuntimeError(
            "放行基线策略一条都没建成。Cedar 默认拒绝，此时网关会拒掉所有调用，"
            "模型将看不到任何工具。请检查上面的生成日志后重跑。"
        )

    state = {
        "region": region,
        "policy_engine_id": engine_id,
        "policy_engine_arn": engine_arn,
        "gateway_id": gateway_id,
        "gateway_arn": gateway_arn,
        "gateway_url": gateway_url,
        "target_id": target_id,
        "target_name": gw.GATEWAY_TARGET_NAME,
        "permit_policy_ids": permit_ids,
        "forbid_policy_ids": [],
    }
    save_json(GATEWAY_STATE_FILE, state)

    print("\n" + "=" * 60)
    print(f"Gateway ARN  {gateway_arn}")
    print(f"工具名前缀   {gw.GATEWAY_TARGET_NAME}___")
    print(f"✓ 状态已写入 {GATEWAY_STATE_FILE}")
    print("\n下一步：python scripts/08_update_harness_tools.py --phase 2")
    return 0


if __name__ == "__main__":
    sys.exit(main())
