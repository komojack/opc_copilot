---
name: reconciliation
description: 处理对账与供应商核价。当创始人问"这笔钱是哪个单子的""这笔账单对不对得上""供应商这个价合理吗"时使用。
allowed-tools: kb_search query_business_data record_decision
---

# 对账与核价作业流程

两类事共用一套方法：**拿一个实际发生的数，去对一个应该是多少的数。**
对得上就放行，对不上就查差在哪，差多少决定是自主处理还是升级。

## A. 进账对账

### 取数

`query_business_data(query_type="unmatched_transactions")` 看全部对不上的，
或 `query_business_data(query_type="entity", entity_id="TXN-2026-xxxx")` 看单笔。

`entity_id` 只接受真实流水号（`TXN-2026-xxxx`）。**不要用金额当编号**
（如把 15600 元编成 `TXN-15600`）——查不到时返回 `found=false`，
此时输出 `[escalate]`，说明该笔进账对不上任何记录、转创始人确认，
**不得编造流水号或声称"系统已自动审核通过""已登记生效"**。

### 对手方不明确时：先探，不要反问

对话只给了名称（如"长焦影像"）没给编号、也没说是客户还是供应商时，
**不要反问创始人"这是客户还是供应商"**——你手里的工具能自己查出来。
先调 `query_business_data(query_type="list_entities")`，按名称在返回的
clients / suppliers 里对一下，拿到 `CLIENT-xxx` 或 `SUP-xxx` 再走对账或
核价流程。反问会显得助理无能，且让创始人重复已经给过的信息。
只有 `list_entities` 里也查不到该名称，才说明对手方未登记，按依据
不足 escalate。

### 认定为"已核销"必须三条同时满足

1. 金额与某张未结报价单**完全一致** —— 不接受近似匹配，不接受"差个手续费"
2. 付款方名称与客户主体一致
3. 该报价单当前状态为未结

**任何一条不满足即为 unmatched，一律 escalate，不得猜测归属。**

这条是硬规矩。"金额差不多、时间对得上、大概率是这家"——这种推理
在对账里是错的。猜错的代价是账目错误，半年后报税时补不回来。

### 常见对不上的原因（供说明，不作为归属依据）

客户用个人账户付款、多笔合并支付、平台扣手续费导致零头、预付款未开票。

## B. 供应商核价

### 取数

`query_business_data(query_type="rate_variance")` 列出所有不符的，
或 `query_business_data(query_type="entity", entity_id="SUP-XXX")` 看单个。

### 算差额

```
expected_amount = agreed_rate.price × latest_bill.quantity
variance        = latest_bill.amount − expected_amount
```

数据里已经算好了 `expected_amount` 和 `variance`，直接读，不要重算。

### 两项额外检查，都容易漏

**漏掉的优惠**：看 `special_terms` 里有没有本应触发但未适用的条款。
例如"单批超 1000 份再降 5%"，账单量 1200 份却按原价计费 —— 
这部分差额要一并算进去。

**未经确认的上浮**：`special_terms` 里若写明某类上浮"需事先书面确认"，
则没有书面确认记录的上浮**一律不认**。供应商口头说"这批是技术文档"
不构成确认依据。

### 差额处置

| 差额 | 处置 |
|---|---|
| 0 | auto，正常付款 |
| ≤200 元 **且** ≤5% | auto，建议照付并提醒下期修正 |
| >200 元 **或** >5% | escalate，暂缓付款 |
| 已付款后才发现 | escalate，追回事宜必须创始人定 |

注意是"或"不是"且"：小金额但比例高（比如 100 元差额占 20%）同样要升级。

## 费率依据的优先级

核价时"当初谈好的费率"以 `rate_source` 字段为准，优先级：

1. 书面合同附件 → 2. 正式报价单 → 3. 邮件确认 → 4. 聊天记录确认

**口头约定不作为依据。** 只有口头约定的，一律 escalate。

## 登记

结论确定后调 `record_decision`，`decision_type` 填 `reconciliation`
或 `supplier_bill`。已付款需追回的情形，`deviates_from_precedent` 填 true。
