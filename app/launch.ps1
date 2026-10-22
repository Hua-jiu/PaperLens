$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$PaperAppDir = $PSScriptRoot
$PaperProjectDir = [System.IO.Directory]::GetParent($PaperAppDir).FullName
$PaperRuntime = Join-Path $PaperProjectDir 'envs\python-windows-x86_64'
$PaperPython = Join-Path $PaperRuntime 'python.exe'
$PaperDryRun = $env:PAPER_READ_DRYRUN -eq '1'
$PaperSupportNames = @('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')
$PaperSupportDir = Join-Path $PaperAppDir 'runtime-support\windows-x86_64'

if (-not [Environment]::Is64BitOperatingSystem) {
    Write-Error '论文阅读器需要 64 位 Windows 10/11。'
    exit 1
}
foreach ($PaperName in @('PYTHONHOME', 'PYTHONPATH', 'PYTHONEXECUTABLE', 'PYTHONSAFEPATH', '__PYVENV_LAUNCHER__')) {
    Remove-Item -LiteralPath ('Env:' + $PaperName) -ErrorAction SilentlyContinue
}
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PIP_NO_COMPILE = '1'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)

function Test-PaperRuntime {
    if (-not [System.IO.File]::Exists($PaperPython)) { return $false }
    $ErrorActionPreference = 'SilentlyContinue'
    $env:PAPER_READ_RUNTIME_CHECK = $PaperRuntime
    $PaperProbe = @'
import os, pathlib, sqlite3, ssl, struct, sys
root = pathlib.Path(os.environ['PAPER_READ_RUNTIME_CHECK']).resolve()
assert sys.version_info[:3] == (3, 13, 9)
assert struct.calcsize('P') == 8
assert pathlib.Path(sys.executable).resolve().parent == root
assert pathlib.Path(sys.prefix).resolve() == root
assert pathlib.Path(sys.base_prefix).resolve() == root
assert pathlib.Path(ssl.__file__).resolve().is_relative_to(root)
assert pathlib.Path(sqlite3.__file__).resolve().is_relative_to(root)
'@
    try {
        $null = & $PaperPython -I -B -X utf8 -c $PaperProbe 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Test-PaperDependencies {
    $ErrorActionPreference = 'SilentlyContinue'
    $env:PAPER_READ_RUNTIME_CHECK = $PaperRuntime
    $PaperProbe = @'
import os, pathlib, sys, pymupdf, markitdown, numpy, onnxruntime
from markitdown.converters import PdfConverter
from markitdown import StreamInfo
root = pathlib.Path(os.environ['PAPER_READ_RUNTIME_CHECK']).resolve()
for module in (pymupdf, markitdown, numpy, onnxruntime):
    assert pathlib.Path(module.__file__).resolve().is_relative_to(root)
'@
    try {
        $null = & $PaperPython -I -B -X utf8 -c $PaperProbe 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

function Test-PaperRuntimeSupport {
    foreach ($PaperDll in $PaperSupportNames) {
        if (-not [System.IO.File]::Exists((Join-Path $PaperRuntime $PaperDll))) { return $false }
    }
    return $true
}

try {
    Set-Location -LiteralPath $PaperAppDir
    if (-not (Test-PaperRuntime)) {
        if ($PaperDryRun) { throw 'dryrun 模式下内置 Windows Python 不可用；不会下载或安装运行时。' }
        & (Join-Path $PaperAppDir 'setup_runtime.ps1')
        if (-not (Test-PaperRuntime)) { throw '内置 Windows Python 自检未通过，请重新运行启动入口。' }
    }
    if (-not (Test-PaperRuntimeSupport) -or -not (Test-PaperDependencies)) {
        if ($PaperDryRun) { throw 'dryrun 模式下 Windows 依赖或项目内置 C++ DLL 不完整；不会安装或修改环境。' }
        $PaperInstallLock = $null
        try {
            $PaperLockPath = Join-Path (Split-Path -Parent $PaperRuntime) '.runtime-install.lock'
            $PaperLockDeadline = [DateTime]::UtcNow.AddMinutes(5)
            while ($null -eq $PaperInstallLock) {
                try {
                    $PaperInstallLock = [System.IO.File]::Open($PaperLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
                }
                catch [System.IO.IOException] {
                    if ([DateTime]::UtcNow -ge $PaperLockDeadline) { throw '另一个窗口仍在准备运行环境，请稍后重新启动。' }
                    Start-Sleep -Milliseconds 250
                }
            }
            foreach ($PaperDll in $PaperSupportNames) {
                $PaperDllTarget = Join-Path $PaperRuntime $PaperDll
                if (-not [System.IO.File]::Exists($PaperDllTarget)) {
                    $PaperDllSource = Join-Path $PaperSupportDir $PaperDll
                    if (-not [System.IO.File]::Exists($PaperDllSource)) { throw ('项目缺少 Windows 支持文件 ' + $PaperDll + '，请使用完整的 Windows 离线包。') }
                    [System.IO.File]::Copy($PaperDllSource, $PaperDllTarget, $false)
                }
            }
            if (-not (Test-PaperDependencies)) {
                Write-Host '正在使用项目内置 Python 准备依赖，首次配置可能需要联网…'
                $PaperPreviousPreference = $ErrorActionPreference
                $ErrorActionPreference = 'SilentlyContinue'
                $null = & $PaperPython -I -B -X utf8 -m pip --version 2>$null
                $PaperHasPip = $LASTEXITCODE -eq 0
                $ErrorActionPreference = $PaperPreviousPreference
                if (-not $PaperHasPip) {
                    & $PaperPython -I -B -X utf8 -m ensurepip --upgrade
                    if ($LASTEXITCODE -ne 0) { throw '内置 pip 初始化失败。' }
                }
                & $PaperPython -B -E -s -X utf8 -c 'from bootstrap import main; main(dryrun=False)'
                if ($LASTEXITCODE -ne 0) { throw 'Windows 依赖配置未完成，请检查网络后重新启动。' }
                if (-not (Test-PaperDependencies)) { throw 'Windows 依赖自检未通过。' }
            }
        }
        finally {
            if ($null -ne $PaperInstallLock) { $PaperInstallLock.Dispose() }
        }
    }
    Remove-Item -LiteralPath 'Env:PAPER_READ_RUNTIME_CHECK' -ErrorAction SilentlyContinue
    $env:PATH = $PaperRuntime + ';' + (Join-Path $PaperRuntime 'Scripts') + ';' + $env:PATH
    & $PaperPython -B -E -s -X utf8 (Join-Path $PaperAppDir 'start.py')
    exit $LASTEXITCODE
}
catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
