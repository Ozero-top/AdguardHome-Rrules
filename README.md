# adguardhome-rules

给 [AdGuard Home 助手（luci-app-adguardhome-lite）](https://github.com/Ozero-top/luci-app-adguardhome-lite) 用的规则构建仓库：每 7 天自动抓取上游规则，归一化、去重、分类，按设备内存分档生成静态规则文件，发布到 `dist` 分支。

## 订阅地址

产物发布在 `dist` 分支，**按设备内存只选一档订阅**，同一档内的文件要全部添加。

可用前缀（国内优先 jsDelivr，不稳定时换 raw）：

```
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/
https://raw.githubusercontent.com/Ozero-top/AdguardHome-Rrules/dist/
```

### 512MB 内存

```
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/512/security.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/512/chinese.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/512/ads.txt
```

### 1GB 内存

```
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/1024/security.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/1024/chinese.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/1024/tracking.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/1024/ads.txt
```

### 2GB 及以上内存

```
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/2048/security.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/2048/chinese.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/2048/tracking.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/2048/ads.txt
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/2048/annoyances.txt
```

### 可选

```
# 白名单，三档通用，建议一起添加
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/allowlist.txt

# 成人内容，默认不启用，需要时手动添加
https://cdn.jsdelivr.net/gh/Ozero-top/AdguardHome-Rrules@dist/manual/nsfw.txt
```

## 档位与分类

| 档位 | 分类文件 | 实测条数 |
|---|---|---|
| 512（512MB 内存） | security 15000、chinese 60000、ads 60000 | 135,000 |
| 1024（1GB 内存） | security 15000、chinese 120000、tracking 8000、ads 89653 | 232,653 |
| 2048（2GB 及以上） | security 236414、chinese 217140、tracking 56122、ads 89653、annoyances 341 | 599,670 |

- 同一档位内各分类文件互不重复：一条域名只归属档内优先级最高的分类。
- 超出单类预算时，按「源可信度（`[source_priority]`）→ 被更多上游同时收录 → 域名」排序截断；截断数量写入 `meta.json`。
- 白名单单独发布为 `allowlist.txt`，三档共用，优先级高于所有拦截规则；构建时白名单域名及其子域会从所有分类文件剔除，只订分类文件也不会误杀。
- 上游误标的域名写进 `tools/protected-domains.txt`：构建时从所有分类文件剔除（含子域），并并入 `allowlist.txt`。例如 `gw.alicdn.com`（阿里系 App 静态资源 CDN，拦掉会导致闲鱼图片空白）。
- 成人内容单独发布为 `manual/nsfw.txt`，不进入任何档位，需手动订阅。
- 公共后缀（`com.cn`、`github.io` 等，按 Public Suffix List 判定）不会被写成拦截规则，避免整片站点被误杀。
- 子域聚合每次构建输出 `build/agg-candidates.txt`：同一根域下 ≥10 个广告子域、且每个至少被 3 个源收录的根域。自动并入默认关闭，实测候选中会出现 americanexpress.co.uk、ricoh.ch 这类有真实主站的域名；确认无误后把根域写进 `tools/ad-roots.txt` 即可并入 `ads`。

预算在 `sources.toml` 的 `[budgets]` 中，按设备内存调整即可。

## 目录结构

```
sources.toml           # 唯一输入：上游清单、分类、档位定义、聚合开关
tools/build.py         # 抓取 → 归一化（含 PSL 校验）→ 分类 → 去重 → 分档产物
tools/ad-roots.txt     # 手工确认的广告根域（整域兜底）
tools/protected-domains.txt  # 构建级免拦清单：上游误标的基础服务域，构建时剔除并写入 allowlist
tools/verify.py        # 门禁：语法、条数、档内重复、波动、源失败率
.github/workflows/build.yml
```

## 本地构建

```bash
python tools/build.py --out build
python tools/verify.py --out build
```

产物结构：

```
build/
├── 512/{security,chinese,ads}.txt
├── 1024/{security,chinese,tracking,ads}.txt
├── 2048/{security,chinese,tracking,ads,annoyances}.txt
├── allowlist.txt
├── manual/nsfw.txt
├── agg-candidates.txt
├── LICENSES.md
└── meta.json
```

## 规则格式

产物为 AdGuard Home 可用的 Adblock 语法，每条一行 `||domain^`，文件头注释包含标题、版本、更新时间、条数、来源与许可链接。

## 调度

- 每 7 天 03:00 UTC 自动重建（`.github/workflows/build.yml`）。
- 支持 `workflow_dispatch` 手动触发。
- 门禁不通过时不发布，`dist` 分支保留上一版产物。

## 许可

各上游规则集的著作权与许可归其作者所有，逐源说明见构建产物中的 `LICENSES.md`。使用与再分发前请核对对应上游许可。
