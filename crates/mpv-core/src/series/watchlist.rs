//! 追剧清单：本地 JSON 文件持久化。

use std::fs;
use std::io;
use std::path::PathBuf;
use std::sync::{Mutex, OnceLock};

use serde::{Deserialize, Serialize};

use crate::common;

/// 单条追剧记录。
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct WatchEntry {
    pub url: String,
    pub nick: String,
    pub total: usize,
}

fn watchlist_path() -> PathBuf {
    common::data_dir().join(".series_watchlist")
}

// ponytail: 全局锁只覆盖本地清单写入；多进程并发时再升级为文件锁。
fn watchlist_lock() -> &'static Mutex<()> {
    static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
    LOCK.get_or_init(|| Mutex::new(()))
}

/// 读取追剧清单；文件不存在代表空清单，损坏文件返回错误。
pub fn load() -> io::Result<Vec<WatchEntry>> {
    let path = watchlist_path();
    let content = match fs::read_to_string(&path) {
        Ok(content) => content,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(vec![]),
        Err(error) => return Err(error),
    };
    serde_json::from_str(&content).map_err(|error| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!("追剧清单 JSON 损坏: {error}"),
        )
    })
}

/// 保存追剧清单，使用临时文件替换避免半写入文件。
pub fn save(items: &[WatchEntry]) -> io::Result<()> {
    let path = watchlist_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let json = serde_json::to_string_pretty(items)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    let temp = path.with_extension("tmp");
    fs::write(&temp, json)?;
    fs::rename(temp, path)
}

/// 添加一部番剧到追剧清单。
pub fn add(url: &str, nick: &str, total: usize) -> io::Result<()> {
    let _guard = watchlist_lock()
        .lock()
        .map_err(|_| io::Error::other("追剧清单锁已损坏"))?;
    let mut items = load()?;
    if items.iter().any(|it| it.url == url) {
        return Ok(());
    }
    items.push(WatchEntry {
        url: url.to_string(),
        nick: nick.to_string(),
        total,
    });
    save(&items)
}
