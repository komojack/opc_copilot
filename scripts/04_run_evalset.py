"""
Phase 1 · 跑评测集，记录 Harness分数
"""

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import (
    DEFAULT_MODEL_ID,
    EVALSET_FILE,
    EVAL_DIR,
    JUDGE_MODEL_ID_DEFAULT,
    consume_stream,
    load_harness_state,
    load_json,
    new_session_id,
    parse_tier,
    save_json,
    strip_thinking,
    with_skill_context,
)

# 裁判模型：JUDGE_MODEL_ID_DEFAULT 已在 common.py 里支持 env 覆盖。
JUDGE_MODEL_ID = JUDGE_MODEL_ID_DEFAULT

# 用例之间稍作停顿，避免连续 15 次调用触发限流
INTER_CASE_DELAY = 1.0

JUDGE_PROMPT = """你在核对一个一人公司运营决策助理的回答是否与创始人当时的真实决策一致。

创始人当时的真实决策（标准答案）：
{ground_truth}

助理的实际回答：
{reply}

判断标准：只看**实质结论和依据**是否与标准答案一致，不看措辞、长度、语气。

打 1 分的条件（必须同时满足）：
- 实质结论与标准答案一致
- 给出的关键依据（如金额、时长、逾期情况、适用规则）与标准答案不矛盾
- 没有编造标准答案里不存在的数字、条款或历史记录

打 0 分的情形（任一即 0）：
- 结论与标准答案相反或实质不同
- 编造了具体数字或条款（助理没有数据源时尤其要警惕这一点）
- 只是含糊地"建议咨询"，没有触及标准答案的实质理由

只输出 JSON，不要任何其他文字：
{{"score": 0 或 1, "reason": "一句话说明"}}"""


def judge(bedrock, ground_truth: str, reply: str) -> dict:
    """LLM 裁判：对照 ground_truth 给 0/1 分。"""
    if not reply.strip():
        return {"score": 0, "reason": "回复为空"}

    try:
        resp = bedrock.converse(
            modelId=JUDGE_MODEL_ID,
            messages=[{
                "role": "user",
                "content": [{"text": JUDGE_PROMPT.format(
                    ground_truth=ground_truth, reply=reply
                )}],
            }],
            inferenceConfig={"maxTokens": 300, "temperature": 0},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
    except Exception as e:
        return {"score": None, "reason": f"裁判调用失败：{type(e).__name__}: {e}"}

    # 模型有时会用 ```json 包起来，剥掉再解析
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("{"):] if "{" in text else text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        return {"score": None, "reason": f"裁判输出无法解析：{text[:120]}"}
    try:
        parsed = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return {"score": None, "reason": f"裁判输出非合法 JSON：{text[:120]}"}

    return {
        "score": parsed.get("score"),
        "reason": parsed.get("reason", ""),
    }


