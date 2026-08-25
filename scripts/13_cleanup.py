"""
Phase 8 · 资源清理
--------------------------------------------------------
运行：
    python scripts/13_cleanup.py            打印将要删除的资源，不实际执行
    python scripts/13_cleanup.py --yes      真正执行
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import EVAL_DIR, PROJECT_ROOT, load_json

STATE_FILES = {
    "harness": ".harness_state.json",
    "gateway": ".gateway_state.json",
    "mcp": ".mcp_state.json",
    "kb": ".kb_state.json",
    "env": ".env_state.json",
}

dry_run = "--yes" not in sys.argv
failures: list[str] = []


def read(name: str) -> dict | None:
    path = os.path.join(PROJECT_ROOT, STATE_FILES[name])
    return load_json(path) if os.path.exists(path) else None


def step(desc: str, fn) -> None:
    """执行一步清理。失败只记录不抛出——一个资源删不掉不该拖住其余的。"""
    if dry_run:
        print(f"  [将删除] {desc}")
        return
    print(f"  删除 {desc} ...", end=" ", flush=True)
    try:
        fn()
        print("✓")
    except Exception as e:
        print(f"⚠ {type(e).__name__}: {str(e)[:120]}")
        failures.append(f"{desc}：{type(e).__name__}")


def empty_bucket(s3, bucket: str) -> None:
    """清空桶（含所有版本），否则 CloudFormation 删栈会失败。"""
    paginator = s3.get_paginator("list_object_versions")
    for page in paginator.paginate(Bucket=bucket):
        objects = [
            {"Key": o["Key"], "VersionId": o["VersionId"]}
            for key in ("Versions", "DeleteMarkers")
            for o in page.get(key, [])
        ]
        if objects:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objects})


def main() -> int:
    env = read("env")
    if not env:
        raise SystemExit("缺少 .env_state.json，无法确定区域")
    region = env["region"]

    print("=" * 60)
    print("Phase 8 · 资源清理" + ("（预演，不实际删除）" if dry_run else ""))
    print("=" * 60)

    control = boto3.client("bedrock-agentcore-control", region_name=region)

    # ---- 1. Harness（会连带删掉托管 Memory）----
    harness = read("harness")
    if harness and harness.get("harness_id"):
        print("\n[1] Harness")
        hid = harness["harness_id"]
        # deleteManagedMemory 默认为 true，托管 Memory 随之删除。
        # 想保留记忆数据的话传 False，它会退化成一个独立的 Memory 资源。
        step(f"Harness {hid}（连带托管 Memory）",
             lambda: control.delete_harness(harnessId=hid))

    # ---- 2. Gateway 与 Policy ----
    gateway = read("gateway")
    if gateway:
        print("\n[2] Gateway 与 Policy")
        gid = gateway.get("gateway_id")
        eid = gateway.get("policy_engine_id")

        if gid and gateway.get("target_id"):
            tid = gateway["target_id"]
            step(f"Gateway target {tid}",
                 lambda: control.delete_gateway_target(gatewayIdentifier=gid, targetId=tid))

        # 策略必须在引擎之前删
        for pid in (gateway.get("forbid_policy_ids") or []) + (gateway.get("permit_policy_ids") or []):
            step(f"Policy {pid}",
                 lambda p=pid: control.delete_policy(policyEngineId=eid, policyId=p))

        if gid:
            step(f"Gateway {gid}",
                 lambda: control.delete_gateway(gatewayIdentifier=gid))
            if not dry_run:
                time.sleep(10)   # 网关未删净时删引擎会报被引用

        if eid:
            step(f"Policy Engine {eid}",
                 lambda: control.delete_policy_engine(policyEngineId=eid))

    # ---- 3. MCP Runtime 及其构建产物 ----
    mcp = read("mcp")
    if mcp:
        print("\n[3] MCP Runtime")
        if mcp.get("runtime_id"):
            rid = mcp["runtime_id"]
            step(f"Runtime {rid}",
                 lambda: control.delete_agent_runtime(agentRuntimeId=rid))

        if mcp.get("ecr_repo_name"):
            ecr = boto3.client("ecr", region_name=region)
            repo = mcp["ecr_repo_name"]
            step(f"ECR 仓库 {repo}",
                 lambda: ecr.delete_repository(repositoryName=repo, force=True))

        if mcp.get("codebuild_name"):
            cb = boto3.client("codebuild", region_name=region)
            proj = mcp["codebuild_name"]
            step(f"CodeBuild 项目 {proj}",
                 lambda: cb.delete_project(name=proj))

    # ---- 4. 清空桶，让删栈能成功 ----
    print("\n[4] 清空 S3 桶（桶本体由 CloudFormation 删）")
    s3 = boto3.client("s3", region_name=region)
    account_id = env["account_id"]

    buckets = [f"opc-copilot-skills-{account_id}-{region}"]
    kb = read("kb")
    if kb and kb.get("docs_bucket"):
        buckets.append(kb["docs_bucket"])

    for bucket in buckets:
        step(f"清空 {bucket}", lambda b=bucket: empty_bucket(s3, b))

    # ---- 5. 本地状态文件 ----
    print("\n[5] 本地状态文件")
    # 根目录的点开头状态文件（.harness_state.json 等）
    local = sorted(
        f for f in os.listdir(PROJECT_ROOT)
        if f.startswith(".") and f.endswith(".json")
    )
    # eval/ 目录下的评测产物（eval_results_phase*.json、attribution_report.json）
    eval_files = sorted(
        f for f in os.listdir(EVAL_DIR)
        if f.endswith(".json")
    ) if os.path.isdir(EVAL_DIR) else []
    for filename in local:
        step(filename, lambda f=filename: os.remove(os.path.join(PROJECT_ROOT, f)))
    for filename in eval_files:
        step(f"eval/{filename}", lambda f=filename: os.remove(os.path.join(EVAL_DIR, f)))

    print("\n" + "=" * 60)
    if dry_run:
        print("以上为预演。确认无误后执行：python scripts/99_cleanup.py --yes")
        return 0

    if failures:
        print(f"⚠ {len(failures)} 项未能删除，需要手工处理：")
        for f in failures:
            print(f"  - {f}")
    else:
        print("✓ AgentCore 侧资源已清理")

    print("\n最后删两个 CloudFormation 栈（知识库、向量桶、IAM 角色随栈删除）：")
    print("  aws cloudformation delete-stack --stack-name opc-copilot")
    print("  # template-global.yaml 对应的栈按你实际的栈名删")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
