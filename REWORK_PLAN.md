# REWORK_PLAN.md — архитектурные решения (Стадия 2)

Это **предложение**, ничего отсюда не исполнено. Основано на находках `AUDIT.md`. Три
раздела ниже соответствуют трём вопросам Стадии 2: судьба `native-core`/`agent-ts`, разбивка
`main.py`/`ui.py`, централизация диспетчера инструментов с уровнями риска.

---

## 1. Судьба `native-core/` + `agent-ts/`

### Факты, а не предположения
Я реально прогнал оба тестовых набора в этой среде (Rust toolchain и Node 24.19 оказались
установлены) — это не мокнутые юнит-тесты ради галочки:

- `cargo test --release` в `native-core/`: **17/17 passed** (9 unit + 8 integration) — ровно
  столько, сколько заявляет readme. Интеграционные тесты реально открывают/печатают/читают/
  закрывают настоящий Notepad, проверяют защиту `kill_process` от системных процессов,
  graceful-fail на несуществующем окне — не заглушки.
- `npm test` в `agent-ts/`: **25/25 passed** (5 файлов), включая `rustBridge.integration.test.ts`,
  который поднимает настоящий `native-core.exe` и прогоняет полный цикл
  `open_application → type_into_element → read_element → close_application` через реальный
  Notepad — то есть весь стек Rust↔TS↔Windows реально работает вместе, не только по отдельности.

Для сравнения: Python-часть (`main.py`/`actions/`) в этой сессии показала конкретные сломанные
вещи (`dev_agent` со сломанным `import sys`, `weather_report`-заглушку, `youtube_video`
trending-заглушку — см. AUDIT.md). **native-core/agent-ts на своём узком срезе (управление
окнами/процессами) сейчас качественнее протестирован и надёжнее, чем эквивалентный код на
Python-стороне** (`window_manager`/`ui_automation` tools в AUDIT.md получили ⚠️ НЕСТАБИЛЬНО
именно из-за проблем поиска по имени — того самого класса задач, что native-core покрывает
тестами `resolve_one_fails_honestly_for_a_named_but_missing_app`,
`stem_normalizes_case_spaces_and_exe_suffix`).

### Но: он не подключён к голосовому циклу
`agent-ts`'s `Agent.ts` реализует полный Claude tool-calling loop независимо от
`main.py`/Gemini Live. Это два параллельных мира: пользователь сейчас управляет компьютером
голосом только через Python-путь (`windows_control/` → win32/UIA/pyautogui/vision), а
`native-core`/`agent-ts`-путь доступен только через `node src/cli.ts agent "..."` в
терминале — то есть ценность для *ежедневного использования ассистентом* сейчас равна нулю,
несмотря на то, что код лучше протестирован.

### Три варианта — аргументы, не решение

**A. Продолжать как параллельную ветку** (нынешний статус-кво, но осознанно)
- За: код уже качественный и протестированный; реализует принцип SAFE/NORMAL/SENSITIVE/
  DANGEROUS, которого нет в Python-стороне (см. §3) — можно использовать как референс, не
  переписывая с нуля.
- Против: две кодовые базы для одной функции (управление окнами) расходятся по поведению
  со временем (Python-сторона уже отличается: `ExecutionContext` в Python имеет свои нюансы
  локализации UWP-приложений, которых нет в тестах Rust-стороны). Даёт readme право писать
  "17 tools... architecture" фичей, которыми голосовой ассистент не пользуется — ровно тот
  вид "строчки в списке фич" из вашей исходной задачи.
- Стоимость: нужно поддерживать актуальность Rust toolchain/Node в setup-инструкциях, тесты
  гонять в CI отдельно, разработчик держит в голове две архитектуры.

**B. Заморозить и явно задокументировать статус** (мой умеренный дефолт)
- Оставить код как есть (он рабочий и протестированный — удалять качественную, покрытую
  тестами реализацию было бы расточительно), но прямо в readme написать: "экспериментальный,
  НЕ используется голосовым циклом, доказывает архитектурный паттерн для будущей миграции,
  не поддерживается активно" — вместо нынешней подачи как почти-готовой фичи с полным
  installation/development/troubleshooting разделом, которая создаёт впечатление, что это
  часть рабочего продукта.
- За: ничего не теряется, лучший протестированный код остаётся доступным как референс/задел
  на будущее, но пользователь Mark XLVIII перестаёт ожидать от него того, чего оно не делает.
  Дешёво: правка readme + пометка в коде, без риска что-то сломать.
- Против: мёртвый вес в репозитории продолжает требовать `cargo build`/`npm install` в setup
  для тех, кто всё же полезет проверять, плюс когнитивная нагрузка "а это точно не используется?"
  для любого будущего аудита/контрибьютора без свежего контекста.

