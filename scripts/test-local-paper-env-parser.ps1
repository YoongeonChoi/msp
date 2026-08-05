[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$targets = @(
    (Join-Path $root "scripts\start-local-paper.ps1"),
    (Join-Path $root "scripts\verify-local-paper.ps1")
)

function Get-ReadDotEnvDefinition {
    param([Parameter(Mandatory = $true)][string]$Path)

    $tokens = $null
    $parseErrors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        $Path,
        [ref]$tokens,
        [ref]$parseErrors
    )
    if ($parseErrors.Count -gt 0) {
        throw "PowerShell AST parse failed: $Path"
    }
    $functions = @(
        $ast.FindAll(
            {
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    $node.Name -eq "Read-DotEnv"
            },
            $true
        )
    )
    if ($functions.Count -ne 1) {
        throw "Read-DotEnv definition count must be exactly one: $Path"
    }
    return $functions[0].Extent.Text
}

function Invoke-ReadDotEnv {
    param(
        [Parameter(Mandatory = $true)][string]$Definition,
        [Parameter(Mandatory = $true)][string]$Path
    )

    $testBlock = [ScriptBlock]::Create(@"
param([string]`$FixturePath)
$Definition
Read-DotEnv -Path `$FixturePath
"@)
    return & $testBlock $Path
}

$fixturePaths = @()
try {
    $validPath = [IO.Path]::GetTempFileName()
    $duplicatePath = [IO.Path]::GetTempFileName()
    $caseDuplicatePath = [IO.Path]::GetTempFileName()
    $fixturePaths = @($validPath, $duplicatePath, $caseDuplicatePath)

    Set-Content -LiteralPath $validPath -Encoding UTF8 -Value @(
        "ALPHA=one",
        "# ignored",
        "BETA=two=three"
    )
    Set-Content -LiteralPath $duplicatePath -Encoding UTF8 -Value @(
        "ALPHA=one",
        "ALPHA=two"
    )
    Set-Content -LiteralPath $caseDuplicatePath -Encoding UTF8 -Value @(
        "ALPHA=one",
        "alpha=two"
    )

    foreach ($target in $targets) {
        $definition = Get-ReadDotEnvDefinition -Path $target
        $values = Invoke-ReadDotEnv -Definition $definition -Path $validPath
        if ($values.Count -ne 2 -or $values["ALPHA"] -ne "one" -or $values["BETA"] -ne "two=three") {
            throw "Read-DotEnv valid fixture mismatch: $target"
        }

        foreach ($duplicateFixture in @($duplicatePath, $caseDuplicatePath)) {
            $rejected = $false
            try {
                Invoke-ReadDotEnv -Definition $definition -Path $duplicateFixture | Out-Null
            }
            catch {
                $rejected = $_.Exception.Message -match "중복된 env key"
            }
            if (-not $rejected) {
                throw "Read-DotEnv accepted a duplicate key fixture: $target"
            }
        }
    }
}
finally {
    foreach ($fixturePath in $fixturePaths) {
        if ($fixturePath -and (Test-Path -LiteralPath $fixturePath)) {
            Remove-Item -LiteralPath $fixturePath
        }
    }
}

Write-Output "PASS local PAPER env parsers reject duplicate keys"
