---
name: iina-live
description: 解析直播平台的真实直播流并用 IINA/mpv 播放。当前支持虎牙(huya.com),后续可扩展其他平台。虎牙复刻自 iina-plus 的解析算法(房间号解析 → mp.huya.com API 拉流 → 本地计算 wsSecret 防盗链签名 → 拼出可播 flv 地址),并修复了「播放约 2 分钟自动断开」的问题。支持本地转流代理(固定地址、断流自愈)、多线路 m3u、单流直链、仅打印地址四种模式。当用户给出直播间地址(如 https://www.huya.com/xxx 或房间号)并想解析/播放/用 IINA 打开直播时使用。
---

# iina-live

解析直播平台的真实直播流并交给 IINA/mpv 播放。当前支持**虎牙(huya.com)**,后续可接入其他平台。
虎牙部分是 [iina-plus](https://github.com/xjbeta/iina-plus) 解析算法的 Python 复刻,额外修复了
虎牙 flv **约 2 分钟自动断流**的问题。

## 运行方式

依赖仅标准库,按用户偏好用 `uv` 跑:

```bash
uv run --python 3.12 ~/.claude/skills/iina-live/cli.py <房间地址> [选项]
```

`<房间地址>` 支持别名(`https://www.huya.com/lpl`)或纯房间号(`https://www.huya.com/660000`)。

## 四种模式(`--mode`)

| 模式 | 说明 | 卡住能否恢复 |
|------|------|-------------|
| `serve`(默认) | **本地转流代理**:起一个 `http://127.0.0.1:<port>/live.flv` 固定地址,虎牙每 ~2 分钟断流时服务器自动重解析+重签+改写 FLV 时间戳无缝续播 | ✅✅ 自动自愈,无需操作 |
| `m3u` | 生成含全部 CDN 线路的本地 m3u 播放列表 | ✅ 在 IINA 播放列表手动切「备用N」 |
| `direct` | 单条 flv 直链丢给 IINA | ❌ 刷新=重载同一条 |
| `print` | 只解析并打印各清晰度/线路地址,不打开播放器 | — |

`serve` 模式会阻塞(常驻服务器)。在本环境里应**后台运行** cli.py 或直接后台运行 `server.py`,
再单独用 `open "iina://..."` 唤起播放器。

**自动关闭**:`serve` 默认在客户端断开后、若 180 秒内无新连接则进程自动退出(避免关掉播放器后代理空占端口)。
用 `--grace <秒>` 调整,`--grace 0` 则永不自动退出(保持常驻)。`server.py` 第 4 个位置参数同义。

**复用代理 / 自动选端口(多房间友好)**:`cli.py` serve 模式**不会**杀掉已有代理。它从 `--port`(默认 8787)
起向后扫描:若某端口上已有本 skill 代理(通过 `/__ping__` 识别)则**直接复用**(开完 IINA 即返回,不占管它);
若默认端口被别的程序占用则**自动跳到下一个空闲端口**新起。因此同时看多个房间时,一个代理经网关路径即可服务全部,
不会互相顶掉。

**路径即房间(网关模式)**:代理按请求路径自动解析房间,一个常驻服务器可播任意房间:
- `http://127.0.0.1:<port>/lpl.flv` → `huya.com/lpl`
- `http://127.0.0.1:<port>/660000.flv` → 房间号 660000
- `http://127.0.0.1:<port>/live.flv` 或 `/` → 启动时指定的默认房间

`.flv` 后缀和查询串会被忽略;别名会自动抓页面转成房间号。

**IINA 窗口标题**:默认显示房间名(主播名)。IINA 对网络直链只显示文件名、`force-media-title` 在标题栏不生效,
因此 `serve`/`m3u` 模式改为给 IINA 一个含 `#EXTINF:-1 ,<标题>` 的本地 m3u(iina-plus 同款机制)来显示标题。
`--title "自定义"` 可覆盖。

## 常用示例

```bash
# 默认:最高清晰度 + 本地代理自愈 + IINA 打开
uv run --python 3.12 cli.py https://www.huya.com/lpl

# 指定清晰度和标题
uv run --python 3.12 cli.py https://www.huya.com/lpl --quality 蓝光4M --title "LPL 直播"

# 只看地址不播放
uv run --python 3.12 cli.py https://www.huya.com/lpl --mode print

# m3u 多线路 / 用 mpv 播放
uv run --python 3.12 cli.py https://www.huya.com/lpl --mode m3u --player mpv
```

后台跑 serve(推荐在本环境里这样用):

```bash
# 1) 后台起代理
uv run --python 3.12 ~/.claude/skills/iina-live/server.py https://www.huya.com/lpl 8787 &
# 2) 唤起 IINA(标题可改)
python3 - <<'PY'
import urllib.parse, subprocess
opts={"force-media-title":"LPL 直播","ytdl":"no",
      "stream-lavf-o":"reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_delay_max=5"}
q=["url="+urllib.parse.quote("http://127.0.0.1:8787/live.flv",safe="")]
for k,v in opts.items(): q.append(f"mpv_{k}="+urllib.parse.quote(v,safe=""))
subprocess.run(["open","iina://open?"+"&".join(q)])
PY
```

## 文件

- `huya.py` — 核心库:`parse()` 解析房间、`iina_url()`/`iina_local_url()`/`m3u_content()` 生成打开方式。
- `server.py` — 本地转流代理(方式②,自愈)。
- `cli.py` — 命令行入口,整合四种模式。

## 原理与关键点

1. **不抓 HTML**:直接调移动端 API `https://mp.huya.com/cache.php?m=Live&do=profileRoom&roomid=<rid>`
   拿 `baseSteamInfoList`(各 CDN 线路)和 `bitRateInfo`(各清晰度)。别名房间先抓页面取 `lProfileRoom`。
2. **防盗链签名 `wsSecret`**(`huya.py` 的 `_rot_uid`/`_ws_secret`/`_sign_url`):
   uid 做 64bit 循环移位;`fm` base64 解出模板 `..._$0_$1_$2_$3`,依次填
   `convertUid / streamName / md5(seqid|ctype|t) / wsTime` 再整体 md5。
3. **播放必带**:`referrer=https://www.huya.com/` + 桌面 UA + `ytdl=no`。
4. **断流修复(本 skill 的关键改进)**:虎牙 flv 每 ~2 分钟正常关闭连接(EOF)。iina-plus 只设了
   `reconnect_streamed=yes`,不处理 EOF,mpv 会当播放结束退出(实测 ~128 秒)。本 skill 用完整
   `reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5`
   (实测跑满 200s+ 无中断),`serve` 模式更进一步在服务器端自动重解析续播。

## 注意

- 签名地址里的 `wsTime` 约 24 小时有效,但单条连接虎牙会周期性(约 2 分钟)主动断开——靠上面的重连/代理续播解决。
- 仅供个人学习与自用播放;遵守虎牙服务条款,勿用于二次分发或商业用途。
