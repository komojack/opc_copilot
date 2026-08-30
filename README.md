# OPC 运营决策 AI 助理

基于 `Amazon Bedrock AgentCore` 渐进式搭建的一人公司（OPC）运营决策助理。
不是一次性配置，而是按 **Eval-First 闭环** 分阶段叠加能力——每个 Phase 跑完都用 15 条历史案例评测，留下"加了这项能力涨了多少分"的对照基准。

能力栈（每个 Phase 叠加一项）：

```
Phase 1  baseline Harness（无工具 / 无记忆）
Phase 2  + 知识库 + MCP 工具服务 + Gateway（放开 kb_search）
Phase 3  + 业务数据查询（query_business_data）
Phase 4  + 按交易对手方隔离的长期记忆（Memory）
Phase 5  + 升级边界策略（Gateway forbid 规则 + record_decision）
Phase 6  + 按需加载的作业流程（Skills）
Phase 7  + 失败归因与评测闭环（Evaluations）
Phase 8  + 资源清理
```

配套的业务文档见上级目录《基于 AgentCore 构建可自主决策的 AI 运营助理.md》，那是讲解稿；本文件是**可照着跑的工程向导**，补齐它没有的前置条件、每步预期输出与报错排查。

---

## 前置条件

### 1. AWS 环境

- 区域：`us-west-2`（脚本与 CFN 默认按此区配置）。
- 已配置 CLI 凭证与默认区域：

  ```bash
  export AWS_REGION=us-west-2
  ```
  
- `boto3` 版本足够新，必须同时具备 `bedrock-agentcore-control.create_harness` 与 `bedrock-agentcore.invoke_harness`。实测 `boto3 >= 1.43.78` 可用；过旧会在 `01_check_env.py` 直接报"客户端没有 create_harness"。
- Bedrock 模型访问：默认模型 `zai.glm-5`，需在该区域已开通访问。


### 2. Python 依赖

```bash
pip install -U boto3 botocore requests bedrock-agentcore-starter-toolkit
```

MCP 工具服务（Phase 2 部署的镜像）另有依赖，见 `mcp_server/requirements.txt`（`mcp<2.0`、`bedrock-agentcore`、`boto3`）——`06_deploy_mcp_runtime.py` 构建镜像时会自动安装，无需本地装。

### 3. 本地数据就位

`business_data/`（clients / suppliers / transactions / contracts）、`kb_docs/`（六篇运营规则）、`evalset/cases.json`（15 条评测用例）随仓库提供。跑前用 `00_validate_data.py` 自检一遍。

---

## Demo 测试

> 命令在 `opc_copilot/` 根目录下执行。云端 用 `/usr/bin/python3.11`，本地用 `python` 即可。
> **顺序不能乱**：每个脚本把状态写进根目录的点开头文件，下一个脚本读它——依赖链见文末"状态文件"。以云端为例：

### Phase 0 · 数据与环境校验（只读，不建资源）

```bash
/usr/bin/python3.11 opc_copilot/scripts/00_validate_data.py     # 校验 KB 文档字节、业务数据交叉引用、评测集字段
/usr/bin/python3.11 opc_copilot/scripts/01_check_env.py         # 探活 boto3 / 控制面 / 身份 / KB / 执行角色
```

**预期输出**：`00` 末行打印 `✓ 全部校验通过，可以进入 Phase 1`；`01` 写出 `.env_state.json` 并打印 `✓ 环境就绪`。
若 `01` 列出 blockers（如执行角色缺权限），按提示修后再跑——它返回非 0 时仍会写出快照，但不要进下一步。

### Phase 1 · baseline Harness + 首次评测

```bash
/usr/bin/python3.11 opc_copilot/scripts/02_create_harness.py    # 创建无工具/无记忆的 Harness，轮询到 READY
/usr/bin/python3.11 opc_copilot/scripts/03_chat.py              # 交互验证通不通（可选）
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py        # 跑 15 条用例，记录 baseline 分数
```

