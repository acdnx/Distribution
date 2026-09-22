#!/usr/bin/env pwsh
# ============================================================================
#  RepoSecretScan —— 仓库敏感信息扫描（L1 已知凭据格式 / L3 git 历史 / L4 本地凭据文件）
# ----------------------------------------------------------------------------
#  与 repo-secret-audit 技能配套。L2（高熵串）由同目录 EntropyScan.py 负责。
#
#  用法：
#    pwsh -File Scan.ps1 -Root <仓库根>
#    pwsh -File Scan.ps1 -Root <仓库根> -History
#    pwsh -File Scan.ps1 -Root <仓库根> -LocalFiles
#    pwsh -File Scan.ps1 -Root <仓库根> -All
#
#  安全设计（重要）：
#    输出**一律截断**，最多回显命中片段的前若干字符。扫描工具绝不能完整回显疑似凭据——
#    否则工具输出、日志、CI artifact 自身就成了新的泄露面。
#
#  退出码：0 = 未命中；1 = 有命中（需人工判断）；2 = 参数/环境错误。
# ============================================================================

[CmdletBinding()]
param(
    [string]$Root = ".",
    [switch]$History,
    [switch]$LocalFiles,
    [switch]$All,
    #: 命中片段最多回显多少字符（默认 80；设 0 表示只报位置不回显内容）
    [int]$PreviewLength = 80
)

$ErrorActionPreference = "Stop"
if ($All) { $History = $true; $LocalFiles = $true }

if (-not (Test-Path $Root)) {
    Write-Host "❌ 仓库根不存在：$Root" -ForegroundColor Red
    exit 2
}
$Root = (Resolve-Path $Root).Path
$script:hitCount = 0

function Write-Hit {
    param([string]$Layer, [string]$Where, [string]$Text)
    $script:hitCount++
    $preview = if ($PreviewLength -gt 0 -and $Text) {
        $t = $Text.Trim()
        if ($t.Length -gt $PreviewLength) { $t.Substring(0, $PreviewLength) + "…" } else { $t }
    } else { "(内容已抑制)" }
    Write-Host ("  [{0}] {1}" -f $Layer, $Where) -ForegroundColor Yellow
    Write-Host ("        {0}" -f $preview) -ForegroundColor DarkGray
}

function Get-ScannableFiles {
    Get-ChildItem -Recurse -File -Path $Root -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notmatch '\\\.git\\' -and
            $_.Length -lt 2MB
        }
}

# ---------------------------------------------------------------------------
# L1：已知凭据格式
# ---------------------------------------------------------------------------
#: 说明：`-----BEGIN.*PRIVATE KEY-----` 常命中**文档里讲解 PEM 格式的文字**，属已知误报，
#: 需人工确认该行是否形如 `` ``-----BEGIN PRIVATE KEY-----`` ``（说明文字）而非真钥。
$KnownPatterns = [ordered]@{
    "AWS Access Key"        = 'AKIA[0-9A-Z]{16}'
    "GitHub PAT (classic)"  = 'ghp_[A-Za-z0-9]{20,}'
    "GitHub PAT (fine)"     = 'github_pat_[A-Za-z0-9_]{20,}'
    "GitHub OAuth"          = 'gho_[A-Za-z0-9]{20,}'
    "JWT"                   = 'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}'
    "OpenAI 风格 sk-"       = '\bsk-[A-Za-z0-9]{20,}'
    "Supabase PAT"          = 'sbp_[A-Za-z0-9]{20,}'
    "Slack token"           = 'xox[baprs]-[A-Za-z0-9-]{10,}'
    "Google API key"        = 'AIza[0-9A-Za-z_-]{35}'
    "PEM 私钥头"            = '-----BEGIN [A-Z ]*PRIVATE KEY-----'
    "带 apikey 的长串"      = 'apikey["'']?\s*[:=]\s*["'']?[A-Za-z0-9_\-\.]{32,}'
}