def main() -> int:
    use_judge = "--no-judge" not in sys.argv
    # --force-skill：按 expected_skills 强制注入流程指令（理解 B 的对照模式）。
    # 用途是解耦两个变量：forced 与 auto 各跑一轮——
    #   forced ≫ auto    → 触发仍是瓶颈，提示词引导值得继续投入
    #   forced ≈ auto 且都差 → 问题在 SKILL.md 内容本身，改提示词没用
    # 注意 skill_hit 在 forced 模式下衡量的是"注入后模型是否服从加载"，
    # 不再是"自动触发是否准确"，两种模式的分数不可直接混比。
    force_skill = "--force-skill" in sys.argv

    state = load_harness_state()
    region = state["region"]
    harness_arn = state["harness_arn"]
    phase = state.get("phase", 1)
    caps = state.get("capabilities", {})

    # Memory 关着的时候 actorId 不产生任何持久化，隔离与否没区别，
    # 就不必给结果里的 actor 名加噪音了。
    memory_on = bool(caps.get("memory"))
    isolate = memory_on and "--no-isolate" not in sys.argv
    run_tag = uuid.uuid4().hex[:6]

    cases = load_json(EVALSET_FILE)["cases"]
    results_file = os.path.join(EVAL_DIR, f"eval_results_phase{phase}.json")

    client = boto3.client("bedrock-agentcore", region_name=region)
    bedrock = boto3.client("bedrock-runtime", region_name=region) if use_judge else None

    enabled = [k for k, v in caps.items() if v] or ["（无，baseline）"]

    print("=" * 60)
    print(f"评测 · Phase {phase}")
    print("=" * 60)
    print(f"已启用能力  {', '.join(enabled)}")
    print(f"用例数      {len(cases)}")
    print(f"LLM 裁判    {JUDGE_MODEL_ID if use_judge else '已关闭'}")
    if force_skill:
        print("Skill 模式  forced（按 expected_skills 注入流程指令，非自动触发）")
    if memory_on:
        print(f"Actor 隔离  {'开（每条用例空记忆起步）' if isolate else '关（记忆跨用例累积）'}")

    results = []
    for i, case in enumerate(cases, 1):
        cid = case["id"]
        expected_tier = case["expected_tier"]
        # 隔离时给 actorId 加一次性后缀，让长期记忆落到本次评测专属的命名空间
        actor_id = f"{case['actor_id']}__run{run_tag}" if isolate else case["actor_id"]

        print(f"\n[{i}/{len(cases)}] {cid} · {case['scenario']} · 期望 {expected_tier}")
        print(f"  问：{case['message']}")

        # forced 模式下把期望流程注进消息头（期望为空的用例不注入，照常自动）
        message = (with_skill_context(case["message"], case.get("expected_skills"))
                   if force_skill else case["message"])

        try:
            response = client.invoke_harness(
                harnessArn=harness_arn,
                runtimeSessionId=new_session_id(),
                actorId=actor_id,
                messages=[{"role": "user", "content": [{"text": message}]}],
            )
            outcome = consume_stream(response)
        except Exception as e:
            print(f"  ✗ 调用失败：{type(e).__name__}: {e}")
            results.append({
                **{k: case[k] for k in ("id", "scenario", "actor_id", "message", "expected_tier")},
                "reply": "",
                "actual_tier": None,
                "tier_hit": False,
                "tool_calls": [],
                "skills_loaded": [],
                "expected_tools": case.get("expected_tools", []),
                "expected_skills": case.get("expected_skills", []),
                "skill_hit": False,
                "judge_score": None,
                "judge_reason": f"调用失败：{type(e).__name__}: {e}",
                "ground_truth": case.get("ground_truth", ""),
                "error": str(e),
            })
            continue

        reply = outcome["text"]
        actual_tier = parse_tier(reply)
        tier_hit = actual_tier == expected_tier

        # 触发断言：期望的 skill 都加载了才算命中；模型多加载了别的
        # 不算失败（有的场景确实可能两个流程都相关）。
        expected_skills = case.get("expected_skills", [])
        skill_hit = all(s in outcome["skills_loaded"] for s in expected_skills)

        print(f"  档位：{actual_tier or '未标注'} {'✓' if tier_hit else '✗'}")
        if outcome.get("skills_loaded"):
            loaded = ', '.join(outcome['skills_loaded'])
            mark = '✓' if skill_hit else '✗'
            print(f"  skill{mark} {loaded}"
                  + (f"（期望 {', '.join(expected_skills)}）" if expected_skills else ""))
        print(f"  答：{reply[:110].replace(chr(10), ' ')}…")

        judged = {"score": None, "reason": "未启用裁判"}
        if use_judge:
            # 剥掉 thinking 块再给裁判：思维链不是给用户看的实质结论，
            # 混进去会让裁判误判"答非所问"。
            judged = judge(bedrock, case["ground_truth"], strip_thinking(reply))
            mark = {1: "✓", 0: "✗"}.get(judged["score"], "?")
            print(f"  依据：{mark} {judged['reason']}")

        results.append({
            **{k: case[k] for k in ("id", "scenario", "actor_id", "message", "expected_tier")},
            "reply": reply,
            "actual_tier": actual_tier,
            "tier_hit": tier_hit,
            "tool_calls": outcome["tool_calls"],
            "skills_loaded": outcome.get("skills_loaded", []),
            "expected_skills": expected_skills,
            "skill_hit": skill_hit,
            "expected_tools": case.get("expected_tools", []),
            "judge_score": judged["score"],
            "judge_reason": judged["reason"],
            "ground_truth": case["ground_truth"],
        })

        time.sleep(INTER_CASE_DELAY)

    # ---------------- 汇总 ----------------
    total = len(results)
    tier_hits = sum(1 for r in results if r["tier_hit"])
    judged_ok = sum(1 for r in results if r["judge_score"] == 1)
    judged_total = sum(1 for r in results if r["judge_score"] in (0, 1))

    # 工具轨迹：期望调用的工具是否都调了。
    # 网关暴露的工具名带 target 前缀（opctools___kb_search），所以用子串匹配。
    # 只有在工具已放开的 Phase 才有意义，baseline 阶段跳过。
    # 用 .get() 而非直接取值：invoke 失败的用例即使前面补了字段，
    # 也可能有旧 results 文件没这两列，直接 [] 会 KeyError 让整个汇总崩掉。
    tool_cases = [r for r in results if r.get("expected_tools")]
    tool_ok = sum(
        1 for r in tool_cases
        if all(any(exp in actual for actual in r.get("tool_calls", []))
               for exp in r["expected_tools"])
    )

    # Skill 触发：期望的 skill 是否被加载。只在 skills 挂上的 Phase 统计
    # （含期望为空但实际加载了的用例——误触发本身也是信号，如 EV-15）。
    skill_cases = [r for r in results if r.get("expected_skills")]
    skill_ok = sum(1 for r in skill_cases if r.get("skill_hit"))

    # 按档位拆开看：整体分数会被样本分布带偏。
    # 比如 escalate 有 10 条，Agent 学成"什么都升级"也能拿到高分，
    # 但 auto 那 4 条会全崩——这正是要单独盯的信号。
    by_tier: dict[str, dict] = {}
    for r in results:
        bucket = by_tier.setdefault(r["expected_tier"], {"total": 0, "hit": 0})
        bucket["total"] += 1
        bucket["hit"] += int(r["tier_hit"])

    summary = {
        "phase": phase,
        "capabilities": caps,
        "model_id": state.get("model_id"),
        "actor_isolated": isolate,
        "skill_mode": "forced" if force_skill else "auto",
        "total": total,
        "tier_accuracy": round(tier_hits / total, 4) if total else 0,
        "judge_accuracy": round(judged_ok / judged_total, 4) if judged_total else None,
        "tool_accuracy": round(tool_ok / len(tool_cases), 4) if tool_cases and caps.get("tools") else None,
        "skill_accuracy": round(skill_ok / len(skill_cases), 4) if skill_cases and caps.get("skills") else None,
        "by_tier": by_tier,
    }

    print("\n" + "=" * 60)
    print(f"Phase {phase} 汇总"
          + ("（forced 模式：skill_hit 衡量注入服从度，非自动触发）" if force_skill else ""))
    print("=" * 60)
    print(f"档位准确率  {tier_hits}/{total} = {summary['tier_accuracy']:.1%}")
    if judged_total:
        print(f"依据正确率  {judged_ok}/{judged_total} = {summary['judge_accuracy']:.1%}")
    if summary["tool_accuracy"] is not None:
        print(f"工具轨迹    {tool_ok}/{len(tool_cases)} = {summary['tool_accuracy']:.1%}")
    if summary["skill_accuracy"] is not None:
        print(f"Skill触发   {skill_ok}/{len(skill_cases)} = {summary['skill_accuracy']:.1%}")
    print("\n分档明细：")
    for tier in sorted(by_tier):
        b = by_tier[tier]
        print(f"  {tier:<10} {b['hit']}/{b['total']}")

    save_json(results_file, {"summary": summary, "results": results})
    print(f"\n明细已写入 {results_file}")

    # 与历史 Phase 对比，这是 Eval-First 闭环的核心产出
    print("\n各 Phase 对比：")
    print(f"  {'Phase':<7}{'档位':<10}{'依据':<10}{'工具':<10}{'Skill':<10}已启用")
    for p in range(1, 9):
        f = os.path.join(EVAL_DIR, f"eval_results_phase{p}.json")
        if not os.path.exists(f):
            continue
        s = load_json(f)["summary"]
        fmt = lambda v: f"{v:.1%}" if isinstance(v, (int, float)) else "-"
        on = ",".join(k for k, v in (s.get("capabilities") or {}).items() if v) or "-"
        print(f"  {p:<7}{fmt(s['tier_accuracy']):<10}"
              f"{fmt(s.get('judge_accuracy')):<10}{fmt(s.get('tool_accuracy')):<10}"
              f"{fmt(s.get('skill_accuracy')):<10}{on}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
