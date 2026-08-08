//! 平台派发层：按 URL 域名找到对应平台模块并解析。
//!
//! 新增平台：在本模块内写一个文件（实现 Platform 接口），再在 `PLATFORMS` 注册即可。

use async_trait::async_trait;
use std::collections::HashMap;

use crate::common::RoomInfo;

pub mod bilibili;
pub mod douyin;
pub mod douyu;
pub mod huya;

/// 平台解析接口。
#[async_trait]
pub trait Platform: Send + Sync {
    /// 匹配的域名关键字列表（如 `["huya.com"]`）。
    fn domains(&self) -> &[&str];

    /// 拉流时使用的 HTTP 头（Referer / User-Agent）。
    fn play_headers(&self) -> HashMap<String, String>;

    /// 解析房间：返回房间信息与各清晰度流。
    async fn parse(&self, client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo>;
}

/// 已注册的直播平台列表。
pub fn platforms() -> Vec<Box<dyn Platform>> {
    vec![
        Box::new(huya::Huya),
        Box::new(bilibili::BilibiliLive),
        Box::new(douyin::Douyin),
        Box::new(douyu::Douyu),
    ]
}

/// 按 URL 域名匹配平台。
pub fn get_platform(url: &str) -> anyhow::Result<Box<dyn Platform>> {
    let host = url::Url::parse(url)
        .map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?
        .host_str()
        .unwrap_or("")
        .to_lowercase();
    for p in platforms() {
        if p.domains().iter().any(|d| host.contains(d)) {
            return Ok(p);
        }
    }
    anyhow::bail!("不支持的平台: {url}")
}

/// 解析 URL 对应的流信息（自动匹配平台）。
pub async fn parse(client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo> {
    get_platform(url)?.parse(client, url).await
}

/// 获取 URL 对应平台的拉流头。
pub fn play_headers(url: &str) -> anyhow::Result<HashMap<String, String>> {
    Ok(get_platform(url)?.play_headers())
}

/// 规范地址（平台可选实现；默认原样返回）。
pub async fn canonical(_client: &reqwest::Client, _url: &str) -> String {
    // 目前仅抖音有 canonical 逻辑，后续平台实现时补充
    _url.to_string()
}

/// 已支持域名列表。
pub fn supported_domains() -> Vec<String> {
    platforms()
        .iter()
        .flat_map(|p| p.domains().iter().map(|d| d.to_string()))
        .collect()
}
