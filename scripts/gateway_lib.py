"""
Gateway 与 Policy 的公共操作（Phase 2 建网关、Phase 5 加策略共用）
--------------------------------------------------------

几个必须知道的语义，写错了会静默失效：

1. **Cedar 默认拒绝 + forbid 优先**。没有任何 permit 命中，请求一律被拒——
   连 tools/list 都会被过滤掉，表现为"模型看不见任何工具"。所以
   permit_baseline 必须在网关可用之前就建好。

2. **policyEngineConfiguration 只在 create_gateway 时随请求生效**。
   走"已存在，复用"分支时引擎不会自动挂上，必须 update_gateway 补挂。
   没有引擎，ENFORCE 形同虚设，所有调用原样放行。

3. **target 名是 Cedar action 名的前缀**（`{target}___{tool}`）。
   target 改名后旧策略的 action 全部失配，策略还在、但一条也命中不了。
   所以策略名里编进了 target 名的哈希，一变就重新生成。

4. **create_policy 是异步的**。拿到 policyId 不代表建成，必须等 ACTIVE。
"""

import hashlib
import sys
import time

from botocore.exceptions import ClientError

GATEWAY_NAME = "opc-copilot-gateway"
POLICY_ENGINE_NAME = "opc_copilot_policy_engine"
# target 名会成为模型看到的工具名前缀（opctools___kb_search），
# 只用小写字母数字——带连字符的名字在部分客户端上会被截断
GATEWAY_TARGET_NAME = "opctools"

WAIT_INTERVAL = 5
MAX_WAIT = 600


def _wait(client, getter, ident, ok, bad, label):
    waited = 0
    while waited < MAX_WAIT:
        resp = getter(ident)
        status = resp.get("status", "UNKNOWN")
        if status in ok:
            return resp
        if status in bad:
            raise RuntimeError(f"{label} 进入失败状态 {status}：{resp.get('statusReasons') or resp}")
        time.sleep(WAIT_INTERVAL)
        waited += WAIT_INTERVAL
        sys.stdout.write(f"\r  {label} {status}，已等待 {waited}s")
        sys.stdout.flush()
    raise TimeoutError(f"{label} 等待超时（{MAX_WAIT}s）")


def build_runtime_mcp_url(region: str, runtime_arn: str) -> str:
    """AgentCore Runtime 的 MCP 端点 URL。ARN 里的斜杠要转义成 %2F。"""
    encoded = runtime_arn.replace("/", "%2F").replace(":", "%3A")
    return (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{encoded}/invocations?qualifier=DEFAULT"
    )


# ---------------------------------------------------------------------------
# Policy Engine
# ---------------------------------------------------------------------------

def get_or_create_policy_engine(client, name: str = POLICY_ENGINE_NAME):
    try:
        engine = client.create_policy_engine(name=name)
        engine_id = engine["policyEngineId"]
        print(f"  新建 Policy Engine {engine_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        match = next(
            (p for p in client.list_policy_engines()["policyEngines"] if p["name"] == name),
            None,
        )
        if not match:
            raise RuntimeError(f"Policy Engine [{name}] 报冲突却又列不出来，请到控制台确认")
        engine_id = match["policyEngineId"]
        print(f"  复用 Policy Engine {engine_id}")

    detail = _wait(
        client,
        lambda i: client.get_policy_engine(policyEngineId=i),
        engine_id,
        ok={"ACTIVE"},
        bad={"CREATE_FAILED", "DELETE_FAILED"},
        label="Policy Engine",
    )
    print()
    return engine_id, detail["policyEngineArn"]


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

def get_or_create_gateway(client, role_arn: str, policy_engine_arn: str,
                          name: str = GATEWAY_NAME):
    """建网关，入站用 AWS_IAM（SigV4）。

    不用 Cognito JWT：Harness 以 agentcore_gateway 工具挂网关时，默认走
    outboundAuth={"awsIam": {}}，用的是 Harness 执行角色的 SigV4 凭证。
    走 JWT 路径就还得引入 Cognito 用户池、发牌、轮换，主线阶段不值得。
    代价是 Cedar 拿不到终端用户身份（见 PLAN.md §二），真租户隔离留到 Phase 8。
    """
    try:
        gw = client.create_gateway(
            name=name,
            description="OPC Ops Copilot 工具网关：规则检索 / 业务数据 / 决策登记",
            roleArn=role_arn,
            protocolType="MCP",
            authorizerType="AWS_IAM",
            policyEngineConfiguration={"arn": policy_engine_arn, "mode": "ENFORCE"},
        )
        gateway_id = gw["gatewayId"]
        print(f"  新建 Gateway {gateway_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        match = next(
            (g for g in client.list_gateways()["items"] if g["name"] == name), None
        )
        if not match:
            raise RuntimeError(f"Gateway [{name}] 报冲突却又列不出来，请到控制台确认")
        gateway_id = match["gatewayId"]
        print(f"  复用 Gateway {gateway_id}")

    gateway = _wait(
        client,
        lambda i: client.get_gateway(gatewayIdentifier=i),
        gateway_id,
        ok={"READY", "ACTIVE"},
        bad={"CREATE_FAILED", "UPDATE_FAILED", "DELETE_FAILED"},
        label="Gateway",
    )
    print()
    return ensure_policy_engine_attached(client, gateway, policy_engine_arn)


