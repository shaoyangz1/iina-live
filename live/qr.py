#!/usr/bin/env python3
"""纯标准库 QR 码生成 + 终端渲染(byte 模式,version 1-10,ECC level L)。

逻辑对齐 Nayuki 的 QR 参考实现(GF(256) / Reed-Solomon 纠错 / 8 掩码择优),
已用 OpenCV 的标准解码器验证 v2/v6/v7 生成的码可正常扫描。用于 B 站扫码登录
在终端打印二维码,不引第三方依赖。

对外:terminal_qr(text) -> str  终端可打印的二维码字符串(半块字符 + quiet zone)。
"""

# ---- GF(256),生成多项式 0x11D ----
EXP = [0] * 512
LOG = [0] * 256
_x = 1
for _i in range(255):
    EXP[_i] = _x
    LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    EXP[_i] = EXP[_i - 255]


def gf_mul(a, b):
    return 0 if (a == 0 or b == 0) else EXP[LOG[a] + LOG[b]]


def rs_gen(ec):
    """纠错码生成多项式,长度 ec+1(最高次系数在 g[0])。"""
    g = [1]
    for i in range(ec):
        g2 = [0] * (len(g) + 1)
        for j in range(len(g)):
            g2[j] ^= g[j]
            g2[j + 1] ^= gf_mul(g[j], EXP[i])
        g = g2
    return g


def rs_encode(data, ec):
    """对一块数据码字算 ec 个 Reed-Solomon 纠错码字。"""
    gen = rs_gen(ec)
    res = [0] * ec
    for b in data:
        factor = b ^ res[0]
        res = res[1:] + [0]
        for i in range(ec):
            res[i] ^= gf_mul(gen[i + 1], factor)
    return res


# ECC level L:每版本 (ec_per_block, [(块数, 每块数据码字), ...])
QR_L = {
    1: (7, [(1, 19)]), 2: (10, [(1, 34)]), 3: (15, [(1, 55)]),
    4: (20, [(1, 80)]), 5: (26, [(1, 108)]), 6: (18, [(2, 68)]),
    7: (20, [(2, 78)]), 8: (24, [(2, 97)]), 9: (30, [(2, 116)]),
    10: (18, [(2, 68), (2, 69)]),
}
# alignment pattern 中心坐标(每版本)
ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30],
         6: [6, 34], 7: [6, 22, 38], 8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}


def encode_codewords(text):
    """把文本编码为(交织后的)码字流。返回 (version, codewords)。"""
    data = text.encode("utf-8")
    for v in range(1, 11):
        ecw, groups = QR_L[v]
        total = sum(c * d for c, d in groups)
        cc = 8 if v <= 9 else 16
        if 4 + cc + 8 * len(data) <= total * 8:
            break
    else:
        raise ValueError("文本过长,超出支持的 version 10(byte/ECC-L)")
    bits = "0100" + format(len(data), f"0{cc}b") + "".join(format(b, "08b") for b in data)
    total_bits = total * 8
    bits += "0" * min(4, total_bits - len(bits))            # 终止符
    if len(bits) % 8:
        bits += "0" * (8 - len(bits) % 8)                   # 补齐字节
    k = 0
    while len(bits) < total_bits:                           # 填充码字 0xEC/0x11
        bits += "11101100" if k % 2 == 0 else "00010001"
        k += 1
    cw = [int(bits[i:i + 8], 2) for i in range(0, total_bits, 8)]
    # 分块 + 交织(数据码字交织在前,纠错码字交织在后)
    dblocks, idx = [], 0
    for cnt, d in groups:
        for _ in range(cnt):
            dblocks.append(cw[idx:idx + d]); idx += d
    eblocks = [rs_encode(b, ecw) for b in dblocks]
    out = []
    for i in range(max(len(b) for b in dblocks)):
        for b in dblocks:
            if i < len(b):
                out.append(b[i])
    for i in range(ecw):
        for b in eblocks:
            out.append(b[i])
    return v, out


