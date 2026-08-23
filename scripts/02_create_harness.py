"""
Phase 1 · 创建 baseline Harness
--------------------------------------------------------
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
        # get_harness 返回 {"harness": {...}}，解包后再取字段。
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
    print("Phase 1 · 创建 baseline Harness（无工具 / 无记忆 / 无 skills）")
    print("=" * 60)
    print(f"\n区域        {region}")
    print(f"执行角色    {role_arn}")
    print(f"模型        {DEFAULT_MODEL_ID}")

    control = boto3.client("bedrock-agentcore-control", region_name=region)

    existing = find_existing(control, HARNESS_NAME)
    if existing:
        harness_id = existing.get("harnessId") or existing.get("id")
        print(f"\n✓ 复用已存在的 Harness：{harness_id}")
        # 复用也要轮询到 READY：上一轮若中断在 UPDATING / CREATE_FAILED，
        # 直接用非就绪的 detail 会导致后续 invoke 拿到含糊的运行时错误。
        detail = wait_ready(control, harness_id)

        # 模型可能在换模型后与当前 DEFAULT_MODEL_ID 不一致（比如从 Claude 切到
        # Nova）。复用的旧 Harness 不会自动换模型——invoke 时仍调旧模型，照样报错。
        # 这里检测到不一致就用一次 UpdateHarness 把模型切过来，重建 baseline 配置。
        current_model = (detail.get("model") or {}).get("bedrockModelConfig", {}).get("modelId")
        if current_model != DEFAULT_MODEL_ID:
            print(f"  模型不一致（当前 {current_model} → 目标 {DEFAULT_MODEL_ID}），"
                  f"UpdateHarness 切换...")
            control.update_harness(
                harnessId=harness_id,
                model={"bedrockModelConfig": {"modelId": DEFAULT_MODEL_ID}},
                systemPrompt=[{"text": SYSTEM_PROMPT}],
                tools=[],
                allowedTools=[],
                memory={"optionalValue": {"disabled": {}}},
            )
            detail = wait_ready(control, harness_id)
            print("✓ 已切换为 baseline 配置（新模型）")
        else:
            print("  （如需重置为 baseline 配置，运行 UpdateHarness 或删除后重建）")
    else:
        print(f"\n创建 Harness：{HARNESS_NAME} ...")
        created = control.create_harness(
            harnessName=HARNESS_NAME,
            executionRoleArn=role_arn,
            model={"bedrockModelConfig": {"modelId": DEFAULT_MODEL_ID}},
            # systemPrompt 是内容块数组，不是裸字符串
            systemPrompt=[{"text": SYSTEM_PROMPT}],
            # baseline 明确关掉这两项：
            #   tools=[]  连内置 shell / file_operations 都不给，
            #             省掉每次请求约 900 个工具定义 token，也杜绝模型
            #             拿 shell 去"算"业务数据造成的干扰
            #   memory    关闭，避免上一轮对话污染下一条评测用例
            tools=[],
            memory={"disabled": {}},
        )
        # create_harness / get_harness 的返回都把 Harness 包在 "harness" 键里，
        # 必须解包才能拿到 arn / status 等字段。
        created = created.get("harness", created)
        harness_id = created.get("harnessId") or created.get("id")
        print(f"  已提交，harnessId={harness_id}，等待就绪...")
        detail = wait_ready(control, harness_id)
        print("✓ Harness 就绪")

    # wait_ready 已把 {"harness": {...}} 解包，ARN 字段是小写的 arn。
    harness_arn = detail.get("arn") or detail.get("harnessArn")
    if not harness_arn:
        raise RuntimeError(
            f"Harness 已就绪但取不到 ARN。get_harness 返回的字段："
            f"{list(detail.keys())}。请把这段内容贴出来排查字段名。"
        )

    state = {
        "region": region,
        "harness_name": HARNESS_NAME,
        "harness_id": harness_id,
        "harness_arn": harness_arn,
        "model_id": DEFAULT_MODEL_ID,
        "phase": 1,
        "capabilities": {
            "tools": False,
            "knowledge_base": False,
            "business_data": False,
            "memory": False,
            "policy": False,
            "skills": False,
        },
    }
    save_json(HARNESS_STATE_FILE, state)

    print(f"\nHarness ARN  {harness_arn}")
    print(f"✓ 状态已写入 {HARNESS_STATE_FILE}")
    print("\n下一步：")
    print("  python scripts/03_chat.py              交互对话")
    print("  python scripts/04_run_evalset.py       跑评测集，记录 baseline 分数")
    return 0


if __name__ == "__main__":
    sys.exit(main())
