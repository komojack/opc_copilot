"""
MCP 服务器：把三个工具暴露给 Agent
--------------------------------------------------------
三个工具从第一次部署起就全部就位，**但 Harness 侧通过 allowedTools 控制
哪些对模型可见**：

    Phase 2   kb_search
    Phase 3   + query_business_data
    Phase 5   + record_decision（同时挂上 Cedar 策略）

这样每个 Phase 的评测分数变化能精确归因到某一个工具，而不用为了加一个
工具就重新构建镜像、重新部署 Runtime。这也正是配置驱动的意义所在。

工具描述文字要写清楚**什么时候用**，不只是"做什么"。模型选错工具的
典型原因就是两个工具的描述都只说了功能、没说边界。

本地测试：
    export KB_ID=<知识库ID>
    pip install -r requirements.txt
    python mcp_server.py        # http://localhost:8000/mcp
"""

import os
import sys

# 保证同目录的 tools_core 能被导入：直接 python mcp_server.py 跑、
# 或从项目根启动，两种情况都要能找到。不加这句从项目根启动会报
# ModuleNotFoundError: No module named 'tools_core'。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

import tools_core

mcp = FastMCP(host="0.0.0.0", stateless_http=True)


@mcp.tool()
def kb_search(query: str) -> dict:
    """检索运营规则知识库，回答"按规矩应该怎么办"这类问题。

    覆盖：报价政策与折扣阶梯、付款条款与对账规则、供应商核价规则、
    续约规则、税务与记账分类、升级确认矩阵。

    用它查**规则本身**。涉及某个具体客户/供应商当前实际状况（有没有逾期、
    折扣额度用了多少、账单对不对得上）的，用 query_business_data。
    一次判断通常两个都要调：先查规则，再查事实，然后对照。

    Args:
        query: 要查的规则，例如"老客户折扣上限是多少"、"供应商账单差多少要升级"。
    Returns:
        found 为 False 表示没检索到；此时不要自行推断规则，按依据不足升级。
    """
    return tools_core.search_kb(query)


@mcp.tool()
def query_business_data(query_type: str, entity_id: str = "") -> dict:
    """查询真实业务事实，回答"现在实际是什么情况"这类问题。

    query_type 取值：
      entity                  查单个实体的完整档案，需配合 entity_id。
                              客户编号形如 CLIENT-ABC，会连同其合同与流水一并返回；
                              供应商 SUP-INK，合同 CT-xxx，流水 TXN-xxx。
      overdue_clients         列出所有有逾期未付款的客户
      unmatched_transactions  列出对不上账或有争议的流水
      expiring_contracts      列出 60 天内到期的合同
      rate_variance           列出账单与约定费率不符的供应商
      list_entities           列出所有客户和供应商的编号，不确定编号时先调这个

    这里返回的是事实，不是规则。判断"允不允许"要结合 kb_search 的规则。

    Args:
        query_type: 上述取值之一。
        entity_id: query_type=entity 时必填的实体编号。
    Returns:
        found 为 False 表示查无此记录；此时不要猜测，按依据不足升级。
    """
    return tools_core.query_business_data(query_type=query_type, entity_id=entity_id)


@mcp.tool()
def record_decision(
    entity_id: str,
    decision_type: str,
    summary: str,
    amount: int = 0,
    is_new_counterparty: bool = False,
    deviates_from_precedent: bool = False,
    counterparty_has_overdue: bool = False,
) -> dict:
    """登记一条运营决策（写操作）。

    确认报价、确认续约条款、核销款项、认可供应商账单时调用。

    后四个参数必须**如实**填写，它们是升级判断的依据，不是可选的修饰。
    不能因为想让流程走顺就填 false —— 网关策略会读这些参数做拦截，
    填错等于绕过治理。判断不了的，就当作 true 填。

    Args:
        entity_id: 对手方编号，如 CLIENT-ABC、SUP-INK。
        decision_type: quote（报价）/ renewal（续约）/ reconciliation（对账）
            / supplier_bill（供应商账单）/ tax（税务分类）之一。
        summary: 这条决策的一句话摘要。
        amount: 本次涉及金额，单位元。不涉及金额填 0。整数（金额阈值在
            网关 Cedar 策略里做整数比较；用 float 会被映射成 Cedar decimal，
            decimal 只能由 decimal("...") 字面量构造，原始 JSON 数字进不来，
            会导致 amount 比较的 forbid 整条 fail-closed 误拦）。
        is_new_counterparty: 对手方是否为无历史成交记录的新客户。
        deviates_from_precedent: 条款或价格是否偏离该对手方的历史约定。
        counterparty_has_overdue: 对手方当前是否有未结清的逾期款项。
    Returns:
        status 为 auto_approved / pending_review / denied 之一。
        pending_review 表示已登记但需创始人确认，不要向对方承诺结果。
    """
    # 必须调 tools_core.record_decision —— 本函数被 @mcp.tool 装饰后，
    # record_decision 这个名字已绑定成 MCP 工具对象，直接调会无限递归。
    return tools_core.record_decision(
        entity_id=entity_id,
        decision_type=decision_type,
        summary=summary,
        amount=amount,
        is_new_counterparty=is_new_counterparty,
        deviates_from_precedent=deviates_from_precedent,
        counterparty_has_overdue=counterparty_has_overdue,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
