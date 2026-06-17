# 将 OneCore 语音（含 Kangkang / Yaoyao）注册到桌面 SAPI，供 pyttsx3 使用
# 必须以管理员身份运行 PowerShell，然后：
#   Set-ExecutionPolicy -Scope Process Bypass -Force
#   cd "本课程目录"
#   .\enable_onecore_sapi_voices.ps1

$ErrorActionPreference = 'Stop'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "[错误] 请右键 PowerShell -> 以管理员身份运行" -ForegroundColor Red
    exit 1
}

$sourcePath = 'HKLM:\SOFTWARE\Microsoft\Speech_OneCore\Voices\Tokens'
$dest64 = 'HKLM:\SOFTWARE\Microsoft\Speech\Voices\Tokens'
$dest32 = 'HKLM:\SOFTWARE\WOW6432Node\Microsoft\SPEECH\Voices\Tokens'

if (-not (Test-Path $sourcePath)) {
    Write-Host "[错误] 未找到 OneCore 语音：$sourcePath"
    exit 1
}

foreach ($dest in @($dest64, $dest32)) {
    if (-not (Test-Path $dest)) {
        New-Item -Path $dest -Force | Out-Null
    }
}

$voices = Get-ChildItem $sourcePath
Write-Host "正在桥接 $($voices.Count) 个 OneCore 语音到 SAPI..."
foreach ($voice in $voices) {
    $name = $voice.PSChildName
    Copy-Item -Path $voice.PSPath -Destination $dest64 -Recurse -Force
    Copy-Item -Path $voice.PSPath -Destination $dest32 -Recurse -Force
    Write-Host "  OK $name"
}

Write-Host ""
Write-Host "完成。请运行: python list_local_voices.py" -ForegroundColor Green
Write-Host "若出现 Kangkang，可在 tubeget.config.json 设置: `"local_voice`": `"kangkang`""
