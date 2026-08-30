"""
Phase 0 · 数据地基校验
--------------------------------------------------------
运行：
    python scripts/00_validate_data.py
"""

import json
import os
import sys

# Windows 控制台默认 GBK，打 ✓/✗ 这类符号会 UnicodeEncodeError。
# 云端 Linux 不会触发，但本地跑校验时会直接崩在第一行输出上。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KB_DOCS_DIR = os.path.join(PROJECT_ROOT, "kb_docs")
BUSINESS_DATA_DIR = os.path.join(PROJECT_ROOT, "business_data")
EVALSET_FILE = os.path.join(PROJECT_ROOT, "evalset", "cases.json")

# 单篇 KB 文档字节上限。真正的硬限是 S3 Vectors 元数据 2048 字节，
# 这里留余量是因为 Bedrock 还会往元数据里塞其它自动生成的键。
KB_DOC_MAX_BYTES = 1800

VALID_TIERS = {"auto", "escalate", "refuse"}
# 不对应任何真实交易对手方的兜底 actor：不涉及具体客户/供应商的对话落在这里
FALLBACK_ACTOR = "founder-general"

errors: list[str] = []
warnings: list[str] = []


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def check_kb_docs() -> None:
    print("\n[1/3] 校验 KB 文档字节数")
    if not os.path.isdir(KB_DOCS_DIR):
        errors.append(f"缺少目录 {KB_DOCS_DIR}")
        return

    docs = sorted(f for f in os.listdir(KB_DOCS_DIR) if f.endswith((".md", ".txt")))
    if not docs:
        errors.append(f"{KB_DOCS_DIR} 下没有 .md/.txt 文档")
        return

    for name in docs:
        size = os.path.getsize(os.path.join(KB_DOCS_DIR, name))
        pct = size / KB_DOC_MAX_BYTES * 100
        flag = "✓"
        if size > KB_DOC_MAX_BYTES:
            flag = "✗"
            errors.append(
                f"{name} 为 {size} 字节，超出上限 {KB_DOC_MAX_BYTES}。"
                f"上云后 ingestion 会因元数据超限失败，请拆分或精简。"
            )
        elif pct > 90:
            flag = "!"
            warnings.append(f"{name} 已占上限 {pct:.0f}%，再加内容容易破线")
        print(f"  {flag} {name:<32} {size:>5} 字节  ({pct:.0f}% of {KB_DOC_MAX_BYTES})")


def check_business_data() -> dict:
    print("\n[2/3] 校验业务数据")
    entity_ids: dict[str, str] = {}   # entity_id -> 来源文件
    client_ids: set[str] = set()

    if not os.path.isdir(BUSINESS_DATA_DIR):
        errors.append(f"缺少目录 {BUSINESS_DATA_DIR}")
        return {}

    # (文件名, 顶层数组键, 实体编号字段)
    specs = [
        ("clients.json", "clients", "client_id"),
        ("suppliers.json", "suppliers", "supplier_id"),
        ("transactions.json", "transactions", "txn_id"),
        ("contracts.json", "contracts", "contract_id"),
    ]

    contracts: list[dict] = []
    transactions: list[dict] = []

    for filename, list_key, id_field in specs:
        path = os.path.join(BUSINESS_DATA_DIR, filename)
        if not os.path.exists(path):
            errors.append(f"缺少业务数据文件 {filename}")
            continue
        try:
            data = load_json(path)
        except json.JSONDecodeError as e:
            errors.append(f"{filename} 不是合法 JSON：{e}")
            continue

        records = data.get(list_key)
        if not isinstance(records, list) or not records:
            errors.append(f"{filename} 缺少非空的 '{list_key}' 数组")
            continue

        for rec in records:
            eid = rec.get(id_field)
            if not eid:
                errors.append(f"{filename} 中有记录缺少 {id_field}")
                continue
            if eid in entity_ids:
                errors.append(f"实体编号重复：{eid}（{entity_ids[eid]} 与 {filename}）")
            entity_ids[eid] = filename
            if id_field == "client_id":
                client_ids.add(eid)

        if list_key == "contracts":
            contracts = records
        elif list_key == "transactions":
            transactions = records

        print(f"  ✓ {filename:<24} {len(records)} 条记录")

    # 交叉引用：合同和流水里提到的 client_id 必须真实存在，否则工具查出来是空的
    for ct in contracts:
        cid = ct.get("client_id")
        if cid and cid not in client_ids:
            errors.append(f"合同 {ct.get('contract_id')} 引用了不存在的客户 {cid}")
    for txn in transactions:
        cid = txn.get("matched_client")
        if cid and cid not in client_ids:
            errors.append(f"流水 {txn.get('txn_id')} 引用了不存在的客户 {cid}")

    return entity_ids


