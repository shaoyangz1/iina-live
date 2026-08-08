//! 虎牙（huya.com）平台解析模块。
//!

use anyhow::Context as _;
use async_trait::async_trait;
use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use crate::common::{http_get, http_get_text, md5, RoomInfo, StreamInfo};
use crate::platforms::Platform;

const UA_MOBILE: &str = "Mozilla/5.0 (iPhone; CPU iPhone OS 14_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.0 Mobile/15E148 Safari/604.1";
const UA_DESKTOP: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15";
const REFERER: &str = "https://www.huya.com/";

/// 参数顺序模板（HuyaUrl.swift 里的 example，保持 query 参数顺序一致）。
const EXAMPLE: &str = "wsSecret=x&wsTime=x&seqid=x&ctype=x&ver=1&fs=bgct&ratio=2000&dMod=mseh-8&sdkPcdn=1_1&u=x&t=100&sv=2407051433&sdk_sid=x&a_block=0&sf=1";

pub struct Huya;

fn api_headers() -> HashMap<String, String> {
    HashMap::from([("User-Agent".into(), UA_MOBILE.into())])
}

fn play_headers_map() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA_DESKTOP.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

// ---- 虎牙签名相关 ----

/// uid 64bit 循环移位：高 32bit 不动，低 32bit 前 8bit 挪到末尾。
fn rot_uid(uid: u64) -> u64 {
    let a = (uid >> 32) as u32;
    let r = (uid & 0xFFFF_FFFF) as u32;
    let n = r.rotate_left(8);
    ((a as u64) << 32) | (n as u64)
}

/// URL 解码（手动实现，handle %XX 和 +）。
fn url_decode(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let b = s.as_bytes();
    let mut i = 0;
    while i < b.len() {
        match b[i] {
            b'%' if i + 2 < b.len() => {
                if let Ok(hex) =
                    u8::from_str_radix(std::str::from_utf8(&b[i + 1..i + 3]).unwrap_or("00"), 16)
                {
                    out.push(hex as char);
                    i += 3;
                    continue;
                }
                out.push('%');
            }
            b'+' => out.push(' '),
            c => out.push(c as char),
        }
        i += 1;
    }
    out
}

/// base64 decode + URL decode（对应 Python `base64.b64decode(urllib.parse.unquote(...))`）。
fn base64_decode_url(encoded: &str) -> Vec<u8> {
    use base64::Engine;
    let decoded = url_decode(encoded);
    base64::engine::general_purpose::STANDARD
        .decode(&decoded)
        .unwrap_or_default()
}

/// 防盗链签名：wsSecret = md5(fm 模板替换 $0/$1/$2/$3 后再取 md5)。
fn ws_secret(
    anti: &HashMap<String, String>,
    convert_uid: u64,
    seqid: u64,
    stream_name: &str,
) -> String {
    let fm_raw = base64_decode_url(anti.get("fm").map(|s| s.as_str()).unwrap_or(""));
    let fm = String::from_utf8_lossy(&fm_raw);
    let wstime = anti.get("wsTime").map(|s| s.as_str()).unwrap_or("");
    let ctype = anti.get("ctype").map(|s| s.as_str()).unwrap_or("");
    let t = anti.get("t").map(|s| s.as_str()).unwrap_or("100");
    let s = md5(&format!("{seqid}|{ctype}|{t}"));
    let u = fm
        .replace("$0", &convert_uid.to_string())
        .replace("$1", stream_name)
        .replace("$2", &s)
        .replace("$3", wstime);
    md5(&u)
}

/// 解析 anticode 字符串 → HashMap<String, String>。
fn parse_anti(anti: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for kv in anti.split('&') {
        if let Some((k, v)) = kv.split_once('=') {
            map.insert(k.to_string(), v.to_string());
        }
    }
    map
}

/// 拼出带签名的 flv 地址（ratio=0 占位，后续按码率替换）。
fn sign_url(
    uid: u64,
    stream_name: &str,
    flv_url: &str,
    flv_suffix: &str,
    flv_anticode: &str,
) -> String {
    let now_ms = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64;
    let seqid = uid.wrapping_add(now_ms);
    let convert_uid = rot_uid(uid);
    let mut anti = parse_anti(flv_anticode);
    let secret = ws_secret(&anti, convert_uid, seqid, stream_name);

    anti.insert("wsSecret".into(), secret);
    anti.insert("u".into(), convert_uid.to_string());
    anti.insert("seqid".into(), seqid.to_string());
    anti.insert("sdk_sid".into(), now_ms.to_string());
    anti.insert("ratio".into(), "0".into());

    let base = flv_url.replace("http://", "https://");
    let pars: Vec<String> = EXAMPLE
        .split('&')
        .filter_map(|item| {
            let (k, default) = item.split_once('=')?;
            let v = anti.get(k).map(|s| s.as_str()).unwrap_or(default);
            Some(format!("{k}={v}"))
        })
        .collect();
    format!("{base}/{stream_name}.{flv_suffix}?{}", pars.join("&"))
}

