"""Voice confirmation for dangerous actions in a live conversation.

Jarvis has full access to the PC: under LLM_PROVIDER=claude_code the brain
runs PowerShell, writes files anywhere, installs software. Everything
harmless just runs. A dangerous step (deleting files, killing processes,
registry/services/firewall changes, installs, shutdown, sending data out)
must be confirmed aloud first, and a handful of steps that can break Windows
itself are never done at all.

How it fits together:
  - claude is started with `--permission-prompt-tool mcp__jarvis__permission_gate`
    and without blanket permission for PowerShell/Bash/Write/Edit, so every
    such call that Claude Code doesn't already consider read-only is sent to
    `gate_handler()` (served from this worker's own JarvisMCPServer).
  - `classify()` decides: "allow" runs now; "confirm" is refused with an
    instruction to ask the user and is remembered as pending; "block" is
    refused for good.
  - `JarvisAgent.on_user_turn_completed` passes every user utterance to
    `note_user_reply()`. A "да" while something is pending approves exactly
    those pending calls for a short while; the model repeats the call and the
    gate lets it through once.

The approval lives only in this process's memory and is set only from real
user speech, so neither the model nor text it reads on a web page can
approve anything. The rules are code on purpose, like policy.py: a prompt can
be argued with, a regex can't.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass

logger = logging.getLogger("jarvis-voice-agent.pc_guard")

ALLOW = "allow"
CONFIRM = "confirm"
BLOCK = "block"

GATE_TOOL_NAME = "permission_gate"
PENDING_TTL_S = 300.0   # how long a question waits for "да"
APPROVED_TTL_S = 120.0  # how long after "да" the model has to repeat the call

_SHELL_TOOLS = {"PowerShell", "Bash"}
_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

# Programming is coding_agent's job -- it runs on the hologram, where the
# owner watches it. The brain writing source files itself would bypass that,
# so writes to code files are turned back with a pointer to coding_agent.
# PC scripts (.ps1/.bat/.cmd) stay the brain's own, and so do its skills'
# scratch areas (its workspace, temp), where e.g. the docx skill writes helpers.
CODE_REASON = "код пишет только coding_agent"
_CODE_EXT_RE = re.compile(
    r"\.(py|pyw|js|mjs|cjs|ts|tsx|jsx|vue|svelte|html?|css|scss|sass|less|java|kt|kts|cs|cpp|cc|cxx|c|h|hpp|"
    r"go|rs|php|rb|swift|dart|lua|r|scala|sql|sh|zsh|ipynb|gd|gdscript)$", re.IGNORECASE)
_CODE_SCRATCH_RE = re.compile(r"claude_workspace|[\\/](temp|tmp)[\\/]|appdata[\\/]local[\\/]temp", re.IGNORECASE)
# Input fields that don't change what a call does (the model rewords them on a retry).
_IGNORED_FIELDS = {"description", "timeout", "run_in_background"}

# --- paths -----------------------------------------------------------------

_SYSTEM_DIRS = (
    r"[a-z]:\\windows\b",
    r"[a-z]:\\program files",
    r"[a-z]:\\programdata\b",
    r"[a-z]:\\boot\b",
    r"[a-z]:\\recovery\b",
    r"[a-z]:\\\$recycle\.bin",
    r"\\system32\b",
    r"\\syswow64\b",
    r"\$env:(windir|systemroot|programfiles|programdata|programw6432|commonprogramfiles)\b",
    r"%(windir|systemroot|programfiles|programdata)%",
    r"\$\{env:(windir|systemroot|programfiles|programdata)\}",
    r"/c/windows\b",
    r"/c/program files",
)
_SYSTEM_DIR_RE = re.compile("|".join(_SYSTEM_DIRS), re.IGNORECASE)
# Places whose files run by themselves or hold keys.
_SENSITIVE_WRITE_RE = re.compile(
    r"\\start menu\\programs\\startup\b|\\\.ssh\\|/\.ssh/|\\microsoft\\windows\\powershell\\.*profile",
    re.IGNORECASE,
)
# A whole drive: "C:\", "D:", "C:\*".
_DRIVE_ROOT_RE = re.compile(r"""(?<![\w\\])[a-z]:\\?\*?(?=["'\s;|)]|$)""", re.IGNORECASE)
_ABS_PATH_RE = re.compile(r"""[a-z]:\\[^\s'"`;|,)]*""", re.IGNORECASE)
# System folders that are meant to be cleaned out.
_SYSTEM_CLEANUP_RE = re.compile(r"\\windows\\temp\b|\\softwaredistribution\\download\b|\\windows\\prefetch\b",
                                re.IGNORECASE)
# Output redirections that only throw text away: "2>$null", "*>&1".
_DISCARD_REDIRECT_RE = re.compile(r"\d?\*?>{1,2}\s*(\$null|&\d)", re.IGNORECASE)
_TEMP_PATH_RE = re.compile(r"\$env:temp\b|\$env:tmp\b|%temp%|\\appdata\\local\\temp\\", re.IGNORECASE)

# --- shell commands ----------------------------------------------------------


def _cmd(*words: str) -> str:
    """A command word at the start of a pipeline segment or after (, &, |, ;."""
    return r"(?:^|[\s;|&(`{])(?:" + "|".join(words) + r")(?:\.exe)?(?=\s|$|[;|)])"


_DELETE_RE = re.compile(
    _cmd("remove-item", "ri", "rm", "rmdir", "rd", "del", "erase", "clear-content", "clc", "shred")
    + r"|\]::delete\(|\.delete\(\s*\)|\bclear-recyclebin\b|\bgit\s+clean\b",
    re.IGNORECASE,
)

# (pattern, reason) -- never done, even if the user says yes.
_BLOCK_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bformat-volume\b|\bclear-disk\b|\binitialize-disk\b|\bremove-partition\b|"
                r"\bdiskpart\b|" + _cmd("format") + r"\s+[a-z]:", re.I), "форматирование или очистка диска"),
    (re.compile(r"\bbcdedit\b|\bbootrec\b|\bbcdboot\b", re.I), "изменение загрузчика Windows"),
    (re.compile(r"\bvssadmin\b.*\bdelete\b|\bwmic\b.*\bshadowcopy\b.*\bdelete\b|"
                r"\bwbadmin\b.*\bdelete\b", re.I), "удаление теневых копий и резервных копий"),
    (re.compile(r"\bcipher\b.*\s/w", re.I), "затирание свободного места на диске"),
    (re.compile(r"\bmkfs\b|\bdd\s+if=", re.I), "низкоуровневая запись на диск"),
    (re.compile(r"\brm\s+-[a-z]*r[a-z]*\s+(/|~|/[a-z]|/[a-z]/windows)/?\*?(\s|$)", re.I), "удаление целого диска"),
]

