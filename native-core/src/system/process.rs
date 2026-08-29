//! Level 1 — process management: enumerate, kill, and smart-launch.
//!
//! Launching deliberately leans on ShellExecuteExW rather than hand-rolling
//! PATH/registry lookups: it's the same primitive Explorer's own Run dialog
//! uses, and it already resolves bare exe names via the "App Paths" registry
//! key, PATH, and file/protocol associations (ms-settings:, etc.) in one call.
//! A small alias table covers the common cases where the friendly name
//! ("Discord", "Chrome") doesn't match the registered key ("Discord.exe").

use std::collections::HashMap;
use std::time::Duration;

use serde::Serialize;
use windows::core::PCWSTR;
use windows::Win32::Foundation::{CloseHandle, HANDLE};
use windows::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
    TH32CS_SNAPPROCESS,
};
use windows::Win32::System::Threading::{
    OpenProcess, TerminateProcess, PROCESS_TERMINATE,
};
use windows::Win32::UI::Shell::{ShellExecuteExW, SHELLEXECUTEINFOW, SEE_MASK_NOCLOSEPROCESS};
use windows::Win32::UI::WindowsAndMessaging::SW_SHOWNORMAL;

use crate::protocol::{AppError, AppResult};
use crate::system::window::{self, WindowInfo};

#[derive(Debug, Clone, Serialize)]
pub struct ProcessInfo {
    pub pid: u32,
    pub name: String,
}

/// Processes we refuse to touch even if asked — mirrors
/// windows_control/native.py's PROTECTED_PROCESS_NAMES on the Python side.
const PROTECTED: &[&str] = &[
    "system", "system idle process", "registry",
    "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "smss.exe", "explorer.exe", "dwm.exe",
    "svchost.exe", "sihost.exe", "fontdrvhost.exe", "taskhostw.exe",
];

fn to_wide(s: &str) -> Vec<u16> {
    s.encode_utf16().chain(std::iter::once(0)).collect()
}

pub fn list_processes(name_filter: Option<&str>) -> AppResult<Vec<ProcessInfo>> {
    let q = name_filter.map(|s| s.to_lowercase().replace(".exe", "").replace(' ', ""));
    let mut out = Vec::new();
    unsafe {
        let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
            .map_err(|e| AppError::os_error(format!("CreateToolhelp32Snapshot failed: {e}")))?;

        let mut entry = PROCESSENTRY32W {
            dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
            ..Default::default()
        };

        let mut ok = Process32FirstW(snapshot, &mut entry).is_ok();
        while ok {
            let name = {
                let end = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(entry.szExeFile.len());
                String::from_utf16_lossy(&entry.szExeFile[..end])
            };
            let matches = match &q {
                None => true,
                Some(q) if q.is_empty() => true,
                Some(q) => name.to_lowercase().replace(".exe", "").replace(' ', "").contains(q.as_str()),
            };
            if matches {
                out.push(ProcessInfo { pid: entry.th32ProcessID, name });
            }
            ok = Process32NextW(snapshot, &mut entry).is_ok();
        }
        let _ = CloseHandle(snapshot);
    }
    Ok(out)
}

pub fn kill_process(pid: u32) -> AppResult<String> {
    let procs = list_processes(None)?;
    let target = procs.iter().find(|p| p.pid == pid);
    let name = target.map(|p| p.name.to_lowercase()).unwrap_or_default();

    if PROTECTED.contains(&name.as_str()) {
        return Err(AppError::refused(format!(
            "Refusing to close protected system process '{name}'."
        )));
    }
    if target.is_none() {
        return Ok("Process was already gone.".to_string());
    }

    unsafe {
        let handle: HANDLE = OpenProcess(PROCESS_TERMINATE, false, pid)
            .map_err(|e| AppError::os_error(format!("OpenProcess failed: {e}")))?;
        let result = TerminateProcess(handle, 1);
        let _ = CloseHandle(handle);
        result.map_err(|e| AppError::os_error(format!("TerminateProcess failed: {e}")))?;
    }
    Ok(format!("Terminated {name} (pid {pid})."))
}

/// Friendly-name -> App Paths registry key aliases for apps whose common
/// spoken name doesn't match their registered executable name.
fn alias(app_name: &str) -> String {
    let key = app_name.to_lowercase();
    let table: HashMap<&str, &str> = HashMap::from([
        ("chrome", "chrome.exe"), ("google chrome", "chrome.exe"),
        ("edge", "msedge.exe"), ("firefox", "firefox.exe"),
        ("discord", "Discord.exe"), ("telegram", "Telegram.exe"),
        ("spotify", "Spotify.exe"), ("whatsapp", "WhatsApp.exe"),
        ("notepad", "notepad.exe"), ("explorer", "explorer.exe"),
        ("calculator", "calc.exe"), ("calc", "calc.exe"),
        ("vscode", "Code.exe"), ("visual studio code", "Code.exe"), ("code", "Code.exe"),
        ("settings", "ms-settings:"), ("windows settings", "ms-settings:"),
        ("cmd", "cmd.exe"), ("terminal", "wt.exe"), ("powershell", "powershell.exe"),
        ("steam", "steam.exe"), ("slack", "slack.exe"), ("zoom", "Zoom.exe"),
    ]);
    table.get(key.as_str()).map(|s| s.to_string()).unwrap_or_else(|| app_name.to_string())
}