// ---- 房间号解析 ----

/// 数字直接用；别名（如 lpl）先抓页面拿 profileRoom。
async fn resolve_rid(client: &reqwest::Client, url: &str) -> anyhow::Result<u64> {
    let parsed = url::Url::parse(url).map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?;
    let slug = parsed
        .path()
        .trim_start_matches('/')
        .split('/')
        .next()
        .unwrap_or("");

    if let Ok(id) = slug.parse::<u64>() {
        return Ok(id);
    }
    let html = http_get_text(client, url, Some(&api_headers())).await?;
    let re = regex::Regex::new(r#""lProfileRoom"\s*:\s*(\d+)"#).unwrap();
    let re2 = regex::Regex::new(r#""profileRoom"\s*:\s*(\d+)"#).unwrap();
    if let Some(caps) = re.captures(&html).or_else(|| re2.captures(&html)) {
        return caps[1]
            .parse::<u64>()
            .map_err(|e| anyhow::anyhow!("解析 profileRoom 失败: {e}"));
    }
    anyhow::bail!("找不到 profileRoom，检查房间地址是否正确")
}

#[async_trait]
impl Platform for Huya {
    fn domains(&self) -> &[&str] {
        &["huya.com"]
    }

    fn play_headers(&self) -> HashMap<String, String> {
        play_headers_map()
    }

    async fn parse(&self, client: &reqwest::Client, url: &str) -> anyhow::Result<RoomInfo> {
        use std::hash::{DefaultHasher, Hasher};

        let rid = resolve_rid(client, url).await?;
        let api_url = format!("https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid={rid}");

        let raw = http_get(client, &api_url, Some(&api_headers())).await?;
        let v: serde_json::Value = serde_json::from_slice(&raw).context("虎牙 API 返回非 JSON")?;
        let data = &v["data"];
        let ld = &data["liveData"];

        let living = data["liveStatus"].as_str() == Some("ON");
        let info = RoomInfo {
            rid: rid.to_string(),
            nick: ld["nick"].as_str().map(|s| s.to_string()),
            title: ld["roomName"]
                .as_str()
                .or_else(|| ld["introduction"].as_str())
                .or_else(|| ld["nick"].as_str())
                .map(|s| s.to_string()),
            living,
            streams: HashMap::new(),
        };
        if !living {
            return Ok(info);
        }

        // uid 生成（与 Python 一致：时间戳取模 + 随机 → % 4_294_967_295）
        let now_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;
        let mut hasher = DefaultHasher::new();
        hasher.write_u64(now_ms);
        let rng = hasher.finish() % 900 + 100; // 100..1000
        let uid = ((now_ms % 10_000_000_000) * 1000 + rng) % 4_294_967_295;

        // 线路排序：txdirect 排后
        let lines = &data["stream"]["baseSteamInfoList"];
        let mut lines: Vec<&serde_json::Value> = lines
            .as_array()
            .map(|a| a.iter().collect())
            .unwrap_or_default();
        lines.sort_by_key(|b| {
            b.get("sFlvUrl")
                .and_then(|v| v.as_str())
                .map(|s| s.contains("txdirect.flv.huya.com"))
                .unwrap_or(false)
        });

        // 为每条线路签名
        let base_urls: Vec<String> = lines
            .iter()
            .map(|b| {
                sign_url(
                    uid,
                    b["sStreamName"].as_str().unwrap_or(""),
                    b["sFlvUrl"].as_str().unwrap_or(""),
                    b["sFlvUrlSuffix"].as_str().unwrap_or(""),
                    b["sFlvAntiCode"].as_str().unwrap_or(""),
                )
            })
            .collect();

        // 清晰度解析（bitRateInfo 是 JSON 字符串数组）
        let mut streams = HashMap::new();
        if let Ok(brs) =
            serde_json::from_str::<serde_json::Value>(ld["bitRateInfo"].as_str().unwrap_or("[]"))
        {
            if let Some(arr) = brs.as_array() {
                for br in arr {
                    let name = br["sDisplayName"].as_str().unwrap_or("").to_string();
                    if name.is_empty() {
                        continue;
                    }
                    let rate = br["iBitRate"].as_u64().unwrap_or(0) as u32;
                    let us: Vec<String> = base_urls
                        .iter()
                        .map(|u| {
                            if rate > 0 {
                                u.replace("&ratio=0", &format!("&ratio={rate}"))
                            } else {
                                u.replace("&ratio=0", "")
                            }
                        })
                        .collect();
                    if let Some(url) = us.first() {
                        streams.insert(
                            name,
                            StreamInfo {
                                quality: rate,
                                url: url.clone(),
                                backups: us[1..].to_vec(),
                                audio: None,
                            },
                        );
                    }
                }
            }
        }

        Ok(RoomInfo { streams, ..info })
    }
}
