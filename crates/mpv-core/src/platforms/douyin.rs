//! 抖音（live.douyin.com）平台解析模块。
//!

use async_trait::async_trait;
use std::collections::HashMap;

use crate::common::{self, RoomInfo, StreamInfo};
use crate::platforms::Platform;
use anyhow::Context as _;

const UA_DESKTOP: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36";
const REFERER: &str = "https://live.douyin.com/";
const ROOM_URL: &str = "https://live.douyin.com/{web_rid}";

pub struct Douyin;

fn play_headers_map() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA_DESKTOP.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

/// key → 中文名（兜底映射）。
fn fallback_name(key: &str) -> &str {
    match key {
        "FULL_HD1" => "原画",
        "HD1" => "高清",
        "SD1" => "标清",
        "SD2" => "流畅",
        _ => key,
    }
}

/// 从房间地址取 web_rid（路径最后一段）。
fn resolve_web_rid(url: &str) -> anyhow::Result<String> {
    let parsed = url::Url::parse(url).map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?;
    let slug = parsed
        .path()
        .trim_start_matches('/')
        .split('/')
        .next_back()
        .unwrap_or("")
        .to_string();
    if slug.is_empty() {
        anyhow::bail!("无法从 URL 提取抖音房间 web_rid");
    }
    Ok(slug)
}

/// 从首页 Set-Cookie 抓 ttwid。
async fn fetch_ttwid(client: &reqwest::Client) -> anyhow::Result<String> {
    let resp = client
        .get(REFERER)
        .header("User-Agent", UA_DESKTOP)
        .send()
        .await
        .context("获取抖音首页失败")?;
    for cookie in resp.headers().get_all("set-cookie") {
        if let Ok(s) = cookie.to_str() {
            if let Some(idx) = s.find("ttwid=") {
                let rest = &s[idx + 6..];
                if let Some(end) = rest.find(';') {
                    return Ok(rest[..end].to_string());
                }
                return Ok(rest.to_string());
            }
        }
    }
    anyhow::bail!("未从首页获取到 ttwid")
}

/// 括号平衡提取：从 start_key 处的 `{` 起，计到匹配的 `}`。
fn balanced_obj<'a>(s: &'a str, start_key: &str) -> Option<&'a str> {
    let i = s.find(start_key)?;
    let i = s[i..].find('{')? + i;
    let mut depth = 0;
    let mut in_str = false;
    let mut esc = false;
    let bytes = s.as_bytes();
    for k in i..bytes.len() {
        let c = bytes[k] as char;
        if in_str {
            if esc {
                esc = false;
            } else if c == '\\' {
                esc = true;
            } else if c == '"' {
                in_str = false;
            }
        } else if c == '"' {
            in_str = true;
        } else if c == '{' {
            depth += 1;
        } else if c == '}' {
            depth -= 1;
            if depth == 0 {
                return Some(&s[i..=k]);
            }
        }
    }
    None
}

