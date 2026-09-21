param(
  [string]$PairCode = "",
  [string]$HostName = "",
  [string]$Root = "",
  [string]$Relay = "https://remote.livingruntime.com"
)

$ErrorActionPreference = "Stop"
$asset = "livingruntime-remote-connector-windows-amd64.exe"
$url = "https://github.com/jboone1989/livingruntime-remote/releases/latest/download/$asset"
$temp = Join-Path $env:TEMP $asset

if (-not $PairCode) { $PairCode = Read-Host "Pairing code from ChatGPT (XXXX-XXXX)" }
if (-not $HostName) { $HostName = Read-Host "SSH host or user@host" }
if (-not $Root) { $Root = Read-Host "Allowed workspace root on that host (for example /home/ubuntu)" }

Write-Host "Downloading LivingRuntime Remote Connector..."
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temp

Write-Host "Checking SSH, pairing this computer, and enabling autostart..."
& $temp install --pair $PairCode --host $HostName --root $Root --relay $Relay
if ($LASTEXITCODE -ne 0) { throw "LivingRuntime Remote Connector installation failed." }

Write-Host ""
Write-Host "LivingRuntime Remote is ready."
Write-Host "Return to ChatGPT and run: connection_status"
