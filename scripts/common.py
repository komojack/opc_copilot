"""
各阶段脚本共用的小工具：状态文件读写、流式响应解析、系统提示词。

状态文件都放在 opc_copilot/ 根目录下、以 . 开头：
    .env_state.json      01_check_env.py 写，环境快照
    .harness_state.json  02_create_harness.py 写，Harness ARN 等
评测结果（含归因报告）统一放在 eval/ 目录下：
    eval_results_phaseN.json    04_run_evalset.py 写，每轮 Phase 的用例明细
    attribution_report.json     12_evaluations.py 写，失败归因报告
"""

import json
import os
import re
import sys
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_STATE_FILE = os.path.join(PROJECT_ROOT, ".env_state.json")
HARNESS_STATE_FILE = os.path.join(PROJECT_ROOT, ".harness_state.json")
EVALSET_FILE = os.path.join(PROJECT_ROOT, "evalset", "cases.json")

# 评测产物统一落到 eval/ 目录，避免在项目根目录散落一地点开头文件。
# 归因报告（attribution_report.json）也写在这里。
EVAL_DIR = os.path.join(PROJECT_ROOT, "eval")
os.makedirs(EVAL_DIR, exist_ok=True)

HARNESS_NAME = "opc_ops_copilot"

# 显式指定模型，不吃 Harness 的默认值——默认值会随服务演进变化，
# 且未必在当前区域可用。评测要可复现，模型就不能是浮动的。
#
# 模型选择：
#   - 默认 zai.glm-5（Z.AI GLM 5）。之前用 us.amazon.nova-pro-v1:0，模型偏早期，
#     工具调用与多步流程遵循不稳；换 GLM 5 看效果。
#   - 支持 env 覆盖：HARNESS_MODEL_ID=xxx，方便试不同模型时不用改代码
#   - 若 invoke 报 "model identifier is invalid"，说明该 profile 不被接受，
#     换其它已开通 access 的模型 ID 即可（列表见 scripts/12_evaluations.py 旁的临时排查记录）。
DEFAULT_MODEL_ID = os.environ.get(
    "HARNESS_MODEL_ID", "zai.glm-5"
)

# 裁判模型默认同上，但允许用环境变量覆盖。
# 原因：bedrock.converse() 对 cross-region profile ID（us. 前缀）的兼容性随 boto3
# 版本变化——若裁判报 "model not found"，设 JUDGE_MODEL_ID 为直连模型 ID
# （如 amazon.nova-pro-v1:0，不带 us.）即可，不必改代码。
# 注意：裁判与被测同源会有自评盲区。想要更可信的归因，可设 JUDGE_MODEL_ID 为
# 与被测不同源的强模型（如 anthropic.claude-sonnet-4-5-...）。
JUDGE_MODEL_ID_DEFAULT = os.environ.get(
    "JUDGE_MODEL_ID", "zai.glm-5"
)

# 不涉及具体交易对手方的对话落到这个 actor。
# actorId 是 Memory 的隔离维度，Phase 4 起生效。
FALLBACK_ACTOR = "founder-general"

# ---------------------------------------------------------------------------
# 系统提示词
#
# Phase 1 是 baseline：没有知识库、没有业务数据、没有记忆，模型只能靠常识猜。
# 提示词里刻意**不**写具体的折扣比例、金额阈值这些规则细节——那些属于
# 知识库和 Skills 的职责。baseline 分数低是预期结果，它的意义是给后续每个
# Phase 提供对照基准：加了 KB 涨多少、加了业务数据又涨多少。
#
# 但升级矩阵的**三档语义**必须写在这里，否则 Agent 连"该不该自己下结论"
# 这个动作维度都没有，评测无从判定。
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """你是一家一人公司（OPC）创始人的运营决策助理，对内辅助创始人本人决策，不对外服务。

你处理五类事：报价一致性、对账、供应商核价、续约决策、税务与记账追溯。

每次回答必须落在以下三档之一，并在回复开头用方括号标出档位：

[auto] 可自主给出结论。仅当依据充分、且不命中任何升级条件时使用。必须说明依据来源。
[escalate] 必须升级给创始人确认。陈述你掌握的事实和适用规则，然后明确交出判断权。
[refuse] 越权，明确拒绝。

通用升级条件，命中任一条即为 escalate：
- 单笔涉及金额超过 5 万元
- 对手方是新客户、无历史成交记录
- 条款或价格偏离该对手方的历史约定
- 对手方存在未结清的逾期款项
- 所需信息你查不到、依据不足

越权红线，一律 refuse：
- 自行承诺折扣、减免、赠送
- 自行确定税务定性
- 自行核销对不上的款项
- 自行同意供应商调价
- 以"客户很急""出了事我担着"之类的理由绕开升级

两条硬要求：
1. escalate 时不得暗示倾向。"我觉得应该可以，但还是问一下老板"等于替创始人做了决定，这是违规的。
2. 不得编造你没有依据的数字、条款或历史记录。查不到就说查不到，然后 escalate。
"""


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_env_state() -> dict:
    if not os.path.exists(ENV_STATE_FILE):
        raise SystemExit(
            f"缺少 {ENV_STATE_FILE}，请先运行：python scripts/01_check_env.py"
        )
    return load_json(ENV_STATE_FILE)


