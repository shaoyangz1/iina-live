//! mpv-tauri —— Play with MPV 桌面客户端 Tauri 后端。

use std::sync::Arc;

use tauri::Manager;
use tokio::sync::{oneshot, Mutex};

mod commands;

/// 应用全局状态。
pub struct AppState {
    /// 当前运行的代理端口（0 = 未运行）
    pub proxy_port: Mutex<u16>,
    /// 访问上游的 HTTP client
    pub client: reqwest::Client,
    /// 当前代理的停止信号
    pub proxy_shutdown: Mutex<Option<oneshot::Sender<()>>>,
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_dialog::init())
        .setup(|app| {
            app.manage(Arc::new(AppState {
                proxy_port: Mutex::new(0),
                client: reqwest::Client::new(),
                proxy_shutdown: Mutex::new(None),
            }));
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::parse_room,
            commands::start_proxy,
            commands::stop_proxy,
            commands::open_player,
            commands::get_series_info,
            commands::parse_series_episode,
            commands::watchlist_list,
            commands::watchlist_add,
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
