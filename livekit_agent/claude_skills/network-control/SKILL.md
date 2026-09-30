---
name: network-control
description: Сеть и интернет на Windows — есть ли интернет, скорость, пинг, IP-адрес (локальный и внешний), Wi-Fi сети и пароль от своего Wi-Fi, подключение к сети, сброс DNS, какие программы используют сеть, открытые порты, устройства в локальной сети, VPN. Use for "нет интернета", "какой у меня IP", "пароль от вайфая", "проверь скорость", "пинг", "кто качает", network troubleshooting.
---

# Сеть и интернет

Чтение сетевого состояния выполняется без подтверждений. Изменение адаптеров, DNS, IP,
брандмауэра — с голосовым подтверждением (система сама остановит команду).
Включить/выключить Wi-Fi и Bluetooth — через `mcp__jarvis__system_control`.

## Есть ли интернет

```powershell
Test-NetConnection 1.1.1.1 -InformationLevel Quiet            # связь
Resolve-DnsName ya.ru -ErrorAction SilentlyContinue | Select -First 1   # DNS
Test-Connection ya.ru -Count 4 | Measure-Object -Property Latency -Average   # PS 7: Latency; PS 5: ResponseTime
Get-NetAdapter | Where Status -eq Up | Select Name, InterfaceDescription, LinkSpeed
```
Порядок вывода: нет связи с 1.1.1.1 → проблема роутера/провайдера/адаптера; связь есть, а
DNS нет → предложи сбросить DNS-кэш (`ipconfig /flushdns`, без подтверждения) или сменить DNS.

## Адреса

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Where { $_.IPAddress -notlike '169.*' -and $_.IPAddress -ne '127.0.0.1' } | Select InterfaceAlias, IPAddress
(Invoke-RestMethod https://api.ipify.org?format=json).ip        # внешний IP
Get-NetRoute -DestinationPrefix 0.0.0.0/0 | Select NextHop       # шлюз (роутер)
```

## Wi-Fi

```powershell
netsh wlan show interfaces            # текущая сеть, сигнал, скорость
netsh wlan show networks mode=bssid   # доступные сети
netsh wlan show profiles              # сохранённые сети
netsh wlan show profile name="ИмяСети" key=clear | Select-String "Key Content|Содержимое ключа"   # пароль сохранённой сети
netsh wlan connect name="ИмяСети"
netsh wlan disconnect
```
Пароль от Wi-Fi вслух называй только владельцу по его прямой просьбе.

## Скорость

Быстрая оценка загрузки без сторонних программ (~100 МБ):
```powershell
$sw = [Diagnostics.Stopwatch]::StartNew()
Invoke-WebRequest https://speed.cloudflare.com/__down?bytes=50000000 -OutFile $env:TEMP\speedtest.bin -UseBasicParsing
$sw.Stop(); [math]::Round(50*8/$sw.Elapsed.TotalSeconds,1)   # Мбит/с
Remove-Item $env:TEMP\speedtest.bin
```
Точный замер — Ookla CLI (`winget install Ookla.Speedtest.CLI`, нужно подтверждение), затем
`speedtest --accept-license --accept-gdpr -f json`.

## Кто использует сеть и порты

```powershell
Get-NetTCPConnection -State Established | Group OwningProcess | Sort Count -Desc | Select -First 10 Count, @{n='Process';e={(Get-Process -Id $_.Name -ErrorAction SilentlyContinue).ProcessName}}
Get-NetTCPConnection -State Listen | Select LocalPort, @{n='Process';e={(Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName}} | Sort LocalPort
Test-NetConnection example.com -Port 443
```

## Устройства в локальной сети

```powershell
Get-NetNeighbor -AddressFamily IPv4 | Where State -in 'Reachable','Stale' | Select IPAddress, LinkLayerAddress
```

## Изменения (с подтверждением)

```powershell
ipconfig /flushdns                                           # без подтверждения
Restart-NetAdapter -Name "Wi-Fi"                             # переподключить адаптер
Set-DnsClientServerAddress -InterfaceAlias "Wi-Fi" -ServerAddresses 1.1.1.1,8.8.8.8   # сменить DNS
Set-DnsClientServerAddress -InterfaceAlias "Wi-Fi" -ResetServerAddresses               # вернуть DNS от роутера
netsh winsock reset; netsh int ip reset                      # полный сброс сети, потом нужна перезагрузка
```
Многие из них требуют прав администратора; при отказе доступа предложи запуск через UAC:
`Start-Process powershell -Verb RunAs -ArgumentList '-NoExit','-Command','<команда>'`.
