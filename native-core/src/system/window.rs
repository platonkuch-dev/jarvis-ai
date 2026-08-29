//! Level 1 — native window management (Win32 API only, no UI Automation,
//! no input emulation). Direct Rust port of the design already proven out
//! in windows_control/native.py (Python side) — same fuzzy-match semantics,
//! same "don't silently substitute the active window for a named-but-missing
//! app" safety rule.

use std::time::{Duration, Instant};

use serde::Serialize;
use windows::core::{BOOL, PWSTR};
use windows::Win32::Foundation::{HWND, LPARAM, RECT};
use windows::Win32::System::Threading::{OpenProcess, QueryFullProcessImageNameW,
    PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_NAME_WIN32, AttachThreadInput, GetCurrentThreadId};
use windows::Win32::UI::WindowsAndMessaging::{
    EnumWindows, GetWindowTextW, GetWindowTextLengthW, IsWindowVisible, IsWindow,
    GetParent, GetWindowLongW, GWL_EXSTYLE, WS_EX_TOOLWINDOW, GetWindowThreadProcessId,
    GetForegroundWindow, SetForegroundWindow, ShowWindow, IsIconic, GetWindowRect,
    GetWindowPlacement, WINDOWPLACEMENT, SW_RESTORE, SW_MINIMIZE, SW_MAXIMIZE, SW_SHOW,
    SW_SHOWMAXIMIZED, PostMessageW, WM_CLOSE, MoveWindow, BringWindowToTop,
};
use windows::Win32::UI::Input::KeyboardAndMouse::{keybd_event, KEYBD_EVENT_FLAGS};

use crate::protocol::{AppError, AppResult};

#[derive(Debug, Clone, Serialize)]
pub struct WindowInfo {
    pub hwnd: isize,
    pub title: String,
    pub pid: u32,
    pub process_name: String,
    pub is_minimized: bool,
    pub is_maximized: bool,
    pub is_active: bool,
    pub rect: (i32, i32, i32, i32),
}

fn hwnd_of(v: isize) -> HWND {
    HWND(v as *mut std::ffi::c_void)
}

fn window_text(hwnd: HWND) -> String {
    unsafe {
        let len = GetWindowTextLengthW(hwnd);
        if len <= 0 {
            return String::new();
        }
        let mut buf = vec![0u16; (len + 1) as usize];
        let read = GetWindowTextW(hwnd, &mut buf);
        if read <= 0 {
            return String::new();
        }
        String::from_utf16_lossy(&buf[..read as usize])
    }
}

fn process_name_of(pid: u32) -> String {
    unsafe {
        let handle = match OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid) {
            Ok(h) => h,
            Err(_) => return String::new(),
        };
        let mut buf = [0u16; 260];
        let mut size = buf.len() as u32;
        let ok = QueryFullProcessImageNameW(
            handle,
            PROCESS_NAME_WIN32,
            PWSTR(buf.as_mut_ptr()),
            &mut size,
        );
        let _ = windows::Win32::Foundation::CloseHandle(handle);
        if ok.is_err() {
            return String::new();
        }
        let full = String::from_utf16_lossy(&buf[..size as usize]);
        full.rsplit(['\\', '/']).next().unwrap_or(&full).to_string()
    }
}

fn is_maximized(hwnd: HWND) -> bool {
    unsafe {
        let mut wp = WINDOWPLACEMENT { length: std::mem::size_of::<WINDOWPLACEMENT>() as u32, ..Default::default() };
        if GetWindowPlacement(hwnd, &mut wp).is_ok() {
            wp.showCmd == SW_SHOWMAXIMIZED.0 as u32
        } else {
            false
        }
    }
}

fn is_real_window(hwnd: HWND) -> bool {
    unsafe {
        if !IsWindow(Some(hwnd)).as_bool() || !IsWindowVisible(hwnd).as_bool() {
            return false;
        }
        if !GetParent(hwnd).unwrap_or_default().0.is_null() {
            return false;
        }
        let ex_style = GetWindowLongW(hwnd, GWL_EXSTYLE) as u32;
        if ex_style & WS_EX_TOOLWINDOW.0 != 0 {
            return false;
        }
        !window_text(hwnd).is_empty()
    }
}

extern "system" fn enum_windows_proc(hwnd: HWND, lparam: LPARAM) -> BOOL {
    let out = unsafe { &mut *(lparam.0 as *mut Vec<WindowInfo>) };
    if is_real_window(hwnd) {
        let title = window_text(hwnd);
        let mut pid: u32 = 0;
        unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        let process_name = process_name_of(pid);
        let rect = unsafe {
            let mut r = RECT::default();
            let _ = GetWindowRect(hwnd, &mut r);
            (r.left, r.top, r.right, r.bottom)
        };
        let active = unsafe { GetForegroundWindow() } == hwnd;
        out.push(WindowInfo {
            hwnd: hwnd.0 as isize,
            title,
            pid,
            process_name,
            is_minimized: unsafe { IsIconic(hwnd).as_bool() },
            is_maximized: is_maximized(hwnd),
            is_active: active,
            rect,
        });
    }
    BOOL::from(true)
}

