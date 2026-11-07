param(
  [string]$SkippedJson = "t_c.json",   # 主扫描产物，用它的 skipped[] 当候选清单
  [string]$ExtraJson   = "linkextra.json",  # 额外要量的链接（如目标仍在 C 盘的短路径）
  [string]$Out         = "links.json",
  [string]$Log         = "links.log.txt"
)

# ！！本文件必须存成 UTF-8 with BOM ！！
# Windows PowerShell 5.1 按 ANSI 解码无 BOM 的 .ps1，中文注释会变成乱码并污染
# param() 的默认值 —— 表现是「显式传参的项都正常，只有靠默认值的那一项静默失效」。
$ErrorActionPreference = "SilentlyContinue"

$RP   = [System.IO.FileAttributes]::ReparsePoint
$ADIR = [System.IO.FileAttributes]::Directory
$MB   = 1048576.0

# ---------- 1. 候选链接 ----------
$cand = New-Object System.Collections.ArrayList
$seen = @{}

function Add-Cand($p) {
  if (-not $p) { return }
  $p = ([string]$p).Trim()
  if ($p -eq "") { return }
  $k = $p.ToLower()
  if ($seen.ContainsKey($k)) { return }
  $seen[$k] = 1
  [void]$cand.Add($p)
}

# 读 JSON：缺文件返回 $null（缺是正常情况），解析失败必须留下痕迹。
# 本脚本是 $ErrorActionPreference = "SilentlyContinue"，不 try 的话 ConvertFrom-Json
# 报的错会被吞掉，表现成「配了 linkextra.json 却一条也没量到」，查都没处查。
$warns = New-Object System.Collections.ArrayList

function Read-JsonFile($p, $label) {
  if (-not $p) { return $null }
  if (-not (Test-Path -LiteralPath $p)) { return $null }
  try {
    $txt = [System.IO.File]::ReadAllText((Resolve-Path -LiteralPath $p -ErrorAction Stop).Path)
    if (-not $txt -or $txt.Trim() -eq "") { throw "文件是空的" }
    return ($txt | ConvertFrom-Json -ErrorAction Stop)
  } catch {
    # 异常信息里会原样带上出错的那段 JSON，末尾可能粘着文件里的换行，
    # 不 Trim 的话写进日志会和下一行粘在一起
    $detail = ([string]$_.Exception.Message).Trim()
    $msg = "$label 读取失败，本次忽略：$detail"
    Write-Host ("  [warn] " + $msg) -ForegroundColor Yellow
    [void]$warns.Add($msg)
    return $null
  }
}

$skippedObj = Read-JsonFile $SkippedJson "主扫描产物"
if ($skippedObj) { foreach ($s in $skippedObj.skipped) { Add-Cand $s } }

# 额外清单只读一次：既当候选，也当「强制保留」白名单
$extra = @()
$extraRaw = Read-JsonFile $ExtraJson "content\linkextra.json"
if ($null -ne $extraRaw) {
  foreach ($s in @($extraRaw)) {
    $t = ([string]$s).Trim()
    if ($t -ne "") { $extra += $t; Add-Cand $t }
  }
}

# ---------- 2. 解析每个链接的目标，留下「目标不在 C 盘」的（外加 Extra 明确指定的）----------
$extraKeep = @{}
foreach ($s in $extra) { $extraKeep[$s.ToLower()] = 1 }

$items = New-Object System.Collections.ArrayList
foreach ($p in $cand) {
  $it = $null
  try { $it = Get-Item -LiteralPath $p -Force } catch { continue }
  if (-not $it) { continue }
  $linkType = $it.LinkType
  if (-not $linkType) { continue }
  $tgt = $it.Target
  if ($tgt -is [array]) { $tgt = $tgt[0] }
  if (-not $tgt) { continue }
  $tgt = ([string]$tgt).Trim()

  $drive = ""
  if ($tgt.Length -ge 2 -and $tgt[1] -eq ':') { $drive = $tgt.Substring(0,1).ToUpper() }
  $force = $extraKeep.ContainsKey($p.ToLower())
  if ($drive -eq "C" -and -not $force) { continue }
  if ($drive -eq "" -and -not $force) { continue }

  [void]$items.Add([pscustomobject]@{
    link   = $p
    kind   = $linkType          # Junction / SymbolicLink
    target = $tgt
    drive  = $drive
  })
}

# ---------- 3. 逐目标递归量体积（跳过目标内部的联接点，避免绕回去/重复计数）----------
$res = New-Object System.Collections.ArrayList
foreach ($m in $items) {
  $tp = $m.target
  $bytes = 0.0
  $files = 0
  $dirs = 0
  $inner = New-Object System.Collections.ArrayList
  $errs = 0
  $exists = $false
  try { $exists = [System.IO.Directory]::Exists($tp) } catch { $exists = $false }

  if ($exists) {
    $stack = New-Object System.Collections.Stack
    $stack.Push($tp)
    while ($stack.Count -gt 0) {
      $dir = [string]$stack.Pop()
      $entries = $null
      try { $entries = [System.IO.Directory]::GetFileSystemEntries($dir) } catch { $errs++; continue }
      for ($k = 0; $k -lt $entries.Length; $k++) {
        $e = $entries[$k]
        try {
          $attr = [System.IO.File]::GetAttributes($e)
          $isDir = (($attr -band $ADIR) -ne 0)
          if ((($attr -band $RP) -ne 0) -and $isDir) { [void]$inner.Add($e); continue }
          if ($isDir) { $stack.Push($e); $dirs++ }
          else {
            $len = 0
            try { $len = [double](New-Object System.IO.FileInfo($e)).Length } catch { $len = 0 }
            $bytes += $len
            $files++
          }
        } catch { $errs++ }
      }
    }
  }

  [void]$res.Add([pscustomobject]@{
    link       = $m.link
    kind       = $m.kind
    target     = $tp
    drive      = $m.drive
    exists     = $exists
    bytes      = [double]$bytes
    files      = $files
    dirs       = $dirs
    innerLinks = @($inner)
    errors     = $errs
  })
}

$res = @($res | Sort-Object -Property bytes -Descending)

$payload = [pscustomobject]@{
  measured = (Get-Date).ToString("yyyy-MM-dd HH:mm")
  items    = $res
}
$json = $payload | ConvertTo-Json -Depth 8 -Compress
[System.IO.File]::WriteAllText($Out, $json, (New-Object System.Text.UTF8Encoding $false))

$lines = New-Object System.Collections.ArrayList
if ($warns.Count -gt 0) {
  foreach ($w in $warns) { [void]$lines.Add("[warn] " + $w) }
  [void]$lines.Add("")
}
$sum = 0.0
foreach ($r in $res) {
  [void]$lines.Add(("{0,10:N2} GB  {1,-8} {2}  ->  {3}  [files={4} dirs={5} inner={6} err={7}]" -f `
    ($r.bytes / 1GB), $r.drive, $r.link, $r.target, $r.files, $r.dirs, $r.innerLinks.Count, $r.errors))
  if ($r.drive -ne "C") { $sum += $r.bytes }
}
[void]$lines.Add("---")
[void]$lines.Add(("count={0}  offC={1:N2} GB" -f $res.Count, ($sum / 1GB)))
# 日志带 BOM：里面有中文告警，PS 5.1 的 Get-Content / 记事本按 ANSI 读无 BOM 的
# UTF-8 会变乱码。links.json 反过来必须无 BOM —— Python 的 json.load 不认 BOM。
[System.IO.File]::WriteAllText($Log, ($lines -join "`r`n"), (New-Object System.Text.UTF8Encoding $true))
