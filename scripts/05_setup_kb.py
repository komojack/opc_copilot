"""
Phase 2 · 知识库数据准备与同步
--------------------------------------------------------
运行：
    /usr/bin/python3.11 opc_copilot/scripts/05_setup_kb.py
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from common import PROJECT_ROOT, load_env_state, save_json

KB_DOCS_DIR = os.path.join(PROJECT_ROOT, "kb_docs")
KB_STATE_FILE = os.path.join(PROJECT_ROOT, ".kb_state.json")

KB_NAME = "opc-product-knowledge-base"   # template-global.yaml 里硬编码的名字
DATA_SOURCE_NAME = "opc-copilot-kb-source"

# 见文件头说明。不要为了"多塞点上下文"调大这个值。
CHUNK_MAX_TOKENS = 150
CHUNK_OVERLAP_PCT = 20

INGESTION_WAIT_INTERVAL = 5
INGESTION_MAX_WAIT = 900

# 冒烟测试用几条真问题，覆盖不同文档，一次看出哪篇没进去
SMOKE_QUERIES = [
    "老客户折扣上限是多少",
    "供应商账单差多少需要升级确认",
    "续约时客户要求上调折扣怎么处理",
]


def find_kb(agent_client) -> dict:
    names = []
    paginator = agent_client.get_paginator("list_knowledge_bases")
    for page in paginator.paginate():
        for kb in page["knowledgeBaseSummaries"]:
            names.append(kb["name"])
            if kb["name"] == KB_NAME:
                return kb
    raise RuntimeError(
        f"没有找到知识库 {KB_NAME}。\n"
        f"当前账户下的 KB：{names or '（空）'}\n"
        f"请确认 template-global.yaml 栈已创建成功。"
    )


def find_docs_bucket(agent_client, kb_id: str, account_id: str, region: str) -> str:
    """从现有数据源反查文档桶名；数据源已被删掉时回退到 CFN 的命名规则。"""
    sources = agent_client.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]
    if sources:
        ds = agent_client.get_data_source(
            knowledgeBaseId=kb_id, dataSourceId=sources[0]["dataSourceId"]
        )["dataSource"]
        arn = ds["dataSourceConfiguration"]["s3Configuration"]["bucketArn"]
        return arn.split(":::")[-1]
    return f"opc-kb-docs-{account_id}-{region}"


def recreate_data_source(agent_client, kb_id: str, bucket_name: str) -> str:
    """删掉所有旧数据源，用小切块配置重建一个。"""
    sources = agent_client.list_data_sources(knowledgeBaseId=kb_id)["dataSourceSummaries"]
    for s in sources:
        print(f"  删除旧数据源 {s['dataSourceId']} ...")
        agent_client.delete_data_source(
            knowledgeBaseId=kb_id, dataSourceId=s["dataSourceId"]
        )
    if sources:
        # 删除是异步的，紧接着创建同名数据源会撞 ConflictException
        time.sleep(10)

    print(f"  创建数据源（切块 {CHUNK_MAX_TOKENS} token / 重叠 {CHUNK_OVERLAP_PCT}%）...")
    resp = agent_client.create_data_source(
        knowledgeBaseId=kb_id,
        name=DATA_SOURCE_NAME,
        description="OPC Ops Copilot 运营规则文档",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {"bucketArn": f"arn:aws:s3:::{bucket_name}"},
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": CHUNK_MAX_TOKENS,
                    "overlapPercentage": CHUNK_OVERLAP_PCT,
                },
            }
        },
        dataDeletionPolicy="DELETE",
    )
    ds_id = resp["dataSource"]["dataSourceId"]
    print(f"  ✓ 数据源 {ds_id}")
    time.sleep(5)
    return ds_id


def upload_docs(region: str, bucket_name: str) -> int:
    """上传 kb_docs/ 下的文档，顺带再守一次字节上限。

    00_validate_data.py 已经查过一遍，这里再查是因为文档可能在两次运行之间
    被改过——上传完才发现超限的话，得等 ingestion 跑完失败才知道。
    """
    s3 = boto3.client("s3", region_name=region)
    count = 0
    for filename in sorted(os.listdir(KB_DOCS_DIR)):
        if not filename.endswith((".md", ".txt")):
            continue
        local_path = os.path.join(KB_DOCS_DIR, filename)
        size = os.path.getsize(local_path)
        if size > 1800:
            raise RuntimeError(
                f"{filename} 为 {size} 字节，超过 1800 上限，上传会导致 ingestion 失败。"
                f"请先精简，再重跑本脚本。"
            )
        # cloudlab 的 SCP 强制 SSE：PutObject 不带 ServerSideEncryption 头会被显式拒绝，
        # 与桶的默认加密无关（SCP 看的是请求头）。
        s3.upload_file(
            local_path, bucket_name, filename,
            ExtraArgs={"ServerSideEncryption": "AES256"},
        )
        print(f"  已上传 {filename}（{size} 字节）")
        count += 1
    if count == 0:
        raise RuntimeError(f"{KB_DOCS_DIR} 下没有 .md/.txt 文档。")
    return count


def run_ingestion(agent_client, kb_id: str, ds_id: str) -> None:
    job_id = agent_client.start_ingestion_job(
        knowledgeBaseId=kb_id, dataSourceId=ds_id, description="OPC rules sync",
    )["ingestionJob"]["ingestionJobId"]
    print(f"  ingestion job {job_id}")

    waited = 0
    while waited < INGESTION_MAX_WAIT:
        job = agent_client.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )["ingestionJob"]
        status = job["status"]
        if status == "COMPLETE":
            st = job.get("statistics", {})
            print(f"  ✓ 同步完成：索引 {st.get('numberOfNewDocumentsIndexed', '?')} 篇 / "
                  f"扫描 {st.get('numberOfDocumentsScanned', '?')} 篇")
            return
        if status in ("FAILED", "STOPPED"):
            raise RuntimeError(
                f"ingestion 失败（{status}）：{job.get('failureReasons', '')}\n"
                f"若报 'Filterable metadata must have at most 2048 bytes'，"
                f"说明切块仍偏大或某篇文档过长。"
            )
        time.sleep(INGESTION_WAIT_INTERVAL)
        waited += INGESTION_WAIT_INTERVAL
        print(f"    {status}，已等待 {waited}s")
    raise TimeoutError(f"ingestion 等待超时（{INGESTION_MAX_WAIT}s）")


def smoke_test(region: str, kb_id: str) -> None:
    rt = boto3.client("bedrock-agent-runtime", region_name=region)
    for q in SMOKE_QUERIES:
        resp = rt.retrieve(
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": q},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": 2}},
        )
        results = resp.get("retrievalResults", [])
        print(f"\n  「{q}」")
        if not results:
            raise RuntimeError("检索结果为空——ingestion 可能还没生效，稍等重跑本脚本。")
        for item in results:
            text = item["content"]["text"].replace("\n", " ")
            src = item.get("location", {}).get("s3Location", {}).get("uri", "")
            print(f"    score={item.get('score', 0):.4f}  {os.path.basename(src)}")
            print(f"    {text[:90]}…")


def main() -> int:
    env = load_env_state()
    region = env["region"]
    account_id = env["account_id"]

    print("=" * 60)
    print("Phase 2 · 知识库数据同步")
    print("=" * 60)

    agent_client = boto3.client("bedrock-agent", region_name=region)

    kb = find_kb(agent_client)
    kb_id = kb["knowledgeBaseId"]
    print(f"\n✓ 知识库 {kb_id}（{kb['name']}）")

    bucket = find_docs_bucket(agent_client, kb_id, account_id, region)
    print(f"✓ 文档桶 {bucket}")

    print("\n重建数据源...")
    ds_id = recreate_data_source(agent_client, kb_id, bucket)

    print("\n上传文档...")
    upload_docs(region, bucket)

    print("\n触发向量同步...")
    run_ingestion(agent_client, kb_id, ds_id)

    print("\n检索冒烟测试：")
    smoke_test(region, kb_id)

    save_json(KB_STATE_FILE, {
        "region": region,
        "kb_id": kb_id,
        "kb_name": kb["name"],
        "data_source_id": ds_id,
        "docs_bucket": bucket,
        "chunk_max_tokens": CHUNK_MAX_TOKENS,
    })
    print(f"\n✓ 知识库就绪，状态已写入 {KB_STATE_FILE}")
    print("\n下一步：/usr/bin/python3.11 opc_copilot/scripts/06_deploy_mcp_runtime.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
