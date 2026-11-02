# SEO (SEO & GEO) morning refresh - Task Scheduler "ads_automation - SEO daily".
# Started with -WindowStyle Hidden: no console window that could be closed mid-run
# (2026-10-19 a closed window killed the run and left logs\seo_dashboard.lock behind).
# Writes the SEO-dashboard sheet; the SEO & GEO dashboard reads it. Log: logs\seo_daily.log
# Output goes through cmd so the log stays UTF-8 (PowerShell 5 redirection writes UTF-16).
Set-Location $PSScriptRoot
New-Item -ItemType Directory -Force logs | Out-Null
$log = Join-Path $PSScriptRoot 'logs\seo_daily.log'
$utf8 = New-Object Text.UTF8Encoding $false
[IO.File]::AppendAllText($log, "===== $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') =====`r`n", $utf8)
$env:PYTHONIOENCODING = 'utf-8'
cmd /c "python -u seo_dashboard.py >> `"$log`" 2>&1"
[IO.File]::AppendAllText($log, "(exit code $LASTEXITCODE)`r`n", $utf8)