**C. Удалить как мёртвый вес**
- За: минимальный репозиторий, одна кодовая база, никакой путаницы "это работает или нет".
- Против: **выбрасывает единственную часть проекта, которая прошла собственный аудит с первого
  раза** — 17+25 реальных тестов против нуля тестов у эквивалентного Python-кода. Если когда-то
  будет решение мигрировать voice/Telegram/память на эту архитектуру (сам readme описывает это
  как долгосрочный план), это решение придётся принимать заново с нуля позже.

### Моя рекомендация
**B (заморозить + документировать)**, с ревизией через фиксированный срок (например,
3 месяца) — либо явно начинается интеграция в голосовой цикл, либо тогда встаёт вопрос C.
Но решение ваше — все три варианта реалистичны, аргументы выше должны быть достаточны, чтобы
выбрать осознанно.

---

## 2. Разбивка `main.py` (1818 строк) и `ui.py` (3099 строк)

### Текущая структура (факт, не оценка)
`main.py` — по сути один класс `JarvisLive` (строки 681–1804, ~1120 строк) плюс горстка
модульных функций сверху (классификация ошибок реконнекта, загрузка промпта). Методы класса
группируются по факту в чёткие кластеры, которые уже сейчас редко пересекаются друг с другом:

| Кластер | Методы | Примерные строки |
|---|---|---|
| Reconnect/lifecycle | `run()`, `_build_config()`, `__init__`, `interrupt()`, `set_speaking()` | 683-825, 1580-1804 |
| Tool-диспетчер | `_execute_tool()` | 829-1050 |
| Аудио pipeline | `_send_realtime()`, `_listen_audio()`, `_play_audio()`, `_receive_audio()` | 1051-1102, 1243-1429 |
| Fast Path (wake-word) | `_run_fast_path()`, `_handle_fast_path_utterance[_inner]()` | 1103-1242 |
| System monitor / Proactive | `_run_system_monitor()`, `_run_proactive_mode()` | 1430-1480 |
| Dashboard-мост | `_relay_phone_audio()`, `_on_phone_connected()`, `_process_dashboard_commands()` | 1481-1534 |
| Telegram-мост | `_process_telegram_commands()` | 1535-1579 |
| Ошибки/классификация | `_flatten_exceptions()`, `_is_audio_device_error()` (module-level) | 104-134 |

`ui.py` — не один God-class, а последовательность независимых `QWidget`-подклассов
(`HudCanvas`, `ReactorSplash`, `MetricBar`, `LogWidget`, `FileDropZone`, `_CameraPreview`,
`SetupOverlay`, `RemoteKeyOverlay`) плюс один большой `MainWindow` (строки 1831–2979,
~1150 строк) и обёртка `JarvisUI` поверх него. Разбивка здесь физически проще: виджеты уже
разделены классами, просто живут в одном файле.

### Принцип миграции: **переезд файлов, не переписывание логики**
Каждый шаг — это перемещение существующего кода в новый модуль с сохранением сигнатур,
плюс тонкий фасад/делегирование там, где нужно сохранить `self.`-доступ к общему состоянию
(сессия, `self.session`, `self.ui`, флаги `_vision_busy` и т.д.). Никакая логика внутри
методов не переписывается на этом этапе — это отдельная задача (Стадия 3), а не эта.

### Предлагаемые модули

```
core/
  session.py       # JarvisLive.__init__, run(), _build_config(), reconnect-классификация
                    # (переносит _flatten_exceptions/_is_audio_device_error из main.py)
  audio_pipeline.py # _send_realtime, _listen_audio, _play_audio, _receive_audio
  fast_path.py      # _run_fast_path, _handle_fast_path_utterance[_inner]
  tool_dispatch.py  # _execute_tool + (Стадия 3) реестр риска — см. §3
  system_monitor_bridge.py  # _run_system_monitor, _run_proactive_mode
                    # (тонкие обёртки — сама логика уже в actions/system_monitor.py,
                    # actions/proactive.py, тут только orchestration-петля)
  dashboard_bridge.py  # _relay_phone_audio, _on_phone_connected, _process_dashboard_commands
  telegram_bridge.py   # _process_telegram_commands

ui/  (переименовать текущий ui.py в пакет)
  hud_canvas.py, reactor_splash.py, metric_bar.py, log_widget.py,
  file_drop_zone.py, camera_preview.py, setup_overlay.py, remote_key_overlay.py,
  main_window.py    # MainWindow (самый большой кусок, ~1150 строк) — если хочется мельче,
                     # можно дальше делить MainWindow на миксины по вкладкам/панелям, но это
                     # уже вторая итерация, не первая
  jarvis_ui.py       # JarvisUI + _RootShim — публичный фасад, который сейчас импортирует main.py
```

