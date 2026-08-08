//! mpv-server 二进制入口：启动本地 FLV 转流代理。
//!
//! 用法：mpv-server [房间地址] [端口] [清晰度] [宽限秒数]

use std::env;

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    tracing_subscriber::fmt::init();

    let args: Vec<String> = env::args().collect();
    let room = args.get(1).cloned().unwrap_or_default();
    let port: u16 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(8787);
    let quality = args.get(3).cloned().filter(|s| !s.is_empty());
    let grace: u64 = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(180);

    mpv_server::run(room, port, quality, grace).await
}
