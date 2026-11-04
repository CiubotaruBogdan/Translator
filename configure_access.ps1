# Creates the local input file for `podman secret create` without displaying it.
# The password value never passes through cmd.exe or a process argument.
[CmdletBinding()]
param([string]$SecretPath = (Join-Path $PSScriptRoot 'translator-password.txt'))

$ErrorActionPreference = 'Stop'
$firstPtr = [IntPtr]::Zero
$secondPtr = [IntPtr]::Zero
$created = $false
try {
    if (Test-Path -LiteralPath $SecretPath) {
        if ((Get-Item -LiteralPath $SecretPath).Length -eq 0) {
            throw 'Fisierul local de parola este gol. Elimina fisierul gol si reia configurarea.'
        }
        Write-Host 'Se foloseste fisierul local de parola existent.'
        exit 0
    }
    $first = Read-Host 'Parola comuna pentru utilizatorul translator (minimum 12 caractere)' -AsSecureString
    $second = Read-Host 'Confirma parola' -AsSecureString
    $firstPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($first)
    $secondPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($second)
    $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($firstPtr)
    $confirmation = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($secondPtr)
    if ($password.Length -lt 12 -or $password.IndexOfAny([char[]]"`r`n`0") -ge 0) {
        throw 'Foloseste minimum 12 caractere, fara newline sau caractere NUL.'
    }
    if (-not [string]::Equals($password, $confirmation, [StringComparison]::Ordinal)) {
        throw 'Parolele nu coincid.'
    }
    # First create an empty file exclusively; protect it before writing secret data.
    $stream = [IO.File]::Open($SecretPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    $stream.Dispose()
    $created = $true
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $acl = New-Object Security.AccessControl.FileSecurity
    $acl.SetOwner($identity.User)
    $acl.SetAccessRuleProtection($true, $false)
    foreach ($sid in @($identity.User, (New-Object Security.Principal.SecurityIdentifier 'S-1-5-18'), (New-Object Security.Principal.SecurityIdentifier 'S-1-5-32-544'))) {
        $rule = New-Object Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'Allow')
        $acl.AddAccessRule($rule)
    }
    Set-Acl -LiteralPath $SecretPath -AclObject $acl
    [IO.File]::WriteAllText($SecretPath, $password, (New-Object Text.UTF8Encoding($false)))
    Write-Host 'Parola a fost salvata local cu acces restrictionat. Nu distribui acest fisier.'
    exit 0
} catch {
    if ($created) { Remove-Item -LiteralPath $SecretPath -Force -ErrorAction SilentlyContinue }
    Write-Error $_.Exception.Message
    exit 1
} finally {
    if ($firstPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($firstPtr) }
    if ($secondPtr -ne [IntPtr]::Zero) { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($secondPtr) }
    $password = $null
    $confirmation = $null
}
