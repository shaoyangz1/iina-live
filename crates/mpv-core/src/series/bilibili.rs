//! 哔哩哔哩番剧/影视 (www.bilibili.com/bangumi/play/...) 点播解析。
//!

//! 取流走 pgc 明文链路：season → pgc/player/web/playurl (fnval=4048 DASH)

use std::collections::HashMap;

use super::{EpisodeMeta, SeasonInfo, SeriesRoomInfo};
use crate::common::{self, StreamInfo};
use anyhow::Context as _;

const UA: &str = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15";
const REFERER: &str = "https://www.bilibili.com/";

pub fn play_headers() -> HashMap<String, String> {
    HashMap::from([
        ("User-Agent".into(), UA.into()),
        ("Referer".into(), REFERER.into()),
    ])
}

/// qn 值 → 显示名。
fn qn_name(qn: u32) -> String {
    match qn {
        127 => "8K",
        126 => "杜比视界",
        125 => "HDR",
        120 => "4K",
        116 => "1080P60",
        112 => "1080P+",
        80 => "1080P",
        74 => "720P60",
        64 => "720P",
        32 => "480P",
        16 => "360P",
        6 => "240P",
        _ => return qn.to_string(),
    }
    .to_string()
}

/// 从番剧地址取 (kind, num): ("ep", 123456) 或 ("ss", 28229)。
fn resolve_id(url: &str) -> anyhow::Result<(String, u64)> {
    let parsed = url::Url::parse(url).map_err(|e| anyhow::anyhow!("无效 URL: {e}"))?;
    let slug = parsed
        .path()
        .trim_start_matches('/')
        .split('/')
        .next_back()
        .unwrap_or("");
    let re = regex::Regex::new(r"^(ep|ss)(\d+)$").unwrap();
    let caps = re
        .captures(slug)
        .context("不是番剧地址（应形如 .../bangumi/play/ep123 或 ss123）")?;
    Ok((caps[1].to_string(), caps[2].parse::<u64>().unwrap()))
}

/// 从 season 分集列表挑目标集：
/// - episode=="latest" → 取最后一个标题为数字的正片；
/// - episode 为正整数 → 按 ep.title == 该数字匹配，同集号取时长最长者；
/// - 否则 ep 地址精确匹配 ep_id，ss 地址取第一集。
fn pick_episode(
    season: &serde_json::Value,
    kind: &str,
    num: u64,
    episode: Option<&str>,
) -> Option<(EpisodeMeta, usize)> {
    let eps_arr = season["episodes"].as_array()?;
    if eps_arr.is_empty() {
        return None;
    }

    let total = count_real_eps(&season["episodes"]);

    match episode {
        Some("latest") => {
            let digit_eps: Vec<_> = eps_arr
                .iter()
                .enumerate()
                .filter(|(_, e)| {
                    e["title"]
                        .as_str()
                        .map(|t| t.trim().chars().all(|c| c.is_ascii_digit()))
                        .unwrap_or(false)
                })
                .collect();
            let best = if !digit_eps.is_empty() {
                digit_eps
                    .into_iter()
                    .max_by_key(|(_, e)| e["title"].as_str().unwrap().parse::<u64>().unwrap_or(0))
                    .map(|(i, e)| (i, e.clone()))
            } else {
                eps_arr.last().map(|e| (eps_arr.len() - 1, e.clone()))
            };
            best.map(|(_idx, e)| (ep_from_value(&e), total))
        }
        Some(ep_str) => {
            if let Ok(n) = ep_str.parse::<usize>() {
                if n < 1 {
                    return None;
                }
                // 先按 ep_title == n 精确匹配
                let hits: Vec<_> = eps_arr
                    .iter()
                    .enumerate()
                    .filter(|(_, e)| e["title"].as_str().map(|t| t.trim()).unwrap_or("") == ep_str)
                    .collect();
                if !hits.is_empty() {
                    let (_, best) = hits
                        .into_iter()
                        .max_by_key(|(_, e)| e["duration"].as_u64().unwrap_or(0))
                        .unwrap();
                    Some((ep_from_value(best), total))
                } else if n <= eps_arr.len() {
                    let e = &eps_arr[n - 1];
                    Some((ep_from_value(e), total))
                } else {
                    None
                }
            } else {
                None
            }
        }
        None => {
            if kind == "ep" {
                eps_arr
                    .iter()
                    .find(|e| e["id"].as_u64() == Some(num))
                    .map(|e| (ep_from_value(e), total))
            } else {
                eps_arr.first().map(|e| (ep_from_value(e), total))
            }
        }
    }
}

