//! Level 2 — UI Automation. Native Rust port of windows_control/uia.py,
//! built on the `uiautomation` crate (a safe wrapper over Microsoft UI
//! Automation COM interfaces) instead of pywinauto.
//!
//! Same semantics as the Python side: an element search matches on
//! control_type AND/OR a case-insensitive substring match against
//! name/automation_id/class_name — not every app exposes a rich
//! accessibility tree, and that's expected; the router falls back further
//! (keyboard/vision) when this comes up empty, exactly like the Python layer.

use std::time::Duration;

use serde::Serialize;
use uiautomation::core::{UIAutomation, UIElement, UITreeWalker};
use uiautomation::patterns::{UIInvokePattern, UIValuePattern};
use uiautomation::types::{ControlType, Handle};
use uiautomation::errors::ERR_NOTFOUND;

use crate::protocol::{AppError, AppResult};

pub fn automation() -> AppResult<UIAutomation> {
    UIAutomation::new().map_err(|e| AppError::os_error(format!("UIAutomation::new failed: {e}")))
}

pub fn element_from_hwnd(automation: &UIAutomation, hwnd_val: isize) -> AppResult<UIElement> {
    automation
        .element_from_handle(Handle::from(hwnd_val))
        .map_err(|e| AppError::not_found(format!("Window element unavailable: {e}")))
}

pub fn control_type_from_str(s: &str) -> Option<ControlType> {
    let s = s.trim().to_lowercase();
    Some(match s.as_str() {
        "button" => ControlType::Button,
        "edit" => ControlType::Edit,
        "text" => ControlType::Text,
        "checkbox" => ControlType::CheckBox,
        "radiobutton" => ControlType::RadioButton,
        "combobox" => ControlType::ComboBox,
        "list" => ControlType::List,
        "listitem" => ControlType::ListItem,
        "menu" => ControlType::Menu,
        "menuitem" => ControlType::MenuItem,
        "tab" => ControlType::Tab,
        "tabitem" => ControlType::TabItem,
        "tree" => ControlType::Tree,
        "treeitem" => ControlType::TreeItem,
        "window" => ControlType::Window,
        "pane" => ControlType::Pane,
        "hyperlink" => ControlType::Hyperlink,
        "document" => ControlType::Document,
        "image" => ControlType::Image,
        "group" => ControlType::Group,
        "toolbar" => ControlType::ToolBar,
        _ => return None,
    })
}

#[derive(Debug, Clone, Serialize)]
pub struct ElementInfo {
    pub name: String,
    pub control_type: String,
    pub automation_id: String,
    pub class_name: String,
    pub rect: Option<(i32, i32, i32, i32)>,
    pub enabled: bool,
}

pub fn describe(el: &UIElement) -> ElementInfo {
    let rect = el.get_bounding_rectangle().ok().map(|r| (r.get_left(), r.get_top(), r.get_right(), r.get_bottom()));
    ElementInfo {
        name: el.get_name().unwrap_or_default(),
        control_type: el.get_control_type().map(|c| format!("{c:?}")).unwrap_or_default(),
        automation_id: el.get_automation_id().unwrap_or_default(),
        class_name: el.get_classname().unwrap_or_default(),
        rect,
        enabled: el.is_enabled().unwrap_or(true),
    }
}

/// Search descendants of `window` for elements matching a name/id/class
/// substring and/or a control type. Empty query + no control_type returns
/// every descendant up to `max_depth`/`limit` (used by get_ui_tree).
pub fn find_elements(
    automation: &UIAutomation,
    window: &UIElement,
    query: &str,
    control_type: Option<&str>,
    max_depth: u32,
    limit: usize,
) -> AppResult<Vec<UIElement>> {
    let mut matcher = automation.create_matcher().from_ref(window).depth(max_depth.max(1)).timeout(0);

    if let Some(ct_str) = control_type {
        match control_type_from_str(ct_str) {
            Some(ct) => matcher = matcher.control_type(ct),
            None => return Err(AppError::invalid_params(format!("Unknown control_type '{ct_str}'."))),
        }
    }

    let q = query.trim().to_lowercase();
    if !q.is_empty() {
        matcher = matcher.filter_fn(Box::new(move |e: &UIElement| {
            let name = e.get_name().unwrap_or_default().to_lowercase();
            let aid = e.get_automation_id().unwrap_or_default().to_lowercase();
            let cls = e.get_classname().unwrap_or_default().to_lowercase();
            Ok(name.contains(&q) || aid == q || aid.contains(&q) || cls.contains(&q))
        }));
    }

    match matcher.find_all() {
        Ok(v) => Ok(v.into_iter().take(limit).collect()),
        Err(e) if e.code() == ERR_NOTFOUND => Ok(Vec::new()),
        Err(e) => Err(AppError::os_error(format!("UI Automation search failed: {e}"))),
    }
}

