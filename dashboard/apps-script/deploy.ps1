# Uploads the current creative-ad-performance-v3.html to the Apps Script project
# and updates the existing web-app deployment (same URL every time).
# Who can open it is set in appsscript.json -> webapp.access (MYSELF / DOMAIN).
#
# Apps Script's HtmlService rewrites the HTML it serves, and that rewrite broke
# the dashboard's JavaScript ("Invalid left-hand side in assignment", 2026-10-06).
# So the page is shipped base64-encoded inside a tiny loader that decodes it in
# the browser and writes the untouched original into the frame.
param([switch]$BuildOnly)
$ErrorActionPreference = 'Stop'
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$clasp = Join-Path $here '.tools\node_modules\.bin\clasp.cmd'
Set-Location $here
$b64 = [Convert]::ToBase64String([IO.File]::ReadAllBytes((Join-Path $here '..\creative-ad-performance-v3.html')))
$loader = @"
<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Creative Performance</title></head>
<body style="font-family:sans-serif;color:#555">Loading dashboard…
<script>
var PAGE_B64 = "$b64";
window.addEventListener('load', function () {
  var bin = atob(PAGE_B64), bytes = new Uint8Array(bin.length);
  for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  var html = new TextDecoder('utf-8').decode(bytes);
  document.open(); document.write(html); document.close();
});
</script>
</body></html>
"@
[IO.File]::WriteAllText((Join-Path $here 'Index.html'), $loader, (New-Object Text.UTF8Encoding $false))
if ($BuildOnly) { return }
& $clasp push --force
$depFile = Join-Path $here '.deployment-id'
if (Test-Path $depFile) {
  & $clasp update-deployment (Get-Content $depFile -Raw).Trim() --description "update $(Get-Date -Format s)"
} else {
  & $clasp create-deployment --description "first deploy"
}
