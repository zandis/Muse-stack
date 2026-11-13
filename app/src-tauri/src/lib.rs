//! The Muse desktop shell (Tauri v2).
//!
//! It owns three things and nothing else:
//!   1. the commands the web view may call: `home_request` (the bridge to the home; the
//!      device token never crosses into the web view), `open_external` (the allow-list
//!      link opener) and `show_main_window`;
//!   2. the menu-bar (tray) icon and the small panel window it opens (`/tray`);
//!   3. the two windows declared in tauri.conf.json.
//!
//! The logic that needs no window (HTTP over the home's socket, the token file, the link
//! allow-list) lives in crates/home-link, where it is unit-tested without WebKit.

use serde_json::Value;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, PhysicalPosition, WebviewWindow, WindowEvent};
use tauri_plugin_opener::OpenerExt;

const MAIN: &str = "main";
const TRAY: &str = "tray";

/// One call to the home control API. The web view sends the method, the path and the JSON
/// body; Rust adds the token and returns `{status, body}`. On failure the web view gets one
/// short word (`unreachable`, `no_token`, `invalid_request`, `bad_reply`) and no detail.
#[tauri::command]
async fn home_request(method: String, path: String, body: Option<Value>) -> Result<muse_home_link::Reply, String> {
    tauri::async_runtime::spawn_blocking(move || muse_home_link::request(&method, &path, body.as_ref()))
        .await
        .map_err(|_| "unreachable".to_string())?
        .map_err(|e| e.code().to_string())
}

/// Open a link in the person's browser, if and only if it is on the allow-list.
#[tauri::command]
fn open_external(app: AppHandle, url: String) -> Result<(), String> {
    let allowed = muse_home_link::allowed_external(&url).ok_or_else(|| "not_allowed".to_string())?;
    app.opener().open_url(allowed, None::<&str>).map_err(|_| "open_failed".to_string())
}

#[tauri::command]
fn show_main_window(app: AppHandle) {
    show_main(&app);
}

fn show_main(app: &AppHandle) {
    if let Some(panel) = app.get_webview_window(TRAY) {
        let _ = panel.hide();
    }
    if let Some(window) = app.get_webview_window(MAIN) {
        let _ = window.unminimize();
        let _ = window.show();
        let _ = window.set_focus();
    }
}

/// Where to put the panel so that it sits by the icon and stays on the screen.
/// `anchor` is the middle of the tray icon; all values are physical pixels.
pub fn panel_position(anchor: (f64, f64), panel: (f64, f64), screen_origin: (f64, f64), screen_size: (f64, f64)) -> (f64, f64) {
    const GAP: f64 = 8.0;
    let (ax, ay) = anchor;
    let (pw, ph) = panel;
    let (sx, sy) = screen_origin;
    let (sw, sh) = screen_size;
    let x = (ax - pw / 2.0).clamp(sx + GAP, (sx + sw - pw - GAP).max(sx + GAP));
    // Menu bar at the top (macOS, most Linux desktops): the panel hangs below the icon.
    // Task bar at the bottom (Windows): it sits above.
    let below = ay < sy + sh / 2.0;
    let y = if below { ay + GAP * 2.0 } else { ay - ph - GAP * 2.0 };
    (x, y.clamp(sy + GAP, (sy + sh - ph - GAP).max(sy + GAP)))
}

fn toggle_panel(panel: &WebviewWindow, anchor: Option<(f64, f64)>) {
    if panel.is_visible().unwrap_or(false) {
        let _ = panel.hide();
        return;
    }
    if let (Some(anchor), Ok(size)) = (anchor, panel.outer_size()) {
        let monitor = panel.available_monitors().ok().and_then(|all| {
            all.into_iter().find(|m| {
                let (p, s) = (m.position(), m.size());
                anchor.0 >= p.x as f64 && anchor.0 < p.x as f64 + s.width as f64 && anchor.1 >= p.y as f64 && anchor.1 < p.y as f64 + s.height as f64
            })
        });
        if let Some(m) = monitor {
            let (x, y) = panel_position(
                anchor,
                (size.width as f64, size.height as f64),
                (m.position().x as f64, m.position().y as f64),
                (m.size().width as f64, m.size().height as f64),
            );
            let _ = panel.set_position(PhysicalPosition::new(x, y));
        }
    }
    let _ = panel.show();
    let _ = panel.set_focus();
}

