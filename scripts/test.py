import json, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
d = json.load(open("/workshop/opc_copilot/.eval_results_phase6.json", encoding="utf-8"))
s = d["summary"]
print(f"model: {s.get('model_id')}  tier/judge/tool: {s['tier_accuracy']} / {s['judge_accuracy']} / {s['tool_accuracy']}")
fails = [r for r in d["results"] if not r.get("tier_hit") or r.get("judge_score") == 0]
print(f"失败 {len(fails)} 条\n")
for r in fails:
    print("="*72)
    print(f"{r['id']} {r['scenario']} | exp {r.get('expected_tier')} / act {r.get('actual_tier')} | tier_hit={r.get('tier_hit')} judge={r.get('judge_score')}")
    print(f"msg  : {r['message']}")
    print(f"tools: exp={r.get('expected_tools')}  act={r.get('tool_calls')}")
    print(f"reply: {(r.get('reply') or '')[:700]}")
    print(f"truth: {(r.get('ground_truth') or '')[:220]}")
    jr = r.get("judge_reason")
    if jr: print(f"judge_rsn: {jr[:220]}")