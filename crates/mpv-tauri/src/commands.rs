//! Tauri Command 实现：解析房间、启动代理、打开播放器、追剧管理。

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Arc;
use tauri::State;
use tokio::sync::oneshot;

use crate::AppState;

/// 房间/番剧元信息。
#[derive(Debug, Serialize)]
pub struct RoomMeta {
    pub rid: String,
    pub nick: Option<String>,
    pub title: Option<String>,
    pub living: bool,
    pub platform: String,
}

/// 追剧条目。
#[derive(Debug, Serialize, Deserialize, Clone)]
pub struct WatchEntry {
    pub url: String,
    pub nick: String,
    pub total: usize,
}

/// 解析房间信息（输入 URL → 返回元数据，不启动播放）。
#[tauri::command]
pub async fn parse_room(state: State<'_, Arc<AppState>>, url: String) -> Result<RoomMeta, String> {
    let info = mpv_core::platforms::parse(&state.client, &url)
        .await
        .map_err(|e| format!("解析失败: {e}"))?;

    Ok(RoomMeta {
        rid: info.rid,
        nick: info.nick,
        title: info.title,
        living: info.living,
        platform: "live".into(),
    })
}

/// 启动本地 FLV 代理，返回端口号。
#[tauri::command]
pub async fn start_proxy(
    state: State<'_, Arc<AppState>>,
    url: String,
    quality: Option<String>,
) -> Result<u16, String> {
    if let Some(shutdown) = state.proxy_shutdown.lock().await.take() {
        let _ = shutdown.send(());
    }
    let port = find_free_port().await;
    let room = url;
    let (shutdown_tx, shutdown_rx) = oneshot::channel();
    let (error_tx, mut error_rx) = oneshot::channel();

    let state_clone = state.inner().clone();
    tokio::spawn(async move {
        if let Err(error) = mpv_server::run_with_shutdown(room, port, quality, 0, async move {
            let _ = shutdown_rx.await;
        })
        .await
        {
            let _ = error_tx.send(error.to_string());
        }
    });

    // 等待代理就绪
    for _ in 0..30 {
        match reqwest::get(format!("http://127.0.0.1:{port}/__ping__")).await {
            Ok(_) => {
                let mut p = state_clone.proxy_port.lock().await;
                *p = port;
                *state_clone.proxy_shutdown.lock().await = Some(shutdown_tx);
                return Ok(port);
            }
            Err(_) => {
                if let Ok(error) = error_rx.try_recv() {
                    return Err(format!("代理启动失败: {error}"));
                }
                tokio::time::sleep(std::time::Duration::from_millis(100)).await;
            }
        }
    }
    let _ = shutdown_tx.send(());
    Err("代理启动超时".into())
}

/// 关闭本地代理。
#[tauri::command]
pub async fn stop_proxy(state: State<'_, Arc<AppState>>) -> Result<(), String> {
    if let Some(shutdown) = state.proxy_shutdown.lock().await.take() {
        let _ = shutdown.send(());
    }
    let mut port = state.proxy_port.lock().await;
    *port = 0;
    Ok(())
}

/// 调用系统播放器打开流地址。
#[cfg(target_os = "macos")]
#[tauri::command]
pub async fn open_player(
    url: String,
    title: String,
    audio: Option<String>,
    headers: Option<HashMap<String, String>>,
    reconnect: bool,
) -> Result<(), String> {
    use std::process::Command;

    let iina_url =
        mpv_core::common::iina_url(&title, &url, headers.as_ref(), audio.as_deref(), reconnect);
    Command::new("open")
        .args(["-a", "IINA", &iina_url])
        .spawn()
        .map_err(|e| format!("无法启动 IINA: {e}"))?;
    Ok(())
}

#[cfg(not(target_os = "macos"))]
#[tauri::command]
pub async fn open_player(
    url: String,
    title: String,
    audio: Option<String>,
    headers: Option<HashMap<String, String>>,
    reconnect: bool,
) -> Result<(), String> {
    use std::process::Command;

    let mut args = vec![
        format!("--force-media-title={title}"),
        "--ytdl=no".to_string(),
    ];
    if reconnect {
        args.push(format!("--stream-lavf-o={}", mpv_core::common::RECONNECT));
    }
    if let Some(headers) = headers {
        if let Some(referer) = headers.get("Referer") {
            args.push(format!("--referrer={referer}"));
        }
        if let Some(user_agent) = headers.get("User-Agent") {
            args.push(format!("--user-agent={user_agent}"));
        }
    }
    if let Some(a) = &audio {
        args.push(format!("--audio-file={a}"));
    }
    args.push(url);

    Command::new("mpv")
        .args(&args)
        .spawn()
        .map_err(|e| format!("无法启动 mpv: {e}"))?;
    Ok(())
}

/// 获取番剧分集列表。
#[tauri::command]
pub async fn get_series_info(
    state: State<'_, Arc<AppState>>,
    url: String,
) -> Result<serde_json::Value, String> {
    let info = mpv_core::series::get_season_info(&state.client, &url)
        .await
        .map_err(|e| format!("解析失败: {e}"))?;

    let episodes: Vec<serde_json::Value> = info
        .episodes
        .iter()
        .map(|e| {
            serde_json::json!({
                "id": e.id,
                "cid": e.cid,
                "bvid": e.bvid,
                "title": e.title,
                "long_title": e.long_title,
                "duration": e.duration,
            })
        })
        .collect();

    Ok(serde_json::json!({
        "kind": info.kind,
        "num": info.num,
        "nick": info.nick,
        "episodes": episodes,
        "season_id": info.season_id,
    }))
}

/// 解析番剧指定集播放流。
#[tauri::command]
pub async fn parse_series_episode(
    state: State<'_, Arc<AppState>>,
    url: String,
    episode: Option<String>,
) -> Result<serde_json::Value, String> {
    let info = mpv_core::series::parse(&state.client, &url, episode.as_deref())
        .await
        .map_err(|e| format!("解析失败: {e}"))?;

    let streams: Vec<serde_json::Value> = info
        .streams
        .iter()
        .map(|(name, s)| {
            serde_json::json!({
                "name": name,
                "quality": s.quality,
                "url": s.url,
                "backups": s.backups,
                "audio": s.audio,
            })
        })
        .collect();

    Ok(serde_json::json!({
        "rid": info.rid,
        "nick": info.nick,
        "title": info.title,
        "living": info.living,
        "streams": streams,
        "headers": mpv_core::series::play_headers(&url),
        "episode_count": info.episode_count,
        "season_id": info.season_id,
    }))
}

/// 追剧清单：列出所有。
#[tauri::command]
pub async fn watchlist_list() -> Result<Vec<WatchEntry>, String> {
    mpv_core::series::watchlist::load()
        .map(|entries| {
            entries
                .into_iter()
                .map(|entry| WatchEntry {
                    url: entry.url,
                    nick: entry.nick,
                    total: entry.total,
                })
                .collect()
        })
        .map_err(|e| format!("读取失败: {e}"))
}

/// 追剧清单：添加。
#[tauri::command]
pub async fn watchlist_add(url: String, nick: String, total: usize) -> Result<(), String> {
    mpv_core::series::watchlist::add(&url, &nick, total).map_err(|e| format!("保存失败: {e}"))
}

/// 找到本地可用端口。
async fn find_free_port() -> u16 {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    listener.local_addr().unwrap().port()
}
