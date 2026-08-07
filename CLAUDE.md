# play-with-mvp 开发约定

面向 IINA/mpv 的**直播**流解析器 + **点播**扩展。三个包:
- `cli.py` — 对外统一命令入口,`uv run cli ...`,自动识别直播/番剧,也支持 `live` / `series` 显式类型;
- `live` — 纯直播(虎牙/抖音/斗鱼/B站直播);
- `series` — 点播(B站番剧/影视),**复用** live 的 common/播放/B站登录 cookie。
live 保持干净:不含任何点播代码。后续开发遵循以下约定。

## 环境与依赖

- Python 固定 `3.14.*`(见 pyproject),用 [uv](https://github.com/astral-sh/uv) 运行。
- **纯标准库,零第三方依赖**。不要引入新依赖——能几行标准库搞定的不装包。
- 入口统一 `uv run cli ...`;`python -m live` / `python -m series` 仅作兼容。
- 播放器按平台选择:macOS 默认 IINA,Windows/Linux 默认 mpv;Windows 用 Scoop 安装 `mpv`。

## 结构

```
cli.py          对外统一 cli 入口(自动派发直播/点播)
live/           直播主包(cli 入口 / server 代理 / common 工具)
  qr.py              纯标准库 QR 生成 + 终端渲染(B 站扫码登录用)
  sites/             直播平台层:__init__.py 派发,每平台一个模块(huya/douyin/douyu/bilibili)
series/         点播扩展(番剧/影视),复用 live.common 与 live.sites.bilibili._load_cookie
  cli.py             direct/print + 选集
  sites/bilibili.py  B 站番剧:pgc playurl → DASH
tests/               标准库 unittest(test_play / test_live / test_series)
```

## 新增平台

在 `live/sites/` 下新建一个模块,实现统一接口,再到 `sites/__init__.py` 的
`SITES` 列表登记即可,`cli` / `server` 无需改动:

- `DOMAINS`      `list[str]`  匹配的域名关键字(如 `["huya.com"]`)
- `PLAY_HEADERS` `dict`       拉流 HTTP 头(Referer/User-Agent;无则留空 dict)
- `parse(url)`   `-> dict`    `{rid, nick, title, living, streams{名:{quality,url,backups}}}`

`quality` 用码率数值(越大越清晰,`0` 视为原画),`common.pick()` 据此选档。

## 测试

- 框架:标准库 `unittest`,**不触网**——网络边界(`fetch` 等)一律做成可注入参数,用假上游驱动。
- 跑:`uv run -m unittest tests.test_play tests.test_live tests.test_series`。
- 非平凡逻辑(虎牙签名、FLV 时间戳改写、各平台解析、选路)改动后补一条断言;纯函数优先用
  独立参考实现比对,别只锁魔数值。
- **提交前必须全绿。**

## 关键点

- **断流修复**是本项目核心价值:很多平台的 flv 每 ~2 分钟正常关闭连接(EOF),播放需带完整
  `reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5`;
  `serve` 模式进一步在服务端重解析 + 改写 FLV 时间戳无缝续播。改动 `server.py` 的续播/去重逻辑要格外小心。
- macOS 的 IINA 打开走 `iina://` scheme,Windows/Linux 走 mpv 直连;IINA 标题栏对网络直链只显示文件名,
  故用本地 m3u 的 `#EXTINF` 名来显示标题。
- **代理复用契约**:cli 生成的本地地址把房间/清晰度写进 query(`?room=&quality=`,`cli._serve_url`),
  server 端 `parse_request` 据此解析(`?room=` 优先,否则路径 slug 网关;`?quality=` 优先,否则全局默认)。
  这让一个常驻代理服务任意平台任意房间。`--mode serve-only` 起的裸代理 `ROOM=None`,裸连 `/live.flv`
  报 400,全靠请求带 `?room=`。改这套 query 契约时 cli/server 两侧要同步,`test_roundtrips` 守着闭环。
- 平台接口随风控变化,某平台解析失败多为接口调整,先看对应 `sites/<平台>.py` 的 `parse()`。
- **series(点播)**:番剧是完整文件,没有断流问题,单独成包、不进 live。走 pgc `playurl`
  (明文、无需 wbi),`fnval=4048` 取 DASH(VIP 高清 1080P+/4K/HDR,音视频分轨:stream 带 `audio` 字段,
  播放时音轨作 `--audio-file`/`mpv_audio-file`;`fnval=1` mp4 仅 720P,作回退)。选集 `--episode N`
  优先按正片集号(ep.title)匹配、同集号取时长最长(避开 44s 看点),`latest` 取末集。它 import
  `live.common` 和 `live.sites.bilibili._load_cookie`(B站登录直播/番剧共用);依赖方向单向
  (series → live),别让 live 反向依赖 series。
- **B 站登录**:`--login bilibili` 走扫码(qrcode/generate → 终端二维码 → poll 轮询 → cookie 落盘
  项目根目录 `.cookie/bilibili`;没有该文件时请重新扫码登录。
  `bilibili._load_cookie()` 只读取这一路径。
  `qr.py` 是从零实现的 QR 编码器(byte/ECC-L/v1-10),改动务必用真实解码器(如 OpenCV)验证可扫,
  别只肉眼看——格式信息行列、alignment 跳过条件都踩过坑。

## 风格

- 注释与 commit 信息用中文,风格对齐现有代码(解释「为什么」而非复述代码)。
- 优先最小改动,不做投机性抽象。
