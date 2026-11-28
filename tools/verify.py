#!/usr/bin/env python3
"""产物门禁：语法、条数、重复、与上轮对比的异常波动。

用法：
    python tools/verify.py --out build
    python tools/verify.py --out build --previous old/meta.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

RULE = re.compile(r"^\|\|[a-z0-9.-]+\^$")
HEADER = re.compile(r"^!")
COUNT = re.compile(r"^! Count: (\d+)$")
DRIFT_LIMIT = 0.30      # 单文件条数相对上轮变化超过 30% 视为异常
DRIFT_MIN = 1000        # 绝对变化不足这么多条时不算异常，避免小文件自然波动误报
MIN_TOTAL = 1000        # 任一档位总条数下限


def fail(errors, message):
    errors.append(message)
    print("  FAIL %s" % message)


def load_meta(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def check_file(path, errors):
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or not lines[0].startswith("! Title:"):
        fail(errors, "%s 缺少标题头" % path.name)
        return 0

    count = None
    for line in lines:
        match = COUNT.match(line)
        if match:
            count = int(match.group(1))
            break
    if count is None:
        fail(errors, "%s 缺少 Count 字段" % path.name)

    rules = [line for line in lines if not HEADER.match(line)]
    for line in rules:
        if not RULE.match(line):
            fail(errors, "%s 存在非法行：%s" % (path.name, line[:60]))
            break
    if count is not None and count != len(rules):
        fail(errors, "%s Count=%d 与实际 %d 条不一致" % (path.name, count, len(rules)))
    return len(rules)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="build")
    parser.add_argument("--previous", default="", help="上一轮 meta.json，用于波动对比")
    parser.add_argument("--allow-drift", action="store_true", help="允许超过 30%% 的波动")
    args = parser.parse_args()

    out = Path(args.out)
    meta_path = out / "meta.json"
    if not meta_path.exists():
        print("没有找到 %s，先运行 tools/build.py" % meta_path)
        return 1
    meta = load_meta(meta_path)
    previous = load_meta(args.previous) if args.previous else None
    errors = []

    # 1. 逐文件校验并统计
    print("产物校验：")
    totals = {}
    for tier, info in meta["tiers"].items():
        total, seen = 0, {}
        for name in info["files"]:
            path = out / tier / ("%s.txt" % name)
            if not path.exists():
                fail(errors, "缺少 %s/%s.txt" % (tier, name))
                continue
            count = check_file(path, errors)
            total += count
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.startswith("!"):
                    if line in seen:
                        fail(errors, "%s 档内重复：%s 同时出现在 %s 和 %s"
                             % (tier, line, seen[line], name))
                        break
                    seen[line] = name
        totals[tier] = total
        print("  %-5s 合计 %7d 条" % (tier, total))
        if total < MIN_TOTAL:
            fail(errors, "%s 档总条数 %d 低于下限 %d" % (tier, total, MIN_TOTAL))

    for name in ("allowlist.txt",):
        path = out / name
        if not path.exists():
            fail(errors, "缺少 %s" % name)
        else:
            print("  %-5s %7d 条" % (name, check_file(path, errors)))

    # 2. 与上轮对比
    if previous and not args.allow_drift:
        for tier, info in meta["tiers"].items():
            for name, current in info["files"].items():
                old = previous.get("tiers", {}).get(tier, {}).get("files", {}).get(name)
                if old is None:
                    continue
                if old == 0 and current == 0:
                    continue
                if old and abs(current - old) / old > DRIFT_LIMIT and abs(current - old) >= DRIFT_MIN:
                    fail(errors, "%s/%s 条数从 %d 变为 %d，波动超过 %d%%"
                         % (tier, name, old, current, int(DRIFT_LIMIT * 100)))

    # 3. 源失败率
    sources = meta.get("sources", {})
    failed = [sid for sid, st in sources.items() if st.get("status") != "ok"]
    if sources and len(failed) / len(sources) > 0.2:
        fail(errors, "%d/%d 个源下载失败，超过 20%%" % (len(failed), len(sources)))

    print("")
    if errors:
        print("校验未通过，共 %d 项问题，不发布产物。" % len(errors))
        return 1
    print("校验通过：%d 个档位，%d 个源。" % (len(totals), len(sources)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
