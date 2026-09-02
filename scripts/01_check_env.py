"""
Phase 0 · 云端环境探活
--------------------------------------------------------
运行：
    /usr/bin/python3.11 opc_copilot/scripts/01_check_env.py

可选环境变量：
    OPC_CFN_STACK_NAME  显式指定 CFN 栈名。
"""

import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import boto3
import botocore

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_STATE_FILE = os.path.join(PROJECT_ROOT, ".env_state.json")

# CFN 里硬编码的 KB 名（template-global.yaml → OpcKnowledgeBase.Name）
KB_NAME = "opc-product-knowledge-base"

# CFN 栈名，从其 Outputs 读执行角色 ARN（比 list_roles 子串匹配可靠）。
# 允许显式覆盖；不设时自动发现——栈名写死默认值会在部署名不同（如 agentcore-assist）
# 时查错栈，进而误触发 list_roles 回退，回退的子串匹配又可能命中旧栈遗留角色。
# 发现路径不用 list_stacks：实例角色常只授了 describe_stacks 而没授 list_stacks。
ROLE_OUTPUT_KEY = "AgentCoreRuntimeExecutionRoleArn"
CFN_STACK_NAME = os.environ.get("OPC_CFN_STACK_NAME", "")

# 回退/反推匹配时用。OpcCopilotHarnessExecutionRole 不是 CFN 栈产物，
# 物理名不带栈名前缀，无法反推，留在列表里只会制造子串误命中。
EXECUTION_ROLE_CANDIDATES = [
    "AgentCoreRuntimeExecutionRole",
]

REQUIRED_HARNESS_ACTIONS = ["ecr-public:GetAuthorizationToken", "sts:GetServiceBearerToken"]

blockers: list[str] = []
notes: list[str] = []


def check_boto3() -> bool:
    print("\n[1/5] boto3 版本与 Harness API")
    print(f"  boto3     {boto3.__version__}")
    print(f"  botocore  {botocore.__version__}")

    ok = True
    for service, api in (("bedrock-agentcore-control", "create_harness"),
                         ("bedrock-agentcore", "invoke_harness")):
        try:
            client = boto3.client(service, region_name=boto3.session.Session().region_name)
        except Exception as e:
            blockers.append(f"无法创建 {service} 客户端：{e}")
            ok = False
            continue
        if hasattr(client, api):
            print(f"  ✓ {service}.{api} 可用")
        else:
            ok = False
            blockers.append(
                f"{service} 客户端没有 {api} —— boto3 版本过旧，"
                f"执行 pip install -U boto3 botocore 后重跑本脚本。"
            )
            print(f"  ✗ {service}.{api} 不存在")
    return ok


def check_harness_reachable(region: str) -> None:
    print("\n[2/5] Harness 控制面可达性")
    try:
        control = boto3.client("bedrock-agentcore-control", region_name=region)
    except Exception as e:
        blockers.append(f"无法创建控制面客户端：{e}")
        return
    if not hasattr(control, "list_harnesses"):
        print("  - 跳过（boto3 无 list_harnesses）")
        return
    try:
        resp = control.list_harnesses()
        existing = resp.get("harnesses", []) or resp.get("harnessSummaries", [])
        print(f"  ✓ 区域 {region} 可用，当前已有 {len(existing)} 个 Harness")
        for h in existing:
            print(f"    - {h.get('harnessName') or h.get('name')}  {h.get('status', '')}")
    except Exception as e:
        blockers.append(
            f"list_harnesses 调用失败：{type(e).__name__} - {e}\n"
            f"    可能是区域不支持 Harness，或调用方缺 bedrock-agentcore:ListHarnesses 权限。"
        )
        print(f"  ✗ {type(e).__name__}")


def check_identity(region: str) -> dict:
    print("\n[3/5] 调用方身份")
    try:
        ident = boto3.client("sts", region_name=region).get_caller_identity()
    except Exception as e:
        blockers.append(f"get_caller_identity 失败：{e}")
        return {}
    print(f"  账号   {ident['Account']}")
    print(f"  身份   {ident['Arn']}")
    return ident


def check_knowledge_base(region: str) -> str | None:
    print("\n[4/5] 知识库（Phase 2 复用，不重建）")
    try:
        agent_client = boto3.client("bedrock-agent", region_name=region)
        paginator = agent_client.get_paginator("list_knowledge_bases")
        for page in paginator.paginate():
            for kb in page["knowledgeBaseSummaries"]:
                if kb["name"] == KB_NAME:
                    print(f"  ✓ 找到 {KB_NAME}：{kb['knowledgeBaseId']}（{kb['status']}）")
                    return kb["knowledgeBaseId"]
    except Exception as e:
        notes.append(f"列举知识库失败：{type(e).__name__} - {e}")
        print(f"  ⚠ {type(e).__name__}")
        return None

    notes.append(
        f"未找到名为 {KB_NAME} 的知识库。Phase 2 之前需确认 CFN 栈已成功创建。"
    )
    print(f"  ⚠ 未找到 {KB_NAME}")
    return None


