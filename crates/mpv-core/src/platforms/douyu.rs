//! 斗鱼（douyu.com）平台解析模块。
//!

use async_trait::async_trait;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::common::{self, RoomInfo, StreamInfo};
use crate::platforms::Platform;
use anyhow::Context as _;

const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36";
const REFERER: &str = "https://www.douyu.com";
const DID: &str = "10000000000000000000000000001501";

pub struct Douyu;

fn api_headers() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

fn play_headers_map() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

/// MD5 鉴权签名：对 secret 迭代 enc_time 次 md5，再拼接 key + salt 取 md5。
fn auth(enc: &serde_json::Value, rid: &str, ts: u64) -> String {
    let mut secret = enc["rand_str"].as_str().unwrap_or("").to_string();
    let key = enc["key"].as_str().unwrap_or("");
    let enc_time = enc["enc_time"].as_u64().unwrap_or(0);
    let is_special = enc.get("is_special").and_then(|v| v.as_u64()).unwrap_or(0);

    for _ in 0..enc_time {
        secret = common::md5(&format!("{secret}{key}"));
    }
    let salt = if is_special == 1 {
        String::new()
    } else {
        format!("{rid}{ts}")
    };
    common::md5(&format!("{secret}{key}{salt}"))
}

/// 从 getH5PlayV1 响应拼完整 flv 地址。
fn play_url(data: &serde_json::Value) -> String {
    let rtmp = data["rtmp_url"]
        .as_str()
        .unwrap_or("")
        .trim_end_matches('/');
    let live = data["rtmp_live"].as_str().unwrap_or("");
    format!("{rtmp}/{live}")
}

/// 房间号解析：数字直接，别名抓 m.douyu.com 页面。
async fn resolve_rid(client: &reqwest::Client, url: &str) -> anyhow::Result<String> {
    let parsed = url::Url::parse(url).map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?;
    let slug = parsed
        .path()
        .trim_start_matches('/')
        .split('/')
        .next()
        .unwrap_or("");

    // query 中的 rid
    for (k, v) in parsed.query_pairs() {
        if k == "rid" && v.chars().all(|c| c.is_ascii_digit()) {
            return Ok(v.to_string());
        }
    }

    if slug.chars().all(|c| c.is_ascii_digit()) {
        return Ok(slug.to_string());
    }

    let html = common::http_get_text(
        client,
        &format!("https://m.douyu.com/{slug}"),
        Some(&api_headers()),
    )
    .await?;

    let re = regex::Regex::new(r#""rid"\s*:\s*(\d+)\s*,\s*"vipId""#).unwrap();
    let re2 = regex::Regex::new(r#""roomInfo"\s*:\s*\{\s*"rid"\s*:\s*(\d+)"#).unwrap();
    if let Some(caps) = re.captures(&html).or_else(|| re2.captures(&html)) {
        return Ok(caps[1].to_string());
    }
    anyhow::bail!("找不到斗鱼房间号，检查地址是否正确")
}

/// betard 接口取房间元数据。
async fn room_meta(
    client: &reqwest::Client,
    rid: &str,
) -> anyhow::Result<(Option<String>, Option<String>, bool)> {
    let url = format!("https://www.douyu.com/betard/{rid}");
    let raw = common::http_get(client, &url, Some(&api_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("betard 非 JSON")?;
    let room = v.get("room").unwrap_or(&v);
    let nick = room["nickname"].as_str().map(|s| s.to_string());
    let title = room["room_name"]
        .as_str()
        .or(room["nickname"].as_str())
        .map(|s| s.to_string());
    let living = room["show_status"].as_u64() == Some(1)
        && room.get("videoLoop").and_then(|v| v.as_u64()) != Some(0);
    Ok((nick, title, living))
}

/// getEncryption → POST getH5PlayV1。
async fn get_encryption(client: &reqwest::Client) -> anyhow::Result<serde_json::Value> {
    let url = format!("https://www.douyu.com/wgapi/livenc/liveweb/websec/getEncryption?did={DID}");
    let raw = common::http_get(client, &url, Some(&api_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("getEncryption 非 JSON")?;
    Ok(v["data"].clone())
}

async fn get_play(
    client: &reqwest::Client,
    rid: &str,
    rate: u32,
    enc_data: &str,
    tt: u64,
    auth: &str,
) -> anyhow::Result<serde_json::Value> {
    let body = format!(
        "cdn=hw-h5&rate={rate}&ver=Douyu_new&iar=0&ive=0&rid={rid}&hevc=0&fa=0&sov=0&enc_data={enc_data}&tt={tt}&did={DID}&auth={auth}"
    );
    let mut headers = api_headers();
    headers.insert(
        "Content-Type".into(),
        "application/x-www-form-urlencoded".into(),
    );

    let mut req = client
        .post(format!("https://www.douyu.com/lapi/live/getH5PlayV1/{rid}"))
        .body(body);
    for (k, v) in &headers {
        req = req.header(k.as_str(), v.as_str());
    }
    let resp = req.send().await.context("getH5PlayV1 失败")?;
    let raw = resp.bytes().await.context("读取响应体失败")?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("getH5PlayV1 非 JSON")?;
    Ok(v["data"].clone())
}

#[async_trait]
impl Platform for Douyu {
    fn domains(&self) -> &[&str] {
        &["douyu.com"]
    }

    fn play_headers(&self) -> HashMap<String, String> {
        play_headers_map()
    }

    async fn parse(&self, client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo> {
        let rid = resolve_rid(client, url).await?;
        let (nick, title, living) = room_meta(client, &rid).await?;

        let mut info = RoomInfo {
            rid: rid.clone(),
            nick,
            title,
            living,
            streams: HashMap::new(),
        };
        if !living {
            return Ok(info);
        }

        let enc = get_encryption(client).await?;
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let auth_val = auth(&enc, &rid, ts);
        let enc_data = enc["enc_data"].as_str().unwrap_or("").to_string();

        // rate=0 拿最高清 + 全部清晰度清单
        let first = get_play(client, &rid, 0, &enc_data, ts, &auth_val).await?;
        let mut rates: Vec<(String, u32, u32)> = Vec::new(); // (name, rate, bit)

        if let Some(arr) = first["multirates"].as_array() {
            for mr in arr {
                let name = mr["name"].as_str().unwrap_or("").to_string();
                let rate = mr["rate"].as_u64().unwrap_or(0) as u32;
                let bit = mr["bit"].as_u64().unwrap_or(0) as u32;
                if !name.is_empty() {
                    rates.push((name, rate, bit));
                }
            }
        }
        // 确保 rate=0 原画在列表里
        if !rates.iter().any(|(_, r, _)| *r == 0) {
            rates.insert(0, ("原画".into(), 0, 0));
        }

        for (name, rate, bit) in &rates {
            let data = if *rate == 0 {
                &first
            } else {
                // 并发取各档会好一些，这里简单顺序取
                &get_play(client, &rid, *rate, &enc_data, ts, &auth_val).await?
            };
            info.streams.insert(
                name.clone(),
                StreamInfo {
                    quality: *bit,
                    url: play_url(data),
                    backups: vec![],
                    audio: None,
                },
            );
        }

        Ok(info)
    }
}