**预期**：`02` 打印 `✓ Harness 就绪` 与 `Harness ARN`，写出 `.harness_state.json`（`phase=1`）。`04` 末尾打印"档位准确率 / 依据正确率"与"各 Phase 对比"表。

### Phase 2 · 知识库 + MCP 工具服务 + Gateway（放开 kb_search）

```bash
/usr/bin/python3.11 opc_copilot/scripts/05_setup_kb.py              # 上传 kb_docs，重建数据源，跑 ingestion，检索冒烟
/usr/bin/python3.11 opc_copilot/scripts/06_deploy_mcp_runtime.py    # 构建 MCP 镜像并部署到 AgentCore Runtime（首次 3-5 分钟）
/usr/bin/python3.11 opc_copilot/scripts/06b_smoke_test.py            # 直连 Runtime 验证三工具可用
/usr/bin/python3.11 opc_copilot/scripts/07_setup_gateway.py          # 建 Policy Engine + Gateway，注册 target，放行基线
/usr/bin/python3.11 opc_copilot/scripts/07b_smoke_test.py            # 经 Gateway 验证（工具名带 opctools___ 前缀）
/usr/bin/python3.11 opc_copilot/scripts/08_update_harness_tools.py --phase 2   # 让 Harness 只看到 kb_search
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py            # 与 Phase 1 对比
```

**预期**：`05` 检索冒烟对三个真问题都返回非空结果；`06` 打印 `✓ MCP Runtime 已就绪` 与 Runtime ARN；`07` 打印 Gateway ARN 与工具名前缀 `opctools___`；`08` 写出 `allowedTools = ['@opcgw/*kb_search']`。

> 关键约束：`06` 配置 Runtime 时 `protocol` 必须是 `MCP` 不是 `HTTP`——配错 Gateway 注册 target 时会失败。

### Phase 3 · 加业务数据查询

```bash
/usr/bin/python3.11 opc_copilot/scripts/08_update_harness_tools.py --phase 3   # 再放开 query_business_data
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py
```

### Phase 4 · 打开 Memory（按交易对手方隔离）

```bash
/usr/bin/python3.11 opc_copilot/scripts/09_enable_memory.py        # 一次 UpdateHarness 启用托管 Memory
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py           # 默认按 actor 隔离，每条用例空记忆起步
```

**预期**：`09` 打印策略（SEMANTIC / SUMMARIZATION / USER_PREFERENCE）、事件保留 60 天，等约 60s 后写 `.harness_state.json`（`phase=4`）。
Memory 实例创建后要 2-5 分钟才进 `ACTIVE`，这段时间对话会"记不住"，属正常，到 Bedrock 控制台 AgentCore → Memory 确认状态即可，不必改配置。

> 踩坑提示：`UpdateHarness` 的 `memory` 字段要在外层多包一层 `optionalValue`，与 `CreateHarness` 的裸结构不同——详见上级手册 Memory 小节。脚本已正确包装，自行拼请求体时注意。

### Phase 5 · 升级边界策略（Gateway forbid + record_decision）

```bash
/usr/bin/python3.11 opc_copilot/scripts/10_setup_policy.py          # 建四条 forbid 策略，先同步 Gateway target 缓存
/usr/bin/python3.11 opc_copilot/scripts/10_setup_policy.py --verify # 网关直连预检：四个用例该拦的拦、该放的放
/usr/bin/python3.11 opc_copilot/scripts/08_update_harness_tools.py --phase 5   # 放开 record_decision
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py
```

**预期**：`--verify` 末尾打印 `✓ 网关拦截全部符合预期`。四条 forbid 分别拦：金额超 5 万、新对手方首单、偏离历史先例、对手方有逾期。

> 必须先做 `--verify` 再放开工具：走对话测试的话，模型可能只在话术上拒绝、压根没调被禁工具，那样策略再对也不会被触发，看起来"通过"其实没验证。

