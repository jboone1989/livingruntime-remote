param(
    [string]$Source = ""
)

$script = Join-Path $PSScriptRoot "install-personal.py"
if ($Source) {
    & python $script --source $Source
} else {
    & python $script
}
exit $LASTEXITCODE
