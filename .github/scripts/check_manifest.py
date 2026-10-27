#!/usr/bin/env python3
"""manifest 体检：校验 AstrBot 插件仓库根目录的两个「元数据文件」。

CI 里由 .github/workflows/ci.yml 的 manifest job 调用；本地也能直接跑：

    GITHUB_REPOSITORY=dian-dian233/astrbot_plugin_agentmail python3 .github/scripts/check_manifest.py

检查项：
  1. metadata.yaml 能被 YAML 解析，且是「键值映射」
  2. 必填字段齐全：name / display_name / desc / version / author / repo / astrbot_version，且都非空
  3. version 形如 0.5.0（release 工作流要靠它跟 tag 对齐）
  4. repo 必须等于 https://github.com/${GITHUB_REPOSITORY}
  5. name 必须等于仓库名（WebUI 靠它认插件，写错了装不上）
  6. _conf_schema.json 是合法 JSON，且每项都有 description / type / default，type 在合法枚举内
"""

import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REQUIRED_FIELDS = ["name", "display_name", "desc", "version", "author", "repo", "astrbot_version"]
ALLOWED_TYPES = {"string", "text", "int", "float", "bool", "list", "dict", "file"}
SEMVER = re.compile(r"^\d+\.\d+\.\d+([-+][0-9A-Za-z.\-]+)?$")

errors = []
warnings = []


def err(msg):
    errors.append(msg)
    print(f"[FAIL] {msg}")


def ok(msg):
    print(f"[ OK ] {msg}")


def warn(msg):
    warnings.append(msg)
    print(f"[WARN] {msg}")


def check_metadata():
    path = os.path.join(ROOT, "metadata.yaml")
    if not os.path.isfile(path):
        err("根目录找不到 metadata.yaml")
        return {}

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    try:
        import yaml
    except ImportError:
        sys.exit("[FAIL] 缺 PyYAML，先 pip install pyyaml")

    try:
        data = yaml.safe_load(raw)
    except Exception as e:  # noqa: BLE001
        err(f"metadata.yaml 不是合法 YAML：{e}")
        return {}

    if not isinstance(data, dict):
        err(f"metadata.yaml 顶层应该是键值映射，实际是 {type(data).__name__}")
        return {}

    ok("metadata.yaml 能被 YAML 解析")

    missing = [k for k in REQUIRED_FIELDS if not str(data.get(k, "")).strip()]
    if missing:
        err(f"metadata.yaml 缺必填字段或值为空：{', '.join(missing)}")
    else:
        ok(f"必填字段齐全：{', '.join(REQUIRED_FIELDS)}")

    version = str(data.get("version", "")).strip()
    if version and not SEMVER.match(version):
        err(f"version={version!r} 不是 x.y.z 形式，release 工作流没法跟 tag 对齐")

    repo = str(data.get("repo", "")).strip()
    expected_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if expected_repo:
        want = f"https://github.com/{expected_repo}"
        if repo != want:
            err(f"metadata.yaml 的 repo 字段是 {repo!r}，应为 {want!r}")
        else:
            ok(f"repo 字段与仓库一致：{repo}")
        repo_name = expected_repo.split("/")[-1]
        if str(data.get("name", "")).strip() != repo_name:
            err(f"name={data.get('name')!r} 与仓库名 {repo_name!r} 不一致（WebUI 可能认不出）")
        else:
            ok(f"name 与仓库名一致：{repo_name}")
    else:
        warn("没设 GITHUB_REPOSITORY，跳过 repo / name 与当前仓库的一致性校验")

    return data


def check_conf_schema():
    path = os.path.join(ROOT, "_conf_schema.json")
    if not os.path.isfile(path):
        err("根目录找不到 _conf_schema.json")
        return

    with open(path, encoding="utf-8") as f:
        raw = f.read()

    try:
        schema = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        err(f"_conf_schema.json 不是合法 JSON：{e}")
        return

    if not isinstance(schema, dict):
        err(f"_conf_schema.json 顶层应该是对象，实际是 {type(schema).__name__}")
        return

    ok(f"_conf_schema.json 是合法 JSON（{len(schema)} 个配置项）")

    bad = []
    for key, item in schema.items():
        if not isinstance(item, dict):
            bad.append(f"{key}: 值不是对象")
            continue
        for field in ("description", "type", "default"):
            if field not in item:
                bad.append(f"{key}: 缺 {field}")
        if "description" in item and not str(item["description"]).strip():
            bad.append(f"{key}: description 是空字符串")
        t = item.get("type")
        if t is not None and t not in ALLOWED_TYPES:
            bad.append(f"{key}: type={t!r} 不在 {sorted(ALLOWED_TYPES)} 里")

    if bad:
        for b in bad:
            err(f"_conf_schema.json → {b}")
    else:
        ok("每个配置项都有 description / type / default，type 合法")


def main():
    print(f"体检目录：{ROOT}\n")
    meta = check_metadata()
    print()
    check_conf_schema()
    print()

    if meta:
        print(
            "概要："
            f"{meta.get('name')} v{meta.get('version')} "
            f"(astrbot_version {meta.get('astrbot_version')})"
        )

    if warnings:
        print(f"\n{len(warnings)} 条 warning（不阻塞）")
    if errors:
        print(f"\n[FAIL] 共 {len(errors)} 个问题：")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\n[ OK ] manifest 体检全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
