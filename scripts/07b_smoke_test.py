"""快速验证 MCP 三工具是否可用 —— 终端直接跑。

运行前提（二选一）：
  - 刚跑完 06_deploy_mcp_runtime.py，有 .mcp_state.json     → 直连 Runtime
  - 已跑完 07_setup_gateway.py，有 .gateway_state.json        → 走 Gateway

用法：
  /usr/bin/python3.11 scripts/06b_smoke_test.py            # 默认从 .gateway_state 取
  /usr/bin/python3.11 scripts/06b_smoke_test.py --runtime  # 强制直连 Runtime

它会做三件事：
  1. 连上 MCP，发 tools/list，确认三个工具都在
  2. 实调 query_business_data(query_type="list_entities")，确认不是空壳
  3. 实调 kb_search(query="老客户折扣上限是多少")，确认 KB_ID 注入对、能检索

只验"部署的东西活着"，不跑评测集。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import PROJECT_ROOT

MCP_STATE = os.path.join(PROJECT_ROOT, ".mcp_state.json")
GW_STATE = os.path.join(PROJECT_ROOT, ".gateway_state.json")
EXPECTED = {"kb_search", "query_business_data", "record_decision"}


def load(path: str) -> dict | None:
    return json.load(open(path)) if os.path.exists(path) else None


def signed_post(url: str, payload: dict, region: str) -> dict:
    """对 AgentCore endpoint 做 SigV4 签名 POST，解析 MCP 的 SSE 响应。

    两个坑（实测）：
    1. 响应是 SSE 流，一行 data: 可能含 \\r\\n 被拆行；要按空行(\\n\\n)切事件，
       把同一事件内所有 data: 行的内容拼接成一条 JSON 再解析。
    2. 中文会显示成 æ£ç´¢ 乱码——必须用 resp.content(字节) 按 UTF-8 解码，
       不能用 resp.text（requests 按 Latin-1 猜的编码不对）。
    """
    import requests
    from botocore.auth import SigV4Auth
    from botocore.awsrequest import AWSRequest
    import boto3

    session = boto3.Session()
    creds = session.get_credentials().get_frozen_credentials()
    headers = {
        "Content-Type": "application/json",
        # MCP 协议强制要求 Accept 同时声明这两种类型，否则 -32011
        "Accept": "application/json, text/event-stream",
    }
    req = AWSRequest(method="POST", url=url, data=json.dumps(payload), headers=headers)
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(req)
    resp = requests.post(url, data=req.data, headers=dict(req.headers), timeout=60)
    ct = resp.headers.get("Content-Type", "")
    # 关键：用 content 按 UTF-8 解码，避免 requests 把中文猜成 Latin-1
    body = resp.content.decode("utf-8", errors="replace")

    if "text/event-stream" in ct or "data:" in body:
        # 按标准 SSE：空行(\n\n)分隔事件；事件内可能有多行 data:，拼接成一条
        data_payload = ""
        for event_block in body.split("\n\n"):
            event_data = ""
            for line in event_block.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    event_data += line[len("data:"):].strip()
            if event_data:
                data_payload = event_data  # 取最后一个有效事件块
        if data_payload:
            try:
                return json.loads(data_payload)
            except json.JSONDecodeError:
                pass
        print(f"  ✗ SSE 解析失败，前 300 字符：{body[:300]}")
        return {"error": "sse parse failed", "raw": body[:500]}

    try:
        return json.loads(body)
    except json.JSONDecodeError:
        print(f"  ✗ 非 JSON 响应，前 300 字符：{body[:300]}")
        return {"error": "non-json", "raw": body[:500]}


def main() -> int:
    via_gateway = "--runtime" not in sys.argv
    gw_state = load(GW_STATE)
    mcp_state = load(MCP_STATE)

    if via_gateway and not gw_state:
        print("没有 .gateway_state.json，改用直连 Runtime（加 --runtime）或先跑 07_setup_gateway.py")
        via_gateway = False
    if not via_gateway and not mcp_state:
        print("缺少 .mcp_state.json，请先跑 06_deploy_mcp_runtime.py")
        return 1

    import gateway_lib as gwlib
    region = (gw_state or mcp_state)["region"]

    if via_gateway:
        # gateway_url 已带 /mcp 后缀（07_setup_gateway 存的就是完整端点），
        # 不要再拼 /mcp，否则变成 .../mcp/mcp 会 404
        url = gw_state["gateway_url"].rstrip("/")
        print(f"通过 Gateway 验证\n  {url}")
    else:
        url = gwlib.build_runtime_mcp_url(region, mcp_state["runtime_arn"])
        print(f"直连 Runtime 验证\n  {url}")

    # 1. tools/list
    print("\n[1/3] tools/list")
    res = signed_post(url, {"jsonrpc": "2.0", "id": "1", "method": "tools/list"}, region)
    if "result" not in res:
        print("  ✗ 失败:", json.dumps(res, ensure_ascii=False)[:300])
        return 1
    tools = res["result"].get("tools", [])
    names = [t["name"] for t in tools]
    print(f"  注册工具 {len(names)} 个：{names}")

    # Gateway 模式下工具名带 target 前缀（opctools___kb_search），直连 Runtime 是裸名。
    # 比对齐全性时统一剥前缀，tools/call 时按原始完整名调（Gateway 要求带前缀）。
    bare = {n.split("___")[-1] for n in names}
    missing = EXPECTED - bare
    if missing:
        print(f"  ✗ 缺工具：{missing}")
        return 1
    print("  ✓ 三个工具齐全")

    # 按裸名取完整工具名：Gateway 要带前缀，Runtime 直连用裸名即可
    def full_name(bare_name: str) -> str:
        return next((n for n in names if n.endswith(f"___{bare_name}") or n == bare_name), bare_name)

    # 2. 实调 query_business_data（不依赖 KB）
    print("\n[2/3] 调用 query_business_data(list_entities)")
    res = signed_post(url, {
        "jsonrpc": "2.0", "id": "2", "method": "tools/call",
        "params": {"name": full_name("query_business_data"),
                   "arguments": {"query_type": "list_entities"}},
    }, region)
    if "result" not in res:
        print("  ✗ 失败:", json.dumps(res, ensure_ascii=False)[:300])
        return 1
    content = res["result"].get("content", [])
    text = content[0].get("text", "") if content else ""
    print(f"  返回 {len(text)} 字符：{text[:120]}")
    if not text:
        print("  ✗ 返回为空或格式异常")
        return 1
    print("  ✓ 业务数据工具可用")

    # 3. 实调 kb_search（验证 KB_ID 注入对）
    print("\n[3/3] 调用 kb_search('老客户折扣上限是多少')")
    res = signed_post(url, {
        "jsonrpc": "2.0", "id": "3", "method": "tools/call",
        "params": {"name": full_name("kb_search"), "arguments": {"query": "老客户折扣上限是多少"}},
    }, region)
    if "result" not in res:
        print("  ✗ 失败:", json.dumps(res, ensure_ascii=False)[:300])
        return 1
    content = res["result"].get("content", [])
    text = content[0].get("text", "") if content else ""
    print(f"  返回 {len(text)} 字符：{text[:120]}")
    if not text:
        print("  ✗ KB 检索返回空，检查 KB_ID 是否注入、知识库是否同步")
        return 1
    print("  ✓ 知识库工具可用")

    print("\n" + "=" * 50)
    print("✓ MCP 三工具全部可用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
