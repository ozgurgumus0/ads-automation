# Publishes creative-ad-performance-v3.html to the Azure Static Web App.
# The deployment token is read from .swa-token next to this script (never committed).
$ErrorActionPreference = 'Stop'
$here  = Split-Path -Parent $MyInvocation.MyCommand.Path
$token = (Get-Content (Join-Path $here '.swa-token') -Raw).Trim()
Copy-Item (Join-Path $here '..\creative-ad-performance-v3.html') (Join-Path $here 'site\index.html') -Force
npx --yes @azure/static-web-apps-cli deploy (Join-Path $here 'site') --deployment-token $token --env production
