//! FLV 断流续播：逐段从上游拉流，改写时间戳，转发给下游播放器。
//!
//! 对应 Python `live/server.py` 的 `_stream()` 方法。

use std::collections::HashMap;

use bytes::Bytes;
use futures::Stream;
use mpv_core::common::pick;
use mpv_core::platforms;

/// 段间隔（ms）：换线/时钟跳变时用于接续。
const GAP: i64 = 40;
/// 原始时间戳差在此以内视为同一时钟（ms）。
const WINDOW: i64 = 60_000;

/// 流中继：逐段从上游拉流，自动重签 + 改写 FLV 时间戳。
pub fn relay_stream(
    room: String,
    urls: Vec<String>,
    headers: HashMap<String, String>,
    quality: Option<String>,
) -> impl Stream<Item = Result<Bytes, std::io::Error>> + Send + 'static {
    let stream = async_stream::stream! {
        let client = reqwest::Client::new();
        let mut urls = urls;
        let mut out_base: Option<i64> = None;
        let mut last_src: Option<i64> = None;
        let mut last_out: i64 = -GAP;
        let mut first_segment = true;
        let mut seg: u64 = 0;
        let mut line: usize = 0;

        loop {
            if line >= urls.len() * 2 {
                break;
            }
            let url = &urls[line % urls.len()];

            let mut req_builder = client.get(url);
            for (k, v) in &headers {
                req_builder = req_builder.header(k.as_str(), v.as_str());
            }
            let resp = match req_builder.send().await {
                Ok(r) => r,
                Err(e) => {
                    eprintln!("[mpv-server] 连接上游失败: {e}");
                    line += 1;
                    continue;
                }
            };
            if !resp.status().is_success() {
                line += 1;
                continue;
            }

            seg += 1;
            eprintln!("[seg {seg}] 线路{} 连接, last_out={last_out}", line % urls.len());

            // 收集本段全部字节
            let body_bytes = match resp.bytes().await {
                Ok(b) => b,
                Err(e) => {
                    eprintln!("[seg {seg}] 读取上游失败: {e}");
                    line += 1;
                    continue;
                }
            };

            let data = body_bytes.as_ref();
            let mut pos: usize = 0;

            // FLV 文件头(9) + PreviousTagSize0(4) = 13 字节
            let _header_end = pos + 13;
            if first_segment {
                if data.len() >= 13 {
                    yield Ok(Bytes::copy_from_slice(&data[..13]));
                    first_segment = false;
                }
                pos = 13;
            } else {
                if data.len() < 13 {
                    break;
                }
                pos = 13;
            }

            let mut resume = last_src;
            let mut first_tag = true;
            let mut dropped: u64 = 0;
            let mut drop_from: Option<i64> = None;

            while pos + 11 <= data.len() {
                let th = &data[pos..pos + 11];
                pos += 11;

                let dsize = ((th[1] as u32) << 16) | ((th[2] as u32) << 8) | (th[3] as u32);
                let ts = ((th[4] as i64) << 16)
                    | ((th[5] as i64) << 8)
                    | (th[6] as i64)
                    | ((th[7] as i64) << 24);

                let data_end = pos + dsize as usize + 4; // +4 for PreviousTagSize
                if data_end > data.len() {
                    break;
                }
                let tag_data = &data[pos..pos + dsize as usize];
                pos += dsize as usize + 4; // skip data + PreviousTagSize(4)

                if first_tag {
                    first_tag = false;
                    if out_base.is_none() {
                        out_base = Some(-ts);
                        resume = None;
                    } else if let Some(ls) = last_src {
                        if (ts - ls).abs() > WINDOW {
                            out_base = Some((last_out + GAP) - ts);
                            resume = None;
                        }
                    }
                }

                // 丢弃重复回放帧
                if let Some(r) = resume {
                    if ts <= r {
                        if drop_from.is_none() {
                            drop_from = Some(ts);
                        }
                        dropped += 1;
                        continue;
                    }
                }

                if dropped > 0 {
                    let df = drop_from.unwrap_or(0);
                    eprintln!(
                        "[seg {seg}] 丢弃重复回放 {dropped} 帧(~{}ms)，从 out_ts={} 续播",
                        resume.unwrap_or(0) - df,
                        ts + out_base.unwrap_or(0)
                    );
                    dropped = 0;
                }

                let ob = out_base.unwrap_or(0);
                let new_ts = ts + ob;

                // 构建 FLV tag header（改写时间戳）+ data + PreviousTagSize
                let prev_size = (11 + dsize) as u32;
                let mut tag_buf = vec![
                    th[0],
                    (dsize >> 16) as u8,
                    (dsize >> 8) as u8,
                    dsize as u8,
                    // timestamp lower 3 bytes
                    ((new_ts as u32) >> 16) as u8,
                    ((new_ts as u32) >> 8) as u8,
                    (new_ts as u32) as u8,
                    // timestamp upper 1 byte
                    (new_ts >> 24) as u8,
                    // stream_id = 0 (3 bytes)
                    0, 0, 0,
                ];
                tag_buf.extend_from_slice(tag_data);
                tag_buf.extend_from_slice(&prev_size.to_be_bytes());

                yield Ok(Bytes::from(tag_buf));

                if new_ts > last_out {
                    last_out = new_ts;
                }
                if last_src.is_none() || ts > last_src.unwrap() {
                    last_src = Some(ts);
                }
            }

            // 段结束，重新解析拿新签名地址
            match platforms::parse(&client, &room).await {
                Ok(info) if info.living => {
                    if let Some((_, s)) = pick(&info.streams, quality.as_deref()) {
                        urls = std::iter::once(s.url.clone())
                            .chain(s.backups.clone())
                            .collect();
                    }
                }
                Ok(_) => {
                    eprintln!("[seg {seg}] 房间已下播");
                    break;
                }
                Err(e) => {
                    eprintln!("[seg {seg}] 重解析失败: {e}");
                }
            }
        }
    };
    stream
}
