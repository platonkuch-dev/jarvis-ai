---
name: system-diagnostics
description: Диагностика и обслуживание Windows — почему тормозит компьютер, что грузит процессор/память/диск, сколько места и что его занимает, чистка временных файлов и кэша, ошибки в журналах, синий экран, здоровье диска (SMART), температура, драйверы, обновления Windows, время работы, батарея. Use for "почему тормозит", "освободи место", "почисти комп", "что с диском", "проверь систему", slow PC, disk space, cleanup, crashes, event log errors.
---

# Диагностика и обслуживание

Сначала собери факты (только чтение — выполняется без подтверждений), потом одной-двумя
фразами скажи главное и предложи действие. Чистку и завершение процессов делай только после
согласия — система всё равно попросит подтверждение.

## «Почему тормозит»

```powershell
$cpu = (Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average
$os = Get-CimInstance Win32_OperatingSystem
$ramUsed = [math]::Round(100 - $os.FreePhysicalMemory / $os.TotalVisibleMemorySize * 100)
"CPU $cpu%  RAM $ramUsed%"
Get-Process | Sort CPU -Desc | Select -First 5 Name,@{n='CPU_s';e={[int]$_.CPU}},@{n='RAM_MB';e={[int]($_.WorkingSet64/1MB)}}
Get-Process | Sort WorkingSet64 -Desc | Select -First 5 Name,@{n='RAM_MB';e={[int]($_.WorkingSet64/1MB)}}
Get-Counter '\PhysicalDisk(_Total)\% Disk Time' -SampleInterval 1 -MaxSamples 3 | % CounterSamples | Select CookedValue
```
Смотри также автозагрузку (`Get-CimInstance Win32_StartupCommand`), свободное место на C:
(меньше 10% — причина тормозов) и аптайм (не перезагружался неделями — предложи перезагрузку).

## Место на диске

```powershell
Get-PSDrive -PSProvider FileSystem | Select Name,@{n='Free_GB';e={[math]::Round($_.Free/1GB,1)}},@{n='Used_GB';e={[math]::Round($_.Used/1GB,1)}}
# крупнейшие папки в профиле
Get-ChildItem $env:USERPROFILE -Directory -Force -ErrorAction SilentlyContinue | ForEach-Object {
  [pscustomobject]@{Folder=$_.Name; GB=[math]::Round((Get-ChildItem $_.FullName -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum/1GB,2)}
} | Sort GB -Desc | Select -First 10
```
Для самых больших файлов есть `mcp__jarvis__file_manager` с action=largest.

## Чистка (после согласия пользователя)

Сначала посчитай, сколько освободится, назови цифру, потом одной командой удали — одно
подтверждение на всё:
```powershell
$paths = "$env:TEMP\*", "$env:LOCALAPPDATA\Temp\*", "C:\Windows\Temp\*", "C:\Windows\SoftwareDistribution\Download\*"
($paths | % { Get-ChildItem $_ -Recurse -Force -ErrorAction SilentlyContinue } | Measure-Object Length -Sum).Sum / 1GB
Remove-Item $paths -Recurse -Force -ErrorAction SilentlyContinue
Clear-RecycleBin -Force -ErrorAction SilentlyContinue
```
Встроенная очистка Windows: `cleanmgr /sagerun:1` (сначала один раз `cleanmgr /sageset:1`).
Кэш браузеров чисти только закрыв браузер и только по прямой просьбе. Папки Загрузки,
Документы и т.п. без явного указания не трогай.

## Ошибки и сбои

```powershell
Get-WinEvent -FilterHashtable @{LogName='System'; Level=1,2; StartTime=(Get-Date).AddDays(-2)} -MaxEvents 20 -ErrorAction SilentlyContinue |
  Select TimeCreated, ProviderName, Id, @{n='Msg';e={$_.Message.Split("`n")[0]}}
# синие экраны и внезапные выключения
Get-WinEvent -FilterHashtable @{LogName='System'; Id=41,1001,6008} -MaxEvents 10 -ErrorAction SilentlyContinue | Select TimeCreated, Id, ProviderName
Get-ChildItem C:\Windows\Minidump -ErrorAction SilentlyContinue | Sort LastWriteTime -Desc | Select -First 5 Name, LastWriteTime
# падения программ
Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000} -MaxEvents 10 -ErrorAction SilentlyContinue | Select TimeCreated, @{n='Msg';e={$_.Message.Split("`n")[0]}}
```

## Железо

```powershell
Get-PhysicalDisk | Select FriendlyName, MediaType, HealthStatus, OperationalStatus, @{n='GB';e={[int]($_.Size/1GB)}}
Get-PhysicalDisk | Get-StorageReliabilityCounter | Select Temperature, Wear, ReadErrorsTotal, WriteErrorsTotal
Get-CimInstance Win32_VideoController | Select Name, DriverVersion, DriverDate
nvidia-smi --query-gpu=name,temperature.gpu,utilization.gpu,memory.used,memory.total --format=csv   # если есть NVIDIA
Get-CimInstance -Namespace root/wmi MSAcpi_ThermalZoneTemperature -ErrorAction SilentlyContinue | % { [math]::Round($_.CurrentTemperature/10-273.15) }
powercfg /batteryreport /output "$env:TEMP\battery.html"   # отчёт о батарее ноутбука
```
Температуру CPU Windows часто не отдаёт без админа — тогда честно скажи, что нужна
программа вроде HWiNFO или LibreHardwareMonitor (можно поставить через software-manager).

## Проверка системных файлов

`sfc /scannow` и `DISM /Online /Cleanup-Image /RestoreHealth` требуют прав администратора и
идут долго (10–30 минут). Предложи, и после «да» запусти в отдельном окне с UAC:
`Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','sfc /scannow'`.

## Обновления Windows

```powershell
Get-HotFix | Sort InstalledOn -Desc | Select -First 5 HotFixID, InstalledOn
Start-Process ms-settings:windowsupdate      # открыть центр обновлений
```