# (pattern, reason) -- needs a spoken "да" first.
_CONFIRM_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bstop-computer\b|\brestart-computer\b|" + _cmd("shutdown", "logoff", "reboot") +
                r"|\bsetsuspendstate\b|\bsuspend-computer\b", re.I), "выключение, перезагрузка или сон ПК"),
    (re.compile(r"\bstop-process\b|" + _cmd("spps", "kill", "taskkill", "pkill", "killall") +
                r"|\.kill\(\s*\)", re.I), "принудительное завершение процессов"),
    (re.compile(r"\b(stop|set|new|remove|suspend|restart)-service\b|" + _cmd("sc") +
                r"\s+(stop|delete|config|create|failure)\b|\bnet\s+stop\b", re.I), "изменение служб Windows"),
    # A bare "winget upgrade" only lists what could be updated.
    (re.compile(r"\bwinget\s+(install|uninstall|remove|import)\b|\bwinget\s+upgrade\b[^;|]*\s--(all|id|name)\b|"
                r"\bwinget\s+upgrade(\s+--?[\w-]+)*\s+(?!-)[\w.+]+|\bchoco\s+(install|uninstall|upgrade)\b|"
                r"\bscoop\s+(install|uninstall)\b|\bmsiexec\b|\b(install|uninstall)-package\b|"
                r"\bremove-appxpackage\b|\badd-appxpackage\b|\b(enable|disable)-windowsoptionalfeature\b|"
                r"\bdism\b", re.I), "установка или удаление программ"),
    (re.compile(r"\binvoke-expression\b|" + _cmd("iex") + r"|\bdownloadstring\b|"
                r"-enc(odedcommand)?\s|\bunblock-file\b|\bset-executionpolicy\b|"
                r"\|\s*(ba)?sh\b|\bstart-process\b[^;|]*-verb\s+runas|\brunas\b|\bstart-bitstransfer\b",
                re.I), "запуск скачанного кода или с правами администратора"),
    (re.compile(r"\b(set|add|remove)-mppreference\b|\bnetsh\s+(advfirewall|firewall)\b|"
                r"\b(new|set|remove|disable|enable)-netfirewall(rule|profile)\b|\bset-mpcomputerstatus\b",
                re.I), "изменение защиты Windows или брандмауэра"),
    (re.compile(r"\bnetsh\s+(interface|wlan\s+delete|winsock\s+reset|int\s+ip\s+reset)\b|"
                r"\b(disable|enable|remove|rename|restart)-netadapter\b|\bset-dnsclientserveraddress\b|"
                r"\b(new|set|remove)-netipaddress\b|\b(new|remove)-netroute\b", re.I), "изменение сетевых настроек"),
    (re.compile(r"\bnet\s+(user|localgroup)\b|\b(new|remove|set|disable|enable|rename)-localuser\b|"
                r"\b(add|remove)-localgroupmember\b|\bicacls\b|\bcacls\b|\btakeown\b|\bset-acl\b",
                re.I), "изменение учётных записей или прав доступа"),
    (re.compile(r"\bschtasks\b[^;|]*/(create|delete|change)\b|\b(register|unregister|set|disable)-scheduledtask\b",
                re.I), "изменение планировщика заданий"),
    (re.compile(r"\b(disable|enable)-pnpdevice\b|\bpnputil\b", re.I), "отключение или включение устройств"),
    (re.compile(r"\bclear-eventlog\b|\bwevtutil\s+(cl|clear-log)\b|\bremove-eventlog\b", re.I), "очистка журналов Windows"),
    (re.compile(r"\bsend-mailmessage\b", re.I), "отправка письма"),
    (re.compile(r"\b(invoke-webrequest|invoke-restmethod|iwr|irm)\b[^;|]*-(method\s+(post|put|patch|delete)|infile|body)\b|"
                r"\bcurl(\.exe)?\b[^;|]*\s(-d|--data\S*|-F|--form|-T|--upload-file|-X\s*(POST|PUT|PATCH|DELETE))\b",
                re.I), "отправка данных в интернет"),
    (re.compile(r"\bgit\s+(push|reset\s+--hard|checkout\s+--\s|restore\b|branch\s+-D|stash\s+(drop|clear)|"
                r"rebase|filter-branch)\b", re.I), "необратимая операция git или публикация"),
    (re.compile(r"\bchmod\s+-R\b|\bchown\b", re.I), "массовое изменение прав"),
    (re.compile(r"\bsetx\b[^;|]*/m\b|\[environment\]::setenvironmentvariable\([^)]*machine",
                re.I), "изменение системных переменных"),
]