def build_matrix(version, codewords):
    """铺功能图案(finder/timing/alignment/version/format 预留)并按 zigzag 放数据。"""
    size = version * 4 + 17
    m = [[0] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]

    def setf(col, row, val):
        m[row][col] = val
        fn[row][col] = True

    # finder + separator(左上/右上/左下)
    for (r0, c0) in [(0, 0), (0, size - 7), (size - 7, 0)]:
        for dr in range(-1, 8):
            for dc in range(-1, 8):
                r, c = r0 + dr, c0 + dc
                if 0 <= r < size and 0 <= c < size:
                    dist = max(abs(dr - 3), abs(dc - 3))
                    setf(c, r, 1 if (dist != 2 and dist != 4 and dr in range(7) and dc in range(7)) else 0)
    # timing
    for i in range(size):
        if not fn[6][i]:
            setf(i, 6, 1 if i % 2 == 0 else 0)
        if not fn[i][6]:
            setf(6, i, 1 if i % 2 == 0 else 0)
    # alignment:只跳过与三个 finder 角重叠的(左上/右上/左下),其余都画
    # (中心落在 timing 线上的 alignment 仍要画,会覆盖那段 timing)
    pos = ALIGN[version]
    n = len(pos)
    for i in range(n):
        for j in range(n):
            if (i, j) in ((0, 0), (0, n - 1), (n - 1, 0)):
                continue
            r, c = pos[i], pos[j]
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    setf(c + dc, r + dr, 1 if max(abs(dr), abs(dc)) != 1 else 0)
    # 预留格式区(值稍后由 draw_format 填)
    for i in range(9):
        if not fn[i][8]:
            setf(8, i, 0)
        if not fn[8][i]:
            setf(i, 8, 0)
    for i in range(8):
        setf(8, size - 1 - i, 0)
        setf(size - 1 - i, 8, 0)
    setf(8, size - 8, 1)  # dark module
    # 版本信息(v>=7)
    if version >= 7:
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        vbits = (version << 12) | rem
        for i in range(18):
            bit = (vbits >> i) & 1
            a, b = size - 11 + i % 3, i // 3
            setf(a, b, bit); setf(b, a, bit)
    # 数据放置(zigzag,从右下起,跳过 col6 timing)
    i = 0
    total_data_bits = len(codewords) * 8
    right = size - 1
    while right > 0:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = (size - 1 - vert) if upward else vert
                if not fn[row][col]:
                    bit = (codewords[i >> 3] >> (7 - (i & 7))) & 1 if i < total_data_bits else 0
                    m[row][col] = bit
                    i += 1
        right -= 2
    return m, fn


def _mask_cond(msk, r, c):
    if msk == 0: return (r + c) % 2 == 0
    if msk == 1: return r % 2 == 0
    if msk == 2: return c % 3 == 0
    if msk == 3: return (r + c) % 3 == 0
    if msk == 4: return (r // 2 + c // 3) % 2 == 0
    if msk == 5: return (r * c) % 2 + (r * c) % 3 == 0
    if msk == 6: return ((r * c) % 2 + (r * c) % 3) % 2 == 0
    return ((r + c) % 2 + (r * c) % 3) % 2 == 0


def apply_mask(m, fn, msk):
    for r in range(len(m)):
        for c in range(len(m)):
            if not fn[r][c] and _mask_cond(msk, r, c):
                m[r][c] ^= 1


def draw_format(m, msk):
    """写两处格式信息(ECC level L + 掩码号,带 BCH 纠错)。"""
    size = len(m)
    data = (1 << 3) | msk           # L 的 format bits = 0b01
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = ((data << 10) | (rem & 0x3FF)) ^ 0x5412
    gb = lambda i: (bits >> i) & 1
    for i in range(6):
        m[i][8] = gb(i)
    m[7][8] = gb(6); m[8][8] = gb(7); m[8][7] = gb(8)
    for i in range(9, 15):
        m[8][14 - i] = gb(i)
    for i in range(8):
        m[8][size - 1 - i] = gb(i)          # 横条:row8, cols size-1..size-8
    for i in range(8, 15):
        m[size - 15 + i][8] = gb(i)         # 竖条:col8, rows size-7..size-1


def penalty(m):
    """标准四条惩罚规则,用于择优掩码(分越低越好)。"""
    size = len(m)
    score = 0
    for line in (m, list(zip(*m))):        # 行 + 列
        for row in line:
            run = 1
            for i in range(1, size):
                if row[i] == row[i - 1]:
                    run += 1
                else:
                    if run >= 5:
                        score += 3 + (run - 5)
                    run = 1
            if run >= 5:
                score += 3 + (run - 5)
    for r in range(size - 1):              # 2x2 同色块
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m)      # 黑占比偏离 50%
    ratio = dark * 100 // (size * size)
    score += (abs(ratio - 50) // 5) * 10
    return score


def make(text):
    """生成最终矩阵(择优掩码 + 写格式信息)。返回 (version, matrix)。"""
    version, cw = encode_codewords(text)
    m, fn = build_matrix(version, cw)
    best, best_score = None, None
    for msk in range(8):
        apply_mask(m, fn, msk)
        draw_format(m, msk)
        s = penalty(m)
        if best_score is None or s < best_score:
            best_score, best = s, [row[:] for row in m]
        apply_mask(m, fn, msk)             # 异或复原,试下一个掩码
    return version, best


def render(m, quiet=4):
    """用半块字符把矩阵渲染成终端字符串(两行模块压一行,接近正方形)。"""
    size = len(m)

    def dark(r, c):
        return m[r][c] if (0 <= r < size and 0 <= c < size) else 0

    lines = []
    for r in range(-quiet, size + quiet, 2):
        s = ""
        for c in range(-quiet, size + quiet):
            top, bot = dark(r, c), dark(r + 1, c)
            s += "█" if top and bot else "▀" if top else "▄" if bot else " "
        lines.append(s)
    return "\n".join(lines)


def terminal_qr(text, quiet=4):
    """便捷入口:文本 -> 终端可打印的二维码字符串。"""
    _, m = make(text)
    return render(m, quiet)
