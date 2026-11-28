#!/usr/bin/env python3
"""对比本版与上一版 meta.json，生成 GitHub Release 说明。

用法：python tools/release_notes.py [本版 meta] [上版 meta] [输出文件]
上版缺失时（首次发布）只输出本版条数。
"""

import json
import sys
from pathlib import Path

TIER_LABEL = {"512": "512MB", "1024": "1GB", "2048": "2GB+"}
CATEGORY_LABEL = {
    "security": "安全防护",
    "chinese": "中文站点",
    "tracking": "跟踪统计",
    "ads": "通用广告",
    "annoyances": "干扰元素",
    "intl": "外语地区",
    "adult": "成人内容",
}


def load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def fmt(number):
    return "{:,}".format(number)


def delta(cur, prev):
    """返回「+123」「-45」「持平」「新增」。"""
    if prev is None:
        return "新增"
    diff = cur - prev
    return "持平" if diff == 0 else "%+d" % diff


def prev_file_count(prev, tier, name):
    if not prev:
        return None
    return prev.get("tiers", {}).get(tier, {}).get("files", {}).get(name)


def render_tiers(cur, prev):
    lines = ["### 档位明细", "", "| 档位 | 分类 | 本版 | 较上版 |", "|---|---|---|---|"]
    prev_tiers = (prev or {}).get("tiers", {})
    for tier in sorted(cur.get("tiers", {}), key=int):
        files = cur["tiers"][tier].get("files", {})
        for name, count in files.items():
            lines.append("| %s | %s | %s | %s |" % (
                TIER_LABEL.get(tier, tier),
                CATEGORY_LABEL.get(name, name),
                fmt(count),
                delta(count, prev_file_count(prev, tier, name)),
            ))
        total = cur["tiers"][tier].get("total", 0)
        prev_total = prev_tiers.get(tier, {}).get("total")
        lines.append("| **%s 合计** | — | **%s** | **%s** |" % (
            TIER_LABEL.get(tier, tier), fmt(total), delta(total, prev_total)))

    dropped = []
    for tier in sorted(cur.get("tiers", {}), key=int):
        for name, count in cur["tiers"][tier].get("dropped", {}).items():
            dropped.append("%s/%s 截断 %s 条" % (
                TIER_LABEL.get(tier, tier), CATEGORY_LABEL.get(name, name), fmt(count)))
    if dropped:
        lines += ["", "预算截断：" + "；".join(dropped) + "。"]
    return lines


def render_sources(cur, prev):
    sources = cur.get("sources", {})
    ok = [sid for sid, info in sources.items() if info.get("status") == "ok"]
    failures = cur.get("failures") or []
    lines = ["### 上游源", ""]
    lines.append("- 抓取 %d 个源，成功 %d，失败 %d%s。" % (
        len(sources), len(ok), len(failures),
        "（%s）" % "、".join("`%s`" % f for f in failures) if failures else ""))

    prev_sources = (prev or {}).get("sources", {})
    changes = []
    for sid in ok:
        now_count = sources[sid].get("domains", 0)
        before = prev_sources.get(sid, {}).get("domains")
        if before is None:
            continue
        diff = now_count - before
        if diff:
            changes.append((abs(diff), sid, diff))
    if changes:
        changes.sort(reverse=True)
        lines.append("- 域名数变化最大：%s。" % "、".join(
            "`%s` %+d" % (sid, diff) for _size, sid, diff in changes[:6]))
    elif prev:
        lines.append("- 各源域名数与上版一致。")

    fresh = [sid for sid in ok if sid not in prev_sources]
    if fresh and prev:
        lines.append("- 新增源：%s。" % "、".join("`%s`" % s for s in fresh))
    return lines


def render_lists(cur):
    lines = ["### 维护清单", ""]
    lines.append("- 白名单 %s 条；构建时从分类文件剔除 %s 条。" % (
        fmt(cur.get("allow_total", 0)), fmt(cur.get("allow_removed", 0))))
    lines.append("- 免拦清单 %d 条（剔除 %s 条）；强制拦截 %d 条（覆盖白名单 %s 条）。" % (
        len(cur.get("protected_domains") or []), fmt(cur.get("protected_removed", 0)),
        len(cur.get("force_block_domains") or []), fmt(cur.get("force_block_overrode", 0))))
    if cur.get("agg_roots_added"):
        lines.append("- 手工广告根域并入 %d 个；聚合候选 %s 个（未自动并入）。" % (
            cur["agg_roots_added"], fmt(cur.get("agg_candidates_total", 0))))
    else:
        lines.append("- 聚合候选 %s 个（未自动并入）。" % fmt(cur.get("agg_candidates_total", 0)))
    return lines


def main():
    cur_path = sys.argv[1] if len(sys.argv) > 1 else "build/meta.json"
    prev_path = sys.argv[2] if len(sys.argv) > 2 else "previous/meta.json"
    out_path = sys.argv[3] if len(sys.argv) > 3 else "release-notes.md"

    cur = load(cur_path)
    if cur is None:
        print("缺少本版 meta：%s" % cur_path, file=sys.stderr)
        return 1
    prev = load(prev_path)

    lines = ["## 规则更新 %s" % cur.get("version", ""), ""]
    if prev:
        lines.append("对比上一版（%s），生成于 %s。" % (
            prev.get("version", "未知"), cur.get("generated_at", "")))
    else:
        lines.append("首次发布，生成于 %s。" % cur.get("generated_at", ""))
    lines += [""]
    lines += render_tiers(cur, prev)
    lines += [""]
    lines += render_sources(cur, prev)
    lines += [""]
    lines += render_lists(cur)
    lines += ["", "订阅地址见仓库 README，按设备内存只选一档。", ""]

    Path(out_path).write_text("\n".join(lines), encoding="utf-8", newline="\n")
    print("写入 %s（%d 行）" % (out_path, len(lines)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
