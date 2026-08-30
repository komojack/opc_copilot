"""
Phase 1 · 创建 baseline Harness（首次）/ 更新系统提示词（已存在时）
--------------------------------------------------------
行为：
  - Harness 不存在 → 创建 baseline（无工具 / 无记忆 / 无 skills）
  - Harness 已存在 → 只推送最新 SYSTEM_PROMPT，其余组件保持服务端原值不变，
                     并从服务端读取实际配置同步回 .harness_state.json

运行：
    python scripts/02_create_harness.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import (
    DEFAULT_MODEL_ID,
    HARNESS_NAME,
    HARNESS_STATE_FILE,
    SYSTEM_PROMPT,
    load_env_state,
    save_json,
)

WAIT_INTERVAL = 5
MAX_WAIT = 600
TERMINAL_STATUSES = {"READY", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"}


def find_existing(control, name: str) -> dict | None:
    """按名字找已存在的 Harness。

    list_harnesses 的返回键在不同 boto3 版本里出现过 harnesses /
    harnessSummaries 两种写法，两个都兜一下。
    """
    try:
        resp = control.list_harnesses()
    except Exception as e:
        print(f"  ⚠ list_harnesses 失败（{type(e).__name__}），按新建处理")
        return None

    for h in resp.get("harnesses", []) or resp.get("harnessSummaries", []):
        if (h.get("harnessName") or h.get("name")) == name:
            return h
    return None


def wait_ready(control, harness_id: str) -> dict:
    """轮询直到 Harness 进入终态，返回解包后的 Harness dict。"""
    waited = 0
    while waited < MAX_WAIT:
        resp = control.get_harness(harnessId=harness_id)
        harness = resp.get("harness", resp)
        status = harness.get("status", "UNKNOWN")
        if status in TERMINAL_STATUSES:
            if status != "READY":
                raise RuntimeError(
                    f"Harness 最终状态为 {status}，"
                    f"失败原因：{harness.get('failureReason', '（服务端未返回）')}"
                )
            return harness
        print(f"  状态：{status}，已等待 {waited}s")
        time.sleep(WAIT_INTERVAL)
        waited += WAIT_INTERVAL
    raise TimeoutError(f"等待 Harness 就绪超时（{MAX_WAIT}s）")


def derive_state_from_detail(detail: dict, region: str, harness_id: str,
                              harness_arn: str) -> dict:
    """从服务端返回的 Harness 详情推导 capabilities 和组件配置。

    不依赖本地旧状态文件，完全以服务端为准——避免状态文件与实际配置漂移。
    """
    tools = detail.get("tools") or []
    allowed = detail.get("allowedTools") or []
    mem = detail.get("memory") or {}
    skills_list = detail.get("skills") or []

    has_gateway = any(t.get("type") == "agentcore_gateway" for t in tools)
    has_kb = any("kb_search" in a for a in allowed)
    has_data = any("query_business_data" in a for a in allowed)
    has_memory = bool(mem.get("managedMemoryConfiguration"))
    has_skills = bool(skills_list)

    # policy 没有直接的字段，有工具且有 allowedTools 限制时视为已配置
    has_policy = has_gateway and bool(allowed)

    # 从 managedMemoryConfiguration 还原记忆配置
    mem_cfg = mem.get("managedMemoryConfiguration", {})
    memory_block: dict = {}
    if has_memory:
        memory_block = {
            "mode": "managed",
            "strategies": mem_cfg.get("strategies", []),
            "event_expiry_days": mem_cfg.get("eventExpiryDuration", 60),
            "actor_dimension": "counterparty",
        }

    # 从 skills 列表还原 S3 URI
    skill_uris = [s["s3"]["uri"] for s in skills_list if s.get("s3", {}).get("uri")]
    skill_names = [uri.rstrip("/").split("/")[-1] for uri in skill_uris]
    skills_block: dict = {}
    if has_skills and skill_uris:
        bucket = skill_uris[0].split("/")[2]  # s3://bucket/... → bucket
        skills_block = {"bucket": bucket, "uris": skill_uris, "names": skill_names}

    # 推导当前 phase（按能力叠加顺序）
    phase = 1
    if has_gateway:
        phase = 3
    if has_memory:
        phase = 4
    if has_policy:
        phase = 5
    if has_skills:
        phase = 6

    state: dict = {
        "region": region,
        "harness_name": HARNESS_NAME,
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "model_id": DEFAULT_MODEL_ID,
        "phase": phase,
        "capabilities": {
            "tools": has_gateway,
            "knowledge_base": has_kb,
            "business_data": has_data,
            "memory": has_memory,
            "policy": has_policy,
            "skills": has_skills,
        },
    }
    if allowed:
        state["allowed_tools"] = allowed
    if memory_block:
        state["memory"] = memory_block
    if skills_block:
        state["skills"] = skills_block

    return state


def main() -> int:
    env = load_env_state()
    region = env["region"]
    role_arn = env.get("execution_role_arn")

    if not role_arn:
        raise SystemExit(
            "环境快照里没有 execution_role_arn。\n"
            "请先部署 template-opc.yaml，然后重跑 scripts/01_check_env.py；\n"
            "或手工把栈输出的 HarnessExecutionRoleArn 填进 .env_state.json 的 "
            "execution_role_arn 字段。"
        )

    print("=" * 60)
    print("Harness · 创建或更新系统提示词")
    print("=" * 60)
    print(f"\n区域        {region}")
    print(f"执行角色    {role_arn}")
    print(f"模型        {DEFAULT_MODEL_ID}")

    control = boto3.client("bedrock-agentcore-control", region_name=region)
    existing = find_existing(control, HARNESS_NAME)

    if existing:
        harness_id = existing.get("harnessId") or existing.get("id")
        print(f"\n✓ 已存在 Harness：{harness_id}")

        # 等待就绪（上一轮若中断在 UPDATING，直接用会报错）
        detail = wait_ready(control, harness_id)

        # 检查模型是否需要切换
        current_model = (detail.get("model") or {}).get("bedrockModelConfig", {}).get("modelId")
        if current_model != DEFAULT_MODEL_ID:
            print(f"  模型不一致（{current_model} → {DEFAULT_MODEL_ID}），同步切换...")

        # 始终推送最新 SYSTEM_PROMPT；模型不一致时一并更新模型。
        # 只传需要变更的字段——其余字段（tools/allowedTools/memory/skills）
        # 不传则服务端保持原值，不会被清空。
        update_kwargs: dict = {"harnessId": harness_id,
                               "systemPrompt": [{"text": SYSTEM_PROMPT}]}
        if current_model != DEFAULT_MODEL_ID:
            update_kwargs["model"] = {"bedrockModelConfig": {"modelId": DEFAULT_MODEL_ID}}

        control.update_harness(**update_kwargs)
        detail = wait_ready(control, harness_id)
        print("✓ SYSTEM_PROMPT 已推送")

    else:
        print(f"\n创建 Harness：{HARNESS_NAME} ...")
        created = control.create_harness(
            harnessName=HARNESS_NAME,
            executionRoleArn=role_arn,
            model={"bedrockModelConfig": {"modelId": DEFAULT_MODEL_ID}},
            systemPrompt=[{"text": SYSTEM_PROMPT}],
            # baseline：明确关掉工具和记忆，避免干扰 Phase 1 评测基准
            tools=[],
            memory={"disabled": {}},
        )
        created = created.get("harness", created)
        harness_id = created.get("harnessId") or created.get("id")
        print(f"  已提交，harnessId={harness_id}，等待就绪...")
        detail = wait_ready(control, harness_id)
        print("✓ Harness 就绪")

    harness_arn = detail.get("arn") or detail.get("harnessArn")
    if not harness_arn:
        raise RuntimeError(
            f"Harness 已就绪但取不到 ARN。返回字段：{list(detail.keys())}"
        )

    # 从服务端实际配置推导状态，避免覆盖已叠加的组件
    state = derive_state_from_detail(detail, region, harness_id, harness_arn)
    save_json(HARNESS_STATE_FILE, state)

    print(f"\nHarness ARN  {harness_arn}")
    print(f"Phase        {state['phase']}")
    caps = state["capabilities"]
    print(f"Capabilities tools={caps['tools']} kb={caps['knowledge_base']} "
          f"data={caps['business_data']} memory={caps['memory']} "
          f"policy={caps['policy']} skills={caps['skills']}")
    print(f"✓ 状态已同步写入 {HARNESS_STATE_FILE}")

    if state["phase"] == 1:
        print("\n下一步（首次创建）：")
        print("  python scripts/03_chat.py              交互对话")
        print("  python scripts/04_run_evalset.py       跑评测集，记录 baseline 分数")
    else:
        print("\n下一步：")
        print("  python scripts/04_run_evalset.py       重跑评测")

    return 0


if __name__ == "__main__":
    sys.exit(main())
