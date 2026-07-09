# iina-live

解析直播平台的真实直播流并用 [IINA](https://iina.io/)/mpv 播放。当前支持**虎牙(huya.com)**,
架构上可扩展到其他平台。虎牙部分是 [iina-plus](https://github.com/xjbeta/iina-plus) 解析算法的
Python 复刻,并**修复了虎牙 flv 播放约 2 分钟自动断开**的问题。

> 也是一个 [Claude Code](https://claude.com/claude-code) skill(见 `SKILL.md`),
> 放到 `~/.claude/skills/iina-live/` 后可用 `/iina-live <直播间地址>` 调用。

## 依赖

仅 Python 标准库。推荐用 [`uv`](https://github.com/astral-sh/uv) 运行:

```bash
uv run --python 3.12 cli.py https://www.huya.com/lpl
```

播放需要本机安装 IINA 或 mpv。

## 四种模式(`--mode`)

| 模式 | 说明 | 卡住能否恢复 |
|------|------|-------------|
| `serve`(默认) | 本地转流代理:固定地址 `http://127.0.0.1:<port>/live.flv`,断流时服务器自动重解析+重签+改写 FLV 时间戳无缝续播 | ✅✅ 自动自愈 |
| `m3u` | 生成含全部 CDN 线路的本地 m3u 播放列表 | ✅ 手动切「备用N」 |
| `direct` | 单条 flv 直链 | ❌ |
| `print` | 只解析打印各清晰度/线路地址 | — |

常用选项:`--quality 蓝光4M`、`--title "自定义"`、`--player mpv`、`--port 8787`、
`--grace <秒>`(serve 无连接自动退出,默认 180,`0` 常驻)。

## 网关模式

`serve` 的代理按请求路径自动解析房间,一个常驻服务器可播任意房间:

- `http://127.0.0.1:<port>/lpl.flv` → `huya.com/lpl`
- `http://127.0.0.1:<port>/660000.flv` → 房间号 660000

## 结构 / 扩展新平台

```
common.py   平台无关工具:reconnect 参数、清晰度选择、iina/m3u 生成
huya.py     虎牙平台模块:DOMAINS / PLAY_HEADERS / parse()
sites.py    派发层:按域名找模块
server.py   本地转流代理(平台无关)
cli.py      命令行入口
```

新增平台只需:①写一个模块实现 `DOMAINS` / `PLAY_HEADERS` / `parse(url) ->
{rid, nick, title, living, streams{名:{quality,url,backups}}}`;②在 `sites.py` 的 `SITES` 里登记。
`server.py` / `cli.py` 无需改动。

## 原理要点(虎牙)

1. 不抓 HTML,直接调移动端 API `mp.huya.com/cache.php?do=profileRoom&roomid=<rid>` 取各 CDN 线路与清晰度;别名房间先抓页面取 `lProfileRoom`。
2. 本地复刻防盗链签名 `wsSecret`:uid 做 64bit 循环移位;`fm` base64 解出模板 `..._$0_$1_$2_$3`,依次填 `convertUid / streamName / md5(seqid|ctype|t) / wsTime` 再整体 md5。
3. 播放必带 `referrer=https://www.huya.com/` + 桌面 UA + `ytdl=no`。
4. 断流修复:虎牙 flv 每 ~2 分钟正常关闭连接(EOF),用完整 `reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5`;`serve` 模式进一步在服务端重解析续播。

## 声明

仅供个人学习与自用播放,遵守各平台服务条款,勿用于二次分发或商业用途。
