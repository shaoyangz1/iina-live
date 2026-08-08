//! mpv-server —— 本地 FLV 转流代理。
//!
//! 给播放器一个固定 localhost 地址，自动处理平台断流重签 + FLV 时间戳无缝拼接。
//!
//! 查询参数：
//! - `?room=<完整地址>`  指定房间（若无则用启动默认）
//! - `?quality=<显示名>`  指定清晰度（若无则用启动默认）

pub mod flv;

use std::collections::HashMap;
use std::future::{Future, IntoFuture};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use axum::extract::{Path, Query};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use mpv_core::common::pick;
use mpv_core::platforms;

/// 全局代理状态。
pub struct AppState {
    /// 默认房间地址（空串 = serve-only 裸代理，裸连报错）
    pub default_room: String,
    /// 默认清晰度
    pub default_quality: Option<String>,
    /// 空闲自动退出秒数（0 = 常驻）
    pub grace: u64,
    /// 最后活动时刻
    pub last_active: Mutex<Instant>,
    /// 活跃连接数
    pub active: Mutex<u32>,
}

/// 解析请求中的 room / quality（对应 Python 的 parse_request）。
pub fn parse_query(
    params: &HashMap<String, String>,
    state: &AppState,
) -> (Option<String>, Option<String>) {
    parse_query_with_path(params, None, state)
}

fn parse_query_with_path(
    params: &HashMap<String, String>,
    path: Option<&str>,
    state: &AppState,
) -> (Option<String>, Option<String>) {
    let room = params
        .get("room")
        .filter(|room| !room.is_empty())
        .cloned()
        .or_else(|| room_from_path(path?, state));
    let quality = params
        .get("quality")
        .filter(|quality| !quality.is_empty())
        .cloned()
        .or_else(|| state.default_quality.clone());
    (room, quality)
}

fn room_from_path(path: &str, state: &AppState) -> Option<String> {
    let mut slug = path.trim_matches('/');
    if let Some(value) = slug.strip_suffix(".flv") {
        slug = value;
    }
    if slug.is_empty() || slug == "live" {
        return (!state.default_room.is_empty()).then(|| state.default_room.clone());
    }
    if slug.starts_with("http://") || slug.starts_with("https://") {
        return Some(slug.to_string());
    }

    let origin = url::Url::parse(&state.default_room)
        .ok()
        .and_then(|url| {
            let scheme = url.scheme();
            let host = url.host_str()?;
            Some(format!("{scheme}://{host}/"))
        })
        .unwrap_or_else(|| "https://www.huya.com/".to_string());
    Some(format!("{origin}{slug}"))
}

/// 健康探测：`/__ping__` → 200 "play-with-mvp iina-live"
async fn ping() -> &'static str {
    "play-with-mvp iina-live"
}

/// 主 FLV 流端点：GET /live.flv?room=...&quality=...
async fn stream_flv(
    Query(params): Query<HashMap<String, String>>,
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
) -> Response {
    stream_flv_inner(params, None, state).await
}

/// 兼容旧代理的路径网关：GET /<房间号>.flv。
async fn stream_flv_path(
    Path(path): Path<String>,
    Query(params): Query<HashMap<String, String>>,
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
) -> Response {
    stream_flv_inner(params, Some(path), state).await
}

async fn stream_flv_inner(
    params: HashMap<String, String>,
    path: Option<String>,
    state: Arc<AppState>,
) -> Response {
    let (room, quality) = parse_query_with_path(&params, path.as_deref(), &state);

    let Some(room) = room else {
        return (
            StatusCode::BAD_REQUEST,
            "no room: use ?room=<url> or /<id>.flv",
        )
            .into_response();
    };

    {
        let mut active = state.active.lock().unwrap();
        *active += 1;
    }
    {
        let mut last = state.last_active.lock().unwrap();
        *last = Instant::now();
    }

    let client = reqwest::Client::new();
    let result = platforms::parse(&client, &room).await;

    {
        let mut active = state.active.lock().unwrap();
        *active = active.saturating_sub(1);
    }

    match result {
        Ok(info) => {
            if !info.living {
                return (StatusCode::SERVICE_UNAVAILABLE, "未开播").into_response();
            }
            let (_, stream) = match pick(&info.streams, quality.as_deref()) {
                Some(s) => s,
                None => {
                    return (StatusCode::SERVICE_UNAVAILABLE, "未取到可播放的 flv 流")
                        .into_response();
                }
            };

            let headers = platforms::play_headers(&room).unwrap_or_default();
            let urls: Vec<String> = std::iter::once(stream.url.clone())
                .chain(stream.backups.clone())
                .collect();
            let body =
                axum::body::Body::from_stream(flv::relay_stream(room, urls, headers, quality));

            Response::builder()
                .header("Content-Type", "video/x-flv")
                .header("Connection", "close")
                .body(body)
                .unwrap()
        }
        Err(e) => (StatusCode::SERVICE_UNAVAILABLE, format!("{e}")).into_response(),
    }
}

