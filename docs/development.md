# 开发指南

[← 返回 README](../README.md) · [使用手册](usage.md) · [架构与接口](architecture.md) · [隐私与安全](privacy.md)

测试、路径自检、页面回归，以及提交前必须遵守的编码约定。

## 目录

- [跑测试](#跑测试)
- [路径形态自检](#路径形态自检)
- [页面回归](#页面回归)
- [提交前检查](#提交前检查)

## 跑测试

`scripts\` 下每个文件职责单一，行为变更后请同步更新对应测试。单跑某一个文件：

```powershell
python scripts\test_drill.py      # 动态下钻、路径安全、扫描参数、选盘判定（24 项）
python scripts\test_scan.py       # 目录树扫描：合并/独立细化等价、junction、深索引（15 项）
python scripts\test_history.py    # 快照归档/清理、两版对比、深层指认、趋势矩阵（29 项）
python scripts\test_storage.py    # 盘根归一、目录键、current / active 指针（17 项）
python scripts\test_app.py        # 留档删除守卫、活动盘记忆、离线多盘打包、提示层级、端口顺延与 bind 重试（34 项）
python scripts\test_ticket.py     # ticket 生成：数据读不全时不删旧 ticket（5 项）
python scripts\test_volumes.py    # 盘符归一、细化根推导、阶段识别、本机磁盘枚举（12 项）
python scripts\selftest.py        # 路径形态：heal 幂等、模板占位符展开、家目录取值
python scripts\probe.py --offline # 给 dist 的单文件版插探针（页面渲染回归）
python scripts\probe.py           # 给 web\index.html 插探针（需先起服务）
```

一次跑完全部 136 项：

```powershell
python -m unittest discover -s scripts -p "test_*.py"
```

## 路径形态自检

`selftest.py` 防的是两类问题。一是真实路径被 `heal` 改坏 —— 导入 ticket 时每条路径都要
经过这一步，一旦改坏整批都无法匹配。二是模板占位符展开：`%DRIVE%` / `%SYSDRIVE%` 展开
为不带冒号的盘符，拼接路径时必须写 `%DRIVE%:\Windows`，缺少冒号会变成 `D\Windows`，
永远匹配不上。占位符的完整清单见[文字层与知识库](content-layer.md#占位符)。

## 页面回归

使用无头 Edge：

```powershell
msedge --headless=new --disable-gpu --virtual-time-budget=25000 `
       --dump-dom "file:///.../dist/_probe_offline.html" > dom.txt
```

探针会依次点击六个面板，把结果写入 `<pre id="__dbg">`。重点检查 `errCount` 为 0、
`tokenLeft` / `concl.factsLeft` 为 0、各类行数与数据条数一致、`overflow.*` 为 0。
`_probe_offline.html`、`web\_probe.html` 为中间产物，验证后请删除。

对接真实接口做回归时，服务与 Edge 必须写在同一个调用里启动，否则后台进程会被回收。
`--virtual-time-budget=25000` 只够跑完探针第一段，需要完整结果时给 60000。

## 提交前检查

- **`.ps1` 存成 UTF-8 with BOM**，**`.bat` 存成 GBK（cp936）无 BOM 且保持 CRLF**。
  原因写在 `.gitattributes` 的注释里：Windows PowerShell 5.1 按 ANSI 解码无 BOM 的
  `.ps1`，中文注释乱码会污染 `param()` 默认值；cmd.exe 按 ANSI 读取 `.bat`，UTF-8 无 BOM
  的中文甚至会把一条 `rem` 注释从中间截断、后半截当作命令执行。
- **不要提交 `data\`、`dist\`、`content\` 里的任何内容** —— 它们含本机账户名、盘符与
  完整目录结构。`.gitignore` 已覆盖这些目录，以及 `output\`、`archived\`、探针中间产物。
- **提交信息不要写入本机路径、进程名或 PID**，这些属于开发机画像。
- 改动涉及端口、选盘、快照、ticket 等行为时，先跑全量测试再提交。
