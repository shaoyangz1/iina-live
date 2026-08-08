//! 哔哩哔哩直播（live.bilibili.com）平台解析模块。
//!
//! 对应 Python `live/sites/bilibili.py`，取流走 getRoomPlayInfo 明文链路（无需 wbi 签名）。

use async_trait::async_trait;
use std::collections::HashMap;

use crate::common::{self, RoomInfo, StreamInfo};
use crate::platforms::Platform;
use anyhow::Context as _;

const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0";
const REFERER: &str = "https://live.bilibili.com/";

pub struct BilibiliLive;

fn api_headers() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Origin".into(), "https://live.bilibili.com".into()),
        ("Referer".into(), REFERER.into()),
    ])
}

fn play_headers_map() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

/// qn 码率值 → 显示名。
fn qn_name(qn: u32) -> String {
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

/// 从 getRoomPlayInfo 响应提取各档流。
/// 完整地址 = host + base_url + extra；同一 codec 的 url_info 多条 = 多线路。
fn streams_from_playinfo(data: &serde_json::Value) -> HashMap<String, StreamInfo> {
    let mut streams: HashMap<String, StreamInfo> = HashMap::new();
    let playurl = data.get("playurl_info").and_then(|v| v.get("playurl"));

    let stream_list = match playurl
        .and_then(|v| v.get("stream"))
        .and_then(|v| v.as_array())
    {
        Some(a) => {
            let mut v: Vec<&serde_json::Value> = a.iter().collect();
            v.sort_by_key(|s| {
                s.get("protocol_name")
                    .and_then(|v| v.as_str())
                    .map(|n| n != "http_stream")
                    .unwrap_or(false)
            });
            v
        }
        None => return streams,
    };

    for stream in &stream_list {
        let Some(formats) = stream.get("format").and_then(|v| v.as_array()) else {
            continue;
        };
        for fmt in formats {
            let Some(codecs) = fmt.get("codec").and_then(|v| v.as_array()) else {
                continue;
            };
            for codec in codecs {
                let base = codec["base_url"].as_str().unwrap_or("");
                let Some(url_info) = codec.get("url_info").and_then(|v| v.as_array()) else {
                    continue;
                };
                let urls: Vec<String> = url_info
                    .iter()
                    .map(|ui| {
                        format!(
                            "{}{}{}",
                            ui["host"].as_str().unwrap_or(""),
                            base,
                            ui.get("extra").and_then(|v| v.as_str()).unwrap_or("")
                        )
                    })
                    .collect();
                if urls.is_empty() {
                    continue;
                }
                let qn = codec["current_qn"].as_u64().unwrap_or(0) as u32;
                let name = qn_name(qn);
                streams.entry(name).or_insert_with(|| StreamInfo {
                    quality: qn,
                    url: urls[0].clone(),
                    backups: urls[1..].to_vec(),
                    audio: None,
                });
            }
        }
    }
    streams
}

/// 短号 → (真房号, 开播?)
async fn resolve_room(client: &reqwest::Client, short: &str) -> anyhow::Result<(u64, bool)> {
    let url = format!("https://api.live.bilibili.com/room/v1/Room/room_init?id={short}");
    let raw = common::http_get(client, &url, Some(&api_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("room_init 非 JSON")?;
    let data = &v["data"];
    let rid = data["room_id"].as_u64().context("room_init 缺少 room_id")?;
    Ok((rid, data["live_status"].as_u64() == Some(1)))
}

/// 取标题/主播名。
async fn room_meta(
    client: &reqwest::Client,
    rid: u64,
) -> anyhow::Result<(Option<String>, Option<String>)> {
    let gi_url = format!("https://api.live.bilibili.com/room/v1/Room/get_info?room_id={rid}");
    let raw = common::http_get(client, &gi_url, Some(&api_headers())).await?;
    let gi: serde_json::Value = serde_json::from_slice(&raw).context("get_info 非 JSON")?;
    let uid = gi["data"]["uid"].as_u64().unwrap_or(0);
    let title = gi["data"]["title"].as_str().map(|s| s.to_string());

    let m_url = format!("https://api.live.bilibili.com/live_user/v1/Master/info?uid={uid}");
    let raw2 = common::http_get(client, &m_url, Some(&api_headers())).await?;
    let m: serde_json::Value = serde_json::from_slice(&raw2).context("Master/info 非 JSON")?;
    let nick = m["data"]["info"]["uname"].as_str().map(|s| s.to_string());
    Ok((nick, title))
}

#[async_trait]
impl Platform for BilibiliLive {
    fn domains(&self) -> &[&str] {
        &["live.bilibili.com"]
    }

    fn play_headers(&self) -> HashMap<String, String> {
        play_headers_map()
    }

    async fn parse(&self, client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo> {
        let parsed = url::Url::parse(url).map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?;
        let short = parsed
            .path()
            .trim_start_matches('/')
            .split('/')
            .next()
            .unwrap_or("");

        let (rid, living) = resolve_room(client, short).await?;
        let mut info = RoomInfo {
            rid: rid.to_string(),
            nick: None,
            title: None,
            living,
            streams: HashMap::new(),
        };
        if !living {
            return Ok(info);
        }

        let (nick, title) = room_meta(client, rid).await?;
        info.nick = nick;
        info.title = title;

        let q = format!(
            "room_id={rid}&protocol=0,1&format=0,1,2&codec=0&qn=10000&platform=web&ptype=8"
        );
        let play_url =
            format!("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo?{q}");
        let raw = common::http_get(client, &play_url, Some(&api_headers())).await?;
        let v: serde_json::Value =
            serde_json::from_slice(&raw).context("getRoomPlayInfo 非 JSON")?;
        let data = &v["data"];
        if data.get("live_status").and_then(|v| v.as_u64()) == Some(0) {
            info.living = false;
            return Ok(info);
        }
        info.streams = streams_from_playinfo(data);
        Ok(info)
    }
}
