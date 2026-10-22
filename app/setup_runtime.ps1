$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2.0

$PaperSetupAppDir = $PSScriptRoot
$PaperSetupProjectDir = [System.IO.Directory]::GetParent($PaperSetupAppDir).FullName
$PaperSetupEnvs = Join-Path $PaperSetupProjectDir 'envs'
$PaperSetupRuntime = Join-Path $PaperSetupEnvs 'python-windows-x86_64'
$PaperSetupUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20251014/cpython-3.13.9%2B20251014-x86_64-pc-windows-msvc-install_only_stripped.tar.gz'
$PaperSetupSha = '76e0a9749c4deeb975a4b6b36d54be4e43f0c2a4c654bedab5d2e4d62dbc3006'

if ($env:PAPERLENS_SETUP_DRYRUN -eq '1') {
    Write-Output 'dryrun=True'
    Write-Output ('runtime=' + $PaperSetupRuntime)
    Write-Output ('url=' + $PaperSetupUrl)
    Write-Output ('sha256=' + $PaperSetupSha)
    Write-Output 'plan=检查 Windows 独立 Python；缺失时下载、校验、暂存自检后安装；不依赖本机 Python。'
    return
}
if (-not [Environment]::Is64BitOperatingSystem) { throw '论文透镜需要 64 位 Windows 10/11。' }
foreach ($PaperName in @('PYTHONHOME', 'PYTHONPATH', 'PYTHONEXECUTABLE', 'PYTHONSAFEPATH', '__PYVENV_LAUNCHER__')) {
    Remove-Item -LiteralPath ('Env:' + $PaperName) -ErrorAction SilentlyContinue
}
$env:PYTHONNOUSERSITE = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PIP_NO_COMPILE = '1'

function Test-PaperSetupRuntime {
    param([string]$RuntimePath)
    $PaperCheckPython = Join-Path $RuntimePath 'python.exe'
    if (-not [System.IO.File]::Exists($PaperCheckPython)) { return $false }
    $ErrorActionPreference = 'SilentlyContinue'
    $env:PAPERLENS_RUNTIME_CHECK = $RuntimePath
    $PaperProbe = @'
import ensurepip, os, pathlib, sqlite3, ssl, struct, sys
root = pathlib.Path(os.environ['PAPERLENS_RUNTIME_CHECK']).resolve()
assert sys.version_info[:3] == (3, 13, 9)
assert struct.calcsize('P') == 8
assert pathlib.Path(sys.executable).resolve().parent == root
assert pathlib.Path(sys.prefix).resolve() == root
assert pathlib.Path(sys.base_prefix).resolve() == root
assert pathlib.Path(ssl.__file__).resolve().is_relative_to(root)
assert pathlib.Path(sqlite3.__file__).resolve().is_relative_to(root)
'@
    try {
        $null = & $PaperCheckPython -I -B -X utf8 -c $PaperProbe 2>$null
        return $LASTEXITCODE -eq 0
    }
    catch { return $false }
}

if (Test-PaperSetupRuntime $PaperSetupRuntime) { return }
$PaperSetupTar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (-not [System.IO.File]::Exists($PaperSetupTar)) {
    throw '自动准备运行时需要 Windows 10 1803 或更新版本自带的 tar.exe；完整离线包可直接运行。'
}