/// 从房间页 SSR 数据提取当前房间对象（用 web_rid 锚定）。
fn room_from_html(html: &str, web_rid: &str) -> anyhow::Result<serde_json::Value> {
    // 找包含当前房间 web_rid 的 roomStore.roomInfo.room 对象
    let needle = format!(r#""id_str":"{web_rid}""#);
    let start = html
        .find(&needle)
        .ok_or_else(|| anyhow::anyhow!("房间页未找到 web_rid={web_rid}"))?;
    // 往前找 roomInfo.room
    if let Some(_o) = balanced_obj(&html[..start], "}") {
        // 找到的是被 } 闭合的未知对象，需要再往前找外层
    }
    // 反向搜 roomInfo.room 后提取
    let search_start = start.saturating_sub(2000);
    let chunk = &html[search_start..html.len().min(search_start + 50000)];

    // 用 roomInfo.room 锚定
    let room_obj = balanced_obj(chunk, "\"roomInfo\":")
        .or_else(|| {
            // roomInfo 内嵌，先取 roomInfo
            let info = balanced_obj(chunk, "\"roomInfo\"")?;
            balanced_obj(info, "\"room\"")
        })
        .ok_or_else(|| anyhow::anyhow!("无法提取房间对象"))?;

    serde_json::from_str::<serde_json::Value>(room_obj).context("房间 JSON 解析失败")
}

fn room_from_room_obj(room: &serde_json::Value, web_rid: &str) -> RoomInfo {
    let status = room.get("status").and_then(|v| v.as_u64()).unwrap_or(0);
    let owner = room.get("owner");
    let nick = owner
        .and_then(|o| o.get("nickname"))
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());
    let title = room
        .get("title")
        .and_then(|v| v.as_str())
        .map(|s| s.to_string());

    let living = status == 2;
    let mut info = RoomInfo {
        rid: web_rid.to_string(),
        nick: nick.clone(),
        title: title.or(nick),
        living,
        streams: HashMap::new(),
    };
    if !living {
        return info;
    }

    let su = room.get("stream_url");
    // SDK 清晰度路径
    if let Some(sdk) = su
        .and_then(|v| v.get("live_core_sdk_data"))
        .and_then(|v| v.get("pull_data"))
    {
        let quals = sdk.get("options").and_then(|v| v.get("qualities"));
        if let Some(stream_data_str) = sdk.get("stream_data").and_then(|v| v.as_str()) {
            if let Ok(flv_map) = serde_json::from_str::<serde_json::Value>(stream_data_str) {
                let flv_data = flv_map.get("data");
                if let Some(qual_arr) = quals.and_then(|v| v.as_array()) {
                    for q in qual_arr {
                        let sdk_key = q.get("sdk_key").and_then(|v| v.as_str()).unwrap_or("");
                        if let Some(flv) = flv_data
                            .and_then(|d| d.get(sdk_key))
                            .and_then(|d| d.get("main"))
                            .and_then(|d| d.get("flv"))
                            .and_then(|v| v.as_str())
                        {
                            let name = q
                                .get("name")
                                .and_then(|v| v.as_str())
                                .unwrap_or("")
                                .to_string();
                            if !name.is_empty() {
                                let rate = q.get("v_bit_rate").and_then(|v| v.as_u64()).unwrap_or(0)
                                    as u32;
                                info.streams.insert(
                                    name,
                                    StreamInfo {
                                        quality: rate,
                                        url: flv.to_string(),
                                        backups: vec![],
                                        audio: None,
                                    },
                                );
                            }
                        }
                    }
                }
            }
        }
    }

    // 兜底：flv_pull_url
    if info.streams.is_empty() {
        if let Some(flv_map) = su
            .and_then(|v| v.get("flv_pull_url"))
            .and_then(|v| v.as_object())
        {
            for (key, val) in flv_map {
                if let Some(url) = val.as_str() {
                    info.streams.insert(
                        fallback_name(key).to_string(),
                        StreamInfo {
                            quality: 0,
                            url: url.to_string(),
                            backups: vec![],
                            audio: None,
                        },
                    );
                }
            }
        }
    }

    info
}

#[async_trait]
impl Platform for Douyin {
    fn domains(&self) -> &[&str] {
        &["live.douyin.com", "douyin.com"]
    }

    fn play_headers(&self) -> HashMap<String, String> {
        play_headers_map()
    }

    async fn parse(&self, client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo> {
        let web_rid = resolve_web_rid(url)?;
        let room_url = ROOM_URL.replace("{web_rid}", &web_rid);

        let ttwid = fetch_ttwid(client).await?;
        let mut headers = play_headers_map();
        headers.insert("Cookie".into(), format!("ttwid={ttwid}"));

        let html = common::http_get_text(client, &room_url, Some(&headers)).await?;
        let room_obj = room_from_html(&html, &web_rid)?;
        Ok(room_from_room_obj(&room_obj, &web_rid))
    }
}