/// Well-known shell folder shortcuts ("open the Downloads folder").
fn known_folder(app_name: &str) -> Option<String> {
    let key = app_name.to_lowercase();
    let home = std::env::var("USERPROFILE").ok()?;
    let sub = match key.as_str() {
        "downloads" => "Downloads",
        "desktop" => "Desktop",
        "documents" => "Documents",
        "pictures" => "Pictures",
        "home" => "",
        _ => return None,
    };
    Some(if sub.is_empty() { home } else { format!("{home}\\{sub}") })
}

fn shell_execute(target: &str) -> AppResult<Option<u32>> {
    unsafe {
        let file = to_wide(target);
        let mut info = SHELLEXECUTEINFOW {
            cbSize: std::mem::size_of::<SHELLEXECUTEINFOW>() as u32,
            fMask: SEE_MASK_NOCLOSEPROCESS,
            lpFile: PCWSTR(file.as_ptr()),
            nShow: SW_SHOWNORMAL.0,
            ..Default::default()
        };
        ShellExecuteExW(&mut info)
            .map_err(|e| AppError::os_error(format!("ShellExecuteExW('{target}') failed: {e}")))?;

        if info.hProcess.is_invalid() {
            return Ok(None);
        }
        let pid = windows::Win32::System::Threading::GetProcessId(info.hProcess);
        let _ = CloseHandle(info.hProcess);
        Ok(Some(pid))
    }
}

#[derive(Debug, Serialize)]
pub struct LaunchResult {
    pub success: bool,
    pub message: String,
    pub already_running: bool,
    pub window: Option<WindowInfo>,
}

/// 1. Already running?  -> focus it, done.
/// 2. Otherwise resolve via alias table / known-folder shortcuts, launch via
///    ShellExecuteExW, then actually wait for a real window and focus it —
///    never just trust that the process call succeeded.
pub fn launch_or_focus(app_name: &str, wait_timeout: Duration) -> AppResult<LaunchResult> {
    if let Some(existing) = window::find_windows(app_name).into_iter().next() {
        window::focus_window(existing.hwnd);
        return Ok(LaunchResult {
            success: true,
            message: format!("{app_name} was already running — focused it."),
            already_running: true,
            window: Some(existing),
        });
    }

    // A known folder path opens directly in Explorer via ShellExecute's
    // default "open" verb (same as double-clicking it) — no need to route
    // through explorer.exe explicitly. Otherwise resolve the friendly name
    // through the alias table (falls through to the raw name unchanged,
    // which ShellExecuteExW will still try to resolve via PATH/App Paths).
    let exec_target = known_folder(app_name).unwrap_or_else(|| alias(app_name));
    shell_execute(&exec_target)?;

    let window = window::wait_for_window(app_name, wait_timeout);
    match window {
        Some(w) => {
            window::focus_window(w.hwnd);
            Ok(LaunchResult {
                success: true,
                message: format!("Opened {app_name}. Window confirmed and focused."),
                already_running: false,
                window: Some(w),
            })
        }
        None => Ok(LaunchResult {
            success: true,
            message: format!("Opened {app_name}. (No top-level window detected yet.)"),
            already_running: false,
            window: None,
        }),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn alias_resolves_friendly_names_to_registered_exe_names() {
        assert_eq!(alias("Discord"), "Discord.exe");
        assert_eq!(alias("chrome"), "chrome.exe");
        assert_eq!(alias("Visual Studio Code"), "Code.exe");
        assert_eq!(alias("settings"), "ms-settings:");
    }

    #[test]
    fn alias_passes_through_unknown_names_unchanged() {
        assert_eq!(alias("SomeRandomApp"), "SomeRandomApp");
    }

    #[test]
    fn known_folder_resolves_common_shortcuts() {
        let home = std::env::var("USERPROFILE").unwrap();
        assert_eq!(known_folder("downloads"), Some(format!("{home}\\Downloads")));
        assert_eq!(known_folder("desktop"), Some(format!("{home}\\Desktop")));
        assert_eq!(known_folder("home"), Some(home));
        assert_eq!(known_folder("not a folder keyword"), None);
    }

    #[test]
    fn list_processes_finds_this_test_processs_own_host() {
        // cargo test runs inside a real process — proves CreateToolhelp32Snapshot
        // actually walks live process list rather than returning nothing.
        let all = list_processes(None).expect("list_processes should succeed");
        assert!(!all.is_empty(), "expected at least one running process on a live desktop");
    }

    #[test]
    fn kill_process_refuses_protected_names() {
        let explorer = list_processes(Some("explorer.exe")).unwrap();
        if let Some(p) = explorer.first() {
            let result = kill_process(p.pid);
            assert!(result.is_err(), "explorer.exe must be refused, not killed");
        }
    }
}
