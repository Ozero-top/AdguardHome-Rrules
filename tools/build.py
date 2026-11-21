#!/usr/bin/env python3
"""规则构建：抓取 → 归一化 → 分类 → 去重 → 分档产物。

用法：
    python tools/build.py                 输出到 build/
    python tools/build.py --out out       指定输出目录
    python tools/build.py --offline       只用 build/cache 里的缓存，不联网

退出码 0 表示构建成功；1 表示有源失败或产物异常。
"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import shutil
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "sources.toml"
CACHE = ROOT / ".cache"
REPO_URL = "https://github.com/Ozero-top/adguardhome-rules"

PSL_URL = "https://publicsuffix.org/list/public_suffix_list.dat"
PSL_TTL = 7 * 24 * 3600
TRANCO_URL = "https://tranco-list.eu/top-1m.csv.zip"
TRANCO_TTL = 7 * 24 * 3600

# 子域聚合门槛：只对「整域只做广告」的高置信根域做整域兜底
AGG_MIN_CHILDREN = 10      # 同一根域下被拦截的子域数量
AGG_MIN_SOURCES = 3        # 每个子域至少被这么多上游同时收录
AGG_RANK_LIMIT = 1000000   # Tranco 前 100 万内的根域不聚合（避免误杀主站）

# 公共后缀规则，构建开始时加载；未加载时跳过相关校验
_PSL_EXACT = set()
_PSL_WILD = set()
_PSL_EXC = set()
_TRANCO = None


# 直连失败后依次尝试国内反代，前缀直接拼在原始 URL 前面
PROXIES = ["", "https://gh-proxy.org/", "https://ghfast.top/", "https://gh-proxy.com/"]
USER_AGENT = "adguardhome-rules-builder/1.0"
TIMEOUT = 25

IPV4_PREFIX = re.compile(r"^(?:0\.0\.0\.0|127\.0\.0\.1|255\.255\.255\.255)\s+(\S+)")
IPV6_PREFIX = re.compile(r"^(?:::1?|0:0:0:0:0:0:0:1)\s+(\S+)")
HOSTS_SUFFIX = re.compile(r"\s+#.*$")
DOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$")
INVALID_DOMAINS = {
    "localhost", "localhost.localdomain", "local", "broadcasthost",
    "ip6-localhost", "ip6-loopback", "ip6-allnodes", "ip6-allrouters",
    "0.0.0.0", "127.0.0.1", "::1",
}


def log(msg):
    print(msg, flush=True)


def fetch(url, offline=False):
    """下载单个源，返回 (文本, 使用的地址, 字节数)。offline 时只读缓存。"""
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    cache_file = CACHE / ("%s.txt" % digest)
    if offline:
        if not cache_file.exists():
            raise RuntimeError("离线模式缺少缓存：%s" % url)
        blob = cache_file.read_bytes()
        return blob.decode("utf-8", "replace"), "cache", len(blob)

    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < 12 * 3600:
        blob = cache_file.read_bytes()
        return blob.decode("utf-8", "replace"), "cache", len(blob)

    last_error = None
    context = ssl.create_default_context()
    for prefix in PROXIES:
        target = prefix + url
        for attempt in range(1):
            try:
                request = urllib.request.Request(target, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(request, timeout=TIMEOUT, context=context) as resp:
                    blob = resp.read()
                if not blob:
                    raise RuntimeError("响应为空")
                CACHE.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(blob)
                return blob.decode("utf-8", "replace"), target, len(blob)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
    raise RuntimeError("下载失败：%s（%s）" % (url, last_error))


def _cached_download(url, cache_name, ttl, offline=False):
    """带缓存的整文件下载，失败依次尝试反代。"""
    cache_file = CACHE / cache_name
    if offline:
        if not cache_file.exists():
            raise RuntimeError("离线模式缺少缓存：%s" % url)
        return cache_file.read_bytes()
    if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
        return cache_file.read_bytes()

    last_error = None
    context = ssl.create_default_context()
    for prefix in PROXIES:
        try:
            request = urllib.request.Request(prefix + url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=120, context=context) as resp:
                blob = resp.read()
            if not blob:
                raise RuntimeError("响应为空")
            CACHE.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(blob)
            return blob
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError("下载失败：%s（%s）" % (url, last_error))


def load_psl(offline=False):
    """加载 Public Suffix List；失败时禁用后缀校验与子域聚合。"""
    global _PSL_EXACT, _PSL_WILD, _PSL_EXC
    try:
        blob = _cached_download(PSL_URL, "psl.dat", PSL_TTL, offline)
    except Exception as exc:  # noqa: BLE001
        log("  警告：公共后缀列表加载失败，跳过后缀校验与子域聚合（%s）" % exc)
        return False

    exact, wild, exceptions = set(), set(), set()
    for line in blob.decode("utf-8", "replace").splitlines():
        text = line.strip()
        if not text or text.startswith("//"):
            continue
        if text.startswith("!"):
            exceptions.add(text[1:].lower())
        elif text.startswith("*."):
            wild.add(text[2:].lower())
        else:
            exact.add(text.lower())
    _PSL_EXACT, _PSL_WILD, _PSL_EXC = exact, wild, exceptions
    log("  公共后缀规则        %7d 条" % (len(exact) + len(wild) + len(exceptions)))
    return True


def load_tranco(offline=False):
    """加载 Tranco Top-1M 热度表；失败时禁用子域聚合（校验仍生效）。"""
    global _TRANCO
    try:
        blob = _cached_download(TRANCO_URL, "tranco-top1m.zip", TRANCO_TTL, offline)
        with zipfile.ZipFile(io.BytesIO(blob)) as archive:
            with archive.open(archive.namelist()[0]) as handle:
                names = set()
                for line in io.TextIOWrapper(handle, encoding="utf-8"):
                    parts = line.strip().split(",")
                    if len(parts) == 2:
                        names.add(parts[1].lower())
    except Exception as exc:  # noqa: BLE001
        log("  警告：Tranco 热度表加载失败，跳过子域聚合（%s）" % exc)
        _TRANCO = None
        return False
    _TRANCO = names
    log("  Tranco 热度表        %7d 条" % len(names))
    return True


def suffix_len(labels):
    """返回公共后缀占用的标签数（标准 PSL 匹配：精确 > 通配 > 例外）。"""
    best = 0
    for i in range(len(labels)):
        candidate = ".".join(labels[i:])
        if candidate in _PSL_EXC:
            best = max(best, len(labels) - i - 1)
        elif candidate in _PSL_EXACT:
            best = max(best, len(labels) - i)
        elif i > 0 and candidate in _PSL_WILD:
            best = max(best, len(labels) - i + 1)
    return best or 1


def is_public_suffix(host):
    """域名本身是公共后缀（com.cn、github.io）时返回 True。"""
    if not _PSL_EXACT and not _PSL_WILD:
        return False
    labels = host.split(".")
    return len(labels) <= suffix_len(labels)


def registrable_domain(host):
    """按 PSL 取可注册域；未加载 PSL 时退化为后两段。"""
    labels = host.split(".")
    if not _PSL_EXACT and not _PSL_WILD:
        return ".".join(labels[-2:]) if len(labels) >= 2 else None
    size = suffix_len(labels)
    if len(labels) <= size:
        return None
    return ".".join(labels[-(size + 1):])


def normalize_domain(raw):
    """清洗单个域名，非法返回 None。"""
    name = raw.strip().lower().strip(".")
    if not name or name in INVALID_DOMAINS:
        return None
    if "*" in name or "?" in name or "/" in name or ":" in name or "@" in name:
        return None
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(name) > 253 or not DOMAIN.match(name):
        return None
    if name.rsplit(".", 1)[-1] in ("local", "localhost", "invalid", "test"):
        return None
    # 公共后缀本身（com.cn、github.io）不能当拦截规则，否则整片站点被误杀
    if is_public_suffix(name):
        return None
    return name


def parse_line(line):
    """返回 (域名, 是否白名单)；无法解析返回 (None, False)。"""
    text = line.strip()
    if not text or text[0] in "!#[":
        return None, False
    whitelist = text.startswith("@@")
    if whitelist:
        text = text[2:]
    if text.startswith("/") or "##" in text or "#@#" in text or "#?#" in text:
        return None, False

    if text.startswith("||"):
        body = text[2:]
        # 带路径的规则（||baidu.com/adrc.js）只针对具体 URL，DNS 过滤无法表达；
        # 提取根域会把整站拦掉，必须整条丢弃。
        if "/" in body:
            return None, False
        if "$" in body:
            rule, mods = body.split("$", 1)
            # $domain=、$removeparam、$cookie 等修饰只针对特定页面或参数，
            # 去掉修饰后变成整域拦截会误杀正常站点，必须丢弃。
            # $third-party 在 DNS 层没有"第三方"概念，但语义就是拦这个域，
            # 降级为纯域规则是安全的，可补回 tanx.com 这类动态子域的根域兜底。
            if {m.strip().lower() for m in mods.split(",") if m.strip()} - {"important", "third-party"}:
                return None, False
            body = rule
        if "^" in body:
            body, rest = body.split("^", 1)
            # ||domain^extra 表示 URL 级例外（如 ^*&cb=），DNS 层无法表达，
            # 保留会把整个域名放行/拦截，直接丢弃。
            if rest not in ("", "|"):
                return None, False
        else:
            body = body.split("|", 1)[0]
        return normalize_domain(body), whitelist

    if text.startswith("|") or text.startswith("http://") or text.startswith("https://"):
        return None, False

    # Surge / Shadowrocket 模块语法：只取 DOMAIN 与 DOMAIN-SUFFIX，
    # URL-REWRITE、IP-CIDR、USER-AGENT 等类型在 DNS 层无法表达，直接丢弃。
    upper = text.upper()
    if upper.startswith("DOMAIN-SUFFIX,") or upper.startswith("DOMAIN,"):
        parts = text.split(",")
        if len(parts) < 2:
            return None, False
        return normalize_domain(parts[1]), whitelist

    text = HOSTS_SUFFIX.sub("", text)
    match = IPV4_PREFIX.match(text) or IPV6_PREFIX.match(text)
    if match:
        return normalize_domain(match.group(1)), whitelist

    return normalize_domain(text.split()[0]), whitelist


def parse_source(text):
    """解析整份规则，返回 (黑名单域名集合, 白名单域名集合)。"""
    blocked, allowed = set(), set()
    for line in text.splitlines():
        domain, is_allow = parse_line(line)
        if not domain:
            continue
        (allowed if is_allow else blocked).add(domain)
    return blocked, allowed


def write_rule_file(path, title, desc, categories, domains, sources, version, now):
    path.parent.mkdir(parents=True, exist_ok=True)
    header = [
        "! Title: %s" % title,
        "! Description: %s" % desc,
        "! Version: %s" % version,
        "! Updated: %s" % now,
        "! Count: %d" % len(domains),
        "! Sources: %s" % ", ".join(sources),
        "! License: %s/blob/main/LICENSES.md" % REPO_URL,
        "!",
    ]
    body = ["||%s^" % domain for domain in domains]
    path.write_text("\n".join(header + body) + "\n", encoding="utf-8", newline="\n")


def render_licenses(config):
    lines = [
        "# 规则来源与许可",
        "",
        "本仓库产物由下列上游规则集合并、去重、分类生成。各上游的著作权与许可归其作者所有，",
        "分发与本仓库产物使用请遵守对应许可；标注「见上游仓库」的条目请以该仓库声明为准。",
        "",
        "| 来源 | 说明 | 许可 | 仓库 |",
        "|---|---|---|---|",
    ]
    for item in config["sources"] + config.get("whitelist", []):
        lines.append("| %s | %s | %s | %s |" % (
            item["name"], item["id"], item.get("license", "-"), item.get("homepage", "-")))
    lines += [
        "",
        "AdGuard 官方规则集（FiltersRegistry / HostlistsRegistry）为 GPL-3.0；",
        "anti-AD 为 GPL-3.0；其余来源许可见上表链接。如上游变更许可，请以变更后的声明为准。",
        "",
    ]
    return "\n".join(lines)


def clean_output(out):
    """只清理本脚本生成的产物，不动输出目录里的其他文件。"""
    for name in ("512", "1024", "2048", "manual", "cache"):
        shutil.rmtree(out / name, ignore_errors=True)
    for name in ("allowlist.txt", "meta.json", "LICENSES.md", "agg-candidates.txt"):
        (out / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "build"))
    parser.add_argument("--offline", action="store_true", help="只用缓存，不联网")
    parser.add_argument("--workers", type=int, default=6)
    args = parser.parse_args()

    out = Path(args.out)
    clean_output(out)
    out.mkdir(parents=True, exist_ok=True)
    config = tomllib.loads(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    version = datetime.now(timezone.utc).strftime("%Y%m%d")

    sources = config["sources"]
    whitelist_sources = config.get("whitelist", [])
    priorities = config.get("source_priority", {})

    # 后缀校验与聚合都依赖 PSL；热度表只用于聚合护栏，失败不影响构建
    load_psl(args.offline)
    load_tranco(args.offline)

    # 抓取（并发）
    results = {}
    failures = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, item["url"], args.offline): item for item in sources + whitelist_sources}
        for future in concurrent.futures.as_completed(futures):
            item = futures[future]
            try:
                text, used, size = future.result()
                results[item["id"]] = {"text": text, "used": used, "bytes": size}
                log("  OK   %-22s %7d 字节" % (item["id"], size))
            except Exception as exc:  # noqa: BLE001
                failures.append(item["id"])
                log("  FAIL %-22s %s" % (item["id"], exc))

    # 归一化 + 分类
    blocked = {}   # domain -> {"categories": set(), "sources": set()}
    allowed = {}   # domain -> set(source name)
    source_stats = {}
    for item in sources:
        data = results.get(item["id"])
        if not data:
            source_stats[item["id"]] = {"status": "failed"}
            continue
        domains, allows = parse_source(data["text"])
        for domain in domains:
            entry = blocked.setdefault(domain, {"categories": set(), "sources": set()})
            entry["categories"].update(item["categories"])
            entry["sources"].add(item["id"])
            # 源可信度：预算截断时优先保留高可信来源的域名
            entry["priority"] = max(entry.get("priority", 0), int(priorities.get(item["id"], 0)))
        for domain in allows:
            allowed.setdefault(domain, set()).add(item["name"])
        source_stats[item["id"]] = {
            "status": "ok",
            "url": item["url"],
            "bytes": data["bytes"],
            "lines": data["text"].count("\n") + 1,
            "domains": len(domains),
            "allow": len(allows),
            "effective_rate": round(len(domains) / max(1, data["text"].count("\n") + 1), 4),
            "categories": item["categories"],
        }

    for item in whitelist_sources:
        data = results.get(item["id"])
        if not data:
            source_stats[item["id"]] = {"status": "failed"}
            continue
        domains, allows = parse_source(data["text"])
        merged = domains | allows
        for domain in merged:
            allowed.setdefault(domain, set()).add(item["name"])
        source_stats[item["id"]] = {
            "status": "ok",
            "url": item["url"],
            "bytes": data["bytes"],
            "lines": data["text"].count("\n") + 1,
            "domains": len(merged),
            "effective_rate": round(len(merged) / max(1, data["text"].count("\n") + 1), 4),
        }

    # 手工维护的清单在预算截断时必须保留，否则会被上游大列表挤掉
    pinned = set()

    # 手工维护的广告根域：上游只收具体子域，动态子域会漏，用这份清单做根域兜底。
    # 不做自动根域聚合，否则会误杀 *.com.br、*.vercel.app 这类公共/托管域。
    roots_file = ROOT / "tools" / "ad-roots.txt"
    roots_added = 0
    if roots_file.exists():
        roots, _ = parse_source(roots_file.read_text(encoding="utf-8"))
        for domain in roots:
            entry = blocked.setdefault(domain, {"categories": set(), "sources": set()})
            if "ads" not in entry["categories"]:
                entry["categories"].add("ads")
                roots_added += 1
            entry["sources"].add("ad-roots")
            pinned.add(domain)
        log("  手工广告根域        %7d 条（新增 %d）" % (len(roots), roots_added))

    # 国内平台自有广告子域：通用列表覆盖不到 B 站、微博、抖音等平台的广告子域
    # 文件内可用 "!category ads" 切换后续条目的归属分类
    platform_file = ROOT / "tools" / "cn-platform-ads.txt"
    platform_added = 0
    if platform_file.exists():
        cat = "chinese"
        platform_domains = []
        for raw_line in platform_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("!category"):
                parts = line.split(None, 1)
                if len(parts) > 1:
                    cat = parts[1].strip()
                continue
            if not line or line.startswith("!") or line.startswith("#"):
                continue
            entry = blocked.setdefault(line, {"categories": set(), "sources": set()})
            if cat not in entry["categories"]:
                entry["categories"].add(cat)
                platform_added += 1
            entry["sources"].add("cn-platform-ads")
            pinned.add(line)
            platform_domains.append(line)
        log("  国内平台广告子域    %7d 条（新增 %d）" % (len(platform_domains), platform_added))

    # 子域聚合：把同一根域下大量广告子域收敛为整域规则，覆盖平台临时新增的子域。
    # 默认只产出候选清单——自动并集会误杀 americanexpress.co.uk、ricoh.ch 这类
    # 有真实主站的域名（其子域被多个列表误标），确认后写进 tools/ad-roots.txt，
    # 或在 sources.toml 的 [aggregation] auto = true 时自动并入。
    agg = config.get("aggregation", {})
    auto_merge = bool(agg.get("auto", False))
    min_children = int(agg.get("min_children", AGG_MIN_CHILDREN))
    min_sources = int(agg.get("min_sources", AGG_MIN_SOURCES))

    agg_roots = []
    agg_candidates = []
    if _TRANCO is not None and (_PSL_EXACT or _PSL_WILD):
        protected = set(allowed)
        protected |= {r for r in (registrable_domain(a) for a in allowed) if r}
        groups = {}
        for domain, entry in blocked.items():
            if domain in protected:
                continue
            root = registrable_domain(domain)
            if not root or root == domain:
                continue
            groups.setdefault(root, []).append((domain, len(entry["sources"])))

        for root, children in groups.items():
            if len(children) < min_children:
                continue
            if root in blocked or root in protected:
                continue
            if min(count for _domain, count in children) < min_sources:
                continue
            # 根域在 Tranco 前 100 万内说明有真实主站流量，聚合会连主站一起拦掉
            if root in _TRANCO:
                continue
            agg_candidates.append((root, len(children), min(c for _d, c in children)))
            if not auto_merge:
                continue
            categories = set()
            shared_sources = None
            for domain, _count in children:
                categories |= blocked[domain]["categories"]
                items = blocked[domain]["sources"]
                shared_sources = set(items) if shared_sources is None else (shared_sources & items)
            blocked[root] = {"categories": categories, "sources": shared_sources or set()}
            agg_roots.append(root)

        log("  子域聚合候选        %7d 条（自动并入 %d）" % (len(agg_candidates), len(agg_roots)))
    else:
        log("  子域聚合            跳过（PSL 或 Tranco 未就绪）")

    # 无论是否参与聚合都输出候选文件，方便人工确认与留存
    agg_candidates.sort()
    candidate_lines = ["# 子域聚合候选：同一根域下大量广告子域", "# 格式：根域  子域数  最小收录源数",
                       "# 确认无误后复制根域到 tools/ad-roots.txt，或在 sources.toml 开启 [aggregation] auto"]
    candidate_lines += ["%s\t%d\t%d" % item for item in agg_candidates]
    (out / "agg-candidates.txt").write_text("\n".join(candidate_lines) + "\n",
                                            encoding="utf-8", newline="\n")

    log("")
    log("合并后黑名单 %d 条，白名单 %d 条" % (len(blocked), len(allowed)))

    # 白名单从黑名单剔除：白名单域名及其子域不应再出现在分类文件里，
    # 否则只订阅分类文件、没订阅 allowlist.txt 的用户仍会被误杀。
    allow_removed = 0
    if allowed:
        victims = []
        for domain in blocked:
            labels = domain.split(".")
            if any(".".join(labels[i:]) in allowed for i in range(len(labels))):
                victims.append(domain)
        for domain in victims:
            del blocked[domain]
        allow_removed = len(victims)
        log("  白名单剔除          %7d 条" % allow_removed)

    # 构建级免拦清单：上游误标的基础服务域（如 gw.alicdn.com）在此剔除，
    # 同时并入白名单产物，保证只订阅 allowlist 的用户也能放行。
    protect_file = ROOT / "tools" / "protected-domains.txt"
    protect_set = set()
    protect_removed = 0
    if protect_file.exists():
        protect_set, _ = parse_source(protect_file.read_text(encoding="utf-8"))
        for domain in protect_set:
            victims = [d for d in blocked if d == domain or d.endswith("." + domain)]
            for victim in victims:
                blocked.pop(victim)
                protect_removed += 1
            allowed.setdefault(domain, set()).add("protected-domains")
        log("  免拦清单            %7d 条（剔除 %d）" % (len(protect_set), protect_removed))

    # 分档：一条规则在档内只归一个分类，按 tiers 顺序取第一个命中
    tiers = config["tiers"]
    budgets = config.get("budgets", {})
    categories = config["categories"]
    meta_tiers = {}
    for tier in sorted(tiers, key=int):
        order = tiers[tier]
        tier_budget = budgets.get(str(tier), {})
        buckets = {name: [] for name in order}
        for domain in sorted(blocked):
            labels = blocked[domain]["categories"]
            for name in order:
                if name in labels:
                    buckets[name].append((domain, blocked[domain].get("priority", 0),
                                          len(blocked[domain]["sources"]), domain in pinned))
                    break
        meta_files = {}
        meta_dropped = {}
        for name in order:
            items = buckets[name]
            limit = tier_budget.get(name)
            dropped = 0
            if limit and len(items) > limit:
                # 手工清单优先，其次源可信度、被更多上游同时收录，最后按域名稳定排序。
                # 只用源数排序会让低预算档按字母序随机切掉高价值域名（如恶意软件清单）。
                items.sort(key=lambda pair: (0 if pair[3] else 1, -pair[1], -pair[2], pair[0]))
                dropped = len(items) - limit
                items = items[:limit]
            domains = sorted(domain for domain, _p, _count, _pinned in items)
            info = categories.get(name, {})
            write_rule_file(
                out / str(tier) / ("%s.txt" % name),
                "AGH Rules %s · %s" % (tier, info.get("title", name)),
                info.get("desc", name),
                [name], domains,
                sorted({s["name"] for s in sources if name in s["categories"]}),
                version, now,
            )
            meta_files[name] = len(domains)
            if dropped:
                meta_dropped[name] = dropped
            log("  %s/%-11s %7d 条%s" % (
                tier, name, len(domains),
                "（预算 %d，截断 %d）" % (limit, dropped) if dropped else ""))
        meta_tiers[str(tier)] = {
            "files": meta_files,
            "total": sum(meta_files.values()),
            "dropped": meta_dropped,
        }

    # 白名单与成人内容单独发布
    allow_sources = {s["name"] for s in whitelist_sources}
    if protect_set:
        allow_sources.add("protected-domains")
    write_rule_file(out / "allowlist.txt", "AGH Rules · 白名单", "放行域名，优先级高于所有拦截规则",
                    ["whitelist"], sorted(allowed), sorted(allow_sources), version, now)
    log("  allowlist.txt      %7d 条" % len(allowed))

    adult = sorted(d for d, entry in blocked.items() if "adult" in entry["categories"])
    if adult:
        write_rule_file(out / "manual" / "nsfw.txt", "AGH Rules · 成人内容", "需手动开启，不进入任何档位",
                        ["adult"], adult, ["HaGeZi NSFW"], version, now)
        log("  manual/nsfw.txt    %7d 条" % len(adult))

    (out / "LICENSES.md").write_text(render_licenses(config), encoding="utf-8", newline="\n")

    meta = {
        "version": version,
        "generated_at": now,
        "blocked_total": len(blocked),
        "allow_total": len(allowed),
        "allow_removed": allow_removed,
        "ad_roots_added": roots_added,
        "protected_domains": sorted(protect_set),
        "protected_removed": protect_removed,
        "agg_roots_added": len(agg_roots),
        "agg_candidates_total": len(agg_candidates),
        "agg_candidates_sample": [item[0] for item in agg_candidates[:50]],
        "tiers": meta_tiers,
        "sources": source_stats,
        "failures": failures,
    }
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                   encoding="utf-8", newline="\n")

    if failures:
        log("")
        log("有 %d 个源下载失败：%s" % (len(failures), ", ".join(failures)))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
