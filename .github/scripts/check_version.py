#!/usr/bin/env python3
"""tag ↔ metadata.yaml version 一致性校验（release 工作流的第一道闸门）。

用法：
    python3 .github/scripts/check_version.py v0.5.0
        → tag 与 metadata.yaml 的 version 一致：往 stdout 打印 version（0.5.0），exit 0
        → 不一致 / 读不到 version：往 stderr 打 ::error:: 注解，exit 1

这个脚本也是「打 tag 前最常见的翻车点」的唯一守卫：忘改 metadata.yaml 就 tag，这里会红。
本地自测：
    python3 .github/scripts/check_version.py v0.5.0    # 应通过
    python3 .github/scripts/check_version.py v9.9.9    # 应失败（示范反例）
"""

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_version():
    path = os.path.join(ROOT, "metadata.yaml")
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    m = re.search(r"^version\s*:\s*(.+?)\s*$", raw, re.MULTILINE)
    if not m:
        return ""
    return m.group(1).strip().strip('"').strip("'")


def main(argv):
    if len(argv) == 2 and argv[1] == "--print-tag":
        version = read_version()
        if not version:
            print("::error file=metadata.yaml::metadata.yaml 里没读到 version 字段", file=sys.stderr)
            return 1
        print(f"v{version}")
        return 0

    if len(argv) != 2:
        print("用法: check_version.py <tag，如 v0.5.0> | check_version.py --print-tag", file=sys.stderr)
        return 2

    tag = argv[1].strip()
    version = read_version()
    if not version:
        print("::error file=metadata.yaml::metadata.yaml 里没读到 version 字段", file=sys.stderr)
        return 1

    expected = f"v{version}"
    if tag != expected:
        print(
            f"::error::tag {tag!r} 与 metadata.yaml 的 version {version!r} 不一致（应为 {expected!r}）",
            file=sys.stderr,
        )
        print(
            f"::error::先改 metadata.yaml 的 version 并提交，再打 tag：git tag {expected} && git push origin {expected}",
            file=sys.stderr,
        )
        return 1

    print(version)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
