//! 平台无关的公共工具：HTTP、MD5、清晰度选择、m3u/iina 链接生成。

use anyhow::Context;
use std::collections::HashMap;
use std::env;
use std::path::PathBuf;

/// 返回登录凭据和本地清单使用的运行时数据目录。
pub fn data_dir() -> PathBuf {
    if let Ok(path) = env::var("PLAY_WITH_MPV_DATA_DIR") {
        if !path.trim().is_empty() {
            return PathBuf::from(path);
        }
    }

    // 开发时沿用仓库目录中的现有数据；发布包则使用用户数据目录。
    if let Ok(current) = env::current_dir() {
        if current.join(".cookie").exists() || current.join(".series_watchlist").exists() {
            return current;
        }
    }

    #[cfg(target_os = "macos")]
    if let Ok(home) = env::var("HOME") {
        return PathBuf::from(home)
            .join("Library")
            .join("Application Support")
            .join("play-with-mpv");
    }

    #[cfg(target_os = "windows")]
    if let Ok(app_data) = env::var("APPDATA") {
        return PathBuf::from(app_data).join("play-with-mpv");
    }

    if let Ok(data_home) = env::var("XDG_DATA_HOME") {
        if !data_home.trim().is_empty() {
            return PathBuf::from(data_home).join("play-with-mpv");
        }
    }
    env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("."))
        .join(".local")
        .join("share")
        .join("play-with-mpv")
}

/// 断流修复用的 reconnect 参数。很多平台 FLV 会周期性正常关闭连接（EOF），
/// 只设 reconnect_streamed 不够，mpv 会当播放结束退出。
pub const RECONNECT: &str =
    "reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5";

/// 通用桌面 UA（无特别要求时用它）。
pub const DEFAULT_UA: &str =
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15";

/// HTTP GET 返回 bytes（reqwest 自动解压 gzip）。
pub async fn http_get(
    client: &reqwest::Client,
    url: &str,
    headers: Option<&HashMap<String, String>>,
) -> anyhow::Result<Vec<u8>> {
    let mut req = client.get(url);
    if let Some(h) = headers {
        for (k, v) in h {
            req = req.header(k.as_str(), v.as_str());
        }
    }
    let resp = req.send().await.context("HTTP GET 失败")?;
    let status = resp.status();
    let body = resp.bytes().await.context("读取响应体失败")?;
    if !status.is_success() {
        anyhow::bail!("HTTP {status}: {url}");
    }
    Ok(body.to_vec())
}

/// HTTP GET 返回解码后的文本。
pub async fn http_get_text(
    client: &reqwest::Client,
    url: &str,
    headers: Option<&HashMap<String, String>>,
) -> anyhow::Result<String> {
    let bytes = http_get(client, url, headers).await?;
    String::from_utf8(bytes).context("响应不是 UTF-8")
}

/// MD5 哈希（hex 字符串）。
pub fn md5(s: &str) -> String {
    use md5::Digest;
    format!("{:x}", md5::Md5::digest(s.as_bytes()))
}

/// B 站 qn 码率值 → 显示名。
pub fn bilibili_qn_name(qn: u32) -> String {
    match qn {
        30000 => "杜比",
        20000 => "4K",
        10000 => "原画",
        400 => "蓝光",
        250 => "超清",
        150 => "高清",
        80 => "流畅",
        _ => return qn.to_string(),
    }
    .to_string()
}

/// 返回详情：
/// - nick: 主播名
/// - title: 房间标题
/// - living: 是否开播
/// - streams: 清晰度名 → StreamInfo
pub struct RoomInfo {
    pub rid: String,
    pub nick: Option<String>,
    pub title: Option<String>,
    pub living: bool,
    pub streams: HashMap<String, StreamInfo>,
}

