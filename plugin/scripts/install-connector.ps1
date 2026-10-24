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

if ($HostName -and $HostName -notin @("local", "localhost", "127.0.0.1", "::1")) {
  throw "Install one Connector on each machine. -HostName SSH routing has been retired."
}

if (-not $PairCode) { $PairCode = Read-Host "Pairing code from ChatGPT (XXXX-XXXX)" }
if (-not $Root) { $Root = Read-Host "Allowed Windows workspace root (for example D:\Projects)" }

Write-Host "Downloading LivingRuntime Remote Connector..."
Invoke-WebRequest -UseBasicParsing -Uri $url -OutFile $temp

Write-Host "Pairing this computer with the native Windows executor and enabling autostart..."
& $temp install --pair $PairCode --local --root $Root --relay $Relay
if ($LASTEXITCODE -ne 0) { throw "LivingRuntime Remote Connector installation failed." }

Write-Host ""
Write-Host "LivingRuntime Remote is ready."
Write-Host "Return to ChatGPT and run: connection_status"
