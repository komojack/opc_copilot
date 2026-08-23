"""
Phase 5 · 加 forbid 策略，把升级边界从提示词挪到网关
--------------------------------------------------------
运行前提：07_setup_gateway.py 已完成。

运行：
    python scripts/10_setup_policy.py
    python scripts/10_setup_policy.py --verify    只做网关直连拦截预检
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
import gateway_lib as gw
from common import PROJECT_ROOT, load_env_state, load_json, save_json

GATEWAY_STATE_FILE = os.path.join(PROJECT_ROOT, ".gateway_state.json")

FORBID_RULES = [
    ("forbid_large_amount", "cedar", """forbid (principal, action == AgentCore::Action::"opctools___record_decision", resource == AgentCore::Gateway::"{GATEWAY_ARN}")
when {
    context.input has amount && context.input.amount > 50000
};"""),
    ("forbid_new_counterparty", "nl",
     "Forbid all users from calling the record_decision tool "
     "when is_new_counterparty is true."),
    ("forbid_deviates_precedent", "nl",
     "Forbid all users from calling the record_decision tool "
     "when deviates_from_precedent is true."),
    ("forbid_counterparty_overdue", "nl",
     "Forbid all users from calling the record_decision tool "
     "when counterparty_has_overdue is true."),
]

# 网关直连预检用例：(描述, 工具参数, 期望是否被拦)
VERIFY_CASES = [
    ("常规决策，应放行", {
        "entity_id": "CLIENT-ECHO", "decision_type": "quote",
        "summary": "回声传媒常规报价 5% 折扣", "amount": 32000,
        "is_new_counterparty": False, "deviates_from_precedent": False,
        "counterparty_has_overdue": False,
    }, False),
    ("金额 8 万超阈值，应拦截", {
        "entity_id": "CLIENT-DELTA", "decision_type": "quote",
        "summary": "德尔塔 8 万项目报价", "amount": 80000,
        "is_new_counterparty": False, "deviates_from_precedent": False,
        "counterparty_has_overdue": False,
    }, True),
    ("新客户首单，应拦截", {
        "entity_id": "CLIENT-BLUE", "decision_type": "quote",
        "summary": "蓝湾文化首单报价", "amount": 20000,
        "is_new_counterparty": True, "deviates_from_precedent": False,
        "counterparty_has_overdue": False,
    }, True),
    ("对手方有逾期，应拦截", {
        "entity_id": "CLIENT-ABC", "decision_type": "quote",
        "summary": "ABC 老客户价报价", "amount": 30000,
        "is_new_counterparty": False, "deviates_from_precedent": False,
        "counterparty_has_overdue": True,
    }, True),
]


def verify(region: str, gateway_url: str, target_name: str) -> int:
    """不经过 Agent，直连网关验证策略是否真的生效。

    这一步不能省。走对话测试的话，模型很可能只是在话术上拒绝、
    压根没去调那个被禁的工具——那样策略再正确也不会被触发，
    CloudWatch 里连一条评估记录都不会有，看起来"通过了"其实什么都没验证。

    认证用 SigV4（网关 authorizerType=AWS_IAM）。关键：用同步 urllib 自己
    构造 JSON-RPC body、自己发请求、自己签名——body 是我们写的，签名时用
    同一个 body，hash 完全对齐，不会出现 mcp 库异步 client 那种"提前签的
    body 和实际 body 不一致 → 401"的问题。方法照搬旧客服 Agent 实现
    scripts/07_run_test_conversations.py 的 _mcp_call（它用 JWT，我们换 SigV4）。
    """
    import json
    import urllib.error
    import urllib.request

    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest

    credentials = boto3.Session().get_credentials().get_frozen_credentials()

    def _parse_mcp_body(body: str) -> dict:
        """网关可能返回纯 JSON，也可能按 MCP streamable-http 返回 SSE
        （text/event-stream），两种都兼容：SSE 时取最后一个 data: 块。"""
        body = body.strip()
        if body.startswith("{"):
            return json.loads(body)
        data_line = None
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data_line = line[len("data:"):].strip()
        if data_line is None:
            raise ValueError(f"无法解析网关响应：{body[:200]}")
        return json.loads(data_line)

    def mcp_call(method: str, params: dict, req_id: str) -> dict:
        """同步发一个 JSON-RPC 请求到网关，用 SigV4 签名。

        body 是这里构造的、签名用的也是这个 body，所以 body hash 一定对齐。
        每次调用都实时签一次——body/参数变了签名跟着变，这是 IAM 网关的正解。
        """
        payload = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
        ).encode("utf-8")

        req = AWSRequest(method="POST", url=gateway_url, data=payload)
        SigV4Auth(credentials, "bedrock-agentcore", region).add_auth(req)
        # SigV4 算出来的头 + MCP/HTTP 必需的非签名头
        headers = dict(req.headers)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"

        request = urllib.request.Request(
            gateway_url, data=payload, method="POST", headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as resp:
                return _parse_mcp_body(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return {"error": {"httpStatus": e.code, "body": e.read().decode("utf-8", "replace")[:500]}}

    print("=" * 60)
    print("网关直连拦截预检（SigV4）")
    print("=" * 60)

    # 1) tools/list —— 挂了 Policy Engine 后列表本身就按策略过滤：
    #    工具能出现，说明 permit_baseline 生效（Cedar 默认拒绝会把它过滤掉）
    data = mcp_call("tools/list", {}, "preflight-list")
    if "error" in data:
        print(f"\n✗ tools/list 失败：{json.dumps(data['error'], ensure_ascii=False)[:300]}")
        print("  401 = 签名没过；403 = Cedar 拒绝；超时 = target 没就绪。")
        return 1
    tool_names = [t["name"] for t in data.get("result", {}).get("tools", [])]
    print(f"\n网关可见工具（{len(tool_names)}）：")
    for n in tool_names:
        print(f"  · {n}")
    if not tool_names:
        print("\n✗ 一个工具都看不到 —— permit_baseline 可能没生效。"
              "Cedar 默认拒绝会把 tools/list 也过滤掉。")
        return 1

    tool_name = next(
        (n for n in tool_names if "record_decision" in n),
        f"{target_name}___record_decision",
    )

    # 2) 逐条调 record_decision，看 forbid 命不命中
    print(f"\n直连调用 {tool_name}：")
    failures = 0
    for desc, args, should_block in VERIFY_CASES:
        data = mcp_call("tools/call", {"name": tool_name, "arguments": args}, "verify")
        text = json.dumps(data, ensure_ascii=False)

        # 网关拦截的特征串。业务层兜底拒绝返回的是正常 JSON，
        # 只看"被拒了"分不清这两者，必须认这个标记。
        blocked = (
            "AuthorizeActionException" in text
            or "policy enforcement" in text
            or "Tool Execution Denied" in text
            or "denied by default" in text
        )
        ok = blocked == should_block
        failures += int(not ok)
        verdict = "网关拦截" if blocked else "放行"
        print(f"  {'✓' if ok else '✗'} {desc}")
        print(f"      实际：{verdict}  期望：{'拦截' if should_block else '放行'}")
        if not ok:
            print(f"      返回：{text[:180]}")

    print("\n" + "=" * 60)
    if failures:
        print(f"✗ {failures} 条预检不符合预期。")
        print("  常见原因：NL2Cedar 生成的条件表达式参数名与工具签名不一致。")
        print("  回看上面打印的 Cedar 原文，确认 context.input.<参数名> 拼写正确。")
    else:
        print("✓ 网关拦截全部符合预期，策略确实生效")
    return 1 if failures else 0


def _cleanup_stale_forbid(control, engine_id: str, base_name: str) -> None:
    """删掉指定 base_name 下的所有旧策略，让重建从干净状态开始。

    用于 forbid_large_amount：它从 NL2Cedar（.greaterThan(decimal(...))）改成
    手写 Cedar（Long 的 > 运算符）后，slug 变了（hash 里加了 cedar| 前缀），
    create_policy_from_cedar 的按名复用逻辑不会删掉旧版。旧的 ACTIVE 但有
    fail-closed bug，forbid 优先会让它盖过新版，预检还是误拦——所以必须显式清。

    只清这一个 base_name：其余 forbid 是 NL2Cedar 生成、slug 没变，复用即可。
    """
    existing = [
        p for p in control.list_policies(policyEngineId=engine_id).get("policies", [])
        if p.get("name", "").startswith(f"{base_name}_")
    ]
    if not existing:
        return
    print(f"\n  清理 [{base_name}] 的旧策略（{len(existing)} 条）：")
    for p in existing:
        pid = p["policyId"]
        control.delete_policy(policyEngineId=engine_id, policyId=pid)
        print(f"    × 删除 {p['name']} ({pid})")
    time.sleep(5)  # 删除是异步的，等它真正消失再重建


def _sync_gateway_target(control, gateway_id: str, target_id: str) -> None:
    """让 Gateway 重新拉取 MCP Runtime 的 tools/list，刷新缓存的工具 schema。

    为什么这一步不可省：amount 从 float 改成 int 后，MCP Runtime 的工具 schema
    里 amount 的类型从 number(→Cedar Decimal) 变成了 integer(→Cedar Long)。
    但 Gateway 在 DEFAULT listing 模式下**缓存**了工具定义，只在
    Create/UpdateGatewayTarget 或 SynchronizeGatewayTargets 时才重新拉取。
    光重部署 Runtime、不刷这个缓存，Cedar 策略求值时拿到的 context.input.amount
    仍是按旧 Decimal schema 标注的类型——Long 的 > 运算符在 Decimal 上 fail，
    forbid_large_amount 又会 fail-closed 误拦，跟没改一样。

    SynchronizeGatewayTargets 是异步的（返回 202），target 状态会经历
    SYNCHRONIZING → READY，这里轮询到 READY 再继续。
    """
    print(f"\n  同步 Gateway target（拉取最新 MCP 工具 schema）...")
    control.synchronize_gateway_targets(
        gatewayIdentifier=gateway_id,
        targetIdList=[target_id],
    )
    waited = 0
    while waited < gw.MAX_WAIT:
        t = control.get_gateway_target(gatewayIdentifier=gateway_id, targetId=target_id)
        status = t.get("status", "UNKNOWN")
        if status == "READY":
            print(f"\n  ✓ target 已同步（lastSynchronizedAt 更新）")
            return
        if status in ("SYNCHRONIZE_UNSUCCESSFUL", "FAILED"):
            raise RuntimeError(
                f"target 同步失败 {status}：{t.get('statusReasons') or t}"
            )
        time.sleep(gw.WAIT_INTERVAL)
        waited += gw.WAIT_INTERVAL
        sys.stdout.write(f"\r    同步中 {status}，已等待 {waited}s")
        sys.stdout.flush()
    raise TimeoutError(f"target 同步等待超时（{gw.MAX_WAIT}s）")


def main() -> int:
    env = load_env_state()
    region = env["region"]

    if not os.path.exists(GATEWAY_STATE_FILE):
        raise SystemExit("缺少 .gateway_state.json，请先运行 scripts/07_setup_gateway.py")
    state = load_json(GATEWAY_STATE_FILE)

    if "--verify" in sys.argv:
        print("=" * 60)
        print("网关直连拦截预检")
        print("=" * 60)
        return verify(region, state["gateway_url"], state["target_name"])

    print("=" * 60)
    print("Phase 5 · Policy 拦截规则")
    print("=" * 60)
    print(f"\nPolicy Engine  {state['policy_engine_id']}")
    print(f"Gateway        {state['gateway_id']}（ENFORCE）")

    control = boto3.client("bedrock-agentcore-control", region_name=region)

    # 先把 Gateway 缓存的工具 schema 刷新到最新（amount 已从 float 改 int）。
    # 必须在重建 forbid 之前：Cedar 策略按 context.input 的类型求值，
    # 缓存没刷成 Long，新的 > 运算符照样 fail。前提是 06 已重部署过 int 版 Runtime。
    _sync_gateway_target(control, state["gateway_id"], state["target_id"])

    # forbid_large_amount 从 NL2Cedar 换成手写 Cedar，slug 变了，先清旧版再重建。
    # 必须在重建之前清：forbid 优先，旧的有 bug 会盖过新的。
    _cleanup_stale_forbid(control, state["policy_engine_id"], "forbid_large_amount")

    all_ids = []
    for base_name, kind, text in FORBID_RULES:
        # 手写 Cedar 里用 {GATEWAY_ARN} 占位（resource 必须约束到具体网关，
        # Policy Engine 拒绝通配 resource）。NL2Cedar 走 start_policy_generation
        # 时会自动把网关 ARN 烘进生成结果，手写没有这层，必须自己替换。
        # 用占位而不是 .format()：when 块里有花括号，format 会撞。
        if kind == "cedar":
            text = text.replace("{GATEWAY_ARN}", state["gateway_arn"])
            ids = gw.create_policy_from_cedar(
                control, state["policy_engine_id"], state["gateway_arn"], base_name, text
            )
        else:
            ids = gw.create_policy_from_text(
                control, state["policy_engine_id"], state["gateway_arn"], base_name, text
            )
        if not ids:
            print(f"  ⚠ [{base_name}] 未生成任何策略，这条边界当前不受网关保护")
        all_ids.extend(ids)

    state["forbid_policy_ids"] = all_ids
    save_json(GATEWAY_STATE_FILE, state)

    print("\n" + "=" * 60)
    print(f"✓ 共 {len(all_ids)} 条 forbid 策略生效")
    print(f"✓ 状态已更新 {GATEWAY_STATE_FILE}")
    print("\n务必先做直连预检，再跑对话测试：")
    print("  python scripts/10_setup_policy.py --verify")
    print("\n预检通过后放开 record_decision：")
    print("  python scripts/08_update_harness_tools.py --phase 5")
    print("  python scripts/04_run_evalset.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
