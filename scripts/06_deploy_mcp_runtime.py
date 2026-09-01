"""
Phase 2 · 把 MCP 工具服务部署到 AgentCore Runtime

运行：
    /usr/bin/python3.11 opc_copilot/scripts/06_deploy_mcp_runtime.py
"""

import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from common import PROJECT_ROOT, load_env_state, load_json, save_json

MCP_DIR = os.path.join(PROJECT_ROOT, "mcp_server")
SRC_DATA_DIR = os.path.join(PROJECT_ROOT, "business_data")
DST_DATA_DIR = os.path.join(MCP_DIR, "business_data")

KB_STATE_FILE = os.path.join(PROJECT_ROOT, ".kb_state.json")
MCP_STATE_FILE = os.path.join(PROJECT_ROOT, ".mcp_state.json")

AGENT_NAME = "opc_copilot_tools"
POLL_INTERVAL = 10
TERMINAL = {"READY", "CREATE_FAILED", "DELETE_FAILED", "UPDATE_FAILED"}


def sync_business_data() -> int:
    """把业务数据复制进 MCP 目录，让它随镜像走。

    每次都全量重建目标目录，避免删掉的记录还残留在镜像里。
    """
    if not os.path.isdir(SRC_DATA_DIR):
        raise RuntimeError(f"缺少 {SRC_DATA_DIR}")
    if os.path.isdir(DST_DATA_DIR):
        shutil.rmtree(DST_DATA_DIR)
    shutil.copytree(SRC_DATA_DIR, DST_DATA_DIR)
    files = [f for f in sorted(os.listdir(DST_DATA_DIR)) if f.endswith(".json")]
    for f in files:
        print(f"    {f}")
    return len(files)


def main() -> int:
    env = load_env_state()
    region = env["region"]

    # 必须用 CFN 建的 AgentCoreRuntimeExecutionRole：内联策略配齐了 bedrock:Retrieve。
    # auto_create_execution_role=True 会让 toolkit 自建角色（CFN 管不到），模板权限落空 → kb_search AccessDenied。
    runtime_role_arn = env.get("runtime_execution_role_arn")
    if not runtime_role_arn:
        raise SystemExit(
            "缺少 runtime_execution_role_arn，请先运行 scripts/01_check_env.py。"
        )

    if not os.path.exists(KB_STATE_FILE):
        raise SystemExit("缺少 .kb_state.json，请先运行 scripts/05_setup_kb.py")
    kb_state = load_json(KB_STATE_FILE)
    kb_id = kb_state["kb_id"]

    print("=" * 60)
    print("Phase 2 · 部署 MCP 工具服务到 AgentCore Runtime")
    print("=" * 60)
    print(f"\n区域    {region}")
    print(f"KB_ID   {kb_id}")
    print(f"执行角色 {runtime_role_arn}")

    print("\n同步业务数据到镜像目录：")
    count = sync_business_data()
    print(f"  ✓ {count} 个数据文件")

    from bedrock_agentcore_starter_toolkit import Runtime

    original_cwd = os.getcwd()
    os.chdir(MCP_DIR)
    try:
        runtime = Runtime()
        print("\n配置 Runtime...")
        runtime.configure(
            entrypoint="mcp_server.py",
            # 用 CFN 角色，不让 toolkit 自建
            auto_create_execution_role=False,
            execution_role=runtime_role_arn,
            auto_create_ecr=True,
            requirements_file="requirements.txt",
            region=region,
            # MCP 协议，不是 HTTP
            protocol="MCP",
            agent_name=AGENT_NAME,
        )
        print("✓ 配置完成，开始构建镜像并部署（首次约 3-5 分钟）...")

        launch_result = runtime.launch(
            env_vars={"KB_ID": kb_id, "AWS_REGION": region},
            auto_update_on_conflict=True,
        )

        status = runtime.status().endpoint["status"]
        while status not in TERMINAL:
            print(f"  状态：{status}，等待中...")
            time.sleep(POLL_INTERVAL)
            status = runtime.status().endpoint["status"]

        if status != "READY":
            raise RuntimeError(f"MCP Runtime 未就绪，最终状态：{status}")
        print("✓ MCP Runtime 已就绪")

        state = {
            "region": region,
            "agent_name": AGENT_NAME,
            "runtime_arn": launch_result.agent_arn,
            "runtime_id": launch_result.agent_id,
            "ecr_repo_name": launch_result.ecr_uri.split("/")[1],
            "codebuild_name": launch_result.codebuild_id.split(":")[0],
            "kb_id": kb_id,
        }
    finally:
        os.chdir(original_cwd)

    save_json(MCP_STATE_FILE, state)
    print(f"\nRuntime ARN  {state['runtime_arn']}")
    print(f"✓ 状态已写入 {MCP_STATE_FILE}")
    print("\n下一步：/usr/bin/python3.11 opc_copilot/scripts/07_setup_gateway.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