def ensure_policy_engine_attached(client, gateway: dict, policy_engine_arn: str) -> dict:
    """复用路径的关键补救：引擎没挂上时补挂。

    漏掉这步的表现极具迷惑性——策略建得好好的、状态 ACTIVE，但一条都不生效，
    所有调用原样放行，而且日志里看不出任何异常。
    """
    current = (gateway.get("policyEngineConfiguration") or {}).get("arn")
    mode = (gateway.get("policyEngineConfiguration") or {}).get("mode")
    if current == policy_engine_arn and mode == "ENFORCE":
        print("  ✓ Policy Engine 已挂载且为 ENFORCE 模式")
        return gateway

    print(f"  补挂 Policy Engine（当前 arn={current} mode={mode}）...")
    client.update_gateway(
        gatewayIdentifier=gateway["gatewayId"],
        name=gateway["name"],
        roleArn=gateway["roleArn"],
        protocolType=gateway["protocolType"],
        authorizerType=gateway["authorizerType"],
        policyEngineConfiguration={"arn": policy_engine_arn, "mode": "ENFORCE"},
    )
    gateway = _wait(
        client,
        lambda i: client.get_gateway(gatewayIdentifier=i),
        gateway["gatewayId"],
        ok={"READY", "ACTIVE"},
        bad={"UPDATE_FAILED"},
        label="Gateway 更新",
    )
    print("\n  ✓ Policy Engine 已补挂")
    return gateway


def get_or_create_target(client, gateway_id: str, mcp_endpoint: str,
                         name: str = GATEWAY_TARGET_NAME, region: str = ""):
    """注册 MCP Runtime 为网关目标，先清掉异名旧 target。

    异名旧 target 不会因为改了常量而消失，会导致网关同时列出两套工具，
    模型可能撞上旧的那套，而策略只对新 target 名生效。

    credentialProvider 必须带 iamCredentialProvider 子结构（service + region），
    光传 credentialProviderType=GATEWAY_IAM_ROLE 会被服务端拒：
    "IamCredentialProvider is required for mcpServer targets using IAM authentication"。
    """
    try:
        for t in client.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]:
            if t["name"] != name:
                print(f"  删除异名旧 target：{t['name']}")
                client.delete_gateway_target(
                    gatewayIdentifier=gateway_id, targetId=t["targetId"]
                )
    except ClientError as e:
        print(f"  列举旧 target 失败（忽略继续）：{e.response['Error']['Code']}")

    try:
        target = client.create_gateway_target(
            gatewayIdentifier=gateway_id,
            name=name,
            description="OPC Ops Copilot MCP 工具服务",
            targetConfiguration={
                # listingMode=DEFAULT 让网关把 MCP 服务的 tools/list 结果透传给模型，
                # 否则模型看不到工具 schema，策略生成也没有材料。
                "mcp": {"mcpServer": {"endpoint": mcp_endpoint, "listingMode": "DEFAULT"}}
            },
            credentialProviderConfigurations=[
                {
                    "credentialProviderType": "GATEWAY_IAM_ROLE",
                    "credentialProvider": {
                        "iamCredentialProvider": {
                            "service": "bedrock-agentcore",
                            "region": region,
                        }
                    },
                }
            ],
        )
        target_id = target["targetId"]
        print(f"  新建 target {target_id}")
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConflictException":
            raise
        match = next(
            t for t in client.list_gateway_targets(gatewayIdentifier=gateway_id)["items"]
            if t["name"] == name
        )
        target_id = match["targetId"]
        print(f"  复用 target {target_id}")

    _wait(
        client,
        lambda i: client.get_gateway_target(gatewayIdentifier=gateway_id, targetId=i),
        target_id,
        ok={"READY", "ACTIVE"},
        bad={"CREATE_FAILED", "UPDATE_FAILED"},
        label="Gateway target",
    )
    print()
    return target_id


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------