/// 启动代理服务器，并在进程收到终止信号时退出。
pub async fn run(
    default_room: String,
    port: u16,
    default_quality: Option<String>,
    grace: u64,
) -> anyhow::Result<()> {
    run_with_shutdown(
        default_room,
        port,
        default_quality,
        grace,
        std::future::pending(),
    )
    .await
}

/// 启动代理服务器；`shutdown` 完成后停止监听。
pub async fn run_with_shutdown<F>(
    default_room: String,
    port: u16,
    default_quality: Option<String>,
    grace: u64,
    shutdown: F,
) -> anyhow::Result<()>
where
    F: Future<Output = ()> + Send + 'static,
{
    let state = Arc::new(AppState {
        default_room: default_room.clone(),
        default_quality,
        grace,
        last_active: Mutex::new(Instant::now()),
        active: Mutex::new(0),
    });

    let room_display = if default_room.is_empty() {
        "(无 — 纯中转,请在地址带 ?room= 或 /<房间号>.flv)".to_string()
    } else {
        default_room.clone()
    };

    println!("默认房间: {room_display}");
    println!("默认地址: http://127.0.0.1:{port}/live.flv");
    if grace > 0 {
        println!("自动关闭: 无连接空闲 {grace}s 后退出");
    }

    let app = axum::Router::new()
        .route("/__ping__", get(ping))
        .route("/live.flv", get(stream_flv))
        .route("/{*path}", get(stream_flv_path))
        .with_state(state.clone());

    let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
    let server = axum::serve(listener, app).into_future();
    tokio::pin!(server);
    tokio::pin!(shutdown);

    let idle_shutdown = async move {
        if grace == 0 {
            std::future::pending::<()>().await;
        }
        loop {
            tokio::time::sleep(Duration::from_secs(5)).await;
            let idle = {
                let active = state.active.lock().unwrap();
                let last = state.last_active.lock().unwrap();
                *active == 0 && last.elapsed().as_secs() > grace
            };
            if idle {
                println!("空闲超过 {grace}s，自动关闭代理。");
                return;
            }
        }
    };
    tokio::pin!(idle_shutdown);

    tokio::select! {
        result = &mut server => result?,
        _ = &mut shutdown => {},
        _ = &mut idle_shutdown => {},
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn state(room: &str, quality: Option<&str>) -> AppState {
        AppState {
            default_room: room.to_string(),
            default_quality: quality.map(str::to_string),
            grace: 0,
            last_active: Mutex::new(Instant::now()),
            active: Mutex::new(0),
        }
    }

    #[test]
    fn query_values_override_defaults() {
        let state = state("https://example.com/default", Some("1080P"));
        let params = HashMap::from([
            ("room".to_string(), "https://example.com/query".to_string()),
            ("quality".to_string(), "720P".to_string()),
        ]);

        assert_eq!(
            parse_query(&params, &state),
            (
                Some("https://example.com/query".to_string()),
                Some("720P".to_string())
            )
        );
    }

    #[test]
    fn serve_only_requires_room() {
        let state = state("", None);
        assert_eq!(parse_query(&HashMap::new(), &state), (None, None));
    }

    #[test]
    fn path_gateway_uses_default_platform_origin() {
        let state = state("https://www.douyu.com/123", None);
        let (room, quality) = parse_query_with_path(&HashMap::new(), Some("456.flv"), &state);
        assert_eq!(room.as_deref(), Some("https://www.douyu.com/456"));
        assert_eq!(quality, None);
    }
}
