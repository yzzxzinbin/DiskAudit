# 文字层与知识库

[← 返回 README](../README.md) · [使用手册](usage.md) · [架构与接口](architecture.md)

体积和条数可以扫描得到，目录用途不能。程序不做猜测，这一层由人或 AI 填写。

## 目录

- [数字层与文字层](#数字层与文字层)
- [ticket](#ticket)
- [purpose.json](#purposejson)
- [report.json](#reportjson)
- [占位符](#占位符)
- [linkextra.json](#linkextrajson)

## 数字层与文字层

报告分两层：数字层由采集脚本现算，文字层由人或 AI 填写。

| 层 | 文件 | 负责方 | 缺失时的行为 |
|---|---|---|---|
| 数字层 | `data\drives\<盘>\tree.json` `links.json` `meta.json` | 采集脚本，每次现算，勿手动修改 | 所有功能不可用 |
| 文字层 | `content\drives\<盘>\purpose.json` | 人 / AI 填写，主要由 ticket 填充 | 自动生成通用库，报告正常输出 |
| 文字层 | `content\drives\<盘>\report.json` | 人 / AI 填写 | 自动生成骨架，文字回退为通用版 |

两者均无需手动创建：缺失时自动生成，缺口进入 ticket。`purpose.json` 是文字层的根基 ——
目录说明无法从扫描结果推导，报告顶部的结论和清理清单都建立在它之上。

## ticket

缺说明的目录汇总到该盘自己的 `content\drives\<盘>\ticket.md`：

```markdown
### 3. `D:\Work\Temp`

- 体积：4.21 GB
- 当前兜底：继承祖先说明
- 说明：
- 评级：
```

文件是自包含的：开头包含给 AI 的完整说明、字段含义、评级取值与注意事项，无需补充
背景。将整个文件发给任意对话模型，填写各节的 `- 说明：` 和 `- 评级：`，保存后覆盖原
文件，再运行 `start.bat`：

```text
  [导入] ticket：目录说明 40 条 · 结论 0 条

  待完善：32 条目录说明 → content\drives\D\ticket.md
```

程序通过指纹判断文件是否被修改：生成时把正文的 md5 写在一行注释里，下次启动时比对，
不一致则导入该盘的 `purpose.json`，再按剩余缺口生成一份新 ticket。每补全一批，下一批
自动衔接，直到没有缺口，ticket 文件随之删除。

不补全也能出报告，相应目录显示通用说明。ticket 中的路径为真实绝对路径，按盘各存一份、
不入库；不使用占位符的理由见[占位符](#占位符)一节。

### ticket 里放什么

默认不限条数，所有通过筛选且仍缺说明的目录一次列出。新机器首次启动时几乎全部目录都
缺说明，ticket 会有几百条，几十到一百多 KB；每导入一批，剩余条数相应减少。

筛选规则只有两条：

- 体积 ≥ 8 MB（更小的由规则兜底）
- 跳过随机命名的目录（`sess_*`、GUID、哈希、`chromium-*`、`*.msi`）和系统内部的碎目录
  （WinSxS、Installer、SoftwareDistribution、Prefetch、Temp）

被过滤的目录不会丢失：ticket 末尾有一节「未列入的目录」，逐条列出路径和体积，并说明
不用填写的原因。

排序依据 `体积 × 兜底权重`，权重为 `none` 3.0 / `inherited` 2.0 / 规则命中 1.2：
完全无说明的目录排在前面，否则列表开头会被体积大但已有通用规则的目录占满。

可以分批填写：每次导入已填写的条目，剩余条目进入下一份 ticket。需要分批时加 `--limit`，
只有限定名额时才做父子去重；全量导出不去重，每个目录都应有自己的一条说明。

## purpose.json

决定每个目录显示的说明与标签。

| 字段 | 含义 |
|---|---|
| `exact` | 精确路径：`{"p": "D:\\Games\\Saves", "t": "说明", "g": "warn", "he": true}` |
| `names` | 名字规则：`{"re": "^cache$", "fl": "i", "t": "说明", "g": "ok"}` |
| `paths` | 路径正则：`{"src": "^[A-Z]:\\\\Windows\\\\WinSxS", "t": "说明", "g": "no"}` |
| `tags` | 四个等级的显示文案：`ok` 可清理 / `warn` 谨慎 / `no` 别动 / `info` 了解 |

取值优先级为精确路径 → 名字规则 → 路径规则 → 最近祖先继承，数组顺序即优先级。
`"he": false` 表示该说明只在侧栏卡片显示，不在目录树每行显示。

`scripts\purpose.py` 中的 `EXACT` / `NAMES` / `PATHS` 是一份不绑定机器的规则库：系统
目录（Windows / Program Files / ProgramData / 用户目录骨架 / 分页与休眠文件）、通用名字
规则（缓存、日志、`node_modules`、`*-updater`）、路径规则（WindowsApps、WinSxS、
Installer、Package Cache）。它在任何 Windows 机器上都能让报告运行起来，本机特有的软件
说明由 ticket 补充。

这份规则库在源码中使用 `%HOME%` / `%DRIVE%` / `%SYSDRIVE%`，`purpose.ensure()` 落盘时
按当前机器展开为真实路径，并丢弃不属于本盘的条目 —— 落盘的 JSON 中不含占位符。

`paths` 的 `src` 是正则，盘符用 `[A-Z]:`、账户层用 `[^\\]+`，如此一条规则在任何盘上都
成立。

修改后无需重新扫描：服务每次请求都重新读取文件；离线版需重新构建。

## report.json

存放程序无法推导的判断：概览结论、每个大目录的一句话说明、A / B / C 清单、禁区理由、
误删陷阱、执行顺序与迁移建议。

体积不应硬编码。可计算的用 `valuePaths` 交给页面现算，无法计算的才写区间：

```json
{ "text": "清理过期日志", "valuePaths": ["C:\\Users\\alice\\AppData\\Local\\Temp"], "prefix": "约 " }
```

| 字段 | 含义 |
|---|---|
| `valuePaths` | 这一步要动的目录，体积 = 实测之和 |
| `prefix` | 显示前缀，如 `约 `（没写 `value` 时生效） |
| `value` | 显示文案；和 `valuePaths` 并存时只管显示，数字仍取实测 |
| `stepsTotal.upto` | 累计行取前 N 步的现算合计 |
| `systemClean.total` | B 类整体量级（如 `3–9 GB`），不写就隐藏副标题 |

`clean[]` 还有两个字段，用于本机扫不到的路径（回收站、`System Volume Information`、
Windows 更新缓存等）：

| 字段 | 效果 |
|---|---|
| `"size": 1.8` | 按估值（GB）入账，页面标 `≈` |
| `"keep": true` | 保留条目，体积显示「未扫描」，不计入合计 |

`report.json` 缺失时页面不受影响：按 `purpose.json` 的评级生成通用版本，隐藏 B 类 /
陷阱 / 执行顺序三块，页头给出提示。

## 占位符

只出现在**模板**中，落盘的数据文件不含。

| 占位符 | 展开成 | 用在哪 |
|---|---|---|
| `%HOME%` | 用户家目录，如 `C:\Users\alice` | `scripts\purpose.py` 的规则库、`web\index.html` 的文案 |
| `%USER%` | 账户名，如 `alice` | 同上 |
| `%DRIVE%` | 被扫描盘符，如 `D`（不带冒号） | 同上 |
| `%SYSDRIVE%` | Windows 系统盘，如 `C`（不带冒号） | 系统目录种子 |
| `%TGT%` | 链接实体所在盘，如 `C 盘 / D 盘` | `web\index.html`（由 `links.json` 统计） |

`content\` 下的知识库、报表文案、ticket，以及 `data\` 下的扫描产物，存储的都是真实
绝对路径。这些文件按盘各存一份、不进版本库、无对外暴露面；在数据层使用占位符会导致
无法判断条目属于哪个盘，读取时还需额外的解析层。

`scripts\pathvars.py` 统一负责展开，服务端、离线构建、ticket 对账、骨架生成共用同一套
值。`%HOME%` 优先取 `tree.json` 的 `home`（采集时记录的真实用户家目录），旧数据没有时
回退到 `subRoot`，仍取不到才使用当前进程的 `~`。`subRoot` 是本次细化扫描的根目录，可能
只是一个工作目录，不能当作家目录使用。系统目录使用 `%SYSDRIVE%` 而非 `%DRIVE%`：后者
只是 `-Drive` 选中的扫描盘，换盘扫描时两者可能不同。

文字中还可使用事实占位符，渲染时替换为现算值：

| 占位符 | 展开成 |
|---|---|
| `{drive}` `{disk.cap}` `{disk.used}` `{disk.free}` `{disk.usedPct}` | 盘符 / 容量 / 已用 / 剩余 / 已用比例 |
| `{links.count}` `{links.size}` `{links.pct}` `{links.total}` | 已迁出条数 / 体积 / 占盘比例 / 链接总数 |
| `{ram}` `{hiber}` `{hiber.pct}` | 物理内存 / 休眠文件体积 / 占内存比例 |
| `{clean.total}` `{clean.count}` | A 类清单合计 / 条数 |
| `{top.name}` `{top.size}` `{top.pct}` `{tree.total}` | 最大占用目录 / 体积 / 占比 / 全盘合计 |

## linkextra.json

扫描器默认从主扫描的 `skipped[]` 中挑选联接点测量目标体积。如有需要测量但未被扫到的
链接（例如目标仍在同盘、路径较短的 junction），可手动创建 `content\linkextra.json`，
内容为路径字符串数组：

```json
[
  "C:\\Users\\alice\\AppData\\Local\\SomeApp\\cache",
  "D:\\Games\\saves"
]
```

文件不存在时自动跳过，不影响扫描。该文件包含本机路径，不入库。
