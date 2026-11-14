# 架构与接口

[← 返回 README](../README.md) · [使用手册](usage.md) · [文字层与知识库](content-layer.md) · [开发指南](development.md)

扫描、文字、渲染三层分开，中间只通过 `data\` 和 `content\` 里的文件交接。
本文给出数据流、快照与多盘的机制、HTTP 接口清单和完整目录结构。

## 目录

- [数据流](#数据流)
- [启动时的初始化顺序](#启动时的初始化顺序)
- [快照与多盘](#快照与多盘)
- [HTTP 接口](#http-接口)
- [目录结构](#目录结构)

## 数据流

```text
scripts\collect\update-data.ps1
  ├─ scan_tree.py       全盘树 + 家目录细化树（细化根在扫描盘内时
  │                     共用一次遍历）  → data\.tmp\tree-full.json / tree-detail.json
  └─ linktargets.ps1    链接目标体积      → data\.tmp\links-raw.json
                              ↓
  scripts\collect\assemble.py   合并 · 过滤包级 junction · 读盘容量 · 读物理内存
                              ↓
                data\drives\<盘>\tree.json  links.json  meta.json        数字层
                              ↓（同一份 payload 归档进该盘的 history\）
  scripts\ticket.py   挑缺口 → 该盘 ticket →（AI 填写）→ 导入该盘文字层
                              ↓
  content\drives\<盘>\purpose.json  report.json
                              ↓
  scripts\app.py ──/api/payload──▶ web\index.html
                              ↓
  scripts\build.py  ──▶  dist\*.html（数据内嵌）
