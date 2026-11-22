# fix_adb_driver.ps1
# 给 Windows 上的安卓 ADB 接口补上 adb 需要的设备接口 GUID
# 由 fix_adb_driver.bat 以管理员权限调用
#
# 背景：部分品牌（华为 / 荣耀等）的 ADB 接口会被系统用通用 WinUSB 驱动接管，
# 而 adb 只枚举带特定 DeviceInterfaceGUIDs 的设备，于是 adb devices 一直为空。
# 这个脚本给那个接口补上 GUID 并重启设备，让 adb 能认出来。

$ErrorActionPreference = 'Continue'
$log = Join-Path $PSScriptRoot 'fix_adb_driver.log'
Start-Transcript -Path $log -Force | Out-Null

$TARGET_GUID = '{F72FE0D4-CBCB-407d-8814-9ED673D0DD6B}'
$BASE = 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB'

Write-Host '=== 0. 查找 adb ==='
$candidates = @(
    $env:ADB_PATH,
    (Get-Command adb -ErrorAction SilentlyContinue).Source,
    (Join-Path $env:LOCALAPPDATA 'Android\Sdk\platform-tools\adb.exe'),
    (Join-Path $PSScriptRoot 'platform-tools\adb.exe'),
    (Join-Path $PSScriptRoot '..\platform-tools\adb.exe'),
    'C:\Android\platform-tools\adb.exe',
    'C:\platform-tools\adb.exe'
)
$ADB = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if ($ADB) {
    Write-Host ("  " + $ADB)
} else {
    Write-Host '  未找到 adb，后面跳过 adb 重启步骤。'
}

Write-Host ''
Write-Host '=== 1. 定位 ADB 接口设备 ==='
Write-Host '  扫描所有 USB 设备，找 CompatibleID 为 Class_ff&SubClass_42&Prot_01 的接口'

$targets = @()
foreach ($vidKey in (Get-ChildItem $BASE -ErrorAction SilentlyContinue)) {
    foreach ($inst in (Get-ChildItem $vidKey.PSPath -ErrorAction SilentlyContinue)) {
        $instPath = "HKLM:\SYSTEM\CurrentControlSet\Enum\USB\$($vidKey.PSChildName)\$($inst.PSChildName)"
        $ids = (Get-ItemProperty -Path $instPath -Name CompatibleIDs -ErrorAction SilentlyContinue).CompatibleIDs
        if (-not $ids) { continue }
        if (($ids -join ' ') -match 'Class_ff&SubClass_42&Prot_01') {
            $targets += $instPath.Substring($instPath.IndexOf('USB\'))
            Write-Host ("  找到: " + $instPath.Substring($instPath.IndexOf('USB\')))
        }
    }
}

if (-not $targets) {
    Write-Host ''
    Write-Host '  没有找到 ADB 接口。按顺序检查：'
    Write-Host '    1. 数据线是否支持数据传输（只能充电的线不行）'
    Write-Host '    2. 手机开发者选项里的「USB 调试」是否打开'
    Write-Host '    3. 手机 USB 连接模式改成「传输文件 / MTP」'
    Write-Host '    4. 拔插一次数据线'
    Stop-Transcript | Out-Null
    Read-Host '按回车键退出'
    exit 1
}

Write-Host ''
Write-Host '=== 2. 写入 GUID 并重启设备 ==='
foreach ($full in $targets) {
    $paramPath = "HKLM:\SYSTEM\CurrentControlSet\Enum\$full\Device Parameters"

    if (-not (Test-Path $paramPath)) {
        New-Item -Path $paramPath -Force | Out-Null
        Write-Host '  已创建 Device Parameters 键'
    }

    $existing = (Get-ItemProperty -Path $paramPath -Name DeviceInterfaceGUIDs -ErrorAction SilentlyContinue).DeviceInterfaceGUIDs
    if ($existing) {
        Write-Host ("  现有 GUID: " + ($existing -join ', '))
    }

    New-ItemProperty -Path $paramPath -Name DeviceInterfaceGUIDs -PropertyType MultiString `
                     -Value $TARGET_GUID -Force | Out-Null
    Write-Host ("  已写入 GUID: " + $TARGET_GUID)

    Write-Host '  重启该 USB 设备使配置生效...'
    $out = pnputil /restart-device "$full" 2>&1
    Write-Host ("  " + ($out -join ' '))
}

if (-not $ADB) {
    Write-Host ''
    Write-Host '>>> GUID 已写入。因为没有找到 adb，请手动执行 adb devices 验证。'
    Stop-Transcript | Out-Null
    Read-Host '按回车键退出'
    exit 0
}

Write-Host ''
Write-Host '=== 3. 重启 adb 服务 ==='
& $ADB kill-server 2>&1 | Out-Null
Start-Sleep -Seconds 2
& $ADB start-server 2>&1 | Out-Null
Start-Sleep -Seconds 3

Write-Host ''
Write-Host '=== 4. 设备列表 ==='
$devices = & $ADB devices -l 2>&1
$devices | ForEach-Object { Write-Host $_ }

Write-Host ''
if ($devices -match '\bdevice\b') {
    Write-Host '>>> 成功：设备已连接。现在可以启动采集程序了。'
} elseif ($devices -match 'unauthorized') {
    Write-Host '>>> 接口通了，但手机未授权。解锁手机，在弹出的「允许 USB 调试」对话框点「允许」。'
} else {
    Write-Host '>>> 仍未识别。尝试：把手机 USB 连接模式改成「传输文件」，重新插拔数据线，再跑一次本脚本。'
}

Write-Host ''
Write-Host ("日志已保存: " + $log)
Stop-Transcript | Out-Null
Read-Host '按回车键退出'