`JarvisLive` в `main.py` (или в новом `core/session.py`) продолжает существовать как класс,
но большинство его текущих методов становятся либо тонкими делегирующими обёртками
(`async def _execute_tool(self, fc): return await tool_dispatch.execute(self, fc)`), либо
переезжают целиком, а `main.py` в итоге сжимается до сборки/склейки модулей + `main()`.

### Пошаговый план (каждый шаг — отдельный коммит, проверяется вручную перед следующим)

1. **`core/session.py`**: перенести `_flatten_exceptions`/`_is_audio_device_error`/
   `_load_system_prompt`/`_clean_transcript` как module-level функции без изменений.
   Проверка: `test_error_classification.py` (уже существует) проходит без изменений импортов
   кроме источника.
2. **`core/audio_pipeline.py`**: перенести 4 аудио-метода как функции, принимающие `self`
   (JarvisLive instance) первым аргументом, вызываются из `run()` как раньше через
   `tg.create_task(audio_pipeline._listen_audio(self))`. Проверка: ручной запуск, голосовой
   разговор происходит как раньше (эхо, интеррапт, TTS).
3. **`core/tool_dispatch.py`**: перенести `_execute_tool` целиком как есть (без реестра
   риска — это отдельный PR по итогам §3). Проверка: вызвать 3-5 tool'ов вручную голосом,
   сверить с поведением до переноса.
4. **`core/fast_path.py`**: перенести fast-path методы. Проверка: сказать wake-word + быструю
   команду, сверить задержку и корректность (уже есть регрессия из Стадии 0 на этот путь —
   перепроверить, что try/except не потерялся при переносе).
5. **`core/dashboard_bridge.py`** и **`core/telegram_bridge.py`**: перенести по отдельности
   (это два независимых коммита, не один) — каждый проверяется отдельно: подключить телефон
   через dashboard, отправить команду в Telegram, убедиться что оба моста работают.
6. **`core/system_monitor_bridge.py`**: перенести, проверить что hardware-алерты и
   proactive-check-in всё ещё срабатывают (с поправкой на баг из AUDIT.md §2.3, который
   стоит чинить отдельно на Стадии 3, не здесь).
7. **`main.py`**: после шагов 1-6 в файле должен остаться только `JarvisLive.__init__`,
   `_build_config`, `run()`, `interrupt`/`set_speaking`/`speak*`, `main()`/`runner()` — то есть
   собственно "session manager", ради которого он и задуман по имени `core/session.py`. На
   этом шаге можно либо оставить остаток в `main.py`, либо довести перенос до конца и сделать
   `main.py` тонким entry point'ом — решить по факту, как выглядит остаток.
8. **`ui.py` → `ui/`-пакет**: перенос виджетов по одному, каждый — независимый коммит
   (виджеты не общаются друг с другом напрямую, только через `MainWindow`/`JarvisUI`, так что
   риск сломать что-то смежное ниже, чем в `main.py`). `MainWindow` — последний и самый
   большой шаг, оставить напоследок.

Каждый шаг **обратим по отдельности** (`git revert` одного коммита не ломает предыдущие) —
это соответствует вашему требованию "не сносить архитектуру одним махом".

---

## 3. Централизованный реестр инструментов с уровнями риска

### Зачем — обоснование из AUDIT.md, не абстрактный принцип
Сейчас в Python-части риск определяется тем, попал ли код внутрь `if`/`elif` в
`_execute_tool` — то есть по факту, что модель произнесла нужное имя тула с нужными
аргументами. Ни одно из следующих действий не требует подтверждения сегодня:
`file_controller`'s `move`/`write`/`rename`, `desktop_control`'s `organize`/`task` (exec()),
`browser_control`'s запуск на реальном профиле, `send_message`, `game_updater`'s
`shutdown_when_done`, `computer_settings`'s `toggle_wifi`/`dark_mode`/`lock_screen` — при
этом `computer_settings`'s `restart`/`shutdown` **уже** реализуют паттерн подтверждения
(`confirmed=yes`), просто вручную и только в одном месте. Это ровно та несогласованность,
которую единый реестр должен убрать.

### Предлагаемая структура (перенос принципа из `agent-ts/PermissionManager.ts`)

```python
# core/tool_registry.py
from dataclasses import dataclass
from enum import Enum

class RiskLevel(Enum):
    SAFE = "safe"            # read-only / тривиально обратимо
    NORMAL = "normal"        # обычное, ожидаемое, обратимое действие
    SENSITIVE = "sensitive"  # трудно обратимо или затрагивает больше, чем цель
    DANGEROUS = "dangerous"  # явно гейтится — не выполняется без confirmed=True

@dataclass
class ToolSpec:
    name: str
    handler: Callable          # то, что сейчас — код внутри elif-ветки
    schema: dict                # то, что уже есть в TOOL_DECLARATIONS
    risk: RiskLevel
    confirmation_prompt: str | None = None  # что сказать голосом при запросе подтверждения
```

