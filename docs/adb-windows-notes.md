# Windows 上 adb 认不到设备的排查笔记

这份笔记记录一个实际踩过的坑：华为 / 荣耀手机 USB 调试全开着，设备管理器里也显示正常，`adb devices` 却一直是空的。

## 现象

```
> pnputil /enum-devices /connected
...
设备描述: ADB Interface
实例 ID: USB\VID_339B&PID_107D&MI_02\6&20b743f8&2&0002
...
```

接口在，驱动也在跑，但：

```
> adb devices
List of devices attached
                          ← 空的
```

`adb pair` 走无线调试也是失败的（配对返回 Success，`adb connect` 一直 TLS 拒绝）。

## 根因

adb 在 Windows 上枚举设备靠的是**设备接口 GUID**。它只认带这个值的接口：

```
{F72FE0D4-CBCB-407d-8814-9ED673D0DD6B}
```

查那个接口的注册表：

```powershell
Get-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_339B&PID_107D&MI_02\*\Device Parameters' -Name DeviceInterfaceGUIDs
```

返回空。再看驱动服务：

```
DeviceDesc   = WinUsb Device
Service      = WINUSB
CompatibleIDs = USB\COMPAT_VID_339b&Class_ff&SubClass_42&Prot_01
```

系统用微软的通用 WinUSB 驱动接管了这个接口。这个驱动不写 `DeviceInterfaceGUIDs`，于是 adb 扫一遍 USB 设备树，什么都找不到。

这不是手机的错，也不是线的错。厂商没有为自家设备提供带正确 INF 的驱动，Windows 就退回通用驱动，缺了这一项配置。

## 解法

往那个接口的 `Device Parameters` 下写一个多字符串值，填上 adb 要的 GUID，然后重启该设备。

```powershell
$guid = '{F72FE0D4-CBCB-407d-8814-9ED673D0DD6B}'
$param = 'HKLM:\SYSTEM\CurrentControlSet\Enum\USB\VID_339B&PID_107D&MI_02\6&20b743f8&2&0002\Device Parameters'

New-ItemProperty -Path $param -Name DeviceInterfaceGUIDs -PropertyType MultiString -Value $guid -Force

pnputil /restart-device 'USB\VID_339B&PID_107D&MI_02\6&20b743f8&2&0002'
```

重启 adb 服务：

```powershell
adb kill-server
adb start-server
adb devices
```

设备就出来了。

仓库里的 `tools/fix_adb_driver.bat` 是这件事的自动化版本，会自动扫描所有 `CompatibleID = Class_ff&SubClass_42&Prot_01` 的接口（这是 ADB 接口的特征），逐个写入 GUID 并重启，不需要手动找实例 ID。

## 几个容易撞上的附带问题

**PowerShell 脚本中文乱码**

PowerShell 5.1 读 `.ps1` 时按系统代码页（简体中文是 GBK）解析。文件存成 UTF-8 不带 BOM，中文全部变成乱码，解析器直接报错：

```
表达式或语句中包含意外的标记"}"
字符串缺少终止符
```

存成 **UTF-8 with BOM** 就好了。这是写 PowerShell 脚本必须记住的一条。

**为什么写好了 GUID 还是要拔插一次**

`pnputil /restart-device` 大部分情况够用。偶尔设备没重新枚举，拔插数据线能强制触发。

**无线调试的坑**

`adb pair` 返回 `Successfully paired`，紧接着 `adb connect` 报 `failed to connect to ...`，反复重试都是这样。端口是通的（socket 能连上），失败发生在 TLS 握手阶段。

安卓 11 以上的无线调试要求两端 adb 版本兼容，版本跨度过大时会这样。这种状态下继续折腾性价比很低，USB 修好一次就一劳永逸。

**授权弹窗不出现**

接口通了、`adb devices` 显示 `unauthorized`，但手机没有任何弹窗。原因通常是之前点过「拒绝」并勾了「不再询问」。撤销方法：开发者选项 → 「撤销 USB 调试授权」，然后拔插一次。
