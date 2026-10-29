$ErrorActionPreference = "Stop"

$pythonDirectory = Join-Path $env:LOCALAPPDATA "Programs\Python"
$python = Get-ChildItem -Path $pythonDirectory -Directory -Filter "Python*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    ForEach-Object { Join-Path $_.FullName "python.exe" } |
    Where-Object { Test-Path $_ } |
    Select-Object -First 1
if (-not $python) {
    $command = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $command -or $command.Source -like "*WindowsApps*") {
        throw "Python 3.12 ou superior não foi encontrado. Instale Python e tente novamente."
    }
    $python = $command.Source
}

$credentialPath = Join-Path $PSScriptRoot "admin.local.xml"
if (Test-Path $credentialPath) {
    $credential = Import-Clixml -Path $credentialPath
    if ($credential -isnot [System.Management.Automation.PSCredential]) {
        throw "O arquivo local de credenciais não contém uma credencial válida."
    }
    $email = $credential.UserName
    $password = $credential.GetNetworkCredential().Password
    if ($password.Length -lt 14) {
        throw "A senha salva precisa ter pelo menos 14 caracteres."
    }
    $env:CIPA_ADMIN_EMAIL = $email
    $env:CIPA_ADMIN_PASSWORD = $password
}

try {
    & $python (Join-Path $PSScriptRoot "server.py")
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }
}
finally {
    if ($password) {
        Remove-Item Env:CIPA_ADMIN_EMAIL -ErrorAction SilentlyContinue
        Remove-Item Env:CIPA_ADMIN_PASSWORD -ErrorAction SilentlyContinue
    }
    $password = $null
}
