---
name: software-manager
description: Установка, обновление и удаление программ на Windows через winget (и Microsoft Store), список установленных программ, проверка обновлений, поиск программы по названию, установка Python/npm-пакетов. Use when the user says "установи", "скачай программу", "обнови все программы", "удали программу", "какие программы установлены", "есть ли обновления", install/uninstall/update software.
---

# Программы: установка, обновление, удаление

Основной инструмент — `winget` (встроен в Windows 11). Установка, обновление и удаление
требуют голосового подтверждения — система сама остановит команду, спроси пользователя
одной фразой и после «да» повтори ту же команду.

## Порядок действий при «установи X»

1. Найди точный ID — не угадывай:
   ```powershell
   winget search "telegram" --accept-source-agreements | Select-Object -First 15
   ```
   Предпочитай пакет из источника `winget` с официальным издателем. Если вариантов несколько
   и неясно, какой нужен, — спроси коротко («десктоп или из магазина?»).
2. Проверь, не установлена ли уже: `winget list --id <ID> -e --accept-source-agreements`.
3. Установи (нужно подтверждение — скажи название программы и что будешь ставить):
   ```powershell
   winget install --id <ID> -e --silent --accept-package-agreements --accept-source-agreements
   ```
4. Проверь код выхода и вывод. Скажи итог коротко: «Поставил Телеграм, можно запускать».
   Открыть после установки — `mcp__jarvis__open_application`.

Популярные ID: Google.Chrome, Mozilla.Firefox, Telegram.TelegramDesktop, Discord.Discord,
Valve.Steam, VideoLAN.VLC, 7zip.7zip, Notepad++.Notepad++, Microsoft.VisualStudioCode,
Spotify.Spotify, OBSProject.OBSStudio, Python.Python.3.12, OpenJS.NodeJS.LTS, Git.Git,
Microsoft.PowerToys, qBittorrent.qBittorrent, Zoom.Zoom, AIMP.AIMP, Gyan.FFmpeg, yt-dlp.yt-dlp.
Всё равно проверь через `winget search` — ID иногда меняются.

## Обновления

```powershell
winget upgrade --accept-source-agreements            # что можно обновить
winget upgrade --id <ID> -e --silent --accept-package-agreements --accept-source-agreements
winget upgrade --all --silent --accept-package-agreements --accept-source-agreements   # всё сразу
```
Перед «обнови всё» назови количество программ и спроси подтверждение одним вопросом.

## Удаление

```powershell
winget list --accept-source-agreements | Select-String -Pattern "spotify"   # найти точное имя/ID
winget uninstall --id <ID> -e --silent
```
Удаление — только по прямой просьбе. Системные компоненты Windows и драйверы не удаляй.

## Список установленного

```powershell
winget list --accept-source-agreements
Get-ItemProperty HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*, HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*, HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\* -ErrorAction SilentlyContinue |
  Where DisplayName | Select DisplayName, DisplayVersion, Publisher | Sort DisplayName
```

## Пакеты для разработки и скиллов

- Python: `python -m pip install --user <пакет>` (ставится без подтверждения).
- Node: `npm install -g <пакет>`.
Ставь только известные пакеты с точным именем — опечатка в имени может оказаться вредоносным пакетом.

## Если winget не справился

- «No package found» — поищи по другому названию или на английском.
- Нужны права администратора (0x8a150056, Access denied) — скажи пользователю, что появится
  окно UAC, и запусти: `Start-Process winget -Verb RunAs -ArgumentList 'install','--id','<ID>','-e'`.
- Программы нет в winget — найди официальный сайт через WebSearch и скажи пользователю, что
  скачать установщик нужно с него; сам не запускай скачанные .exe с непроверенных сайтов.