/// 单档清晰度的流信息。
#[derive(Debug, Clone)]
pub struct StreamInfo {
    /// 码率数值（越大越清晰；0 视为原画）。
    pub quality: u32,
    /// 主线流地址。
    pub url: String,
    /// 备用线路，按优先级排列。
    pub backups: Vec<String>,
    /// DASH 点播时独立音轨地址（播放时作为 --audio-file 合并）。
    pub audio: Option<String>,
}

/// 清晰度选择：`quality` 为 None 取最高（原画优先，quality==0 视为原画）；
/// 否则按显示名或码率字符串匹配。返回 (名称, stream)。
pub fn pick<'a>(
    streams: &'a HashMap<String, StreamInfo>,
    quality: Option<&str>,
) -> Option<(&'a str, &'a StreamInfo)> {
    if streams.is_empty() {
        return None;
    }
    if let Some(q) = quality {
        for (name, s) in streams {
            if q == name || q == s.quality.to_string() {
                return Some((name.as_str(), s));
            }
        }
    }
    // 默认取 quality 最高的：原画（quality==0）优先
    streams
        .iter()
        .max_by_key(|(_, s)| (s.quality == 0, s.quality))
        .map(|(n, s)| (n.as_str(), s))
}

/// 生成 `iina://open?url=...` scheme URL，用于 macOS 打开 IINA。
/// `reconnect` 控制是否加断流修复选项（直播用，点播不需要）。
pub fn iina_url(
    title: &str,
    flv: &str,
    headers: Option<&HashMap<String, String>>,
    audio: Option<&str>,
    reconnect: bool,
) -> String {
    let mut opts: Vec<(String, String)> = vec![
        ("force-media-title".into(), title.to_string()),
        ("ytdl".into(), "no".into()),
    ];
    if reconnect {
        opts.push(("stream-lavf-o".into(), RECONNECT.into()));
    }
    if let Some(h) = headers {
        if let Some(referer) = h.get("Referer") {
            opts.push(("mpv_referrer".into(), referer.clone()));
        }
        if let Some(ua) = h.get("User-Agent") {
            opts.push(("mpv_user-agent".into(), ua.clone()));
        }
    }
    if let Some(a) = audio {
        opts.push(("mpv_audio-file".into(), a.to_string()));
    }
    let url_encoded = url_escape(flv);
    let mut params: Vec<String> = vec![format!("url={url_encoded}")];
    for (k, v) in &opts {
        params.push(format!("mpv_{k}={}", url_escape(v)));
    }
    format!("iina://open?{}", params.join("&"))
}

/// 生成打开本地 m3u 的 iina:// scheme（通过 #EXTINF 显示标题）。
pub fn iina_local_url(
    title: &str,
    local_url: &str,
    headers: Option<&HashMap<String, String>>,
    audio: Option<&str>,
    reconnect: bool,
) -> String {
    iina_url(title, local_url, headers, audio, reconnect)
}

/// 多线路 m3u 播放列表（卡住可切备用线路）。
pub fn m3u_content(title: &str, stream: &StreamInfo) -> String {
    let mut out = vec!["#EXTM3U".to_string()];
    let mut urls = vec![stream.url.clone()];
    urls.extend(stream.backups.clone());
    for (i, u) in urls.iter().enumerate() {
        if i == 0 {
            out.push(format!("#EXTINF:-1 ,{title}"));
        } else {
            out.push(format!("#EXTINF:-1 ,{title} - 备用{i}"));
        }
        out.push(u.clone());
    }
    out.join("\n") + "\n"
}

/// 单条 m3u（通过 #EXTINF 让 IINA 显示标题）。
pub fn single_m3u(title: &str, url: &str) -> String {
    format!("#EXTM3U\n#EXTINF:-1 ,{title}\n{url}\n")
}

/// URL 编码（IINA scheme 专用，safe 字符留空）。
fn url_escape(s: &str) -> String {
    let mut result = String::with_capacity(s.len());
    for byte in s.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                result.push(byte as char)
            }
            _ => {
                result.push_str(&format!("%{:02X}", byte));
            }
        }
    }
    result
}