fn count_real_eps(eps: &serde_json::Value) -> usize {
    let arr = match eps.as_array() {
        Some(a) => a,
        None => return 0,
    };
    arr.iter()
        .filter(|e| {
            e["title"]
                .as_str()
                .map(|t| t.trim().chars().all(|c| c.is_ascii_digit()))
                .unwrap_or(false)
        })
        .map(|e| e["title"].as_str().unwrap().parse::<usize>().unwrap_or(0))
        .max()
        .unwrap_or(arr.len())
}

fn ep_from_value(e: &serde_json::Value) -> EpisodeMeta {
    EpisodeMeta {
        id: e["id"].as_u64().unwrap_or(0),
        cid: e["cid"].as_u64().unwrap_or(0),
        bvid: e["bvid"].as_str().unwrap_or("").to_string(),
        title: e["title"].as_str().unwrap_or("").to_string(),
        long_title: e["long_title"].as_str().unwrap_or("").to_string(),
        duration: e["duration"].as_u64().unwrap_or(0),
    }
}

/// 从 DASH 提取各档流（含 audio 独立音轨）。
fn streams_from_dash(dash: &serde_json::Value) -> HashMap<String, StreamInfo> {
    let mut streams = HashMap::new();
    let videos = dash["video"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or(&[]);
    let audios = dash["audio"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or(&[]);
    if videos.is_empty() || audios.is_empty() {
        return streams;
    }

    let audio_url = audios
        .iter()
        .max_by_key(|a| a["bandwidth"].as_u64().unwrap_or(0))
        .map(base_url);
    let Some(audio_url) = audio_url else {
        return streams;
    };

    // 按 qn 降序
    let mut qns: Vec<u32> = videos
        .iter()
        .filter_map(|v| v["id"].as_u64().map(|n| n as u32))
        .collect();
    qns.sort_unstable_by(|a, b| b.cmp(a));
    qns.dedup();

    for qn in qns {
        let cands: Vec<_> = videos
            .iter()
            .filter(|v| v["id"].as_u64() == Some(qn as u64))
            .collect();
        let v = cands
            .iter()
            .find(|v| {
                v["codecs"]
                    .as_str()
                    .map(|c| c.starts_with("avc"))
                    .unwrap_or(false)
            })
            .or_else(|| cands.first())
            .cloned();
        let Some(v) = v else { continue };
        let name = qn_name(qn);
        let url = base_url(v);
        let backups: Vec<String> = v
            .get("backupUrl")
            .or_else(|| v.get("backup_url"))
            .and_then(|x| x.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|b| b.as_str().map(|s| s.to_string()))
                    .collect()
            })
            .unwrap_or_default();
        streams.entry(name).or_insert_with(|| StreamInfo {
            quality: qn,
            url,
            backups,
            audio: Some(audio_url.clone()),
        });
    }

    // DASH 音视频分轨：每档视频附上独立音频轨
    for s in streams.values_mut() {
        s.audio = Some(audio_url.clone());
    }

    streams
}

/// mp4(fnval=1) 回退：从 durl 取单段合并流。
fn streams_from_play(play: &serde_json::Value) -> HashMap<String, StreamInfo> {
    let durl = play["durl"].as_array().map(|a| a.as_slice()).unwrap_or(&[]);
    if durl.is_empty() {
        return HashMap::new();
    }
    let qn = play["quality"].as_u64().unwrap_or(0) as u32;
    let name = qn_name(qn);
    let first = &durl[0];
    let url = first["url"].as_str().unwrap_or("").to_string();
    let backups: Vec<String> = first
        .get("backup_url")
        .and_then(|x| x.as_array())
        .map(|a| {
            a.iter()
                .filter_map(|b| b.as_str().map(|s| s.to_string()))
                .collect()
        })
        .unwrap_or_default();
    HashMap::from([(
        name,
        StreamInfo {
            quality: qn,
            url,
            backups,
            audio: None,
        },
    )])
}

fn base_url(x: &serde_json::Value) -> String {
    x.get("baseUrl")
        .or_else(|| x.get("base_url"))
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string()
}