def _extract_cedar(obj):
    """从嵌套的 definition 结构里挖出 Cedar 原文，字段名各版本不一，递归找。"""
    if isinstance(obj, str):
        return obj if "permit" in obj or "forbid" in obj else None
    if isinstance(obj, dict):
        for key in ("statement", "cedarStatement", "policyStatement", "content"):
            if key in obj:
                found = _extract_cedar(obj[key])
                if found:
                    return found
        for v in obj.values():
            found = _extract_cedar(v)
            if found:
                return found
    if isinstance(obj, list):
        for v in obj:
            found = _extract_cedar(v)
            if found:
                return found
    return None


def print_cedar(client, engine_id: str, policy_id: str) -> None:
    """打印生成出来的 Cedar 原文。

    自然语言生成的策略必须肉眼核对——NL2Cedar 能把 permit/forbid 骨架
    生成对，但条件表达式（尤其是参数名和比较运算）时有偏差。
    不看原文就上线，等于把治理托付给一次没检查过的翻译。
    """
    try:
        p = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
    except ClientError as e:
        print(f"    （读取 Cedar 失败：{e.response['Error']['Code']}）")
        return
    cedar = _extract_cedar(p.get("definition"))
    if cedar:
        for line in cedar.strip().splitlines():
            print(f"    │ {line}")
    else:
        print("    （未能从 definition 中解析出 Cedar 原文）")


def _wait_policy_active(client, engine_id: str, policy_id: str, max_wait: int = 180) -> None:
    waited = 0
    while waited < max_wait:
        resp = client.get_policy(policyEngineId=engine_id, policyId=policy_id)
        status = resp.get("status")
        if status == "ACTIVE":
            return
        if status in ("CREATE_FAILED", "DELETE_FAILED"):
            # 把失败原因完整带出来——statusReasons / validationFindings 是
            # 诊断 CREATE_FAILED 的唯一线索，丢了就只能瞎猜。
            reasons = resp.get("statusReasons") or []
            findings = resp.get("validationFindings") or []
            msg = f"Policy {policy_id} 状态 {status}"
            if reasons:
                msg += f"\n  statusReasons: {reasons}"
            if findings:
                msg += f"\n  validationFindings: {findings}"
            raise RuntimeError(msg)
        time.sleep(3)
        waited += 3
    raise TimeoutError(f"Policy {policy_id} 未在 {max_wait}s 内变为 ACTIVE")


def create_policy_from_text(client, engine_id: str, gateway_arn: str,
                            base_name: str, raw_text: str,
                            validation_mode: str = "FAIL_ON_ANY_FINDINGS") -> list[str]:
    """用自然语言生成 Cedar 策略并创建。

    策略名里编进 (网关 ARN + target 名 + 描述文本) 的哈希：任何一项变了都会
    得到新名字，从根上避免"复用了绑定旧网关/旧工具名的 Cedar"这类静默失效。
    target 名必须参与哈希——它是 action 名的前缀，改名后旧策略全部失配。

    validation_mode：
      FAIL_ON_ANY_FINDINGS（默认）—— forbid 策略用这个，带 when 条件，不会
        被判 Overly Permissive，校验能抓出真实的 Cedar 语法/参数名错误。
      IGNORE_ALL_FINDINGS —— permit_baseline 用这个。baseline 是**无条件**放行
        三个工具（真正的限制由 Phase 5 的 forbid 施加），Policy Engine 把这种
        "对 (principal, action, resource) 三元组不加 when 约束"的 permit
        判为 Overly Permissive 而拒绝——这是服务端后来收紧的安全护栏。
        在 forbid 优先于 permit 的架构里，permit 宽松是设计使然，这里是误报。
    """
    slug = hashlib.sha256(
        f"{gateway_arn}|{GATEWAY_TARGET_NAME}|{raw_text}".encode("utf-8")
    ).hexdigest()[:8]
    full_name = f"{base_name[:39]}_{slug}"

    existing = [
        p["policyId"] for p in
        client.list_policies(policyEngineId=engine_id).get("policies", [])
        if p.get("name", "").startswith(full_name)
    ]
    if existing:
        statuses = {
            pid: client.get_policy(policyEngineId=engine_id, policyId=pid)["status"]
            for pid in existing
        }
        if all(s == "ACTIVE" for s in statuses.values()):
            print(f"\n  策略 [{full_name}] 已存在且 ACTIVE（{len(existing)} 条），复用")
            for pid in existing:
                print_cedar(client, engine_id, pid)
            return existing
        print(f"\n  策略 [{full_name}] 状态异常 {statuses}，删除后重建")
        for pid in existing:
            client.delete_policy(policyEngineId=engine_id, policyId=pid)
        time.sleep(5)

    print(f"\n  生成策略 [{base_name}]：{raw_text}")
    gens = client.list_policy_generations(policyEngineId=engine_id)["policyGenerations"]
    match = next((g for g in gens if g["name"] == full_name), None)

    if match and match["status"] in ("GENERATING", "GENERATED"):
        generation_id = match["policyGenerationId"]
        status = match["status"]
    else:
        if match:  # GENERATE_FAILED 等：同名会撞车，先删
            client.delete_policy_generation(
                policyEngineId=engine_id, policyGenerationId=match["policyGenerationId"]
            )
        gen = client.start_policy_generation(
            policyEngineId=engine_id,
            resource={"arn": gateway_arn},
            content={"rawText": raw_text},
            name=full_name,
        )
        generation_id, status = gen["policyGenerationId"], gen["status"]

    waited, gen = 0, None
    while status == "GENERATING" and waited < 180:
        time.sleep(5)
        waited += 5
        gen = client.get_policy_generation(
            policyEngineId=engine_id, policyGenerationId=generation_id
        )
        status = gen["status"]
        sys.stdout.write(f"\r    生成中 {status}，已等待 {waited}s")
        sys.stdout.flush()
    print()

    if status != "GENERATED":
        raise RuntimeError(f"策略生成失败 {status}：{gen.get('statusReasons') if gen else ''}")

    assets = client.list_policy_generation_assets(
        policyEngineId=engine_id, policyGenerationId=generation_id
    ).get("policyGenerationAssets", [])
    if not assets:
        print(f"  ⚠ 生成返回 0 个 asset，没有创建任何策略。"
              f"自然语言描述可能被判定为无需新增规则，请改写后重试。")
        return []

    created = []
    for i, asset in enumerate(assets):
        policy = client.create_policy(
            name=f"{full_name}_{i}",
            policyEngineId=engine_id,
            definition={"policyGeneration": {
                "policyGenerationId": generation_id,
                "policyGenerationAssetId": asset["policyGenerationAssetId"],
            }},
            enforcementMode="ACTIVE",
            validationMode=validation_mode,
        )
        pid = policy["policyId"]
        _wait_policy_active(client, engine_id, pid)
        print(f"    ✓ Policy {pid} ACTIVE（来自：{asset.get('rawTextFragment', '')[:60]}）")
        print_cedar(client, engine_id, pid)
        created.append(pid)
    return created