def check_evalset(entity_ids: dict) -> None:
    print("\n[3/3] 校验评测集")
    if not os.path.exists(EVALSET_FILE):
        errors.append(f"缺少评测集 {EVALSET_FILE}")
        return
    try:
        cases = load_json(EVALSET_FILE).get("cases", [])
    except json.JSONDecodeError as e:
        errors.append(f"评测集不是合法 JSON：{e}")
        return

    if not cases:
        errors.append("评测集为空")
        return

    tier_count: dict[str, int] = {}
    seen_ids: set[str] = set()

    for case in cases:
        cid = case.get("id", "<无 id>")
        if cid in seen_ids:
            errors.append(f"评测用例 id 重复：{cid}")
        seen_ids.add(cid)

        for field in ("message", "expected_tier", "ground_truth", "actor_id"):
            if not case.get(field):
                errors.append(f"用例 {cid} 缺少字段 {field}")

        tier = case.get("expected_tier")
        if tier not in VALID_TIERS:
            errors.append(f"用例 {cid} 的 expected_tier='{tier}' 非法，应为 {VALID_TIERS}")
        else:
            tier_count[tier] = tier_count.get(tier, 0) + 1

        # actor_id 就是 Memory 的隔离维度，必须是真实对手方或兜底 actor，
        # 写错了 Phase 4 会静默取到空记忆，很难排查
        actor = case.get("actor_id")
        if actor and actor != FALLBACK_ACTOR and actor not in entity_ids:
            errors.append(
                f"用例 {cid} 的 actor_id='{actor}' 不是已知实体，"
                f"也不是兜底 actor '{FALLBACK_ACTOR}'"
            )

        # expected_tools 缺失会让 04_run_evalset.py 在汇总阶段 KeyError 崩掉，
        # 而且崩在最后、不在单条用例上——这里提前挡住。
        # baseline 阶段不用它，但字段要齐，否则后续 Phase 补数据时容易漏写。
        if "expected_tools" not in case:
            errors.append(f"用例 {cid} 缺少字段 expected_tools（baseline 阶段可给空列表 []）")
        elif not isinstance(case["expected_tools"], list):
            errors.append(f"用例 {cid} 的 expected_tools 必须是列表，当前是 {type(case['expected_tools']).__name__}")

    print(f"  ✓ {len(cases)} 条用例")
    for tier in sorted(tier_count):
        print(f"    {tier:<10} {tier_count[tier]} 条")

    # 三档都要有样本，否则评测覆盖不到某一档，分数会虚高
    for tier in VALID_TIERS:
        if tier not in tier_count:
            warnings.append(f"评测集里没有 expected_tier='{tier}' 的用例，该档覆盖不到")


def main() -> int:
    print("=" * 60)
    print("Phase 0 · 数据地基校验")
    print("=" * 60)

    check_kb_docs()
    entity_ids = check_business_data()
    check_evalset(entity_ids)

    print("\n" + "=" * 60)
    for w in warnings:
        print(f"⚠ {w}")
    if errors:
        print(f"\n✗ 校验未通过，{len(errors)} 个问题：")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\n✓ 全部校验通过，可以进入 Phase 1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