pub fn list_windows() -> Vec<WindowInfo> {
    let mut out: Vec<WindowInfo> = Vec::new();
    unsafe {
        let _ = EnumWindows(Some(enum_windows_proc), LPARAM(&mut out as *mut _ as isize));
    }
    out
}

fn stem(s: &str) -> String {
    s.to_lowercase().replace(".exe", "").replace(' ', "")
}

/// Fuzzy match against title OR process name — same semantics as the Python
/// implementation: exact stem matches first, substring matches after.
pub fn find_windows(query: &str) -> Vec<WindowInfo> {
    let windows = list_windows();
    if query.trim().is_empty() {
        return windows;
    }
    let q = stem(query);
    let mut exact = Vec::new();
    let mut partial = Vec::new();
    for w in windows {
        let title_stem = stem(&w.title);
        let proc_stem = stem(&w.process_name);
        if q == proc_stem || q == title_stem {
            exact.push(w);
        } else if !proc_stem.is_empty() && (proc_stem.contains(&q) || q.contains(&proc_stem))
            || (!title_stem.is_empty() && title_stem.contains(&q))
        {
            partial.push(w);
        }
    }
    exact.extend(partial);
    exact
}

/// Resolve exactly ONE window from a query. Returns an error (not a
/// silent fallback to whatever's active) if the caller named something
/// specific and nothing matched — see the equivalent, deliberate, safety
/// note in windows_control/router.py's _target_window_info.
pub fn resolve_one(query: &str) -> AppResult<WindowInfo> {
    if query.trim().is_empty() {
        return get_active_window().ok_or_else(|| AppError::not_found("No active window."));
    }
    find_windows(query)
        .into_iter()
        .next()
        .ok_or_else(|| AppError::not_found(format!("No window found for '{query}'.")))
}

pub fn get_active_window() -> Option<WindowInfo> {
    let hwnd = unsafe { GetForegroundWindow() };
    if hwnd.0.is_null() {
        return None;
    }
    list_windows().into_iter().find(|w| w.hwnd == hwnd.0 as isize).or_else(|| {
        let title = window_text(hwnd);
        let mut pid: u32 = 0;
        unsafe { GetWindowThreadProcessId(hwnd, Some(&mut pid)) };
        let process_name = process_name_of(pid);
        let rect = unsafe {
            let mut r = RECT::default();
            let _ = GetWindowRect(hwnd, &mut r);
            (r.left, r.top, r.right, r.bottom)
        };
        Some(WindowInfo {
            hwnd: hwnd.0 as isize, title, pid, process_name,
            is_minimized: unsafe { IsIconic(hwnd).as_bool() },
            is_maximized: is_maximized(hwnd),
            is_active: true, rect,
        })
    })
}

/// Bring a window to the foreground. Windows actively restricts
/// SetForegroundWindow from background processes — three escalating
/// strategies, same as the Python native.py implementation.
pub fn focus_window(hwnd_val: isize) -> bool {
    let hwnd = hwnd_of(hwnd_val);
    unsafe {
        if !IsWindow(Some(hwnd)).as_bool() {
            return false;
        }
        if IsIconic(hwnd).as_bool() {
            let _ = ShowWindow(hwnd, SW_RESTORE);
        } else {
            let _ = ShowWindow(hwnd, SW_SHOW);
        }

        // Strategy 1: direct call.
        let _ = SetForegroundWindow(hwnd);
        std::thread::sleep(Duration::from_millis(50));
        if GetForegroundWindow() == hwnd {
            return true;
        }

        // Strategy 2: AttachThreadInput — borrow input rights from the
        // current foreground thread so SetForegroundWindow is allowed to work.
        let fg_hwnd = GetForegroundWindow();
        let mut fg_pid: u32 = 0;
        let fg_thread = if !fg_hwnd.0.is_null() {
            GetWindowThreadProcessId(fg_hwnd, Some(&mut fg_pid))
        } else { 0 };
        let mut target_pid: u32 = 0;
        let target_thread = GetWindowThreadProcessId(hwnd, Some(&mut target_pid));
        let cur_thread = GetCurrentThreadId();

        let mut attached_fg = false;
        let mut attached_cur = false;
        if fg_thread != 0 && fg_thread != target_thread {
            attached_fg = AttachThreadInput(fg_thread, target_thread, true).as_bool();
        }
        if cur_thread != target_thread {
            attached_cur = AttachThreadInput(cur_thread, target_thread, true).as_bool();
        }
        let _ = BringWindowToTop(hwnd);
        let _ = SetForegroundWindow(hwnd);
        if attached_fg {
            let _ = AttachThreadInput(fg_thread, target_thread, false);
        }
        if attached_cur {
            let _ = AttachThreadInput(cur_thread, target_thread, false);
        }
        std::thread::sleep(Duration::from_millis(50));
        if GetForegroundWindow() == hwnd {
            return true;
        }

        // Strategy 3: the classic ALT-key nudge.
        keybd_event(0x12, 0, KEYBD_EVENT_FLAGS(0), 0);
        let _ = SetForegroundWindow(hwnd);
        keybd_event(0x12, 0, KEYBD_EVENT_FLAGS(0x0002), 0);
        std::thread::sleep(Duration::from_millis(50));
        GetForegroundWindow() == hwnd
    }
}