### Phase 6 · Skills

```bash
/usr/bin/python3.11 opc_copilot/scripts/11_upload_skills.py         # 上传 skills/ 到 S3，挂到 Harness
/usr/bin/python3.11 opc_copilot/scripts/04_run_evalset.py
```

**预期**：`11` 列出上传的 skill（quote-review / reconciliation / renewal-and-tax），打印各自的 S3 URI，写出 `phase=6`。

> 改了 `SKILL.md` 重传后，已有会话不会更新——用 `:new` 开新会话再验证。

### Phase 7 · 失败归因

```bash
/usr/bin/python3.11 opc_copilot/scripts/12_evaluations.py           # 对最近一 Phase 的失败用例做根因归因
# 可选：/usr/bin/python3.11 opc_copilot/scripts/12_evaluations.py --register   # 注册线上持续评估的 Evaluator
```

**预期**：对每条失败用例输出根因（`kb_gap` / `data_gap` / `threshold` / `skill_gap` / `memory_gap` / `prompt_gap` / `model_limit`）与"改哪里"，按根因归组打印修正清单，写出 `eval/attribution_report.json`。按清单改对应文件 → 重跑受影响部署脚本（05/06/10/11）→ 重跑 04 对比前后。

### Phase 8 · 清理

```bash
/usr/bin/python3.11 opc_copilot/scripts/13_cleanup.py          # 预演，只打印将删的资源
/usr/bin/python3.11 opc_copilot/scripts/13_cleanup.py --yes     # 真正删除 AgentCore 侧资源 + 清空桶 + 删本地状态文件
```



---

## 脚本索引

| 脚本 | Phase | 作用 | 依赖的状态文件 | 产出 |
|---|---|---|---|---|
| `00_validate_data.py` | 0 | 校验 KB 文档字节、业务数据交叉引用、评测集字段 | — | 退出码 |
| `01_check_env.py` | 0 | 探活 boto3 / 控制面 / 身份 / KB / 执行角色 | — | `.env_state.json` |
| `02_create_harness.py` | 1 | 创建 baseline Harness，轮询到 READY | `.env_state.json` | `.harness_state.json` |
| `03_chat.py` | 1 | 交互对话，`:actor` 切换对手方 | `.harness_state.json` | — |
| `04_run_evalset.py` | 1-6 | 跑 15 条用例，LLM 裁判打分，跨 Phase 对比 | `.harness_state.json` | `eval/eval_results_phaseN.json` |
| `05_setup_kb.py` | 2 | 上传 kb_docs，重建数据源，ingestion，检索冒烟 | `.env_state.json` | `.kb_state.json` |
| `06_deploy_mcp_runtime.py` | 2 | 构建 MCP 镜像并部署到 Runtime | `.kb_state.json` | `.mcp_state.json` |
| `06b_smoke_test.py` | 2 | 验证 MCP 三工具（默认经 Gateway，`--runtime` 直连） | `.mcp_state.json` / `.gateway_state.json` | 退出码 |
| `07_setup_gateway.py` | 2 | 建 Policy Engine + Gateway，注册 target，放行基线 | `.mcp_state.json` | `.gateway_state.json` |
| `07b_smoke_test.py` | 2 | 经 Gateway 验证三工具 | `.gateway_state.json` | 退出码 |
| `08_update_harness_tools.py` | 2/3/5 | 按 `--phase` 放开 Harness 可见工具 | `.harness_state.json` + `.gateway_state.json` | 更新 `.harness_state.json` |
| `09_enable_memory.py` | 4 | 开关托管 Memory（`--disable` 关闭） | `.harness_state.json` | 更新 `.harness_state.json` |
| `10_setup_policy.py` | 5 | 建 forbid 策略（`--verify` 直连预检） | `.gateway_state.json` | 更新 `.gateway_state.json` |
| `11_upload_skills.py` | 6 | 上传 skills/ 到 S3 并挂到 Harness | `.harness_state.json` + `.env_state.json` | 更新 `.harness_state.json` |
| `12_evaluations.py` | 7 | 失败归因（`--register` 注册 Evaluator） | `.harness_state.json` + `eval/*.json` | `eval/attribution_report.json` |
| `13_cleanup.py` | 8 | 清理（`--yes` 执行，默认预演） | `.env_state.json` | 删除资源与状态文件 |

