#!/usr/bin/env python3
"""本地转流代理:给播放器一个固定的 localhost 地址。

每段连接结束时(很多平台的 flv 每 ~2 分钟正常关闭一次),服务器自动重新解析+重签,
并改写 FLV 时间戳把新段无缝拼到上一段之后 —— 播放器完全无感、自愈,无需手动刷新。

自动关闭: 客户端断开后,若在宽限期(默认 180 秒)内无新连接则进程自动退出,
避免关掉播放器后代理空占端口常驻。设 GRACE<=0 可关闭该行为(保持常驻)。

用法: python server.py <房间地址> [端口=8787] [清晰度] [宽限秒数=180]
"""
import sys
import time
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sites
from common import pick

ROOM = sys.argv[1] if len(sys.argv) > 1 else "https://www.huya.com/lpl"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8787
QUALITY = sys.argv[3] if len(sys.argv) > 3 else None
GRACE = int(sys.argv[4]) if len(sys.argv) > 4 else 180  # 秒;<=0 表示永不自动退出

# 默认房间的来源(scheme://host/),供路径网关按同平台拼房间地址
_o = urllib.parse.urlparse(ROOM)
_ORIGIN = f"{_o.scheme}://{_o.netloc}/" if _o.netloc else "https://www.huya.com/"


def room_from_path(path: str) -> str:
    """把请求路径解析成房间地址(按默认房间所在平台)。

    /live.flv 或 / → 启动时指定的默认房间(ROOM)
    /lpl.flv       → <默认平台>/lpl
    /660000.flv    → <默认平台>/660000
    完整 http 路径直接用;忽略 .flv 后缀与查询串。
    """
    slug = urllib.parse.unquote(urllib.parse.urlparse(path).path).strip("/")
    if slug.endswith(".flv"):
        slug = slug[:-4]
    if not slug or slug == "live":
        return ROOM
    if slug.startswith("http"):
        return slug
    return _ORIGIN + slug

# 活动连接计数与最后活动时间,供自动关闭看门狗判断
_lock = threading.Lock()
_active = 0
_last_active = time.time()


def resolve_lines(room):
    """重新解析指定房间,返回 (线路列表[主+备用], 标题, 拉流头)。"""
    info = sites.parse(room)
    if not info["living"]:
        raise RuntimeError("未开播")
    _, s = pick(info, QUALITY)
    return [s["url"]] + s["backups"], info["title"], sites.play_headers(room)


def read_exact(fp, n):
    buf = b""
    while len(buf) < n:
        c = fp.read(n - len(buf))
        if not c:
            break
        buf += c
    return buf


def open_stream(url, headers):
    req = urllib.request.Request(url, headers=headers or {})
    return urllib.request.urlopen(req, timeout=15)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        global _active, _last_active
        room = room_from_path(self.path)
        try:
            urls, title, headers = resolve_lines(room)
        except Exception as e:
            self.send_error(503, str(e))
            return

        with _lock:
            _active += 1
        try:
            self._stream(urls, title, room, headers)
        finally:
            with _lock:
                _active -= 1
                _last_active = time.time()

    def _stream(self, urls, title, room, headers):
        self.send_response(200)
        self.send_header("Content-Type", "video/x-flv")
        self.send_header("Connection", "close")
        self.end_headers()
        w = self.wfile

        GAP = 40           # 段间隔(ms)
        out_max = -GAP     # 已输出的最大时间戳；初值 -GAP 让第 1 段从 0 开始
        first_segment = True
        seg = 0
        line = 0
        while True:
            url = urls[line % len(urls)]
            try:
                fp = open_stream(url, headers)
            except Exception:
                line += 1
                if line > len(urls) * 2:
                    break
                continue
            seg += 1
            print(f"[seg {seg}] 线路{line % len(urls)} 连接，out_max={out_max}", flush=True)

            # FLV 文件头(9)+PreviousTagSize0(4)：仅第一段转发，后续段丢弃
            header = read_exact(fp, 13)
            if len(header) < 13:
                break
            if first_segment:
                try:
                    w.write(header)
                    w.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return
                first_segment = False

            offset = None      # 本段时间戳偏移，见到首帧时按 out_max 对齐
            try:
                while True:
                    th = read_exact(fp, 11)
                    if len(th) < 11:
                        break  # 本段结束（虎牙断开）→ 跳出去重连
                    dsize = (th[1] << 16) | (th[2] << 8) | th[3]
                    ts = (th[7] << 24) | (th[4] << 16) | (th[5] << 8) | th[6]
                    data = read_exact(fp, dsize)
                    prev = read_exact(fp, 4)
                    if len(data) < dsize or len(prev) < 4:
                        break
                    # 虎牙 ts 是延续的大值且重连不归零，用偏移把本段首帧对齐到
                    # 上一段输出之后，保证跨段时间戳连续、不跳变
                    if offset is None:
                        offset = (out_max + GAP) - ts
                    new_ts = ts + offset
                    nh = bytes([th[0],
                                (dsize >> 16) & 0xFF, (dsize >> 8) & 0xFF, dsize & 0xFF,
                                (new_ts >> 16) & 0xFF, (new_ts >> 8) & 0xFF, new_ts & 0xFF,
                                (new_ts >> 24) & 0xFF,
                                th[8], th[9], th[10]])
                    try:
                        w.write(nh)
                        w.write(data)
                        w.write(prev)
                    except (BrokenPipeError, ConnectionResetError):
                        return  # 播放器关了 → 结束
                    if new_ts > out_max:
                        out_max = new_ts
            except Exception as e:
                print(f"[seg {seg}] 读取异常: {e!r}", flush=True)
            finally:
                try:
                    fp.close()
                except Exception:
                    pass

            # 段结束后重新解析拿全新签名地址
            try:
                urls, _, headers = resolve_lines(room)
            except Exception:
                pass


def watchdog(httpd):
    """无连接且空闲超过 GRACE 秒则关闭服务器，使进程自然退出。"""
    while True:
        time.sleep(5)
        with _lock:
            idle = _active == 0 and (time.time() - _last_active) > GRACE
        if idle:
            print(f"空闲超过 {GRACE}s，自动关闭代理。", flush=True)
            httpd.shutdown()
            return


if __name__ == "__main__":
    print(f"默认房间: {ROOM}")
    print(f"默认地址: http://127.0.0.1:{PORT}/live.flv")
    print(f"任意房间: http://127.0.0.1:{PORT}/<房间号或别名>.flv  (如 /lpl.flv、/660000.flv)")
    if GRACE > 0:
        print(f"自动关闭: 无连接空闲 {GRACE}s 后退出")
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    if GRACE > 0:
        threading.Thread(target=watchdog, args=(httpd,), daemon=True).start()
    httpd.serve_forever()
