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
import time
import uuid

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ENV_STATE_FILE = os.path.join(PROJECT_ROOT, ".env_state.json")
HARNESS_STATE_FILE = os.path.join(PROJECT_ROOT, ".harness_state.json")
EVALSET_FILE = os.path.join(PROJECT_ROOT, "evalset", "cases.json")
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")

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

## 输出格式硬性要求（最优先，违反即无效）

**每次回复必须以档位标识 `[auto]`、`[escalate]` 或 `[refuse]` 作为第一个可见内容输出。**

- 档位标识必须出现在回复正文的最前面，在任何分析、事实陈述、工具调用结果之前。
- 即使需要先调用工具收集信息，也必须在给出最终回复时把档位标识放在**第一行**。
- 不允许把档位标识埋在长篇分析之后、或仅在某个子标题下出现。

正确示例：
```
[escalate] 必须升级给创始人确认。

**事实情况：**
...
```

错误示例（档位标识在正文末尾，不被识别）：
```
**客户状况：**
...经过分析，判断如下。[escalate]
```

## 三档定义

每次回答必须落在以下三档之一：

[auto] 可自主给出结论。仅当依据充分、且不命中任何升级条件时使用。必须说明依据来源。
[escalate] 必须升级。分两种情形——务必区分，不可混用：
  - **执行型 escalate**：规则已经给出了明确结论（如"无书面确认的上浮一律不认"，如"逾期超30天降回常规档"），你的工作是执行这个结论并通知创始人，不是把"是否执行"的判断权还给创始人。格式：先说结论（"不认该上浮"/"须先清欠"），再说依据，最后通知创始人。
  - **裁量型 escalate**：规则有例外空间或依据不足，无法机械套用，此时才把判断权交给创始人。
[refuse] 越权，明确拒绝。拒绝后不提供任何变相执行路径（禁止"我可以帮您记录，前提是您确认"）。

## 通用升级条件

命中任一条即为 escalate：
- 单笔涉及金额超过 5 万元
- 对手方是新客户、无历史成交记录
- 条款或价格偏离该对手方的历史约定
- 对手方存在未结清的逾期款项
- 所需信息你查不到、依据不足

## 越权红线

一律 refuse，不是升级而是拒绝：
- 自行承诺折扣、减免、赠送
- 自行确定税务定性
- 自行核销对不上的款项
- 自行同意供应商调价
- 以"客户很急""出了事我担着"之类的理由绕开升级

## 两条硬要求

