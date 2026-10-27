param(
    [ValidateSet('x64','x86')][string]$Architecture = 'x64',
    [string]$Python = 'python'
)
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
$bits = & $Python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0) { throw 'Cannot run the selected Python interpreter.' }
$expected = if ($Architecture -eq 'x64') { '64' } else { '32' }
if ("$bits".Trim() -ne $expected) { throw "Use $expected-bit Python for $Architecture. PyInstaller is not a cross-compiler." }
& $Python -m unittest discover -s tests -q
if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
& $Python -m PyInstaller --noconfirm --log-level WARN --onefile --noconsole --uac-admin `
    --name "GcpVpnManager-windows-$Architecture" `
    --distpath "releases\windows-$Architecture" `
    --workpath "build\$Architecture" --specpath build `
    --paths src src\app.py
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
$output = "releases\windows-$Architecture\GcpVpnManager-windows-$Architecture.exe"
& $Python scripts\verify_exe.py $output $Architecture
if ($LASTEXITCODE -ne 0) { throw 'Executable verification failed.' }
& $Python scripts\collect_notices.py "releases\windows-$Architecture\licenses"
if ($LASTEXITCODE -ne 0) { throw 'License collection failed.' }
& $Python -m pip freeze | Out-File "releases\windows-$Architecture\dependencies.txt" -Encoding utf8