def _describe_stack(cfn, name: str) -> dict | None:
    """按名取栈；不存在/无权限返回 None（探测属正常路径，不打告警刷屏）。"""
    try:
        stacks = cfn.describe_stacks(StackName=name).get("Stacks", [])
        return stacks[0] if stacks else None
    except Exception:
        return None


def _role_output(stack: dict | None) -> str | None:
    if not stack:
        return None
    for out in stack.get("Outputs", []) or []:
        if out.get("OutputKey") == ROLE_OUTPUT_KEY:
            return out["OutputValue"]
    return None


_role_names_cache: list[str] | None = None


def _matching_role_names() -> list[str] | None:
    """list_roles 里名字含候选关键字的角色名；None=列举失败，[]=确无匹配。

    结果缓存，整个进程只列举一次（自动发现与回退路径共用）。
    """
    global _role_names_cache
    if _role_names_cache is not None:
        return _role_names_cache
    try:
        iam = boto3.client("iam")
        paginator = iam.get_paginator("list_roles")
        names = [
            r["RoleName"]
            for page in paginator.paginate()
            for r in page["Roles"]
            if any(c in r["RoleName"] for c in EXECUTION_ROLE_CANDIDATES)
        ]
    except Exception as e:
        print(f"  ⚠ list_roles 失败：{type(e).__name__} - {e}")
        return None  # 不缓存失败——留 None 标记，回退路径据此给出准确提示
    _role_names_cache = names
    return names


def _discover_role(region: str) -> str | None:
    """从 list_roles 反推候选栈名，再逐个 describe_stacks 验证。

    CFN 给栈内 IAM 角色的物理名是 <栈名>-<逻辑ID>-<随机后缀>，例如
    agentcore-assist-AgentCoreRuntimeExecutionRole-AbCdEfGhIj。据此把
    角色物理名截去 -<逻辑ID>-<后缀> 即得候选栈名，再 describe 验证 Outputs。
    不走 list_stacks——实验环境的实例角色常只授按名 describe，没授列举。
    多个栈都导出了角色时取 CreationTime 最新的（最新的部署优先）。
    """
    role_names = _matching_role_names()
    if role_names is None:
        print("  ⚠ 无法列举 IAM 角色（权限不足），自动发现中止")
        return None

    candidates: list[str] = []
    for rn in role_names:
        for c in EXECUTION_ROLE_CANDIDATES:
            marker = f"-{c}-"
            if marker in rn:
                stack_name = rn.split(marker)[0]
                if stack_name and stack_name not in candidates:
                    candidates.append(stack_name)
                break

    if not candidates:
        return None

    cfn = boto3.client("cloudformation", region_name=region)
    hits: list[tuple] = []
    for name in candidates:
        stack = _describe_stack(cfn, name)
        role = _role_output(stack)
        if stack and role:
            hits.append((stack.get("CreationTime"), name, role))
    if not hits:
        return None
    _, stack_name, role = max(hits, key=lambda t: t[0])
    # 回填模块级常量，让状态快照记下实际使用的栈名
    globals()["CFN_STACK_NAME"] = stack_name
    print(f"  · 自动发现栈：{stack_name}" + (
        f"（候选：{', '.join(n for _, n, _ in hits)}）" if len(hits) > 1 else ""
    ))
    return role


def find_role_from_cfn_outputs(region: str) -> str | None:
    """从 CFN 栈 Outputs 读 AgentCoreRuntimeExecutionRoleArn。

    显式指定栈名（OPC_CFN_STACK_NAME）时只查那个栈；查无此栈或未导出该
    Output 时打印原因并降级到自动发现——导出了这个 Output 的活跃栈即
    部署了 template-global.yaml 的栈，比 list_roles 子串匹配可靠。
    """
    cfn = boto3.client("cloudformation", region_name=region)

    if not CFN_STACK_NAME:
        role = _discover_role(region)
        if not role:
            print(f"  ⚠ 自动发现未找到导出 {ROLE_OUTPUT_KEY} 的栈")
        return role

    try:
        stacks = cfn.describe_stacks(StackName=CFN_STACK_NAME).get("Stacks", [])
    except Exception as e:
        # 不静默吞掉——打印真实原因（栈不存在 vs 权限不足），再降级发现
        print(f"  ⚠ describe_stacks({CFN_STACK_NAME}) 失败：{type(e).__name__} - {e}")
        stacks = []

    role = _role_output(stacks[0] if stacks else None)
    if role:
        return role

    if not stacks:
        print(f"  ⚠ 栈 {CFN_STACK_NAME} 查询未果，降级自动发现")
    else:
        print(f"  ⚠ 栈 {CFN_STACK_NAME} 未导出 {ROLE_OUTPUT_KEY}，降级自动发现")
    return _discover_role(region)