def create_policy_from_cedar(client, engine_id: str, gateway_arn: str,
                             base_name: str, cedar_text: str,
                             validation_mode: str = "FAIL_ON_ANY_FINDINGS") -> list[str]:
    """直接用 Cedar 原文创建策略，绕过 NL2Cedar 生成。

    NL2Cedar 不可控：自然语言生成的条件表达式时常有参数名/类型偏差。
    典型坑——amount 比较被生成成 context.input.amount.greaterThan(decimal(...))，
    当 input.amount 是 JSON 整数（如 32000）时，greaterThan 在 decimal 类型上
    求值可能抛异常，Cedar 默认 fail-closed → 无差别拦截（包括不该拦的）。

    这种带类型/方法调用的条件，手写更可靠。把手写 Cedar 放在
    definition.cedar.statement，不经过 policyGeneration。

    slug 编进 Cedar 原文：改了条件就重新生成，不复用旧的。
    """
    slug = hashlib.sha256(
        f"{gateway_arn}|{GATEWAY_TARGET_NAME}|cedar|{cedar_text}".encode("utf-8")
    ).hexdigest()[:8]
    full_name = f"{base_name}_{slug}"

    existing = [
        p["policyId"] for p in
        client.list_policies(policyEngineId=engine_id).get("policies", [])
        if p.get("name", "").startswith(full_name)
    ]
    if existing:
        statuses = {
            pid: client.get_policy(policyEngineId=engine_id, policyId=pid)["status"]
            for pid in existing
        }
        if all(s == "ACTIVE" for s in statuses.values()):
            print(f"\n  策略 [{full_name}] 已存在且 ACTIVE（{len(existing)} 条），复用")
            for pid in existing:
                print_cedar(client, engine_id, pid)
            return existing
        print(f"\n  策略 [{full_name}] 状态异常 {statuses}，删除后重建")
        for pid in existing:
            client.delete_policy(policyEngineId=engine_id, policyId=pid)
        time.sleep(5)

    print(f"\n  手写 Cedar 策略 [{base_name}]：")
    for line in cedar_text.strip().splitlines():
        print(f"    │ {line}")

    policy = client.create_policy(
        name=f"{full_name}_0",
        policyEngineId=engine_id,
        definition={"cedar": {"statement": cedar_text}},
        enforcementMode="ACTIVE",
        validationMode=validation_mode,
    )
    pid = policy["policyId"]
    _wait_policy_active(client, engine_id, pid)
    print(f"    ✓ Policy {pid} ACTIVE（手写 Cedar）")
    print_cedar(client, engine_id, pid)
    return [pid]