Write-Host "`n=== L1：已知凭据格式扫描 ===" -ForegroundColor Cyan
Write-Host "  仓库根：$Root"
$files = Get-ScannableFiles
foreach ($label in $KnownPatterns.Keys) {
    $pattern = $KnownPatterns[$label]
    $hits = $files | Select-String -Pattern $pattern -ErrorAction SilentlyContinue
    foreach ($hit in $hits) {
        $rel = $hit.Path.Replace($Root, "").TrimStart("\")
        Write-Hit -Layer $label -Where ("{0}:{1}" -f $rel, $hit.LineNumber) -Text $hit.Line
    }
}
if ($script:hitCount -eq 0) { Write-Host "  ✅ 未命中任何已知凭据格式" -ForegroundColor Green }

# ---------------------------------------------------------------------------
# L4：未跟踪的凭据文件与 .gitignore 覆盖
# ---------------------------------------------------------------------------
if ($LocalFiles) {
    Write-Host "`n=== L4：本地凭据文件与 .gitignore 覆盖 ===" -ForegroundColor Cyan

    $credFiles = Get-ChildItem -Recurse -File -Force -Path $Root -ErrorAction SilentlyContinue |
        Where-Object {
            $_.FullName -notmatch '\\\.git\\' -and
            $_.Name -match '^\.env|secret|credential|\.pem$|\.key$|token|password'
        }
    if ($credFiles) {
        foreach ($f in $credFiles) {
            $rel = $f.FullName.Replace($Root, "").TrimStart("\")
            Write-Hit -Layer "凭据类文件" -Where $rel -Text ("{0} 字节" -f $f.Length)
        }
    } else {
        Write-Host "  ✅ 工作区无凭据类文件" -ForegroundColor Green
    }

    # 已跟踪的凭据文件：.gitignore 管不住它们，必须 git rm --cached
    Push-Location $Root
    try {
        $tracked = git ls-files 2>$null | Select-String -Pattern '\.env$|\.pem$|\.key$|secret|credential'
        if ($tracked) {
            foreach ($t in $tracked) { Write-Hit -Layer "已跟踪(需 git rm --cached)" -Where $t.Line -Text "" }
        } else {
            Write-Host "  ✅ 无凭据类文件被 git 跟踪" -ForegroundColor Green
        }

        # .gitignore 是否覆盖 .env
        $ignore = Join-Path $Root ".gitignore"
        if (Test-Path $ignore) {
            $envCovered = Select-String -Path $ignore -Pattern '^\.env' -Quiet
            if ($envCovered) {
                Write-Host "  ✅ .gitignore 已覆盖 .env" -ForegroundColor Green
            } else {
                Write-Hit -Layer ".gitignore" -Where ".gitignore" -Text "未覆盖 .env / .env.*，建议补充"
            }
        } else {
            Write-Hit -Layer ".gitignore" -Where "(缺失)" -Text "仓库无 .gitignore"
        }
    } finally { Pop-Location }
}

# ---------------------------------------------------------------------------
# L3：git 全历史（最易漏的一层：曾提交过、后来删掉的凭据仍可检索）
# ---------------------------------------------------------------------------
if ($History) {
    Write-Host "`n=== L3：git 全历史扫描（仅保留首个命中片段，避免刷屏与回显滥用）===" -ForegroundColor Cyan
    Push-Location $Root
    try {
        $log = git log --all -p --pretty=format:'COMMIT:%h %s' 2>$null
        if (-not $log) {
            Write-Host "  ⚠️ 无 git 历史或不是 git 仓库，跳过" -ForegroundColor Yellow
        } else {
            $patterns = @(
                'eyJ[A-Za-z0-9_-]{30,}',                                    # JWT
                'apikey["'']?\s*[:=]\s*["'']?[A-Za-z0-9_\-\.]{40,}',        # 长 apikey
                'AKIA[0-9A-Z]{16}',                                         # AWS
                'ghp_[A-Za-z0-9]{20,}',                                     # GitHub PAT
                '-----BEGIN [A-Z ]*PRIVATE KEY-----'                        # PEM（多为文档文字）
            )
            $historyHits = 0
            foreach ($p in $patterns) {
                $found = $log | Select-String -Pattern $p | Select-Object -First 5
                foreach ($f in $found) {
                    $historyHits++
                    Write-Hit -Layer "历史" -Where ("pattern={0}" -f $p.Substring(0, [Math]::Min(18, $p.Length))) -Text $f.Line
                }
            }
            if ($historyHits -eq 0) {
                Write-Host "  ✅ 历史上从未出现过长密钥/JWT/私钥" -ForegroundColor Green
            } else {
                Write-Host "  ⚠️ 历史命中需逐条确认（文档示例文字常见误报）；" -ForegroundColor Yellow
                Write-Host "     若确认真实凭据已进入历史：该凭据应视为**已泄露**，必须轮换；" -ForegroundColor Yellow
                Write-Host "     清理历史（filter-repo / BFG）属破坏性操作，需所有者明确授权。" -ForegroundColor Yellow
            }
        }
    } finally { Pop-Location }
}

# ---------------------------------------------------------------------------
Write-Host ""
if ($script:hitCount -eq 0) {
    Write-Host "✅ 未命中（注意：L2 高熵串需另跑 EntropyScan.py；本结果不等于已完全脱敏）" -ForegroundColor Green
    exit 0
}
Write-Host ("⚠️ 命中 {0} 处 —— 逐条人工判断（文档示例文字属已知误报）" -f $script:hitCount) -ForegroundColor Yellow
exit 1
