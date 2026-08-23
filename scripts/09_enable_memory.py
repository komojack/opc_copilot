"""
Phase 4 · 打开 Memory，按交易对手方隔离
--------------------------------------------------------
运行：
    python scripts/09_enable_memory.py
    python scripts/09_enable_memory.py --disable    关掉，回到无记忆状态
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import HARNESS_STATE_FILE, load_harness_state, save_json

# SEMANTIC       从对话里抽事实（给这家报过什么价、谈过什么条款）
# SUMMARIZATION  会话滚动摘要，按 actor + session 存
# USER_PREFERENCE 抽偏好——这里承载"创始人对这个对手方的处理惯例"
#
# 不开 EPISODIC：它抽的是"经验反思"，抽取延迟明显更高，
# 而本场景的价值主要在事实与惯例，不在反思。
MEMORY_STRATEGIES = ["SEMANTIC", "SUMMARIZATION", "USER_PREFERENCE"]
EVENT_EXPIRY_DAYS = 60


def main() -> int:
    parser = argparse.ArgumentParser(description="开关 Harness 托管 Memory")
    parser.add_argument("--disable", action="store_true", help="关闭 Memory")
    args = parser.parse_args()

    state = load_harness_state()
    region = state["region"]
    harness_id = state["harness_id"]

    control = boto3.client("bedrock-agentcore-control", region_name=region)

    print("=" * 60)
    print("Phase 4 · Memory")
    print("=" * 60)

    if args.disable:
        print("\n关闭 Memory ...")
        # UpdateHarness 的 memory 字段要包一层 optionalValue，
        # 与 CreateHarness 的裸结构不同，直接照搬 create 的形状会报参数校验错
        control.update_harness(
            harnessId=harness_id,
            memory={"optionalValue": {"disabled": {}}},
        )
        state["capabilities"]["memory"] = False
        save_json(HARNESS_STATE_FILE, state)
        print("✓ Memory 已关闭")
        return 0

    print(f"\nHarness    {harness_id}")
    print(f"策略       {', '.join(MEMORY_STRATEGIES)}")
    print(f"事件保留   {EVENT_EXPIRY_DAYS} 天")
    print("隔离维度   actorId = 交易对手方编号")

    print("\n提交 UpdateHarness ...")
    control.update_harness(
        harnessId=harness_id,
        memory={"optionalValue": {"managedMemoryConfiguration": {
            "strategies": MEMORY_STRATEGIES,
            "eventExpiryDuration": EVENT_EXPIRY_DAYS,
        }}},
    )
    print("✓ 已提交")

    # Memory 实例创建后有几分钟才 ACTIVE，这段时间里对话会表现为"记不住"。
    # 这里等一下并给出提示，免得被误判为配置错误。
    print("\n托管 Memory 实例创建中，通常需要 2-5 分钟才会 ACTIVE。")
    for i in range(6):
        time.sleep(10)
        sys.stdout.write(f"\r  已等待 {(i + 1) * 10}s ...")
        sys.stdout.flush()
    print("\n  （若随后对话表现为记不住，再等几分钟重试，不必改配置）")

    state["capabilities"]["memory"] = True
    state["phase"] = 4
    state["memory"] = {
        "mode": "managed",
        "strategies": MEMORY_STRATEGIES,
        "event_expiry_days": EVENT_EXPIRY_DAYS,
        "actor_dimension": "counterparty",
    }
    save_json(HARNESS_STATE_FILE, state)
    print(f"\n✓ 状态已更新 {HARNESS_STATE_FILE}（phase=4）")

    print("\n验证隔离的手动步骤：")
    print("  python scripts/03_chat.py CLIENT-ABC")
    print('    > 记一下，ABC 这家以后一律要求预付 50%')
    print("    > :actor CLIENT-BLUE")
    print('    > 这家客户有什么特殊约定吗？        ← 不应该知道 ABC 的事')
    print("    > :actor CLIENT-ABC")
    print('    > 这家客户有什么特殊约定吗？        ← 应该记得预付 50%')
    print("\n然后跑评测：")
    print("  python scripts/04_run_evalset.py                  用例间互相隔离（默认）")
    print("  python scripts/04_run_evalset.py --no-isolate     让记忆跨用例累积")
    return 0


if __name__ == "__main__":
    sys.exit(main())