/// 取番剧基本信息和分集列表（不取播放流）。
pub async fn get_season_info_impl(
    client: &reqwest::Client,
    url: &str,
) -> anyhow::Result<SeasonInfo> {
    let (kind, num) = resolve_id(url)?;
    let key = if kind == "ep" { "ep_id" } else { "season_id" };
    let api = format!("https://api.bilibili.com/pgc/view/web/season?{key}={num}");
    let raw = common::http_get(client, &api, Some(&play_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("season 接口非 JSON")?;
    let result = &v["result"];
    let eps_arr = result["episodes"]
        .as_array()
        .map(|a| a.as_slice())
        .unwrap_or(&[]);
    let episodes: Vec<EpisodeMeta> = eps_arr.iter().map(ep_from_value).collect();

    Ok(SeasonInfo {
        kind,
        num,
        nick: result["season_title"]
            .as_str()
            .or_else(|| result["title"].as_str())
            .map(|s| s.to_string()),
        episodes,
        season_id: result["season_id"].as_u64(),
    })
}

/// 解析番剧，返回信息 + 各档流（DASH: 视频轨 + audio 音轨）。
pub async fn parse_impl(
    client: &reqwest::Client,
    url: &str,
    episode: Option<&str>,
) -> anyhow::Result<SeriesRoomInfo> {
    let (kind, num) = resolve_id(url)?;
    let key = if kind == "ep" { "ep_id" } else { "season_id" };
    let api = format!("https://api.bilibili.com/pgc/view/web/season?{key}={num}");
    let raw = common::http_get(client, &api, Some(&play_headers())).await?;
    let v: serde_json::Value = serde_json::from_slice(&raw).context("season 接口非 JSON")?;
    let result = &v["result"];

    let (ep_meta, ep_count) = match pick_episode(result, &kind, num, episode) {
        Some(pair) => pair,
        None => {
            return Ok(SeriesRoomInfo {
                rid: format!("{kind}{num}"),
                nick: result["season_title"]
                    .as_str()
                    .or_else(|| result["title"].as_str())
                    .map(|s| s.to_string()),
                title: None,
                living: false,
                streams: HashMap::new(),
                episode_count: 0,
                season_id: result["season_id"].as_u64(),
            });
        }
    };

    let nick = result["season_title"]
        .as_str()
        .or_else(|| result["title"].as_str())
        .map(|s| s.to_string());
    let parts: Vec<&str> = [
        result["season_title"].as_str(),
        result["title"].as_str(),
        Some(ep_meta.long_title.as_str()),
    ]
    .into_iter()
    .flatten()
    .filter(|s| !s.is_empty())
    .collect();
    let title = if !parts.is_empty() {
        Some(parts.join(" "))
    } else {
        nick.clone()
    };

    // fnval=4048 → DASH；fourk=1 放行 4K
    let q = format!(
        "cid={}&bvid={}&qn=127&fnver=0&fnval=4048&fourk=1",
        ep_meta.cid, ep_meta.bvid
    );
    let play_url = format!("https://api.bilibili.com/pgc/player/web/playurl?{q}");
    let raw2 = common::http_get(client, &play_url, Some(&play_headers())).await?;
    let play: serde_json::Value = serde_json::from_slice(&raw2).context("playurl 接口非 JSON")?;

    if play["code"].as_i64() == Some(-10403) {
        anyhow::bail!(
            "该内容需要大会员/地区限制；请先用 `uv run cli --login bilibili` 登录会员账号"
        );
    }

    let res = &play["result"];
    let streams = streams_from_dash(&res["dash"]);
    let streams = if streams.is_empty() {
        streams_from_play(res) // 回退 mp4
    } else {
        streams
    };

    Ok(SeriesRoomInfo {
        rid: format!("{kind}{num}"),
        nick,
        title,
        living: !streams.is_empty(),
        streams,
        episode_count: ep_count,
        season_id: result["season_id"].as_u64(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn chooses_longest_duplicate_episode_and_latest() {
        let season = serde_json::json!({
            "episodes": [
                {"id": 1, "cid": 11, "title": "1", "duration": 44000},
                {"id": 2, "cid": 22, "title": "1", "duration": 1241000},
                {"id": 3, "cid": 33, "title": "2", "duration": 1200000}
            ]
        });

        let selected = pick_episode(&season, "ss", 1, Some("1")).expect("episode");
        assert_eq!(selected.0.cid, 22);
        let latest = pick_episode(&season, "ss", 1, Some("latest")).expect("episode");
        assert_eq!(latest.0.cid, 33);
        assert_eq!(latest.1, 2);

        let sparse = serde_json::json!({
            "episodes": [
                {"id": 4, "cid": 44, "title": "185", "duration": 44000},
                {"id": 5, "cid": 55, "title": "185", "duration": 1241000}
            ]
        });
        let full = pick_episode(&sparse, "ss", 1, Some("185")).expect("episode");
        assert_eq!(full.0.cid, 55);
    }

    #[test]
    fn attaches_highest_bandwidth_audio_to_each_video_quality() {
        let dash = serde_json::json!({
            "video": [
                {"id": 80, "codecs": "avc1", "baseUrl": "https://video/1080"},
                {"id": 120, "codecs": "hev1", "baseUrl": "https://video/4k"}
            ],
            "audio": [
                {"bandwidth": 100, "baseUrl": "https://audio/low"},
                {"bandwidth": 200, "baseUrl": "https://audio/high"}
            ]
        });

        let streams = streams_from_dash(&dash);
        assert_eq!(streams["4K"].audio.as_deref(), Some("https://audio/high"));
        assert_eq!(
            streams["1080P"].audio.as_deref(),
            Some("https://audio/high")
        );
    }
}
