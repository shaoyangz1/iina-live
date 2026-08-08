//! mpv-core —— 直播/点播流解析核心库。
//!
//! 提供：
//! - 平台派发层（platforms 模块）：按 URL 域名匹配平台并解析流
//! - 公共工具（common 模块）：HTTP、MD5、清晰度选择、m3u/iina 链接生成
//!
//! 目前支持的直播平台：虎牙

pub mod auth;
pub mod common;
pub mod platforms;
pub mod series;