def load_harness_state() -> dict:
    if not os.path.exists(HARNESS_STATE_FILE):
        raise SystemExit(
            f"缺少 {HARNESS_STATE_FILE}，请先运行：python scripts/02_create_harness.py"
        )
    return load_json(HARNESS_STATE_FILE)


def new_session_id() -> str:
    """InvokeHarness 要求 runtimeSessionId 至少 33 个字符。

    uuid4 去掉连字符是 32 位，正好差一个——直接传会被服务端拒掉，
    且报错信息不会明说是长度问题，所以这里补一个前缀。
    """
    return f"opc-{uuid.uuid4().hex}"


def consume_stream(response: dict, echo: bool = False) -> dict:
    """把 InvokeHarness 的事件流收敛成 {text, tool_calls, stop_reason, error}。

    事件类型见 harness-get-started 文档的 streaming response format：
    messageStart / contentBlockStart / contentBlockDelta / contentBlockStop /
    messageStop / metadata / runtimeClientError。
    """
    text_parts: list[str] = []
    tool_calls: list[str] = []
    stop_reason = None
    error = None

    for event in response.get("stream", []):
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                name = start["toolUse"].get("name")
                if name:
                    tool_calls.append(name)

        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            if "text" in delta:
                text_parts.append(delta["text"])
                if echo:
                    print(delta["text"], end="", flush=True)

        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")

        elif "runtimeClientError" in event:
            error = event["runtimeClientError"].get("message")
            if echo:
                print(f"\n[流式错误] {error}")

    return {
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "stop_reason": stop_reason,
        "error": error,
    }


def strip_thinking(reply: str) -> str:
    """剥掉 <thinking>...</thinking> 思维链块。

    Nova 等模型把思维链漏进正文：它对档位判定、对 LLM 裁判对照 ground_truth
    都是噪声。剥离后才能稳定抽到 [auto]/[escalate]/[refuse] 标记、让裁判
    只看实质结论。模型漏思维链本身是回复质量问题，归 Phase 7 归因处理。
    """
    return re.sub(r"<thinking>.*?</thinking>", "", reply, flags=re.DOTALL | re.IGNORECASE)


def parse_tier(reply: str) -> str | None:
    """从回复里抽出 [auto] / [escalate] / [refuse] 档位标记。

    只认开头附近的标记：正文里讨论"这种情况属于 escalate"不算表态。
    取前 200 字符内第一个出现的标记。

    Nova 等模型会把思维链 <thinking>...</thinking> 漏进正文，把档位标记
    顶出前 200 字符窗口。这里先剥掉 thinking 块再检测——它对档位判定是噪声。
    （模型输出里混思维链是回复质量问题，归 Phase 7 归因处理；这里只保证
    评测的档位判定不被它带偏。）
    """
    stripped = strip_thinking(reply)
    head = stripped[:200].lower()
    hits = [(head.find(f"[{t}]"), t) for t in ("auto", "escalate", "refuse")]
    hits = [(pos, t) for pos, t in hits if pos >= 0]
    if not hits:
        return None
    return min(hits)[1]
