<#
.SYNOPSIS
  DiskAudit 数据源刷新。

.DESCRIPTION
  跑一遍只读采集，整形后原子替换 data/ 里的数据源文件：
    data/tree.json    目录树（全盘 + 指定子目录细化）
    data/links.json   迁移链接与目标实测体积
    data/meta.json    盘容量、扫描时间、计数

  只负责「数字」。目录说明与报告文字由 app.py 在扫描后生成 ticket，交给 AI 补。
  目录树由 scan_tree.py（Python，os.scandir）采集：细化根在扫描盘内时，
  全盘树与细化树共用一次遍历，细化子树不会被重访一遍。

  中间产物先落 data/.tmp，全部成功才换到 data/；任何一步失败都不会碰已有数据。
  全程只读：不移动、不删除任何用户文件。

.EXAMPLE
  .\scripts\collect\update-data.ps1
        默认刷新系统盘（全盘 + 当前用户家目录细化 + 链接目标测量）
        家目录取自 $env:USERPROFILE，盘符取自 $env:SystemDrive —— 换机器不用改脚本

.EXAMPLE
  .\scripts\collect\update-data.ps1 -Drive D: -DetailRoot D:\Work
        换成扫 D 盘，细化 D:\Work

.EXAMPLE
  .\scripts\collect\update-data.ps1 -Drive D: -NoDetail
        扫 D 盘但不做细化。细化根默认取家目录，不在 D 盘时脚本自己也会跳过，
        这里显式声明可以让日志少一句提示

.EXAMPLE
  .\scripts\collect\update-data.ps1 -SkipLinks
        只扫目录树，跳过链接目标测量（快很多）

.EXAMPLE
  .\scripts\collect\update-data.ps1 -Clean
        跑完后清掉 data/.tmp 里的中间产物
#>
param(
  [string]$Drive        = $env:SystemDrive,
  [string]$DetailRoot   = $env:USERPROFILE,
  [int]$Depth           = 6,
  [double]$MinMB        = 40,
  [double]$DetailMinMB  = 12,
  [double]$FileMinMB    = 20,
  [int]$KeepSnapshots   = 64,
  [switch]$SkipLinks,
  [switch]$NoDetail,
  [switch]$Clean
)

# 默认值全部来自环境变量，脚本本身不含任何本机路径：
#   -Drive      默认 $env:SystemDrive（通常 C:）
#   -DetailRoot 默认 $env:USERPROFILE（当前账户家目录）
if (-not $Drive)      { $Drive = "C:" }
if (-not $DetailRoot) { $DetailRoot = $env:USERPROFILE }
$detailSkip = ""
if ($NoDetail)        { $DetailRoot = ""; $detailSkip = "-NoDetail" }

# ！！本文件必须存成 UTF-8 with BOM ！！
# Windows PowerShell 5.1 会按 ANSI 解码无 BOM 的 .ps1，中文注释变乱码后
# 会污染 param() 的默认值 —— 症状是「显式传参的项都正常，只有靠默认值的
# 那一项静默失效」，不报错不警告。
$ErrorActionPreference = "Stop"

$collect = $PSScriptRoot
if (-not $collect) { $collect = (Get-Location).Path }
# 本脚本在 scripts\collect\，产品根目录要上跳两级
$root = Split-Path (Split-Path $collect -Parent) -Parent

$dataDir = Join-Path $root "data"
$tmpDir  = Join-Path $dataDir ".tmp"
$logDir  = Join-Path $dataDir ".log"
$scripts = $collect

