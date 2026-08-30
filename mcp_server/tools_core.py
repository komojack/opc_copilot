"""
工具业务逻辑（不含 MCP 装饰器）
--------------------------------------------------------
抽离出来是为了能脱离 MCP 服务单独验证规则正确性——起一个完整 MCP
服务只为测"逾期客户查得对不对"太重了。

三个工具，职责严格分开：

  kb_search             规则和政策，查的是"应该怎么办"。数据在 Bedrock KB
                        （S3 Vectors 向量存储），语义检索。

  query_business_data   业务事实，查的是"现在实际是什么情况"。数据是随镜像
                        打包的 JSON 实体档案，精确查询 + 聚合。
                        **不走向量检索**——聚合类问题（哪些客户逾期了）
                        语义检索答不了，也不保证召回全部符合条件的记录。

  record_decision       写操作，登记一条决策。

record_decision 的参数设计本身就是治理的一部分：升级触发条件
（金额、是否新对手方、是否偏离先例、对手方是否逾期）被拆成了显式的
标量参数，而不是让模型在自然语言里"顺带"处理。这样 AgentCore Policy
才能在 Gateway 层直接读 context.input.amount 做判断，不需要去理解原话。

⚠️ 本方案用于实验场景，演示 AgentCore 能力。决策日志是内存字典，
生产环境应替换为真实的持久化存储。
"""

import json
import os
from datetime import datetime, timezone

import boto3

KB_ID = os.environ.get("KB_ID", "")
AWS_REGION = os.environ.get("AWS_REGION", "us-west-2")

# 业务数据目录。部署时由 06_deploy_mcp_runtime.py 从项目根的 business_data/
# 复制到本文件同级，随镜像一起打包。
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "business_data")

# 升级阈值。与 kb_docs/escalation_matrix.md 保持一致——这里是业务层兜底，
# Gateway 的 Cedar 策略是第一道防线，两处都要有。
AMOUNT_ESCALATION_THRESHOLD = 50000

DECISION_LOG: list[dict] = []

_cache: dict[str, list] = {}


def _load(filename: str, list_key: str) -> list:
    """读实体档案，带进程内缓存。数据随镜像走，读一次就够。"""
    if list_key in _cache:
        return _cache[list_key]
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        _cache[list_key] = []
        return []
    with open(path, encoding="utf-8") as f:
        _cache[list_key] = json.load(f).get(list_key, [])
    return _cache[list_key]


def _clients() -> list:
    return _load("clients.json", "clients")


def _suppliers() -> list:
    return _load("suppliers.json", "suppliers")


def _transactions() -> list:
    return _load("transactions.json", "transactions")


def _contracts() -> list:
    return _load("contracts.json", "contracts")


# ---------------------------------------------------------------------------
# kb_search
# ---------------------------------------------------------------------------

def search_kb(query: str, top_k: int = 4) -> dict:
    """检索规则知识库（只读）。"""
    if not KB_ID:
        return {"found": False, "message": "环境变量 KB_ID 未设置，无法检索知识库。"}

    client = boto3.client("bedrock-agent-runtime", region_name=AWS_REGION)
    resp = client.retrieve(
        knowledgeBaseId=KB_ID,
        retrievalQuery={"text": query},
        retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": top_k}},
    )
    results = [
        {
            "text": item["content"]["text"],
            "score": round(item.get("score", 0.0), 4),
            "source": item.get("location", {}).get("s3Location", {}).get("uri", ""),
        }
        for item in resp.get("retrievalResults", [])
    ]
    if not results:
        return {
            "found": False,
            "message": "知识库里没有检索到相关规则。不要凭空推断规则，按依据不足升级处理。",
        }
    return {"found": True, "query": query, "results": results}


# ---------------------------------------------------------------------------
# query_business_data
# ---------------------------------------------------------------------------

def _find_entity(entity_id: str) -> dict | None:
    """按编号在四类实体里找。编号前缀已经隐含了类型，不用调用方再指定。"""
    for records, id_field, kind in (
        (_clients(), "client_id", "client"),
        (_suppliers(), "supplier_id", "supplier"),
        (_contracts(), "contract_id", "contract"),
        (_transactions(), "txn_id", "transaction"),
    ):
        for rec in records:
            if rec.get(id_field) == entity_id:
                return {"entity_type": kind, **rec}
    return None


def _client_full_profile(client_id: str) -> dict | None:
    """客户档案 + 关联合同 + 关联流水。

    一次返回完整上下文，省掉模型多轮试探。检索单元是"一个实体的完整档案"，
    这正是这套数据不走向量库的原因——向量检索给不了完整档案。
    """
    client = next((c for c in _clients() if c["client_id"] == client_id), None)
    if not client:
        return None
    return {
        "entity_type": "client",
        **client,
        "contracts": [c for c in _contracts() if c.get("client_id") == client_id],
        "transactions": [t for t in _transactions() if t.get("matched_client") == client_id],
    }