**Важное отличие от `agent-ts`-версии, а не 1-в-1 копия:** сегодняшний `PermissionManager.ts`
гейтит подтверждением только `DANGEROUS` — `SENSITIVE` выполняется автономно
("SAFE/NORMAL/SENSITIVE run autonomously"). Это осмысленно для его текущего набора из 17
tool'ов, где единственный `SENSITIVE` — `kill_process` с собственной защитой
protected-process-имён. Но в Python-части находки Стадии 1 (`file_controller` move/write,
`browser_control` на боевом профиле, `send_message`) — это категория рисков заметно выше,
чем `kill_process`. **Рекомендация: в Python-версии гейтить подтверждением и `SENSITIVE`, и
`DANGEROUS`**, оставляя автономными только `SAFE`/`NORMAL` — иначе перенос принципа не
устранит ни одну из находок AUDIT.md (все они NORMAL или SENSITIVE по своей природе, не
DANGEROUS в узком голливудском смысле "shutdown/mass delete").

### Черновая классификация 25 tool'ов (стартовая точка для Стадии 3, не финал)

| Risk | Инструменты |
|---|---|
| SAFE | `system_status`, `web_search`, `weather_report`(после починки), `youtube_video get_info/trending`, `flight_finder`, `window_manager`(read-only режимы), `ui_automation`(read-only режимы), `computer_control`(screenshot), `send_screenshot`/`screen_process`(angle=screen), `screen_watch`, `close_camera`, `code_helper`, `file_processor`(read-only actions) |
| NORMAL | `open_app`, `reminder`, `save_memory`, `computer_settings`(volume/brightness) |
| SENSITIVE — требует подтверждения | `file_controller`(move/write/rename/delete), `browser_control`(запуск на реальном профиле), `desktop_control`(organize/clean), `computer_settings`(toggle_wifi/dark_mode/lock_screen/sleep_display), `screen_process`(angle=camera), `window_manager`/`ui_automation`(click/type/close/kill-режимы), `send_message`, `dev_agent`(после починки — install/run) |
| DANGEROUS — требует подтверждения, самый явный голосовой prompt | `computer_settings`(restart/shutdown — уже так реализовано, просто формализовать), `shutdown_jarvis`, `game_updater`(shutdown_when_done=true), `desktop_control`(action='task' — генерация+exec() кода — по-хорошему стоит вообще пересмотреть архитектуру, не просто гейтить, см. AUDIT.md находку #2), `file_processor`(action='run' — та же history, что и desktop_control task) |

Два пункта (`desktop_control.task`, `file_processor.run`) отмечены DANGEROUS, но подтверждение
голосом — недостаточная защита сама по себе, т.к. проблема в том, ЧТО выполняется
(неограниченный `exec()`/`subprocess.run` на LLM-сгенерированном коде), а не в том, что
выполнение случилось без спроса. Это отдельное архитектурное решение для Стадии 3
(следует ли вообще оставлять эти два action, либо заменить на настоящую песочницу), а не
то, что решается одним лишь реестром рисков.

### Как это меняет `_execute_tool`
Вместо `if name == "X": ... elif name == "Y": ...` — таблица `TOOL_REGISTRY: dict[str, ToolSpec]`,
и `_execute_tool` становится:
1. найти `ToolSpec` по имени;
2. если `risk in (SENSITIVE, DANGEROUS)` и не подтверждено в этом же разговоре — вернуть
   Gemini текст-запрос на подтверждение (голосом, через `confirmation_prompt`) вместо
   выполнения, и запомнить "жду подтверждения на X" в состоянии сессии;
3. если следующая реплика пользователя — подтверждение, выполнить `handler` по-настоящему;
4. иначе выполнить сразу (`SAFE`/`NORMAL`).

Это не "написать заново диспетчер" — почти вся текущая логика внутри каждой ветки `elif`
переезжает в `handler`-функции без изменений; меняется только сама структура диспетчеризации
и добавляется шаг проверки риска. Хорошо ложится на разбивку `core/tool_dispatch.py` из §2.

---

## Что дальше
Жду вашего решения по трём пунктам (native-core: A/B/C; разбивка main.py/ui.py: ок/правки;
реестр риска: гейтить только DANGEROUS или SENSITIVE+DANGEROUS тоже) — после этого Стадия 3
реализует одобренное, включая фикс найденных в AUDIT.md сломанных вещей.