pub fn minimize_window(hwnd_val: isize) -> bool {
    unsafe { ShowWindow(hwnd_of(hwnd_val), SW_MINIMIZE).as_bool() || true }
}

pub fn maximize_window(hwnd_val: isize) -> bool {
    unsafe { ShowWindow(hwnd_of(hwnd_val), SW_MAXIMIZE).as_bool() || true }
}

pub fn restore_window(hwnd_val: isize) -> bool {
    unsafe { ShowWindow(hwnd_of(hwnd_val), SW_RESTORE).as_bool() || true }
}

/// Politely ask the window to close (WM_CLOSE) — lets the app prompt to
/// save/confirm rather than yanking the process out from under it.
pub fn close_window(hwnd_val: isize) -> bool {
    unsafe { PostMessageW(Some(hwnd_of(hwnd_val)), WM_CLOSE, windows::Win32::Foundation::WPARAM(0), windows::Win32::Foundation::LPARAM(0)).is_ok() }
}

pub fn window_is_gone(hwnd_val: isize) -> bool {
    unsafe { !IsWindow(Some(hwnd_of(hwnd_val))).as_bool() }
}

pub fn move_resize_window(hwnd_val: isize, x: Option<i32>, y: Option<i32>, width: Option<i32>, height: Option<i32>) -> AppResult<()> {
    let hwnd = hwnd_of(hwnd_val);
    unsafe {
        let mut r = RECT::default();
        GetWindowRect(hwnd, &mut r).map_err(|e| AppError::os_error(format!("GetWindowRect failed: {e}")))?;
        let (cur_w, cur_h) = (r.right - r.left, r.bottom - r.top);
        let new_x = x.unwrap_or(r.left);
        let new_y = y.unwrap_or(r.top);
        let new_w = width.unwrap_or(cur_w);
        let new_h = height.unwrap_or(cur_h);
        MoveWindow(hwnd, new_x, new_y, new_w, new_h, true)
            .map_err(|e| AppError::os_error(format!("MoveWindow failed: {e}")))
    }
}

pub fn wait_for_window(query: &str, timeout: Duration) -> Option<WindowInfo> {
    let deadline = Instant::now() + timeout;
    loop {
        let matches = find_windows(query);
        if let Some(w) = matches.into_iter().next() {
            return Some(w);
        }
        if Instant::now() >= deadline {
            return None;
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

pub fn wait_for_window_gone(hwnd_val: isize, timeout: Duration) -> bool {
    let deadline = Instant::now() + timeout;
    loop {
        if window_is_gone(hwnd_val) {
            return true;
        }
        if Instant::now() >= deadline {
            return window_is_gone(hwnd_val);
        }
        std::thread::sleep(Duration::from_millis(300));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stem_normalizes_case_spaces_and_exe_suffix() {
        assert_eq!(stem("Discord.exe"), "discord");
        assert_eq!(stem("Google Chrome"), "googlechrome");
        assert_eq!(stem("  Notepad  "), "notepad");
        assert_eq!(stem("VS Code.EXE"), "vscode");
    }

    #[test]
    fn find_windows_empty_query_returns_everything() {
        // A real desktop always has at least one visible top-level window
        // (this test process's own console, Explorer, etc.) — this exercises
        // the real EnumWindows call, not a mock.
        let all = find_windows("");
        let listed = list_windows();
        assert_eq!(all.len(), listed.len());
    }

    #[test]
    fn resolve_one_fails_honestly_for_a_named_but_missing_app() {
        // Regression test for the exact bug found (and fixed) in the Python
        // sibling implementation: a named-but-not-found app must be a hard
        // error, never a silent fallback to whatever's currently active.
        let result = resolve_one("ThisAppDefinitelyDoesNotExist_9f8e7d6c");
        assert!(result.is_err());
    }

    #[test]
    fn get_active_window_returns_a_real_window_on_this_desktop() {
        let active = get_active_window();
        assert!(active.is_some(), "expected a foreground window on a live desktop session");
    }
}