# Running something that came from the internet: an executable or script in
# Downloads / Temp, or a Windows script host. Merely saving it (-OutFile) is fine.
_DOWNLOAD_TARGET_RE = re.compile(r"""(-outfile|-o|--output|-destination)\s+("[^"]*"|'[^']*'|\S+)""", re.IGNORECASE)
_DOWNLOADED_EXEC_RE = re.compile(
    # launched: first word of a command, after & / Start-Process / Invoke-Item / cmd /c / powershell -File
    r"""(?:^|[;|&({]|\b(?:start-process|saps|start|invoke-item|ii)\b(?:\s+-filepath)?|\bcmd(?:\.exe)?\s+/c|"""
    r"""\bpowershell(?:\.exe)?(?:\s+-\w+)*)\s*(?:"[^"\n]*?|'[^'\n]*?|[^;|"'\n\s]*?)"""
    r"""(\\downloads\\|\\appdata\\local\\temp\\|\$env:te?mp\\|%temp%\\|/downloads/|/tmp/)[^;|"'\n]*?"""
    r"""\.(exe|msi|msix|appx|bat|cmd|ps1|vbs|vbe|js|jse|wsf|hta|scr|jar|com|pif|lnk|reg)\b""",
    re.IGNORECASE,
)
_SCRIPT_HOST_RE = re.compile(_cmd("mshta", "wscript", "cscript", "regsvr32", "rundll32", "installutil", "msbuild"),
                             re.IGNORECASE)