def query_business_data(query_type: str, entity_id: str = "") -> dict:
    """查询业务事实。

    query_type 是一个封闭集合，不是自由查询语言——边界清晰，模型不会写出
    查不动的表达式，也便于 Policy 层理解调用意图。
    """
    qt = (query_type or "").strip()

    if qt == "entity":
        if not entity_id:
            return {"found": False, "message": "query_type=entity 时必须提供 entity_id。"}
        if entity_id.startswith("CLIENT-"):
            data = _client_full_profile(entity_id)
        else:
            data = _find_entity(entity_id)
        if not data:
            return {
                "found": False,
                "message": f"没有编号为 {entity_id} 的记录。不要猜测，按依据不足升级处理。",
            }
        return {"found": True, "query_type": qt, "data": data}

    if qt == "overdue_clients":
        rows = [
            {k: c[k] for k in ("client_id", "name", "overdue_amount", "overdue_days")}
            for c in _clients() if c.get("overdue_amount", 0) > 0
        ]
        return {"found": True, "query_type": qt, "count": len(rows), "data": rows}

    if qt == "unmatched_transactions":
        rows = [t for t in _transactions() if t.get("status") in ("unmatched", "disputed")]
        return {"found": True, "query_type": qt, "count": len(rows), "data": rows}

    if qt == "expiring_contracts":
        # 到期前 60 天启动评估，与 kb_docs/renewal_rules.md 一致
        rows = sorted(
            (c for c in _contracts() if c.get("days_to_expiry", 9999) <= 60),
            key=lambda c: c.get("days_to_expiry", 9999),
        )
        return {"found": True, "query_type": qt, "count": len(rows), "data": rows}

    if qt == "rate_variance":
        rows = [s for s in _suppliers() if s.get("variance", 0) != 0]
        return {"found": True, "query_type": qt, "count": len(rows), "data": rows}

    if qt == "list_entities":
        return {
            "found": True,
            "query_type": qt,
            "data": {
                "clients": [{"id": c["client_id"], "name": c["name"]} for c in _clients()],
                "suppliers": [{"id": s["supplier_id"], "name": s["name"]} for s in _suppliers()],
            },
        }

    return {
        "found": False,
        "message": (
            f"不支持的 query_type='{query_type}'。可用值："
            "entity, overdue_clients, unmatched_transactions, "
            "expiring_contracts, rate_variance, list_entities"
        ),
    }


# ---------------------------------------------------------------------------
# record_decision
# ---------------------------------------------------------------------------

def record_decision(
    entity_id: str,
    decision_type: str,
    summary: str,
    amount: int = 0,
    is_new_counterparty: bool = False,
    deviates_from_precedent: bool = False,
    counterparty_has_overdue: bool = False,
) -> dict:
    """登记一条运营决策。

    三种结果：
      auto_approved   不命中任何升级条件，直接登记生效
      pending_review  命中升级条件，登记后等创始人确认
      denied          业务层兜底拒绝（线上这类请求在 Gateway 已被 Cedar 拦下，
                      正常走不到这里；能走到说明策略没生效，值得排查）
    """
    if not entity_id or not summary:
        return {"found": False, "message": "缺少 entity_id 或 summary，无法登记。"}

    reasons = []
    if amount > AMOUNT_ESCALATION_THRESHOLD:
        reasons.append(f"金额 {amount} 超过 {AMOUNT_ESCALATION_THRESHOLD} 阈值")
    if is_new_counterparty:
        reasons.append("新对手方首单")
    if deviates_from_precedent:
        reasons.append("条款或价格偏离历史先例")
    if counterparty_has_overdue:
        reasons.append("对手方存在未结清逾期款项")

    if reasons:
        status = "pending_review"
        note = "已登记，等待创始人确认。触发原因：" + "；".join(reasons)
    else:
        status = "auto_approved"
        note = "未命中升级条件，已按标准规则登记生效。"

    decision = {
        "decision_id": f"DEC-{len(DECISION_LOG) + 1001:04d}",
        "entity_id": entity_id,
        "decision_type": decision_type,
        "summary": summary,
        "amount": amount,
        "is_new_counterparty": is_new_counterparty,
        "deviates_from_precedent": deviates_from_precedent,
        "counterparty_has_overdue": counterparty_has_overdue,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    DECISION_LOG.append(decision)
    return {"found": True, **decision, "note": note, "escalation_reasons": reasons}