pub fn find_one(
    automation: &UIAutomation,
    window: &UIElement,
    query: &str,
    control_type: Option<&str>,
    max_depth: u32,
    index: usize,
) -> AppResult<UIElement> {
    let matches = find_elements(automation, window, query, control_type, max_depth, (index + 1).max(25))?;
    matches.into_iter().nth(index).ok_or_else(|| {
        AppError::not_found(format!("No UI element matched query='{query}' control_type={control_type:?}."))
    })
}

pub fn click_element(el: &UIElement, double: bool, right: bool) -> AppResult<()> {
    let _ = el.set_focus();
    let result = if double {
        el.double_click()
    } else if right {
        el.right_click()
    } else {
        el.click()
    };
    match result {
        Ok(()) => Ok(()),
        Err(click_err) => {
            // Fallback: Invoke pattern works for some controls even when a
            // synthetic mouse click can't reach them (off-screen, occluded).
            match el.get_pattern::<UIInvokePattern>().and_then(|p| p.invoke()) {
                Ok(()) => Ok(()),
                Err(_) => Err(AppError::os_error(format!("Click failed: {click_err}"))),
            }
        }
    }
}

pub fn type_into_element(el: &UIElement, text: &str, clear_first: bool) -> AppResult<()> {
    let _ = el.set_focus();

    // Fast path: ValuePattern (Edit-style controls).
    if let Ok(value_pattern) = el.get_pattern::<UIValuePattern>() {
        let final_text = if clear_first {
            text.to_string()
        } else {
            format!("{}{}", value_pattern.get_value().unwrap_or_default(), text)
        };
        if value_pattern.set_value(&final_text).is_ok() {
            return Ok(());
        }
    }

    // Generic fallback: real synthetic input into whatever now has focus.
    if clear_first {
        let _ = el.send_keys("{Ctrl}a{Delete}", 10);
    }
    el.send_text_by_clipboard(text)
        .or_else(|_| el.send_text(text, 10))
        .map_err(|e| AppError::os_error(format!("Type failed: {e}")))
}

pub fn read_element(el: &UIElement) -> ElementInfo {
    describe(el)
}

pub fn element_text(el: &UIElement) -> String {
    if let Ok(vp) = el.get_pattern::<UIValuePattern>() {
        if let Ok(v) = vp.get_value() {
            if !v.is_empty() {
                return v;
            }
        }
    }
    el.get_name().unwrap_or_default()
}

pub fn clear_element(el: &UIElement) -> AppResult<()> {
    if let Ok(vp) = el.get_pattern::<UIValuePattern>() {
        if vp.set_value("").is_ok() {
            return Ok(());
        }
    }
    let _ = el.set_focus();
    el.send_keys("{Ctrl}a{Delete}", 10)
        .map_err(|e| AppError::os_error(format!("Clear failed: {e}")))
}

#[derive(Debug, Serialize)]
pub struct TreeNode {
    pub name: String,
    pub control_type: String,
    pub automation_id: String,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    pub children: Vec<TreeNode>,
}

/// Bounded, depth/count-limited tree walk — small enough to hand to an LLM,
/// same contract as windows_control/uia.py's get_ui_tree.
pub fn get_ui_tree(automation: &UIAutomation, root: &UIElement, max_depth: u32, max_elements: usize) -> AppResult<TreeNode> {
    let walker = automation
        .create_tree_walker()
        .map_err(|e| AppError::os_error(format!("create_tree_walker failed: {e}")))?;
    let mut count = 0usize;
    walk(&walker, root, 0, max_depth, max_elements, &mut count)
}

fn walk(walker: &UITreeWalker, el: &UIElement, depth: u32, max_depth: u32, max_elements: usize, count: &mut usize) -> AppResult<TreeNode> {
    *count += 1;
    let info = describe(el);
    let mut node = TreeNode {
        name: info.name,
        control_type: info.control_type,
        automation_id: info.automation_id,
        children: Vec::new(),
    };

    if depth < max_depth && *count < max_elements {
        if let Ok(mut child) = walker.get_first_child(el) {
            loop {
                if *count >= max_elements {
                    break;
                }
                if let Ok(child_node) = walk(walker, &child, depth + 1, max_depth, max_elements, count) {
                    node.children.push(child_node);
                }
                match walker.get_next_sibling(&child) {
                    Ok(next) => child = next,
                    Err(_) => break,
                }
            }
        }
    }

    Ok(node)
}

pub fn wait_for_element(
    automation: &UIAutomation,
    window: &UIElement,
    query: &str,
    control_type: Option<&str>,
    max_depth: u32,
    timeout: Duration,
) -> AppResult<UIElement> {
    let deadline = std::time::Instant::now() + timeout;
    loop {
        if let Ok(el) = find_one(automation, window, query, control_type, max_depth, 0) {
            return Ok(el);
        }
        if std::time::Instant::now() >= deadline {
            return Err(AppError::not_found(format!(
                "'{query}' did not appear within {:.0}s.",
                timeout.as_secs_f32()
            )));
        }
        std::thread::sleep(Duration::from_millis(400));
    }
}
