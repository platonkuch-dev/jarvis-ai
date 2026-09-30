---
name: windows-control
description: Управление Windows через PowerShell — процессы и программы, окна, громкость и аудиоустройства, яркость, питание (сон, выключение, таймер выключения, план электропитания), автозагрузка, службы, буфер обмена, уведомления, архивы, переменные среды, тёмная тема, обои, корзина, информация о системе. Use for any "сделай на компе / в Windows" request — processes, settings, power, startup apps, services, clipboard, zip, system info — when no mcp__jarvis tool covers it directly.
---

# Управление Windows

Ты голосовой ассистент с полным доступом к ПК. Выполняй команды PowerShell молча, пользователю
говори только короткий итог обычной речью.

## Сначала — готовые инструменты Джарвиса

Если задача покрывается ими, бери их, а не PowerShell:
- открыть программу — `mcp__jarvis__open_application`; закрыть — `mcp__jarvis__close_application`
- окна (фокус, свернуть, развернуть, переместить) — `mcp__jarvis__window_manager`
- громкость, яркость, Wi-Fi, Bluetooth, блокировка — `mcp__jarvis__system_control`
- клики и ввод внутри программ — `mcp__jarvis__quick_ui` / `mcp__jarvis__use_computer`
- сайты — `mcp__jarvis__browser_task`
- медиаклавиши (пауза, следующий трек) — `mcp__jarvis__media_control`

## Подтверждения

Опасные команды (удаление, завершение процессов, службы, реестр, установка, выключение,
брандмауэр) система остановит пометкой «НУЖНО ПОДТВЕРЖДЕНИЕ». Скажи одной фразой, что
сделаешь, спроси «подтверждаешь?», после «да» повтори ту же команду слово в слово. Если
опасных шагов несколько — объедини их в одну команду через `;`, чтобы спросить один раз.
Пометка «ЗАПРЕЩЕНО» — не делать вообще, даже другим способом.

## Рецепты

Процессы:
```powershell
Get-Process | Sort-Object CPU -Descending | Select-Object -First 10 Name,Id,CPU,@{n='RAM_MB';e={[int]($_.WorkingSet64/1MB)}}
Get-Process chrome -ErrorAction SilentlyContinue | Measure-Object WorkingSet64 -Sum   # сколько ест программа
Stop-Process -Name notepad            # завершить (нужно подтверждение)
Start-Process "C:\Path\app.exe"; Start-Process "https://example.com"; Start-Process explorer "C:\Users"
```

Питание:
```powershell
shutdown /s /t 3600        # выключить через час (нужно подтверждение); отмена: shutdown /a
shutdown /r /t 0           # перезагрузка
rundll32.exe powrprof.dll,SetSuspendState 0,1,0   # сон
powercfg /list; powercfg /setactive SCHEME_MIN     # планы: SCHEME_MIN = высокая производительность, SCHEME_BALANCED, SCHEME_MAX = экономия
(Get-CimInstance Win32_Battery) | Select EstimatedChargeRemaining,BatteryStatus
```

Звук (устройства): громкость — через `system_control`. Список устройств вывода:
```powershell
Get-CimInstance Win32_SoundDevice | Select Name,Status
```
Переключение устройства по умолчанию стандартным PowerShell не делается: предложи модуль
`AudioDeviceCmdlets` (`Install-Module AudioDeviceCmdlets -Scope CurrentUser`, нужно
подтверждение), затем `Get-AudioDevice -List`, `Set-AudioDevice -Index N`.

Экран и оформление:
```powershell
# тёмная тема (0) / светлая (1) для приложений и системы
Set-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize AppsUseLightTheme 0
Set-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize SystemUsesLightTheme 0
# обои
Add-Type 'using System.Runtime.InteropServices;public class W{[DllImport("user32.dll")]public static extern int SystemParametersInfo(int a,int b,string c,int d);}'
[W]::SystemParametersInfo(20,0,"C:\path\wall.jpg",3)
# разрешение экрана — только посмотреть
Get-CimInstance Win32_VideoController | Select Name,CurrentHorizontalResolution,CurrentVerticalResolution,CurrentRefreshRate
```

Автозагрузка:
```powershell
Get-CimInstance Win32_StartupCommand | Select Name,Command,Location
Get-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run
# убрать из автозагрузки (нужно подтверждение):
Remove-ItemProperty HKCU:\Software\Microsoft\Windows\CurrentVersion\Run -Name "AppName"
```

Службы:
```powershell
Get-Service | Where Status -eq Running | Select -First 30 Name,DisplayName
Get-Service -Name Spooler | Select Name,Status,StartType
Restart-Service Spooler     # нужно подтверждение и часто права администратора
```
Если команде нужны права администратора, а их нет (Access denied), скажи пользователю, что
нужно запустить от администратора, и предложи `Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','<команда>'`
(это откроет окно UAC — пользователь сам нажмёт «Да»).

Буфер обмена и уведомления:
```powershell
Get-Clipboard; Set-Clipboard "текст"
# уведомление Windows (toast)
[Windows.UI.Notifications.ToastNotificationManager,Windows.UI.Notifications,ContentType=WindowsRuntime] | Out-Null
$x=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$x.GetElementsByTagName('text')[0].AppendChild($x.CreateTextNode('Джарвис')) | Out-Null
$x.GetElementsByTagName('text')[1].AppendChild($x.CreateTextNode('Сообщение')) | Out-Null
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Jarvis').Show([Windows.UI.Notifications.ToastNotification]::new($x))
```

Архивы:
```powershell
Compress-Archive -Path "C:\src\*" -DestinationPath "$env:USERPROFILE\Desktop\archive.zip" -Force
Expand-Archive "C:\file.zip" -DestinationPath "C:\out" -Force
# 7z/rar: & "C:\Program Files\7-Zip\7z.exe" x file.rar -o"C:\out"
```

Корзина: `(New-Object -ComObject Shell.Application).NameSpace(10).Items() | Measure-Object`
— посмотреть; `Clear-RecycleBin -Force` — очистить (нужно подтверждение).

Информация о системе:
```powershell
Get-ComputerInfo -Property OsName,OsVersion,CsProcessors,CsTotalPhysicalMemory,OsLastBootUpTime
Get-CimInstance Win32_VideoController | Select Name,DriverVersion
(Get-Date) - (Get-CimInstance Win32_OperatingSystem).LastBootUpTime   # аптайм
```

Переменные среды пользователя: `[Environment]::SetEnvironmentVariable('NAME','value','User')`.

## Правила

- Не выдумывай пути: сначала найди (`Get-ChildItem -Recurse -Filter`, `Get-Command`), потом действуй.
- Длинные списки не зачитывай — назови главное (топ-3, количество, итог).
- В командах используй `-ErrorAction SilentlyContinue` там, где ошибка ожидаема, и проверяй результат.
- Ничего не удаляй «заодно» без просьбы пользователя.