# Registry. The user's own settings (HKCU: theme, Explorer options) are
# harmless; the machine hive, autostart, policies and deletions are not.
_REG_WRITE_RE = re.compile(
    _cmd("reg") + r"\s+(add|delete|import|restore|load|unload|copy)\b|"
    r"\b(set|new|remove|rename|clear|copy|move)-itemproperty\b|\b(new-item|set-item|remove-item)\b[^;|]*\bhk\w*:",
    re.IGNORECASE,
)
_REG_DELETE_RE = re.compile(_cmd("reg") + r"\s+delete\b|\b(remove|clear)-itemproperty\b", re.IGNORECASE)
_REG_SENSITIVE_RE = re.compile(
    r"\bhk(lm|cr|u|cc)\b|hkey_(local_machine|classes_root|users|current_config)|registry::|"
    r"\\run(once)?\b|\\policies\\|\\winlogon\b|\\environment\b|\\shell folders\b|\\image file execution options\b|"
    r"\\classes\\|\\services\\",
    re.IGNORECASE,
)

_SYSTEM_WRITE_RE = re.compile(
    _cmd("set-content", "sc", "add-content", "ac", "out-file", "copy-item", "cp", "copy", "cpi", "move-item",
         "mv", "move", "mi", "new-item", "ni", "md", "mkdir", "rename-item", "ren", "rni", "expand-archive",
         "xcopy", "robocopy", "tee-object", "tee")
    + r"|>{1,2}",
    re.IGNORECASE,
)


def _only_temp_paths(command: str) -> bool:
    """True if every path the command names is inside %TEMP% (scratch files the
    model made itself), and it names at least one."""
    abs_paths = _ABS_PATH_RE.findall(command)
    temp = os.path.normcase(os.environ.get("TEMP", "") or "")
    if not abs_paths and not _TEMP_PATH_RE.search(command):
        return False
    for p in abs_paths:
        norm = os.path.normcase(p.rstrip("\\"))
        if not (temp and norm.startswith(temp)) and "\\appdata\\local\\temp\\" not in norm + "\\":
            return False
    return not _DRIVE_ROOT_RE.search(_TEMP_PATH_RE.sub("", command))


