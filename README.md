# adguardhome-rules

给 [AdGuard Home 助手（luci-app-adguardhome-lite）](https://github.com/Ozero-top/luci-app-adguardhome-lite) 用的规则构建仓库：每 7 天自动抓取上游规则，归一化、去重、分类，按设备内存分档生成静态规则文件，发布到 `dist` 分支。

## 订阅地址

```
https://cdn.jsdelivr.net/gh/Ozero-top/adguardhome-rules@dist/512/ads.txt
```

国内访问 jsDelivr 不稳定时可换 raw：

```
https://raw.githubusercontent.com/Ozero-top/adguardhome-rules/dist/512/ads.txt
```

## 档位与分类

| 档位 | 分类文件 | 单类预算 | 实测总条数 |
|---|---|---|---|
| 512（512MB 内存） | security、chinese、ads | 1.5 万 / 6 万 / 6 万 | 约 13.3 万 |
| 1024（1GB 内存） | + tracking、annoyances | 1.5 万 / 12 万 / 8 千 / 12 万 / 3 千 | 约 26.2 万 |
| 2048（2GB 及以上） | + social | 不限 | 约 57.3 万 |

- 同一档位内各分类文件互不重复：一条域名只归属档内优先级最高的分类。
- 超出单类预算时，按「被更多上游同时收录」的次数优先保留，再按域名排序截断；截断数量写入 `meta.json`。
- 白名单单独发布为 `allowlist.txt`，三档共用，优先级高于所有拦截规则。
- 成人内容单独发布为 `manual/nsfw.txt`，不进入任何档位，需手动订阅。

预算在 `sources.toml` 的 `[budgets]` 中，按设备内存调整即可。

## 目录结构

```
sources.toml           # 唯一输入：上游清单、分类、档位定义
tools/build.py         # 抓取 → 归一化 → 分类 → 去重 → 分档产物
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
├── 1024/{security,chinese,tracking,ads,annoyances}.txt
├── 2048/{security,chinese,tracking,ads,annoyances,social}.txt
├── allowlist.txt
├── manual/nsfw.txt
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