---

## 常见报错排查

下表来自各脚本的真实错误处理分支，按出现阶段排列。

| 现象 | 阶段 | 原因与修法 |
|---|---|---|
| `客户端没有 create_harness — boto3 版本过旧` | 01 | `pip install -U boto3 botocore` 后重跑 |
| `未配置默认区域` | 01 | `export AWS_REGION=us-west-2` |
| `执行角色缺 ecr-public:GetAuthorizationToken` | 01 | 补充角色权限                                                 |
| `xxx.json 为 N 字节，超出上限 1800` | 00/05 | KB 文档过大，S3 Vectors 元数据 2048 字节硬限。拆分或精简该篇 |
| ingestion 报 `Filterable metadata must have at most 2048 bytes` | 05 | 切块仍偏大或某篇过长。切块上限已设 150 token，检查是否单篇内容过密 |
| Harness 最终状态 `CREATE_FAILED` | 02 | 脚本会打印 `failureReason`，按其内容排查（常见为执行角色权限或模型不可用） |
| invoke 报 `model identifier is invalid` | 任意 | 该模型 ID 不被接受。`export HARNESS_MODEL_ID=<已开通的模型>` 换一个 |
| `MCP Runtime 未就绪` / Gateway 注册 target 失败 | 06/07 | 多为 `protocol` 配成 HTTP。`06` 里必须是 `MCP` |
| 数据源 `ConflictException` | 05 | 删后立即建同名会撞。脚本已内置 10s 等待；若仍撞，等几秒重跑 |
| `未找到 Gateway 执行角色` | 07 | 候选名都没匹配。在 `.env_state.json` 手填 `gateway_role_arn`，或 `export AGENTCORE_GATEWAY_ROLE_ARN=...` |
| forbid 策略 `CREATE_FAILED` | 07/10 | NL2Cedar 按 plain English 设计验证：中文、主语含糊、工具名带中文修饰都会失败。改用英文表述，工具名照 schema 原文 |
| forbid_large_amount 误拦常规请求（fail-closed） | 10 | amount 从 float 改 int 后，Gateway 缓存的工具 schema 还是旧 Decimal 类型。脚本已内置 `SynchronizeGatewayTargets` 刷新 + 清旧版策略；前提是 `06` 已重部署 int 版 Runtime |
| 网关预检 `401` / `403` / 超时 | 10 `--verify` | 401=SigV4 签名没过；403=Cedar 拒绝；超时=target 未就绪 |
| `skill 目录名与 frontmatter 的 name 不一致` | 11 | 两者必须相同，否则 Harness 加载报错。改 `SKILL.md` 的 `name:` 或目录名 |
| 对话"记不住" | 04 | Memory 刚启用未到 ACTIVE。等 2-5 分钟，到控制台确认状态后重试，不改配置 |
| 裁判报 `model not found` | 04 | cross-region profile（`us.` 前缀）兼容性随 boto3 变。`export JUDGE_MODEL_ID=<直连 ID，不带 us.>` |
| 评测汇总阶段 `KeyError` 崩在最后 | 04 | 多为用例缺 `expected_tools` 字段。`00_validate_data.py` 已前置挡住，跑前务必先过 |
| Skills 改了不生效 | 06 之后 | 已有会话缓存了旧 skill。`03_chat.py` 里用 `:new` 开新会话 |
| CloudFormation 删栈失败 | 13 | 桶非空。`13_cleanup.py --yes` 会先清空桶；若仍失败，检查是否有手动上传的对象 |