def classify_command(command: str) -> tuple[str, str]:
    """A PowerShell/Bash command line -> (ALLOW | CONFIRM | BLOCK, reason)."""
    text = command.strip()
    for pattern, reason in _BLOCK_RULES:
        if pattern.search(text):
            return BLOCK, reason
    if _DELETE_RE.search(text):
        touches_system = _SYSTEM_DIR_RE.search(text)
        if touches_system or _DRIVE_ROOT_RE.search(text):
            recursive = re.search(r"-r(ecurse)?\b|\s/s\b|-rf\b", text, re.I)
            if (recursive or not touches_system) and not _SYSTEM_CLEANUP_RE.search(text):
                return BLOCK, "удаление системной папки или целого диска"
            return CONFIRM, "удаление в системной папке"
        if _only_temp_paths(text):
            return ALLOW, ""
        return CONFIRM, "удаление файлов или папок"
    for pattern, reason in _CONFIRM_RULES:
        if pattern.search(text):
            return CONFIRM, reason
    if _DOWNLOADED_EXEC_RE.search(_DOWNLOAD_TARGET_RE.sub("", text)) or _SCRIPT_HOST_RE.search(text):
        return CONFIRM, "запуск скачанного файла или скрипта"
    if _REG_WRITE_RE.search(text) and (_REG_DELETE_RE.search(text) or _REG_SENSITIVE_RE.search(text)):
        return CONFIRM, "изменение системного реестра или автозагрузки"
    writes = _SYSTEM_WRITE_RE.search(_DISCARD_REDIRECT_RE.sub("", text))
    if writes and _SYSTEM_DIR_RE.search(text):
        return CONFIRM, "изменение файлов в системной папке"
    if writes and _SENSITIVE_WRITE_RE.search(text):
        return CONFIRM, "запись в автозагрузку, профиль PowerShell или ключи SSH"
    return ALLOW, ""


