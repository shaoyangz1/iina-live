//! 点播（番剧/影视）平台派发层。
//!
//! 复用 `crate::common` 工具，与直播平台平行。

use std::collections::HashMap;

use crate::common::StreamInfo;

pub mod bilibili;
pub mod watchlist;

/// 返回点播上游所需的 HTTP 头。
pub fn play_headers(url: &str) -> HashMap<String, String> {
    if url
        .parse::<url::Url>()
        .ok()
        .and_then(|parsed| parsed.host_str().map(str::to_lowercase))
        .is_some_and(|host| host.contains("bilibili.com"))
    {
        bilibili::play_headers()
    } else {
        HashMap::new()
    }
}

/// 选集信息（包含全部剧集列表）。
pub struct SeasonInfo {
    /// "ep" 或 "ss"
    pub kind: String,
    pub num: u64,
    pub nick: Option<String>,
    pub episodes: Vec<EpisodeMeta>,
    pub season_id: Option<u64>,
}

/// 单集元数据。
#[derive(Debug, Clone)]
pub struct EpisodeMeta {
    pub id: u64,
    pub cid: u64,
    pub bvid: String,
    pub title: String,
    pub long_title: String,
    pub duration: u64,
}

/// 番剧解析结果（除 RoomInfo 字段外，增加选集信息）。
pub struct SeriesRoomInfo {
    pub rid: String,
    pub nick: Option<String>,
    pub title: Option<String>,
    pub living: bool,
    pub streams: HashMap<String, StreamInfo>,
    pub episode_count: usize,
    pub season_id: Option<u64>,
}

/// 获取番剧基本信息 + 分集列表。
pub async fn get_season_info(client: &reqwest::Client, url: &str) -> anyhow::Result<SeasonInfo> {
    let host = url::Url::parse(url)
        .map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?
        .host_str()
        .unwrap_or("")
        .to_lowercase();
    if host.contains("bilibili.com") {
        bilibili::get_season_info_impl(client, url).await
    } else {
        anyhow::bail!("不支持的点播平台: {url}")
    }
}

/// 解析指定集的播放流。
pub async fn parse(
    client: &reqwest::Client,
    url: &str,
    episode: Option<&str>,
) -> anyhow::Result<SeriesRoomInfo> {
    let host = url::Url::parse(url)
        .map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?
        .host_str()
        .unwrap_or("")
        .to_lowercase();
    if host.contains("bilibili.com") {
        bilibili::parse_impl(client, url, episode).await
    } else {
        anyhow::bail!("不支持的点播平台: {url}")
    }
}