# 细化扫描的 Depth 是相对 DetailRoot 的。为了合并后全盘树仍不超过根下
# $Depth 层，需要先扣掉 DetailRoot 自身相对盘根的层数。
$detailDepth     = -1
$detailScanDepth = $Depth
try {
  $driveBase  = [System.IO.Path]::GetFullPath($Drive.TrimEnd('\') + '\').TrimEnd('\')
  $detailBase = [System.IO.Path]::GetFullPath($DetailRoot).TrimEnd('\')
  if ($detailBase.Equals($driveBase, [System.StringComparison]::OrdinalIgnoreCase)) {
    $detailDepth = 0
  } elseif ($detailBase.StartsWith($driveBase + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    $rel = $detailBase.Substring($driveBase.Length).Trim('\')
    if ($rel) { $detailDepth = $rel.Split('\').Length } else { $detailDepth = 0 }
  }
  if ($detailDepth -ge 0) {
    $detailScanDepth = [Math]::Max(0, $Depth - $detailDepth)
  }
} catch { }

function Say($m) { Write-Host $m }

# ⚠️ 编码有个反直觉的点：[Console]::OutputEncoding 是**双向**的 ——
# 它既决定「怎么解码外部程序（Python）的输出」，也决定「PowerShell 自己怎么写出去」。
# 所以不能单方面调它来迁就 Python：PS 自己的中文会跟着换编码，在 cp936 宿主里全乱码
# （表现为「鏁版嵁婧愬埛鏂」，而这本来是对的）。
#
# 正确姿势是反过来 —— 让 Python 迁就宿主：按控制台的实际代码页输出。
# 这样「宿主解码 PS 的输出」和「PS 解码 Python 的输出」用的是同一个编码，两头都能对上。
$pyEnc = "utf-8"
try {
  $cp = [Console]::OutputEncoding.CodePage
  if ($cp -gt 0) { $pyEnc = "cp$cp" }        # 中文 Windows -> cp936（GBK 超集）
} catch { }
$env:PYTHONIOENCODING = $pyEnc

function Find-Python {
  # py.exe 是官方 launcher，优先；python.exe 可能是 Microsoft Store 的存根，
  # 所以两个都试，并且真的跑一次 --version 确认可用。
  $cands = New-Object System.Collections.ArrayList
  foreach ($exe in @("py.exe", "python.exe", "python3.exe")) {
    $c = Get-Command $exe -ErrorAction SilentlyContinue
    if ($c) { [void]$cands.Add($c.Source) }
  }
  foreach ($p in $cands) {
    try {
      $v = & $p --version 2>&1
      if ($LASTEXITCODE -eq 0 -and ("$v" -match "Python")) { return $p }
    } catch { }
  }
  return $null
}

# 细化根必须在扫描盘内。盘外的枝在合并时找不到对应分支，白白多扫一遍
# （家目录几万文件，够扫好几分钟），换盘扫描时这是纯浪费。
if ($DetailRoot -and $detailDepth -lt 0) {
  $detailSkip = "$DetailRoot 不在 $Drive 内"
  $DetailRoot = ""
  $detailScanDepth = $Depth
}

Say ""
Say "  DiskAudit · 数据源刷新"
Say "  ------------------------------------------------------------"
Say ("  盘符        {0}" -f $Drive)
if ($DetailRoot) {
  Say ("  细化目录    {0}（门槛 {1} MB · 相对深度 {2} 层）" -f $DetailRoot, $DetailMinMB, $detailScanDepth)
} else {
  Say "  细化目录    不做"
}
Say ("  全盘门槛    {0} MB · 单文件 {1} MB · 深度 {2} 层" -f $MinMB, $FileMinMB, $Depth)
Say ("  链接测量    {0}" -f $(if ($SkipLinks) { "跳过" } else { "启用的" }))
Say ""

foreach ($d in @($dataDir, $tmpDir, $logDir)) {
  if (-not (Test-Path -LiteralPath $d)) {
    New-Item -ItemType Directory -Path $d -Force | Out-Null
  }
}

# 开跑前先清掉上次留下的中间产物，免得失败时被旧文件顶替
Get-ChildItem -LiteralPath $tmpDir -File -ErrorAction SilentlyContinue |
  Remove-Item -Force -ErrorAction SilentlyContinue

# 扫描本身也走 Python（scan_tree.py），所以采集开始前就要找到它
$py = Find-Python
if (-not $py) {
  throw "找不到可用的 Python（扫描与整形都依赖它）。请安装 Python 3 并加入 PATH。"
}

$sw = [System.Diagnostics.Stopwatch]::StartNew()

# ---------------------------------------------------------------- 1. 目录树
# 全盘树与细化树（细化根在扫描盘内时）由 scan_tree.py 一次遍历同时产出；
# 细化根不在盘内时，脚本内部按旧管线语义对细化根单独再扫一遍。
if ($DetailRoot) {
  Say "[1/3] 扫描 $Drive\ 目录树（全盘 + 细化 $DetailRoot 一次遍历）…"
} else {
  Say "[1/3] 扫描 $Drive\ 目录树（仅全盘）…"
}
$scanArgs = @(
  (Join-Path $scripts "scan_tree.py"),
  # 根路径不能带尾反斜杠：PS 5.1 给「含空格 + 尾反斜杠」的原生参数加引号时，
  # 结尾的 \" 会被 C 运行时当成转义引号，后面的所有参数全被吞进第一个参数。
  # scan_tree 对不带尾斜杠的根自己补齐，这里一律剃掉。
  "--root", ([string]$Drive).TrimEnd('\'),
  "--depth", "$Depth", "--min-mb", "$MinMB", "--file-min-mb", "$FileMinMB",
  "--out", (Join-Path $tmpDir "tree-full.json"),
  "--log", (Join-Path $logDir "scan-full.log")
)
if ($DetailRoot -and (Test-Path -LiteralPath $DetailRoot)) {
  $scanArgs += @(
    "--detail-root", $DetailRoot,
    "--detail-depth", "$detailScanDepth",
    "--detail-min-mb", "$DetailMinMB",
    "--detail-out", (Join-Path $tmpDir "tree-detail.json"),
    "--detail-log", (Join-Path $logDir "scan-detail.log")
  )
} else {
  Say $(if ($detailSkip) { "      细化跳过（$detailSkip）" }
        else { "      细化跳过（$DetailRoot 不存在）" })
}
& $py $scanArgs

$fullPath = Join-Path $tmpDir "tree-full.json"
if (-not (Test-Path -LiteralPath $fullPath)) {
  throw "扫描没有产出 tree-full.json，已中止（data\ 里的旧数据未被改动）"
}
Say ("      完成 {0:N1}s" -f $sw.Elapsed.TotalSeconds)

# ---------------------------------------------------------------- 2. 链接
if ($SkipLinks) {
  Say "[2/3] 跳过链接目标测量（-SkipLinks）"
} else {
  Say "[2/3] 测量迁移链接的目标体积 …"
  & (Join-Path $scripts "linktargets.ps1") `
      -SkippedJson $fullPath `
      -ExtraJson (Join-Path $root "content\linkextra.json") `
      -Out (Join-Path $tmpDir "links-raw.json") `
      -Log (Join-Path $logDir "links.log")
  Say ("      完成 {0:N1}s" -f $sw.Elapsed.TotalSeconds)
}

$elapsed = $sw.Elapsed.TotalSeconds

# ---------------------------------------------------------------- 3. 整形
Say ""
Say "[整形] 合并目录树 · 过滤包级 junction · 读取盘容量 …"

$asmArgs = @(
  (Join-Path $scripts "assemble.py"),
  "--tmp", $tmpDir,
  "--data", $dataDir,
  "--drive", $Drive,
  "--keep-snapshots", "$KeepSnapshots",
  "--elapsed", "$elapsed"
)
# 没做细化时不能传空的 --detail-root：PS 5.1 把空字符串参数整个吞掉，
# 于是 --detail-root 会把后面的 --keep-snapshots 吃成自己的值，argparse 直接报错。
if ($DetailRoot) { $asmArgs += @("--detail-root", $DetailRoot) }

& $py $asmArgs
$rc = $LASTEXITCODE

if ($rc -ne 0) {
  throw "整形失败（assemble.py 退出码 $rc），data\ 里的旧数据未被改动"
}

Say ""
Say ("完成 · 总耗时 {0:N1}s" -f $sw.Elapsed.TotalSeconds)
Say "     刷新浏览器（或重启 start.bat）即可看到新数据。"

if ($Clean) {
  Get-ChildItem -LiteralPath $tmpDir -File -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue
  Say "     已清理 data/.tmp"
}
