# play-with-mvp

用 **IINA/mpv** 播放直播与 B 站番剧。直播支持虎牙、抖音、斗鱼、哔哩哔哩，并通过本地代理自动跨 FLV 断流续播。

平台解析在后台完成,给播放器一个稳定地址来播放。

## 依赖

- macOS：使用 [IINA](https://iina.io/)，也可安装 [mpv 官网](https://mpv.io/) 的 mpv (`brew install --cask iina` / `brew install mpv`)
- Windows：使用 [mpv 官网](https://mpv.io/)，通过 Scoop 安装：`scoop install mpv`
- [uv](https://github.com/astral-sh/uv)(纯标准库、无第三方依赖;Python 3.14 由 uv 自动装好)

程序会按平台选择默认播放器：macOS 默认 IINA，Windows/Linux 默认 mpv；也可用 `--player iina|mpv` 显式指定。

安装 uv:

```bash
brew install uv
# 或
curl -LsSf https://astral.sh/uv/install.sh | sh
```

## 快速开始

在仓库根目录运行；在别处可加 `--directory <仓库路径>`：

```bash
# 虎牙(推荐:serve 模式,本地代理,自动跨断流自愈)
uv run cli https://www.huya.com/lpl

# 抖音
uv run cli https://live.douyin.com/123456

# 斗鱼
uv run cli https://www.douyu.com/123456

# 哔哩哔哩
uv run cli https://live.bilibili.com/123456

# B 站番剧会自动识别
uv run cli https://www.bilibili.com/bangumi/play/ss28747 --episode latest
```

也可用 `uv run cli live ...` 或 `uv run cli series ...` 显式指定类型，适合短链接等无法自动判断的地址。

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
uv run cli --mode serve-only
```

再从别的命令行用 `serve` 播放不同房间——都会复用上面的代理，各自打开默认播放器，
而断流/转流 `[seg N]` 日志统一打在常驻代理那个终端:

```bash
uv run cli https://www.huya.com/lpl
uv run cli https://live.douyin.com/123456
```

请求把房间与清晰度写进 query(`?room=<完整地址>&quality=<档>`),故复用别处代理时也按本次请求解析、
不受其启动平台/清晰度限制;也支持路径网关 `http://127.0.0.1:<port>/lpl.flv`(按代理默认平台拼)。

## 常用选项

```
url           直播间地址(如 https://live.bilibili.com/24678311),--mode serve-only 可省
--quality Q   清晰度显示名或码率(如 "原画" / 蓝光10M / 2000),默认最高
--line K      direct/m3u 选第 K 条线路(0 起),默认 0
--title T     自定义播放器窗口标题,默认用房间名(主播名)
--mode M      serve(默认) / serve-only / m3u / direct / print
--port P      serve 模式端口,默认 8787
--player P    播放器:macOS 默认 iina,Windows/Linux 默认 mpv
--grace S     serve 模式无连接空闲 S 秒后自动退出,<=0 常驻,默认 180
```

房间地址支持别名(`https://www.huya.com/lpl`)与纯房间号/短号(`https://www.huya.com/660000`);
斗鱼也支持 `...?rid=` 形式。

## 说明

- **窗口标题**:默认显示房间名(主播名)。macOS 的 IINA 对网络直链只显示文件名、`force-media-title` 在标题栏
  不生效,故 `serve`/`m3u` 模式改为给 IINA 一个含 `#EXTINF:-1 ,<标题>` 的本地 m3u 来显示标题；mpv
  则直接用 `--force-media-title`。`--title` 可覆盖。
- **请求头**:`serve` 模式的 referer/UA 由本地代理负责,播放器直连 localhost 无需带头;`direct`/`m3u`
  直连平台 CDN 时,mpv 用 `--referrer`/`--user-agent`,macOS IINA 用 `iina://` 的 `mpv_referrer`/`mpv_user-agent`。
- **端口探测并发**:某些启用了 TUN/过滤驱动的代理软件会让"连接被拒绝"延迟 ~2s,serve 模式并发探测端口,
  避免首次启动被拖慢;发现本项目已在跑的代理(靠 `/__ping__` 识别)则直接复用、不重复起。
- **哔哩哔哩原画/登录**:B 站原画/4K 需登录后取流。两种方式:
  1. **扫码登录**:`uv run cli --login bilibili` —— 终端打印二维码(纯标准库自绘),用「哔哩哔哩」
     手机 App 扫码确认,cookie 自动存到项目根目录 `.cookie/bilibili`(仅本人可读),之后取流自动带上；如果文件不存在,请重新扫码登录。
  2. 不读取环境变量或其他目录中的 cookie。

  都没有则走免登录,最高约蓝光。查看登录态与有效期:`uv run cli --login-status`
  (显示用户名、会员类型、cookie 剩余天数——过期时间从 SESSDATA 本地解析,登录态联网确认)。

> 代码内部仍将直播与点播分别放在 `live`、`series`，对外统一通过 `cli` 命令运行。

## 番剧/影视点播(series)

番剧是**点播**(完整文件、无断流问题),与直播两回事,内部单独放在 `series` 包(复用 `live` 的公共件与 B 站登录 cookie):

```bash
uv run cli series https://www.bilibili.com/bangumi/play/ss28747              # 整季地址,默认首集
uv run cli series https://www.bilibili.com/bangumi/play/ss28747 --episode 185 # 选第 185 集(正片集号)
uv run cli series https://www.bilibili.com/bangumi/play/ss28747 --episode latest
uv run cli series https://www.bilibili.com/bangumi/play/ep285395             # ep 地址精确到某集
```

- `--episode N`:**正片集号**(优先按分集标题里的集号匹配,长番混入重制版/特别篇/看点时也能对上);`latest` 为最新一集;`ep` 地址也支持 `--episode`,并会打印整季 `ss` 号(从 ep 反查 ss)。
- 取 **DASH 流**(音视频分轨,自动把音轨作 `--audio-file` 交给播放器),大会员正片用 `uv run cli --login bilibili` 的 cookie 解锁,画质可达 **1080P高码率 / 4K / HDR**(默认最高,`--quality` 指定档位)。
- 只有 direct(直接打开)与 `--print`(打印地址)两种;`--player iina|mpv`。

## 项目结构

```
cli.py          # 对外统一入口(uv run cli)
  cli.py                 # 自动识别直播/番剧，也支持 live/series 显式子命令
live/               # 直播主包(纯直播)
  __main__.py            # 兼容旧的模块运行方式
  cli.py                 # 命令行:参数解析、端口选择、启动播放器
  server.py              # 本地转流代理:跨断流自愈、FLV 时间戳改写
  common.py              # 公共工具:HTTP(gzip/POST)、清晰度选择、IINA/mpv 参数、m3u 生成(series 也复用)
  qr.py                  # 纯标准库 QR 生成 + 终端渲染(B 站扫码登录用)
  sites/
    __init__.py          # 直播平台派发层(按域名路由)
    huya.py douyin.py douyu.py bilibili.py   # 四个直播平台解析
series/             # 点播扩展(番剧/影视),复用 live 的公共件与 B 站登录 cookie
  __main__.py            # 兼容旧的模块运行方式
  cli.py                 # 命令行:direct/print + 选集
  sites/bilibili.py      # B 站番剧:pgc playurl → DASH 高清(视频轨 + 音轨)
tests/                   # 标准库 unittest(test_play / test_live / test_series),零依赖、不触网
```

新增平台见 [CLAUDE.md](CLAUDE.md)。

## 测试

纯标准库、不触网,直接跑:

```bash
uv run -m unittest tests.test_play tests.test_live tests.test_series
```

覆盖清晰度选择、m3u、虎牙签名、gzip、serve 代理解析、各平台解析纯函数与派发,以及 series
的番剧解析/选集/DASH 提流。

## 免责声明

仅供个人学习与自用播放,请遵守各平台服务条款,勿用于商业或侵权用途。
