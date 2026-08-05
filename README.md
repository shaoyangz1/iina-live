# iina-live

解析直播平台的真实直播流并用 [IINA](https://iina.io/)/mpv 播放。当前支持**虎牙(huya.com)、
抖音(live.douyin.com)、斗鱼(douyu.com)、哔哩哔哩(live.bilibili.com)**,架构上可继续扩展。
虎牙部分是 [iina-plus](https://github.com/xjbeta/iina-plus) 解析算法的 Python 复刻,并**修复了
虎牙 flv 播放约 2 分钟自动断开**的问题。

> 开发约定见 [`CLAUDE.md`](CLAUDE.md)。

## 依赖

仅 Python 标准库,Python 3.14(由 `uv` 依 `pyproject.toml` 自动装好):

```bash
uv run -m iina_live https://www.huya.com/lpl           # 虎牙
uv run -m iina_live https://live.douyin.com/123456     # 抖音
uv run -m iina_live https://www.douyu.com/123456       # 斗鱼
uv run -m iina_live https://live.bilibili.com/24678311 # 哔哩哔哩
```

(在仓库根目录运行,`iina_live` 包才能被 `-m` 找到;或加 `--directory <仓库路径>`。)

播放需要本机安装 IINA 或 mpv。

各平台房间地址支持别名与纯房间号/短号;斗鱼也支持 `...?rid=` 形式。
**B 站原画/4K** 需登录取流:设环境变量 `BILI_COOKIE`(浏览器里的 `SESSDATA`)即可解锁,
不设则走免登录、最高约蓝光。

## 五种模式(`--mode`)

| 模式 | 说明 | 卡住能否恢复 |
|------|------|-------------|
| `serve`(默认) | 本地转流代理:固定地址 `http://127.0.0.1:<port>/live.flv`,断流时服务器自动重解析+重签+改写 FLV 时间戳无缝续播 | ✅✅ 自动自愈 |
| `serve-only` | 只起常驻代理、不打开播放器(房间可省)。纯中转,别处用 `serve` 复用它播放,断流/转流日志集中在这个进程,方便同时开多个房间 | ✅✅ |
| `m3u` | 生成含全部 CDN 线路的本地 m3u 播放列表 | ✅ 手动切「备用N」 |
| `direct` | 单条 flv 直链 | ❌ |
| `print` | 只解析打印各清晰度/线路地址 | — |

常用选项:`--quality 蓝光4M`、`--title "自定义"`、`--player mpv`、`--port 8787`、
`--grace <秒>`(serve 无连接自动退出,默认 180,`0` 常驻)。

## 网关 / 复用:一个常驻代理播任意房间

`serve` 模式**不会**杀掉已有代理:从 `--port` 起扫描,发现本 skill 的代理(靠 `/__ping__` 识别)就
直接复用。请求把房间与清晰度写进 query(`?room=<完整地址>&quality=<档>`),故复用别处代理时也按
本次请求解析、不受其启动平台/清晰度限制。也支持路径网关:

- `?room=https://live.bilibili.com/123` → 该 B 站房间(跨平台复用)
- `http://127.0.0.1:<port>/lpl.flv` → `huya.com/lpl`(按代理默认平台拼)
- `http://127.0.0.1:<port>/660000.flv` → 房间号 660000

先起一个常驻裸代理,再从别处复用它播不同房间(日志都集中在裸代理进程):

```bash
uv run -m iina_live --mode serve-only            # 常驻裸代理
uv run -m iina_live https://www.huya.com/lpl     # 另开命令行,复用上面的代理
uv run -m iina_live https://live.douyin.com/123456
```

## 结构 / 扩展新平台

```
iina_live/
  __main__.py       入口(python -m iina_live)
  cli.py            命令行:参数解析、端口选择、启动播放器
  common.py         平台无关工具:清晰度选择、iina/m3u 生成、http_get(gzip/POST)、reconnect 参数
  server.py         本地转流代理(平台无关):跨断流自愈、FLV 时间戳改写
  sites/
    __init__.py     派发层:按域名找平台模块
    huya.py         虎牙:mp.huya.com API + 本地 wsSecret 防盗链签名
    douyin.py       抖音:抓房间页 SSR 数据(ttwid cookie),取 flv
    douyu.py        斗鱼:getEncryption + 纯 MD5 auth + POST getH5PlayV1
    bilibili.py     哔哩哔哩:room_init + getRoomPlayInfo(免签名,多线路)
tests/              标准库 unittest,零依赖、不触网
```

每个平台模块都实现统一接口:`DOMAINS` / `PLAY_HEADERS` / `parse(url) ->
{rid, nick, title, living, streams{名:{quality,url,backups}}}`。新增平台只需:①在 `sites/` 里写一个
这样的模块;②在 `sites/__init__.py` 的 `SITES` 里登记。`server.py` / `cli.py` 无需改动。

## 测试

纯标准库 `unittest`,零依赖、不触网(触网部分用注入 fetch / 假 payload 绕开):

```bash
python3 -m unittest tests.test_iina_live
```

覆盖清晰度选择、m3u 生成、虎牙签名(uid 移位 / wsSecret)、gzip 解压、serve 代理按路径
解析房间,以及抖音/斗鱼/B 站的解析纯函数与派发路由。

## 原理要点

**虎牙**
1. 不抓 HTML,直接调移动端 API `mp.huya.com/cache.php?do=profileRoom&roomid=<rid>` 取各 CDN 线路与清晰度;别名房间先抓页面取 `lProfileRoom`。
2. 本地复刻防盗链签名 `wsSecret`:uid 做 64bit 循环移位;`fm` base64 解出模板 `..._$0_$1_$2_$3`,依次填 `convertUid / streamName / md5(seqid|ctype|t) / wsTime` 再整体 md5。
3. 播放必带 `referrer=https://www.huya.com/` + 桌面 UA + `ytdl=no`。

**抖音**:enter 接口现要求 `a_bogus` 签名,改从房间页内嵌 `self.__pace_f` SSR 数据块取——用当前 `web_rid` 锚定注水后的真数据块(避开初始空壳),反转义后括号平衡抠出 `roomInfo.room`;清晰度优先 `live_core_sdk_data`,回退 `flv_pull_url`。只收 flv。

**斗鱼**:走「免 JS」链路,不依赖房间页混淆 JS——`getEncryption` 下发密钥,对 `(secret+key)` 迭代 `enc_time` 次纯 MD5 算出 `auth`,再 POST `getH5PlayV1` 拿 flv。

**哔哩哔哩**:直播取流无需 wbi 签名(与点播不同),`room_init` 短号转真房号 → `getRoomPlayInfo` 明文 query 拿多档多线路 flv;原画/4K 靠 `BILI_COOKIE` 解锁。

**断流修复(通用)**:很多平台 flv 每 ~2 分钟正常关闭连接(EOF),用完整 `reconnect=1,reconnect_streamed=1,reconnect_at_eof=1,reconnect_on_network_error=1,reconnect_delay_max=5`;`serve` 模式进一步在服务端重解析续播。

## 声明

仅供个人学习与自用播放,遵守各平台服务条款,勿用于二次分发或商业用途。