$PaperSetupStage = $null
$PaperSetupBackup = $null
$PaperSetupLock = $null
$PaperSetupPlaced = $false
$PaperSetupCommitted = $false
try {
    $null = [System.IO.Directory]::CreateDirectory($PaperSetupEnvs)
    $PaperSetupLockPath = Join-Path $PaperSetupEnvs '.runtime-install.lock'
    $PaperSetupDeadline = [DateTime]::UtcNow.AddMinutes(5)
    while ($null -eq $PaperSetupLock) {
        try {
            $PaperSetupLock = [System.IO.File]::Open($PaperSetupLockPath, [System.IO.FileMode]::OpenOrCreate, [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
        }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $PaperSetupDeadline) { throw '另一个窗口仍在准备运行时，请稍后重新启动。' }
            Start-Sleep -Milliseconds 250
        }
    }
    if (Test-PaperSetupRuntime $PaperSetupRuntime) { return }
    $PaperSetupStage = Join-Path $PaperSetupEnvs ('.python-runtime-' + [Guid]::NewGuid().ToString('N'))
    $null = [System.IO.Directory]::CreateDirectory($PaperSetupStage)
    $PaperSetupArchive = Join-Path $PaperSetupStage 'runtime.tar.gz'
    $PaperSetupStagedRuntime = Join-Path $PaperSetupStage 'python'
    Write-Host '正在下载项目内置 Windows Python，无需安装本机 Python 或 Conda…'
    $PaperPreviousTls = [Net.ServicePointManager]::SecurityProtocol
    try {
        [Net.ServicePointManager]::SecurityProtocol = $PaperPreviousTls -bor [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -UseBasicParsing -Uri $PaperSetupUrl -OutFile $PaperSetupArchive -TimeoutSec 300
    }
    finally {
        [Net.ServicePointManager]::SecurityProtocol = $PaperPreviousTls
    }
    if ((Get-FileHash -LiteralPath $PaperSetupArchive -Algorithm SHA256).Hash -ne $PaperSetupSha) {
        throw 'Windows Python 下载校验失败，已有环境未修改，请重新启动。'
    }
    & $PaperSetupTar -xzf $PaperSetupArchive -C $PaperSetupStage
    if ($LASTEXITCODE -ne 0) { throw 'Windows Python 解包失败，已有环境未修改。' }
    $PaperSetupSupportDir = Join-Path $PaperSetupAppDir 'runtime-support\windows-x86_64'
    foreach ($PaperSetupDll in @('msvcp140.dll', 'msvcp140_1.dll', 'vcruntime140.dll', 'vcruntime140_1.dll')) {
        $PaperSetupDllSource = Join-Path $PaperSetupSupportDir $PaperSetupDll
        if (-not [System.IO.File]::Exists($PaperSetupDllSource)) { throw ('项目缺少 Windows 支持文件 ' + $PaperSetupDll + '，请使用完整的 Windows 离线包。') }
        $PaperSetupDllTarget = Join-Path $PaperSetupStagedRuntime $PaperSetupDll
        [System.IO.File]::Copy($PaperSetupDllSource, $PaperSetupDllTarget, $true)
    }
    if (-not (Test-PaperSetupRuntime $PaperSetupStagedRuntime)) {
        throw '下载的 Windows Python 自检失败，已有环境未修改。'
    }
    $PaperStagedPython = Join-Path $PaperSetupStagedRuntime 'python.exe'
    $PaperPreviousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'SilentlyContinue'
    $null = & $PaperStagedPython -I -B -X utf8 -m pip --version 2>$null
    $PaperHasPip = $LASTEXITCODE -eq 0
    $ErrorActionPreference = $PaperPreviousPreference
    if (-not $PaperHasPip) {
        & $PaperStagedPython -I -B -X utf8 -m ensurepip --upgrade
        if ($LASTEXITCODE -ne 0) { throw '内置 Windows pip 初始化失败。' }
    }
    # Another launch may have completed while this download was in progress.
    if (Test-PaperSetupRuntime $PaperSetupRuntime) { return }
    if ([System.IO.Directory]::Exists($PaperSetupRuntime)) {
        $PaperSetupBackup = Join-Path $PaperSetupStage 'previous-runtime'
        [System.IO.Directory]::Move($PaperSetupRuntime, $PaperSetupBackup)
    }
    [System.IO.Directory]::Move($PaperSetupStagedRuntime, $PaperSetupRuntime)
    $PaperSetupPlaced = $true
    if (-not (Test-PaperSetupRuntime $PaperSetupRuntime)) {
        throw 'Windows Python 迁移自检失败，正在恢复原有环境。'
    }
    $PaperSetupCommitted = $true
    Write-Host '项目内置 Windows Python 已准备就绪。'
}
finally {
    try {
        if (-not $PaperSetupCommitted) {
            if ($PaperSetupPlaced -and [System.IO.Directory]::Exists($PaperSetupRuntime)) {
                [System.IO.Directory]::Delete($PaperSetupRuntime, $true)
            }
            if ($PaperSetupBackup -and [System.IO.Directory]::Exists($PaperSetupBackup)) {
                try { [System.IO.Directory]::Move($PaperSetupBackup, $PaperSetupRuntime) }
                catch { throw ('旧运行时已保留在 ' + $PaperSetupBackup + '，自动恢复未完成。') }
            }
        }
        if ($PaperSetupStage -and [System.IO.Directory]::Exists($PaperSetupStage)) {
            [System.IO.Directory]::Delete($PaperSetupStage, $true)
        }
    }
    finally {
        if ($null -ne $PaperSetupLock) { $PaperSetupLock.Dispose() }
    }
}