def check_execution_role(region: str, account_id: str) -> str | None:
    print("\n[5/5] Harness 执行角色")

    role_arn = find_role_from_cfn_outputs(region)

    if not role_arn:
        print("  ⚠ CFN 路径未取到角色 ARN，回退 list_roles 匹配")
        role_names = _matching_role_names()
        if not role_names:  # None（列举失败）或 []（确无匹配角色）
            if role_names is None:
                notes.append(
                    "无法列举 IAM 角色确认执行角色（权限不足属正常）。"
                    f"可手工把角色 ARN 填入 {ENV_STATE_FILE} 的 execution_role_arn 字段。"
                )
            else:
                notes.append(
                    "未找到执行角色。请确认已部署 template-global.yaml，"
                    "或用 OPC_CFN_STACK_NAME 指定部署的栈名。"
                )
            print("  ⚠ 未找到执行角色")
            return None

        role_name = sorted(role_names)[-1]
        # 回退路径本就不可靠（可能命中旧栈遗留角色），明确标注存疑
        notes.append(
            f"执行角色 {role_name} 来自 list_roles 回退匹配（非 CFN 栈输出），"
            f"若后续权限报错请以 OPC_CFN_STACK_NAME 指定正确栈名重跑。"
        )
        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    src = f"（来自栈 {CFN_STACK_NAME}）" if CFN_STACK_NAME else "（回退匹配，未确认所属栈）"
    print(f"  ✓ {role_arn.split('/')[-1]}{src}")

    iam = boto3.client("iam")
    try:
        result = iam.simulate_principal_policy(
            PolicySourceArn=role_arn,
            ActionNames=REQUIRED_HARNESS_ACTIONS,
        )
        for r in result["EvaluationResults"]:
            action = r["EvalActionName"]
            decision = r["EvalDecision"]
            if decision == "allowed":
                print(f"    ✓ {action}")
            else:
                print(f"    ✗ {action} → {decision}")
                blockers.append(
                    f"执行角色缺 {action}。请重新部署 template-global.yaml 补齐（EcrPublicImagePull Sid）。"
                )
    except Exception as e:
        notes.append(
            f"无法模拟策略（{type(e).__name__}），未确认是否具备 "
            f"{' / '.join(REQUIRED_HARNESS_ACTIONS)}。"
        )
        print(f"    ⚠ 策略模拟不可用：{type(e).__name__}")

    return role_arn


def main() -> int:
    print("=" * 60)
    print("Phase 0 · 云端环境探活（只读，不创建资源）")
    print("=" * 60)

    region = boto3.session.Session().region_name
    if not region:
        print("\n✗ 未配置默认区域，请先 export AWS_REGION=us-west-2")
        return 1
    print(f"\n区域：{region}")

    boto3_ok = check_boto3()
    if boto3_ok:
        check_harness_reachable(region)
    else:
        print("\n[2/5] Harness 控制面可达性\n  - 跳过（boto3 不支持）")

    ident = check_identity(region)
    account_id = ident.get("Account", "")

    kb_id = check_knowledge_base(region)
    role_arn = check_execution_role(region, account_id) if account_id else None

    state = {
        "region": region,
        "account_id": account_id,
        "kb_id": kb_id,
        # 02(Harness) 和 06(Runtime) 都用 CFN 的 AgentCoreRuntimeExecutionRole
        "execution_role_arn": role_arn,
        "runtime_execution_role_arn": role_arn,
        # 全路径回退到 list_roles 时可能仍未发现栈名，记 null 而非空串——
        # 空串会让下游 env.get('cfn_stack_name', 'agentcore') 拿到 "" 而非默认值
        "cfn_stack_name": CFN_STACK_NAME or None,
        "boto3_version": boto3.__version__,
        "harness_api_available": boto3_ok,
    }
    with open(ENV_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 60)
    for n in notes:
        print(f"⚠ {n}")
    if blockers:
        print(f"\n✗ 有 {len(blockers)} 个阻塞项：")
        for b in blockers:
            print(f"  - {b}")
        print(f"\n环境快照仍已写入 {ENV_STATE_FILE}")
        return 1

    print(f"\n✓ 环境就绪，快照已写入 {ENV_STATE_FILE}")
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
