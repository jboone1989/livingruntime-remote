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
if (-not $Root) { $Root = Read-Host "Allowed Windows workspace root (for example D:\Projects)" }

Write-Host "Downloading LivingRuntime Remote Connector..."
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temp

if ($HostName) {
  Write-Host "Checking SSH, pairing this computer, and enabling autostart..."
  & $temp install --pair $PairCode --host $HostName --root $Root --relay $Relay
} else {
  Write-Host "Pairing this computer with the native Windows executor and enabling autostart..."
  & $temp install --pair $PairCode --local --root $Root --relay $Relay
}
if ($LASTEXITCODE -ne 0) { throw "LivingRuntime Remote Connector installation failed." }

Write-Host ""
Write-Host "LivingRuntime Remote is ready."
Write-Host "Return to ChatGPT and run: connection_status"