/// The two words of the tray menu, in the system's language (zh-TW or English).
pub fn menu_labels(locale: Option<&str>) -> (&'static str, &'static str, &'static str) {
    if locale.is_some_and(|l| l.to_ascii_lowercase().starts_with("zh")) {
        ("打開 Muse", "小面板", "結束 Muse")
    } else {
        ("Open Muse", "Small panel", "Quit Muse")
    }
}

fn system_locale() -> Option<String> {
    ["LC_ALL", "LC_MESSAGES", "LANG"].iter().find_map(|k| std::env::var(k).ok().filter(|v| !v.is_empty()))
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let (open_label, panel_label, quit_label) = menu_labels(system_locale().as_deref());
    let open = MenuItem::with_id(app, "open", open_label, true, None::<&str>)?;
    let panel = MenuItem::with_id(app, "panel", panel_label, true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", quit_label, true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &panel, &quit])?;

    let mut tray = TrayIconBuilder::with_id("muse")
        .tooltip("Muse")
        .menu(&menu)
        // Left click opens the panel; the menu is for the right click. On Linux the tray
        // sends no click events, so there the menu (which holds "Small panel") is the way in.
        .show_menu_on_left_click(cfg!(target_os = "linux"))
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => show_main(app),
            "panel" => {
                if let Some(panel) = app.get_webview_window(TRAY) {
                    toggle_panel(&panel, None);
                }
            }
            "quit" => app.exit(0),
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click { button: MouseButton::Left, button_state: MouseButtonState::Up, rect, .. } = event {
                if let Some(panel) = tray.app_handle().get_webview_window(TRAY) {
                    let scale = panel.scale_factor().unwrap_or(1.0);
                    let pos = rect.position.to_physical::<f64>(scale);
                    let size = rect.size.to_physical::<f64>(scale);
                    toggle_panel(&panel, Some((pos.x + size.width / 2.0, pos.y + size.height / 2.0)));
                }
            }
        });
    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }
    tray.build(app)?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![home_request, open_external, show_main_window])
        .setup(|app| {
            setup_tray(app.handle())?;
            Ok(())
        })
        .on_window_event(|window, event| {
            // The panel behaves like a menu: it goes away when the person clicks elsewhere,
            // and closing it only hides it.
            if window.label() == TRAY {
                match event {
                    WindowEvent::Focused(false) => {
                        let _ = window.hide();
                    }
                    WindowEvent::CloseRequested { api, .. } => {
                        api.prevent_close();
                        let _ = window.hide();
                    }
                    _ => {}
                }
            }
        })
        .run(tauri::generate_context!())
        .expect("the Muse app could not start");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_panel_hangs_below_a_top_bar_icon_and_stays_on_screen() {
        // Icon near the right end of a 1920x1080 screen's top bar; panel 360x540.
        let (x, y) = panel_position((1900.0, 12.0), (360.0, 540.0), (0.0, 0.0), (1920.0, 1080.0));
        assert_eq!((x, y), (1552.0, 28.0));
        // In the middle it is centred on the icon.
        assert_eq!(panel_position((960.0, 12.0), (360.0, 540.0), (0.0, 0.0), (1920.0, 1080.0)).0, 780.0);
    }

    #[test]
    fn the_panel_sits_above_a_bottom_bar_icon() {
        let (x, y) = panel_position((1800.0, 1060.0), (360.0, 540.0), (0.0, 0.0), (1920.0, 1080.0));
        assert_eq!((x, y), (1552.0, 504.0));
    }

    #[test]
    fn the_panel_respects_a_second_screen_and_a_small_one() {
        let (x, y) = panel_position((1930.0, 10.0), (360.0, 540.0), (1920.0, 0.0), (1280.0, 720.0));
        assert_eq!((x, y), (1928.0, 26.0));
        // A screen smaller than the panel: pinned to the top-left gap rather than off-screen.
        assert_eq!(panel_position((100.0, 10.0), (360.0, 540.0), (0.0, 0.0), (320.0, 480.0)), (8.0, 8.0));
    }

    #[test]
    fn the_menu_is_in_the_systems_language() {
        assert_eq!(menu_labels(Some("zh_TW.UTF-8")).0, "打開 Muse");
        assert_eq!(menu_labels(Some("en_GB.UTF-8")).0, "Open Muse");
        assert_eq!(menu_labels(None).2, "Quit Muse");
    }
}
