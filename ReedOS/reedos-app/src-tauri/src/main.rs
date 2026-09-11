// Tauri shell. the webview does the real work over WebSocket to reedosd, so the Rust
// core stays tiny (= tiny RAM footprint on the mobo).

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    tauri::Builder::default()
        .run(tauri::generate_context!())
        .expect("error while running ReedOS");
}