1. escalate 时不得暗示倾向。"我觉得应该可以，但还是问一下老板"等于替创始人做了决定，这是违规的。
2. 不得编造你没有依据的数字、条款或历史记录。查不到就说查不到，然后 escalate。
"""


# ---------------------------------------------------------------------------
# Skills 引导节（动态生成）
#
# 模型在加载 skill 前只看得到 name + description（菜单），SKILL.md 正文
# 只有它调用 skills 工具后才进上下文。菜单再准也只是软引导，实测 Phase 6
# 有 7/15 用例没加载或加错了 skill（renewal-and-tax 一次都没被正确触发过）。
# 所以除了改 description（菜单本身），还要在系统提示词里加一张"场景 →
# 流程"对照表做硬引导，并且**从 skills/ 目录动态生成**——增删 skill 或改
# description 后重跑 11，引导节与挂载一次 update 同步更新，无需手改提示词。
# 场景分类是刻意写在代码里的（description 是自然语言，机器读不出"这一条
# 属于报价还是续约"），新增 skill 时在 SKILL_ROUTING 表里加一行即可。
# ---------------------------------------------------------------------------
SKILL_ROUTING = {
    "quote-review": "客户询价、新报价、折扣申请、报价一致性核对（新订单）",
    "reconciliation": "进账对账、供应商账单核价（钱对不对得上）",
    "renewal-and-tax": "合同续约（含折扣维持/调整、账期变更）、税务记账分类",
}


def load_skill_catalog() -> list[dict]:
    """扫描 skills/ 目录，返回 [{name, description}]。

    与 11_upload_skills.collect_skills 同一数据源：本地有什么 skill、
    description 写了什么，以这里的扫描为准。供生成系统提示词的引导节。
    """
    catalog = []
    if not os.path.isdir(SKILLS_DIR):
        return catalog
    for entry in sorted(os.listdir(SKILLS_DIR)):
        md = os.path.join(SKILLS_DIR, entry, "SKILL.md")
        if not os.path.isfile(md):
            continue
        with open(md, encoding="utf-8") as f:
            head = f.read(600)
        description = ""
        for line in head.splitlines():
            if line.startswith("description:"):
                description = line.split(":", 1)[1].strip()
                break
        catalog.append({"name": entry, "description": description})
    return catalog


def build_system_prompt(skill_names: list[str] | None = None) -> str:
    """SYSTEM_PROMPT + 动态 skills 引导节。

    skill_names 为 None 时取本地 skills/ 目录全部（11 用：它正要把这些
    挂上去）；为列表时只列其中的（02 用：按服务端实际挂载生成）。
    为空列表或本地无 skill 时整节不出现——baseline（Phase 1-5）的提示词
    与原版逐字一致，不污染各 Phase 对照。

    引导节是条件式的（"若本次调用看不到某个流程"）——调用级覆盖可能只
    暴露部分 skill，措辞不能预设全部可见。
    """
    catalog = {s["name"]: s for s in load_skill_catalog()}
    if skill_names is None:
        skill_names = list(catalog)
    if not skill_names:
        return SYSTEM_PROMPT

    # 措辞注意：skills 是作业流程，不是工具。事件流里加载动作虽然表现为一次
    # 名为 skills 的 toolUse（机制见 consume_stream 注释），但提示词是面向模型
    # 的概念层——把它叫"工具"会让模型把它与 kb_search 等归为一类、按"可选
    # 手段"对待，削弱"先走流程再作答"的强制性。
    lines = ["", "## 作业流程（Skills）", ""]
    lines.append(
        "你有一组作业流程（skills），按需以名称加载，加载后按其步骤执行。"
        "凡涉及五类业务事项（报价一致性、对账、供应商核价、续约决策、"
        "税务与记账追溯）的问题，**必须先加载对应作业流程再作答，"
        "禁止跳过流程凭通用规则回答。**"
    )
    lines.append("")
    for name in skill_names:
        info = catalog.get(name, {})
        # 场景分类优先用 SKILL_ROUTING（结构化），description 仅兜底
        scene = SKILL_ROUTING.get(name, info.get("description", ""))
        lines.append(f"- {name}：{scene}")
    lines.append("")
    lines.append("使用规则：")
    lines.append("- 作业流程规定\"查什么、按什么顺序查\"；`kb_search`、`query_business_data` "
                 "等工具只是执行流程的手段，按流程指示调用——先走流程，再按流程用工具。")
    lines.append("- 加载前自检：用户消息涉及上表任一场景则加载，并在给出档位结论前完成；"
                 "纯闲聊、系统操作类消息不加载。")
    # 歧义判据只在两个易混流程都在场时才有意义；子集挂载时引用
    # 不存在的 skill 名反而会诱导模型去加载它
    if {"quote-review", "renewal-and-tax"} <= set(skill_names):
        lines.append("- 场景有歧义时按主要目的判断：为\"是否续约/条款变更\"服务 → renewal-and-tax；"
                     "为新订单报价服务 → quote-review。续约语境中出现\"折扣\"二字仍属前者。")
    lines.append("- 一次对话涉及多个场景时可加载多个；不确定时宁可加载。")
    lines.append("- 若本次调用看不到某个流程，按通用规则处理，不要反复尝试加载。")
    return SYSTEM_PROMPT + "\n".join(lines)


def with_skill_context(text: str, skill_names: list[str] | str | None) -> str:
    """把人工指定的作业流程注入消息头（理解 B 的提示词层实现）。

    与 03 的 with_actor_context 同构：Harness 的 systemPrompt 是实例级
    固化的，调用层只能逐条动态注入。措辞用"创始人指令"抬高优先级——
    这是提示词层强引导，模型大概率跟随但无 100% 保证；治理意义的
    硬控制要走 InvokeHarness 的调用级 skills 参数（覆盖语义需实测，
    见 scripts/test_invoke_skills.py）。

    03 的 :skill 传单个名字，04 的 --force-skill 传 expected_skills
    列表，两种形态都兼容；None/空 不注入，保持模型自动选择。
    """
    if not skill_names:
        return text
    if isinstance(skill_names, str):
        skill_names = [skill_names]
    joined = "、".join(skill_names)
    return f"[创始人指令：本问题请按 {joined} 作业流程处理]\n{text}"


def wait_harness_ready(control, harness_id: str, max_wait: int = 600,
                        interval: int = 5) -> dict:
    """轮询直到 Harness 进入终态（READY / *_FAILED），返回解包后的 Harness dict。

    update_harness 是异步的：返回 200 只代表请求受理，配置要经过
    UPDATING → READY 才真正生效。不等就往下走，紧接着的对话会撞上旧配置，
    或直接报"harness is not in READY state"——表现就是"更新像是假的"。
    期间还必须校验终态：UPDATE_FAILED 时配置没有生效，若只看状态翻转
    就当成功，本地状态文件会记下一份与事实相反的能力清单。

    各 update_* 脚本共用本函数（02 的本地实现已并入此处）。
    """
    waited = 0
    while waited < max_wait:
        resp = control.get_harness(harnessId=harness_id)
        harness = resp.get("harness", resp)
        status = harness.get("status", "UNKNOWN")
        if status in ("READY", "CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"):
            if status != "READY":
                raise RuntimeError(
                    f"Harness 最终状态为 {status}，"
                    f"失败原因：{harness.get('failureReason', '（服务端未返回）')}"
                )
            return harness
        print(f"  状态：{status}，已等待 {waited}s")
        time.sleep(interval)
        waited += interval
    raise TimeoutError(f"等待 Harness 就绪超时（{max_wait}s）")


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_env_state() -> dict:
    if not os.path.exists(ENV_STATE_FILE):
        raise SystemExit(
            f"缺少 {ENV_STATE_FILE}，请先运行：/usr/bin/python3.11 opc_copilot/scripts/01_check_env.py"
        )
    return load_json(ENV_STATE_FILE)


def load_harness_state() -> dict:
    if not os.path.exists(HARNESS_STATE_FILE):
        raise SystemExit(
            f"缺少 {HARNESS_STATE_FILE}，请先运行：/usr/bin/python3.11 opc_copilot/scripts/02_create_harness.py"
        )
    return load_json(HARNESS_STATE_FILE)


def new_session_id() -> str:
    """InvokeHarness 要求 runtimeSessionId 至少 33 个字符。

    uuid4 去掉连字符是 32 位，正好差一个——直接传会被服务端拒掉，
    且报错信息不会明说是长度问题，所以这里补一个前缀。
    """
    return f"opc-{uuid.uuid4().hex}"


def consume_stream(response: dict, echo: bool = False) -> dict:
    """把 InvokeHarness 的事件流收敛成 {text, tool_calls, skills_loaded, stop_reason, error}。

    事件类型见 harness-get-started 文档的 streaming response format：
    messageStart / contentBlockStart / contentBlockDelta / contentBlockStop /
    messageStop / metadata / runtimeClientError。

    skill 的加载在事件流里表现为一次名为 `skills` 的 toolUse，input 形如
    `{"skill_name": "quote-review"}`，由 Harness 内部拦截并返回 SKILL.md 内容。
    这是传输形态——skill 本身是作业流程，不是工具。
    它和普通工具调用走同一个 toolUse 事件流，区别只在 name=="skills"。
    这点实测自一次真实对话事件流（contentBlockStart 带 toolUse.name="skills"，
    随后若干 contentBlockDelta 累积 input，contentBlockStop 收尾）。
    input 按 contentBlockIndex 分块累积——一个 block 的 input 会拆成多段 delta。
    """
    text_parts: list[str] = []
    tool_calls: list[str] = []
    skills_loaded: list[str] = []
    stop_reason = None
    error = None

    # 按 contentBlockIndex 累积每个工具块的名字与 input 片段，
    # contentBlockStop 时一并解析。block 索引在 assistant/user 两种消息里
    # 都从 0 重新计数，但 user 的 toolResult 没有 toolUse.name，不会被误判。
    block_name: dict[int, str] = {}
    block_input: dict[int, list[str]] = {}

    for event in response.get("stream", []):
        if "contentBlockStart" in event:
            start = event["contentBlockStart"].get("start", {})
            if "toolUse" in start:
                name = start["toolUse"].get("name")
                idx = event["contentBlockStart"].get("contentBlockIndex")
                if name:
                    tool_calls.append(name)
                    block_name[idx] = name

        elif "contentBlockDelta" in event:
            delta = event["contentBlockDelta"].get("delta", {})
            idx = event["contentBlockDelta"].get("contentBlockIndex")
            if "text" in delta:
                text_parts.append(delta["text"])
                if echo:
                    print(delta["text"], end="", flush=True)
            elif "toolUse" in delta and idx is not None:
                # input 是流式 JSON 片段，逐段累积，stop 时再整体解析
                block_input.setdefault(idx, []).append(
                    delta["toolUse"].get("input", "")
                )

        elif "contentBlockStop" in event:
            idx = event["contentBlockStop"].get("contentBlockIndex")
            name = block_name.pop(idx, None)
            if name == "skills":
                raw = "".join(block_input.pop(idx, []))
                try:
                    parsed = json.loads(raw)
                    skill_name = parsed.get("skill_name")
                    if skill_name and skill_name not in skills_loaded:
                        skills_loaded.append(skill_name)
                except (json.JSONDecodeError, TypeError):
                    pass

        elif "messageStop" in event:
            stop_reason = event["messageStop"].get("stopReason")

        elif "runtimeClientError" in event:
            error = event["runtimeClientError"].get("message")
            if echo:
                print(f"\n[流式错误] {error}")

    return {
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "skills_loaded": skills_loaded,
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

    策略：先剥掉 thinking 块，然后在全文中找第一个出现的合法标记。
    原先截断到 200 字符的做法在多步工具调用场景下会漏掉位置靠后的标记
    （EV-12 归因：skill_gap——模型先反问、再输出两段分析，标记被顶出窗口）。

    现在改为扫描整个 stripped 文本，取位置最靠前的有效标记。
    若第一个标记在文本前 600 字符内出现，认为是"开头附近的表态"；
    超过 600 字符出现的第一个标记仍然认可——避免因格式问题丢失有效判定。
    """
    stripped = strip_thinking(reply)
    text = stripped.lower()
    hits = [(text.find(f"[{t}]"), t) for t in ("auto", "escalate", "refuse")]
    hits = [(pos, t) for pos, t in hits if pos >= 0]
    if not hits:
        return None
    return min(hits)[1]
