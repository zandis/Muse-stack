fn main() {
    // Listing the commands here turns each into a permission (`allow-home-request`, …)
    // that a window has only if capabilities/default.json grants it. Without this list,
    // every registered command would be callable from every window.
    tauri_build::try_build(
        tauri_build::Attributes::new().app_manifest(tauri_build::AppManifest::new().commands(&["home_request", "open_external", "show_main_window"])),
    )
    .expect("tauri build script failed");
}
