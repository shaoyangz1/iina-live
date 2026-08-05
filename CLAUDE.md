# iina-live 开发约定

面向 IINA/mpv 的直播流解析器。后续开发遵循以下约定。

## 环境与依赖

- Python 固定 `3.14.*`(见 pyproject),用 [uv](https://github.com/astral-sh/uv) 运行。
- **纯标准库,零第三方依赖**。不要引入新依赖——能几行标准库搞定的不装包。
- 入口统一 `uv run -m iina_live`;不再有根级脚本。
- 播放需本机装 IINA 或 mpv。

## 结构

```
iina_live/           主包(cli 入口 / server 代理 / common 工具)
  qr.py              纯标准库 QR 生成 + 终端渲染(B 站扫码登录用)
  sites/             平台层:__init__.py 派发,每平台一个模块(huya/douyin/douyu/bilibili)
tests/               标准库 unittest
```

## 新增平台

在 `iina_live/sites/` 下新建一个模块,实现统一接口,再到 `sites/__init__.py` 的
`SITES` 列表登记即可,`cli` / `server` 无需改动:

- `DOMAINS`      `list[str]`  匹配的域名关键字(如 `["huya.com"]`)
- `PLAY_HEADERS` `dict`       拉流 HTTP 头(Referer/User-Agent;无则留空 dict)
- `parse(url)`   `-> dict`    `{rid, nick, title, living, streams{名:{quality,url,backups}}}`

`quality` 用码率数值(越大越清晰,`0` 视为原画),`common.pick()` 据此选档。

## 测试

- 框架:标准库 `unittest`,**不触网**——网络边界(`fetch` 等)一律做成可注入参数,用假上游驱动。
- 跑:`uv run -m unittest tests.test_iina_live`(或 `python3 -m unittest tests.test_iina_live`)。
- 非平凡逻辑(虎牙签名、FLV 时间戳改写、各平台解析、选路)改动后补一条断言;纯函数优先用
  独立参考实现比对,别只锁魔数值。
- **提交前必须全绿。**

## 关键点

- **断流修复**是本项目核心价值:很多平台的 flv 每 ~2 分钟正常关闭连接(EOF),播放需带完整
  `reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5`;
  `serve` 模式进一步在服务端重解析 + 改写 FLV 时间戳无缝续播。改动 `server.py` 的续播/去重逻辑要格外小心。
- 播放器打开走 `iina://` scheme 或 mpv 直连;IINA 标题栏对网络直链只显示文件名,故用本地 m3u 的
  `#EXTINF` 名来显示标题。
- **代理复用契约**:cli 生成的本地地址把房间/清晰度写进 query(`?room=&quality=`,`cli._serve_url`),
  server 端 `parse_request` 据此解析(`?room=` 优先,否则路径 slug 网关;`?quality=` 优先,否则全局默认)。
  这让一个常驻代理服务任意平台任意房间。`--mode serve-only` 起的裸代理 `ROOM=None`,裸连 `/live.flv`
  报 400,全靠请求带 `?room=`。改这套 query 契约时 cli/server 两侧要同步,`test_roundtrips` 守着闭环。
- 平台接口随风控变化,某平台解析失败多为接口调整,先看对应 `sites/<平台>.py` 的 `parse()`。
- **B 站登录**:`--login bilibili` 走扫码(qrcode/generate → 终端二维码 → poll 轮询 → cookie 落盘
  `~/.config/iina-live/bilibili_cookie`)。`bilibili._load_cookie()` 供取流用(env `BILI_COOKIE` 优先)。
  `qr.py` 是从零实现的 QR 编码器(byte/ECC-L/v1-10),改动务必用真实解码器(如 OpenCV)验证可扫,
  别只肉眼看——格式信息行列、alignment 跳过条件都踩过坑。

## 风格

- 注释与 commit 信息用中文,风格对齐现有代码(解释「为什么」而非复述代码)。
- 优先最小改动,不做投机性抽象。
