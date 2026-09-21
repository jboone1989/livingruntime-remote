param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$TunnelArgs
)

$script = Join-Path $PSScriptRoot "tunnel.py"
& python $script @TunnelArgs
exit $LASTEXITCODE
