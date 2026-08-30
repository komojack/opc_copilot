"""
Phase 6 · 上传 Skills 到 S3 并挂到 Harness
--------------------------------------------------------
运行：
    python scripts/11_upload_skills.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import boto3
from botocore.exceptions import ClientError
from common import HARNESS_STATE_FILE, PROJECT_ROOT, load_env_state, load_harness_state, save_json

SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
S3_PREFIX = "skills"


def find_skills_bucket(s3, account_id: str, region: str) -> str:
    """template-opc.yaml 建的桶，名字是确定的。"""
    name = f"opc-copilot-skills-{account_id}-{region}"
    try:
        s3.head_bucket(Bucket=name)
        return name
    except ClientError as e:
        raise SystemExit(
            f"找不到 Skills 桶 {name}（{e.response['Error']['Code']}）。\n"
            f"请先部署 template-opc.yaml：\n"
            f"  aws cloudformation deploy --template-file template-opc.yaml \\\n"
            f"      --stack-name opc-copilot --capabilities CAPABILITY_NAMED_IAM"
        )


def collect_skills() -> list[str]:
    """列出 skills/ 下所有含 SKILL.md 的子目录。

    skill 的 name 字段必须与目录名一致，否则 Harness 加载时会告警
    （strict 模式下直接报错）。这里顺手校验一遍。
    """
    if not os.path.isdir(SKILLS_DIR):
        raise SystemExit(f"缺少目录 {SKILLS_DIR}")

    found = []
    for entry in sorted(os.listdir(SKILLS_DIR)):
        skill_dir = os.path.join(SKILLS_DIR, entry)
        md_path = os.path.join(skill_dir, "SKILL.md")
        if not os.path.isdir(skill_dir) or not os.path.exists(md_path):
            continue

        with open(md_path, encoding="utf-8") as f:
            head = f.read(600)
        declared = None
        for line in head.splitlines():
            if line.startswith("name:"):
                declared = line.split(":", 1)[1].strip()
                break
        if declared != entry:
            raise SystemExit(
                f"skill 目录名与 frontmatter 的 name 不一致：\n"
                f"  目录 {entry} / name: {declared}\n"
                f"  两者必须相同，否则 Harness 加载时会告警或报错。"
            )
        found.append(entry)

    if not found:
        raise SystemExit(f"{SKILLS_DIR} 下没有找到任何含 SKILL.md 的子目录")
    return found


def upload_skill(s3, bucket: str, skill_name: str) -> str:
    """上传一个 skill 目录（含 scripts/ references/ assets/ 等子目录）。"""
    local_root = os.path.join(SKILLS_DIR, skill_name)
    count = 0
    for dirpath, _dirnames, filenames in os.walk(local_root):
        for fn in filenames:
            local_path = os.path.join(dirpath, fn)
            rel = os.path.relpath(local_path, local_root).replace(os.sep, "/")
            key = f"{S3_PREFIX}/{skill_name}/{rel}"
            s3.upload_file(local_path, bucket, key)
            count += 1
    uri = f"s3://{bucket}/{S3_PREFIX}/{skill_name}/"
    print(f"  ✓ {skill_name:<20} {count} 个文件 → {uri}")
    return uri


def main() -> int:
    env = load_env_state()
    region, account_id = env["region"], env["account_id"]
    state = load_harness_state()

    print("=" * 60)
    print("Phase 6 · Skills")
    print("=" * 60)

    s3 = boto3.client("s3", region_name=region)
    bucket = find_skills_bucket(s3, account_id, region)
    print(f"\nSkills 桶  {bucket}")

    skill_names = collect_skills()
    print(f"\n上传 {len(skill_names)} 个 skill：")
    uris = [upload_skill(s3, bucket, name) for name in skill_names]

    print("\n挂到 Harness ...")
    control = boto3.client("bedrock-agentcore-control", region_name=region)
    control.update_harness(
        harnessId=state["harness_id"],
        skills=[{"s3": {"uri": uri}} for uri in uris],
    )
    print("✓ 已更新")

    state["capabilities"]["skills"] = True
    state["phase"] = 6
    state["skills"] = {"bucket": bucket, "uris": uris, "names": skill_names}
    save_json(HARNESS_STATE_FILE, state)
    print(f"✓ 状态已更新 {HARNESS_STATE_FILE}（phase=6）")

    print("\n注意：Skills 每个会话首次调用时拉取一次，会话内持久化。")
    print("改了 SKILL.md 重传后，已有会话不会更新——用 :new 开新会话再验证。")
    print("\n下一步：")
    print("  python scripts/03_chat.py CLIENT-ABC")
    print("    > ABC 想按老客户价报 4.8 万，行吗？")
    print("    观察模型是否主动调用 skills 工具加载 quote-review")
    print("  python scripts/04_run_evalset.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