def classify(tool_name: str, tool_input: dict | None) -> tuple[str, str]:
    """Any Claude Code tool call -> (ALLOW | CONFIRM | BLOCK, reason)."""
    tool_input = tool_input or {}
    if tool_name in _SHELL_TOOLS:
        return classify_command(str(tool_input.get("command", "")))
    if tool_name in _WRITE_TOOLS:
        path = str(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        if _SYSTEM_DIR_RE.search(path):
            return CONFIRM, "изменение файла в системной папке"
        if _SENSITIVE_WRITE_RE.search(path):
            return CONFIRM, "запись в автозагрузку, профиль PowerShell или ключи SSH"
        if _CODE_EXT_RE.search(path) and not _CODE_SCRATCH_RE.search(path):
            return BLOCK, CODE_REASON
        return ALLOW, ""
    return ALLOW, ""


# --- pending / approved state -------------------------------------------------


@dataclass
class _Pending:
    summary: str
    created: float


_pending: dict[str, _Pending] = {}
_approved: dict[str, float] = {}


def _key(tool_name: str, tool_input: dict) -> str:
    shown = {k: v for k, v in tool_input.items() if k not in _IGNORED_FIELDS}
    if "command" in shown:
        shown["command"] = " ".join(str(shown["command"]).split())
    raw = tool_name + "\n" + json.dumps(shown, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _summary(tool_name: str, tool_input: dict) -> str:
    if tool_name in _SHELL_TOOLS:
        return str(tool_input.get("command", ""))[:300]
    if tool_name in _WRITE_TOOLS:
        return f"{tool_name} {tool_input.get('file_path') or tool_input.get('notebook_path')}"
    return tool_name


def _expire(now: float) -> None:
    for k in [k for k, p in _pending.items() if now - p.created > PENDING_TTL_S]:
        del _pending[k]
    for k in [k for k, exp in _approved.items() if now > exp]:
        del _approved[k]


def has_pending() -> bool:
    _expire(time.time())
    return bool(_pending)


def check(tool_name: str, tool_input: dict) -> tuple[str, str]:
    """classify() plus the approval bookkeeping -> (ALLOW | CONFIRM | BLOCK, reason).
    CONFIRM means "refused for now, remembered as pending"."""
    now = time.time()
    _expire(now)
    verdict, reason = classify(tool_name, tool_input)
    if verdict != CONFIRM:
        return verdict, reason
    key = _key(tool_name, tool_input)
    if _approved.pop(key, None) is not None:
        logger.info("approved by voice, running: %s", _summary(tool_name, tool_input))
        return ALLOW, reason
    _pending[key] = _Pending(summary=_summary(tool_name, tool_input), created=now)
    logger.info("needs voice confirmation (%s): %s", reason, _summary(tool_name, tool_input))
    return CONFIRM, reason


def confirm_message(reason: str) -> str:
    return (
        f"НУЖНО ПОДТВЕРЖДЕНИЕ ПОЛЬЗОВАТЕЛЯ ({reason}). Действие НЕ выполнено. "
        "Не пытайся сделать то же самое другой командой или другим инструментом. "
        "Одной короткой фразой скажи пользователю, что именно сделаешь, и спроси, подтверждает ли он. "
        "Когда он скажет «да», система пропустит ровно эту команду — повтори её без единого изменения."
    )


def block_message(reason: str) -> str:
    if reason == CODE_REASON:
        return (
            "НЕ ПИШИ КОД САМ: программирование идёт только через mcp__jarvis__coding_agent "
            "(action=\"start\", project — папка проекта или короткое имя нового, task — техзадание), "
            "его ход пользователь смотрит на голограмме. Вызови coding_agent с этой задачей."
        )
    return (
        f"ЗАПРЕЩЕНО ({reason}): это может сломать Windows, такое не выполняется даже с подтверждением. "
        "Не пытайся обойти запрет. Скажи пользователю, что это можно сделать только вручную."
    )


# --- the user's answer ----------------------------------------------------------

_YES_WORDS = {
    "да", "ага", "угу", "давай", "подтверждаю", "подтверждено", "согласен", "согласна", "окей", "ок",
    "ok", "okay", "конечно", "разрешаю", "выполняй", "делай", "валяй", "удаляй", "жми", "yes", "yep",
    "можно", "го", "поехали", "точно", "естественно", "сделай", "действуй",
}
_NO_WORDS = {"нет", "не", "отмена", "отмени", "отменяй", "стоп", "отбой", "no", "nope", "нельзя", "погоди", "подожди"}
_WORD_RE = re.compile(r"[a-zа-яё]+", re.IGNORECASE)


def _parse_answer(text: str) -> str | None:
    words = [w.lower().replace("ё", "е") for w in _WORD_RE.findall(text)]
    if not words:
        return None
    if any(w in _NO_WORDS for w in words):
        return "no"
    if len(words) <= 8 and (words[0] in _YES_WORDS or any(w in ("подтверждаю", "разрешаю") for w in words)):
        return "yes"
    return None


def note_user_reply(text: str) -> str | None:
    """Every user utterance goes through here. If a dangerous action is
    waiting and this is a clear yes/no, record it and return a note to add to
    the user's message for the model; otherwise None."""
    now = time.time()
    _expire(now)
    if not _pending:
        return None
    answer = _parse_answer(text)
    if answer is None:
        return None
    summaries = "; ".join(p.summary for p in _pending.values())
    if answer == "yes":
        for key in _pending:
            _approved[key] = now + APPROVED_TTL_S
        _pending.clear()
        logger.info("user approved by voice: %s", summaries)
        return (f"[Система: пользователь голосом ПОДТВЕРДИЛ: {summaries}. "
                "Повтори эту команду сейчас без изменений.]")
    _pending.clear()
    logger.info("user declined by voice: %s", summaries)
    return f"[Система: пользователь ОТКАЗАЛСЯ от действия: {summaries}. Не выполняй его.]"


# --- Claude Code's --permission-prompt-tool ------------------------------------------


async def gate_handler(arguments: dict) -> str:
    """Handler for mcp__jarvis__permission_gate: Claude Code sends
    {"tool_name", "input", "tool_use_id"} and expects a JSON string back."""
    tool_name = str(arguments.get("tool_name", ""))
    tool_input = arguments.get("input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}
    verdict, reason = check(tool_name, tool_input)
    if verdict == ALLOW:
        return json.dumps({"behavior": "allow", "updatedInput": tool_input}, ensure_ascii=False)
    message = block_message(reason) if verdict == BLOCK else confirm_message(reason)
    return json.dumps({"behavior": "deny", "message": message}, ensure_ascii=False)


def gate_tool():
    from jarvis_mcp import MCPTool

    return MCPTool(
        GATE_TOOL_NAME,
        "Служебный: проверка разрешений для Claude Code. Не вызывай его сам.",
        {"type": "object", "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                                          "tool_use_id": {"type": "string"}},
         "additionalProperties": True},
        gate_handler,
    )
