"""
Phase 0 · 云端环境探活
--------------------------------------------------------
运行：
    /usr/bin/python3.11 opc_copilot/scripts/01_check_env.py
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
CFN_STACK_NAME = os.environ.get("OPC_CFN_STACK_NAME", "agentcore")

# list_roles 回退匹配时用
EXECUTION_ROLE_CANDIDATES = [
    "OpcCopilotHarnessExecutionRole",
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


def find_role_from_cfn_outputs(region: str) -> str | None:
    """从 CFN 栈 Outputs 读 AgentCoreRuntimeExecutionRoleArn。"""
    cfn = boto3.client("cloudformation", region_name=region)
    try:
        resp = cfn.describe_stacks(StackName=CFN_STACK_NAME)
    except Exception as e:
        # 不静默吞掉——打印真实原因，否则会误判"未导出"而走回退
        print(f"  ⚠ describe_stacks({CFN_STACK_NAME}) 失败：{type(e).__name__} - {e}")
        return None
    stacks = resp.get("Stacks", [])
    if not stacks:
        return None
    for out in stacks[0].get("Outputs", []) or []:
        if out.get("OutputKey") == "AgentCoreRuntimeExecutionRoleArn":
            return out["OutputValue"]
    return None


def check_execution_role(region: str, account_id: str) -> str | None:
    print("\n[5/5] Harness 执行角色")

    role_arn = find_role_from_cfn_outputs(region)

    if not role_arn:
        print(f"  ⚠ CFN 栈 {CFN_STACK_NAME} 未导出角色 ARN，回退 list_roles 匹配")
        iam = boto3.client("iam")
        all_names: list[str] = []
        try:
            paginator = iam.get_paginator("list_roles")
            for page in paginator.paginate():
                all_names.extend(r["RoleName"] for r in page["Roles"])
        except Exception as e:
            notes.append(f"列举 IAM 角色失败（权限不足属正常）：{type(e).__name__}")
            print(f"  ⚠ 无法列举角色：{type(e).__name__}")
            return None

        role_name = None
        for candidate in EXECUTION_ROLE_CANDIDATES:
            match = next((n for n in all_names if candidate in n), None)
            if match:
                role_name = match
                break

        if not role_name:
            notes.append(
                f"未找到执行角色。请确认 CFN 栈 {CFN_STACK_NAME} 已部署 template-global.yaml。"
            )
            print(f"  ⚠ 未找到执行角色")
            return None

        role_arn = f"arn:aws:iam::{account_id}:role/{role_name}"

    print(f"  ✓ {role_arn.split('/')[-1]}")

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
        "cfn_stack_name": CFN_STACK_NAME,
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