```

采集全部成功后才原子替换该盘目录中的文件（先写 `.new` 再 `os.replace`），任一步失败
则旧数据原样保留，不会出现不完整的数据源。

## 启动时的初始化顺序

`scripts\app.py` 的 `ensure()` 按固定顺序执行，顺序不可调整：ticket 需要知识库
的评级挑选候选，知识库需要扫描数据确定有哪些目录：

1. 没有任何盘的扫描数据：执行扫描
2. 该盘没有 `purpose.json`：按本机生成一份通用基础库
3. 该盘没有 `report.json`：生成报告骨架，文字留空
4. ticket 指纹不匹配：解析 → 导入该盘 `content\` → 回填空缺的描述位
5. 仍有缺口：为该盘重新生成 `ticket.md`

`ensure()` 只为 `current.json` 记录的那个盘出单；其他盘的缺口需要对该盘执行一次
`--update --drive <盘>:` 才会成票。

## 快照与多盘

每次刷新（`assemble.py` 落盘之后）将同一份数字层归档为一张快照，存入该盘的
`data\drives\<盘>\history\snap-<时间>.json`，默认保留 64 张，`--keep-snapshots` 可调，
`--skip-scan` 不产生快照。快照按盘各存一份 —— 不同盘的目录树没有可比性，混合对比只会
产生大量「消失 / 新增」噪音。面板顶部标明当前盘与其他盘的快照数量，切换即可查看。

累积两张后，「快照对比」面板即可给出：

- 两次扫描之间总量的变化，哪些目录增长最快、缩减最快
- 哪些目录首次收录（新建，或刚突破收录门槛，文案会加以区分，不冒充创建时间）
- 哪些目录不再收录（已删除，或跌破门槛）
- 任意目录在全部快照上的体积趋势。点击涨跌榜任意一行，曲线即叠加进趋势图；默认为
  相对变化视图，以最早一次扫描为基线只看涨跌，几十 MB 量级的波动仍可辨识，可切换为
  总量视图
- 深层指认：展开涨跌榜中目录行的 ▸，一路下钻到最深的目录或大文件，每层无法解释的
  碎项按残差标注

目录树中悬停任意一行，卡片停靠为一块摘要：三格指标、构成条（体积分布在哪些子项上，
点击图例可直接在树中定位）、用途说明、迷你趋势图，底部为复制路径 / 复制链接 /
打开文件夹 / 趋势一排操作。趋势按钮仅在含有快照信息的目录上出现。数据在页面加载时
一次性预取，悬停不发起请求。

每次扫描除了更新 `data\`（当前盘），还会在 `data\drives\<盘>\` 为该盘保留一份独立数据
（`tree` / `links` / `meta`），换盘扫描互不覆盖，也不参与快照裁剪。扫描过多个盘后，
点击顶栏盘符即可在几个盘之间切换，整页按该盘的留档重载；行尾的删除按钮只删除该盘的
留档（`tree` / `links` / `meta` / `drive.json`），`history\` 下的历史快照原样保留 ——
留档可通过重扫再生成，快照不可再生。以上操作均不触及磁盘本身。留档是扫描时点的快照，
实时下钻仍要求盘挂载在本机。

离线单文件的快照对比为降级版，见[使用手册 · 离线单文件](usage.md#离线单文件)。

## HTTP 接口

| 路径 | 内容 |
|---|---|
| `GET /` | 报告页面 |
| `GET /assets/*` | 静态资源 |
| `GET /api/payload` | 一次取齐 `tree / links / meta / purpose / report`；`?root=<盘根>` 取该盘的留档（并记住），不带则取 `active.json` 记录的盘 |
| `GET /api/drives` | `data/drives/` 里各盘留档摘要 + 当前盘标记；`?root=` 指定「正在看哪个盘」，缺省取 `active.json` |
| `GET /api/volumes` | 本机磁盘（盘符、卷标、容量、已用比例）、每个盘的留档与当前盘标记、当前扫描任务；`?root=` 指定「正在看哪个盘」，缺省取 `active.json` |
| `POST /api/scan` | 请求体 `{"drive":"D:"}`，后台起一次扫描；只接受本机固定盘 |
| `GET /api/scan` | 当前扫描任务：阶段、耗时、日志尾、退出码 |
| `POST /api/scan/abort` | 中止正在运行的扫描 |
| `GET /api/tree` 等 | 单独取某一项（只认默认盘，按盘取请走 `/api/payload?root=`） |
| `GET /api/health` | 探活 + 数据源新鲜度 |
| `GET /api/drill?path=<绝对路径>` | 实时返回该目录一层子项，体积按整棵子树汇总 |
| `GET /api/open?path=<绝对路径>` | 用资源管理器打开该目录（文件则定位并选中） |
| `GET /api/history` | 快照摘要列表（各盘合并，带 `root`，前端按当前盘过滤），不含正文；`?root=` 指定读哪个盘 |
| `GET /api/history/diff?from=<id>&to=<id>` | 两张快照的结构化对比，缺省是上一张 → 最新 |
| `GET /api/history/trend?path=<树内路径>` | 该目录在全部同 root 快照上的体积序列 |
| `GET /api/history/matrix` | 当前全树路径 × 快照的稀疏体积矩阵 |
| `GET /api/history/deepdiff?from=<id>&to=<id>&path=<目录>` | 该目录内逐层下钻的变化指认 |

`/api/drill` 要求请求来自环回地址、`Host` 为 `localhost / 127.0.0.1 / [::1]`，以防
DNS rebinding；路径经 `realpath` 后必须落在 `tree.json` 的 `root` 内，不跟随联接点。
每次只读取当前一层，不写缓存、不落盘。小于 1KB 的文件不单独成行，单目录文件行最多
500 个。

`/api/open` 是「目录用途」卡片上文件夹图标的后端。存在 `Referer` 时校验同源，防止
浏览器中的其他页面将其用作跳板；路径必须真实存在才执行操作，不存在则返回 404。

`/api/scan` 与 `/api/scan/abort` 使用同一套本机 + 同源校验，且只接受本机真实存在的固定
盘符：传入路径、不存在的盘或可移动盘均直接返回错误，扫描参数不经过页面。同一时刻只
运行一个任务，重复发起返回 409。采集输出写入 `data\.log\scan-<时间>.log`，接口只返回
最后若干行。

所有接口均返回 `Cache-Control: no-store`。

## 目录结构

```text
DiskAudit\
├─ start.bat                 启动服务（→ scripts\app.py）
├─ update.bat                刷新数据/文字层并后台构建离线版（→ scripts\app.py --update）
├─ web\index.html            报告页面（CSS/JS 内联，数据槽留空）
├─ assets\echarts.min.js     图表库
├─ docs\                     使用手册、文字层、架构、隐私、开发文档与界面截图
├─ content\                  文字层，按盘各一份（不入库，首次扫描时自动创建）
│   ├─ linkextra.json        额外需测量体积的链接（全局，可选）
│   └─ drives\<盘>\
│       ├─ purpose.json      目录用途知识库（purpose.ensure() 建，ticket 填充）
│       ├─ report.json       报表语义层（report.seed() 按扫描现生成）
│       └─ ticket.md         该盘的待补说明，补全后自动删除
├─ scripts\
│   ├─ app.py                入口：启动编排 + 本地服务
│   ├─ storage.py            按盘路径层：drives 目录命名、current / active 指针
│   ├─ drill.py              动态下钻：路径校验 + 单层实时扫描
│   ├─ history.py            快照历史：归档 / 对比 / 趋势 / 首次见于
│   ├─ ticket.py             ticket：生成 / 校验 / 解析 / 导入
│   ├─ purpose.py            通用规则库 + 四级回退对账
│   ├─ report.py             报表骨架生成 + 空描述回填
│   ├─ pathvars.py           家目录 / 系统盘取值，旧 ticket 的盘符修正
│   ├─ volumes.py            本机磁盘枚举 + 选盘参数推导
│   ├─ build.py              打包单文件离线版
│   ├─ probe.py              生成自检探针页（开发用）
│   ├─ selftest.py           路径形态自检（heal 幂等、模板占位符展开）
│   ├─ test_drill.py         动态下钻与扫描参数测试
│   ├─ test_scan.py          目录树扫描测试
│   ├─ test_history.py       快照历史测试
│   ├─ test_storage.py       盘根归一、目录键、current / active 指针
│   ├─ test_app.py           留档删除守卫、活动盘记忆、离线多盘打包
│   ├─ test_ticket.py        ticket 生成的数据不全边界
│   ├─ test_volumes.py       盘符归一、细化根推导、阶段识别
│   └─ collect\              采集层，只产出数字
│       ├─ update-data.ps1   扫描编排
│       ├─ scan_tree.py      目录树采集（Python，全盘 + 细化一次遍历）
│       ├─ linktargets.ps1   链接目标体积实测
│       └─ assemble.py       合并整形 · 读盘容量 · 读物理内存
├─ data\                     扫描产物，按盘各一份（不入库）
│   ├─ current.json          上次扫描的盘
│   ├─ active.json           界面当前查看的盘（切盘时写入，重开后仍为该盘）
│   └─ drives\<盘>\
│       ├─ tree.json         目录树
│       ├─ links.json        迁移链接与目标实测体积
│       ├─ meta.json         盘容量与计数
│       ├─ drive.json        留档摘要（列表页读它）
│       └─ history\          这个盘的快照
└─ dist\                     离线报告（不入库）
```

`scripts\` 下每个文件职责单一，行为变更后请同步更新测试。
