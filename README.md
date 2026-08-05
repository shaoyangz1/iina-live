# iina-live

用 **IINA/mpv** 看直播:解析直播平台(当前支持虎牙 huya.com、抖音 live.douyin.com、斗鱼 douyu.com、哔哩哔哩 live.bilibili.com)的真实直播流地址,交给 [IINA](https://iina.io/)/mpv 播放。

平台解析在后台完成,给播放器一个稳定地址来播放。

## 依赖

- macOS + [IINA](https://iina.io/) 或 mpv(`brew install --cask iina` / `brew install mpv`)
- [uv](https://github.com/astral-sh/uv)(纯标准库、无第三方依赖;Python 3.14 由 uv 自动装好)

安装 uv:

```bash
brew install uv
# 或
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 快速开始

在仓库根目录运行(`-m` 才能找到 `iina_live` 包;在别处可加 `--directory <仓库路径>`):

```bash
# 虎牙(推荐:serve 模式,本地代理,自动跨断流自愈)
uv run -m iina_live https://www.huya.com/lpl

# 抖音
uv run -m iina_live https://live.douyin.com/123456

# 斗鱼
uv run -m iina_live https://www.douyu.com/123456

# 哔哩哔哩
uv run -m iina_live https://live.bilibili.com/123456

# 或直接用 python
python3 -m iina_live https://www.huya.com/lpl
```

## 五种模式(`--mode`)

| 模式 | 说明 |
|------|------|
| `serve`(默认) | 本地转流代理,给播放器一个固定地址,自动跨 ~2 分钟断流无缝续播 |
| `serve-only` | 只起一个常驻代理、不打开播放器(房间地址可省)。纯中转、不绑默认房间(裸连 `/live.flv` 报错),别处用 `serve` 复用它播放,断流/转流日志都集中在这个进程,方便从多个命令行同时开多个播放 |
| `m3u` | 生成多线路播放列表,卡住时在播放列表里切「备用N」线路 |
| `direct` | 单条 flv 直链,最简单,卡住无法自动恢复 |
| `print` | 只解析并打印各清晰度/线路地址,不打开播放器 |

### 一个常驻代理，多处复用

先起一个常驻代理(地址可省，日志都集中在这个进程):

```bash
uv run -m iina_live --mode serve-only
```

再从别的命令行用 `serve` 播放不同房间——都会复用上面的代理，各自打开 IINA，
而断流/转流 `[seg N]` 日志统一打在常驻代理那个终端:

```bash
uv run -m iina_live https://www.huya.com/lpl
uv run -m iina_live https://live.douyin.com/123456
```

请求把房间与清晰度写进 query(`?room=<完整地址>&quality=<档>`),故复用别处代理时也按本次请求解析、
不受其启动平台/清晰度限制;也支持路径网关 `http://127.0.0.1:<port>/lpl.flv`(按代理默认平台拼)。

## 常用选项

```
url           直播间地址(如 https://live.bilibili.com/24678311),--mode serve-only 可省
--quality Q   清晰度显示名或码率(如 "原画" / 蓝光10M / 2000),默认最高
--line K      direct/m3u 选第 K 条线路(0 起),默认 0
--title T     自定义 IINA 窗口标题,默认用房间名(主播名)
--mode M      serve(默认) / serve-only / m3u / direct / print
--port P      serve 模式端口,默认 8787
--player P    direct/m3u 模式播放器:iina(默认) / mpv
--grace S     serve 模式无连接空闲 S 秒后自动退出,<=0 常驻,默认 180
```

房间地址支持别名(`https://www.huya.com/lpl`)与纯房间号/短号(`https://www.huya.com/660000`);
斗鱼也支持 `...?rid=` 形式。

## 说明

- **窗口标题**:默认显示房间名(主播名)。IINA 对网络直链只显示文件名、`force-media-title` 在标题栏
  不生效,故 `serve`/`m3u` 模式改为给 IINA 一个含 `#EXTINF:-1 ,<标题>` 的本地 m3u 来显示标题;mpv 则
  直接用 `--force-media-title`。`--title` 可覆盖。
- **请求头**:`serve` 模式的 referer/UA 由本地代理负责,播放器直连 localhost 无需带头;`direct`/`m3u`
  直连平台 CDN 时,mpv 用 `--referrer`/`--user-agent`,IINA 用 `iina://` 的 `mpv_referrer`/`mpv_user-agent`。
- **端口探测并发**:某些启用了 TUN/过滤驱动的代理软件会让"连接被拒绝"延迟 ~2s,serve 模式并发探测端口,
  避免首次启动被拖慢;发现本项目已在跑的代理(靠 `/__ping__` 识别)则直接复用、不重复起。
- **哔哩哔哩原画/登录**:B 站原画/4K 需登录后取流。两种方式:
  1. **扫码登录**:`uv run -m iina_live --login bilibili` —— 终端打印二维码(纯标准库自绘),用「哔哩哔哩」
     手机 App 扫码确认,cookie 自动存到 `~/.config/iina-live/bilibili_cookie`(仅本人可读),之后取流自动带上;
  2. 或设环境变量 `BILI_COOKIE`(浏览器里的 `SESSDATA`)。

  都没有则走免登录,最高约蓝光。
- **B 站番剧(点播)**:也支持番剧/影视地址(番剧是**点播**,不是直播):

  ```bash
  uv run -m iina_live https://www.bilibili.com/bangumi/play/ss26801            # 整季地址,默认第 1 集
  uv run -m iina_live https://www.bilibili.com/bangumi/play/ss26801 --episode 5 # 选第 5 集
  uv run -m iina_live https://www.bilibili.com/bangumi/play/ep285395           # ep 地址精确到某集
  ```

  给 `ss`(整季)地址时用 `--episode N`(第几集,1 起)选集;`ep` 地址本身已精确到某集,但**同样支持
  `--episode`**(内部按 ep 已取到整季分集),且解析时会打印出该内容的整季 `ss` 号(等于从 ep 反查 ss)。点播没有断流
  问题,故自动走 `direct`(不启 serve 代理),取 fnval=1 的 **mp4 合并流**直链交给播放器。大会员/付费
  正片用 `--login bilibili` 的 cookie 解锁;画质通常 720P–1080P(更高清的 DASH 分轨暂未做)。

## 项目结构

```
iina_live/               # 主包
  __main__.py            # 入口(uv run -m iina_live)
  cli.py                 # 命令行:参数解析、端口选择、启动播放器
  server.py              # 本地转流代理:跨断流自愈、FLV 时间戳改写
  common.py              # 公共工具:HTTP(gzip/POST)、清晰度选择、iina/m3u 生成
  qr.py                  # 纯标准库 QR 生成 + 终端渲染(B 站扫码登录用)
  sites/
    __init__.py          # 平台派发层(按域名路由)
    huya.py              # 虎牙解析:本地 wsSecret 签名 flv 地址
    douyin.py            # 抖音解析:ttwid cookie / 房间页 SSR 数据
    douyu.py             # 斗鱼解析:getEncryption + 纯 MD5 auth + getH5PlayV1
    bilibili.py          # B 站直播解析:room_init + getRoomPlayInfo(免签名)
    bangumi.py           # B 站番剧(点播):pgc playurl → mp4 直链,走 direct
tests/                   # 标准库 unittest,零依赖、不触网
```

新增平台见 [CLAUDE.md](CLAUDE.md)。

## 测试

纯标准库、不触网,直接跑:

```bash
python3 -m unittest tests.test_iina_live
# 或
uv run -m unittest tests.test_iina_live
```

覆盖清晰度选择、m3u 生成、虎牙签名(uid 移位 / wsSecret)、gzip 解压、serve 代理的按请求
`room`/`quality` 解析,以及抖音/斗鱼/B 站解析纯函数与派发路由。

## 免责声明

仅供个人学习与自用播放,请遵守各平台服务条款,勿用于商业或侵权用途。
