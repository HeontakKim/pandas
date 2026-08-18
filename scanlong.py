#!/usr/bin/env python3
"""
긴 줄을 찾아 '그 안에 뭐가 들었는지'까지 알려준다.
바이트 모드 + 스트리밍이라 1GB 짜리 줄이 있어도 메모리를 안 먹는다.

사용: python3 scanlong.py sim.clean.log [최소길이]
"""
import os, sys, collections

path = sys.argv[1]
MIN = int(sys.argv[2]) if len(sys.argv) > 2 else 10000
CHUNK = 1 << 22

def classify(hist, n, head, tail):
    nul = hist.get(0, 0)
    if nul == n:
        return "전부 NUL (0x00) — 깨진 구간. 내용 없음"
    if nul > n * 0.9:
        return "NUL %.1f%% — 대부분 깨진 구간" % (nul * 100.0 / n)
    if len(hist) == 1:
        b = next(iter(hist))
        return "같은 바이트 0x%02x 만 %s 개 반복" % (b, format(n, ","))
    if len(hist) <= 4:
        return "바이트 %d종만 반복: %s" % (
            len(hist), " ".join("0x%02x" % b for b in sorted(hist)))
    printable = sum(v for k, v in hist.items() if 32 <= k < 127 or k in (9,))
    if printable > n * 0.95:
        return "실제 텍스트 (출력가능 %.1f%%) — 내용 확인 필요" % (printable*100.0/n)
    return "혼합 (출력가능 %.1f%%, NUL %.1f%%)" % (
        printable*100.0/n, nul*100.0/n)

f = open(path, "rb")
off = 0          # 현재 줄 시작 오프셋
ln = 0           # 줄 번호
cur = 0          # 현재 줄 길이
hist = collections.Counter()
head = b""
found = tot_long = 0

def finish(has_nl):
    global found, tot_long
    if cur >= MIN:
        found += 1
        tot_long += cur
        print("줄 %-10s offset %-14s 길이 %14s (%7.2f MB)"
              % (format(ln, ","), format(off, ","), format(cur, ","), cur/1e6))
        print("   내용: %s" % classify(hist, cur, head, b""))
        print("   앞 48바이트: %s" % head[:48].hex(" "))
        try:
            print("   그대로 보면: %r" % head[:48].decode("utf-8", "replace"))
        except Exception:
            pass
        if not has_nl:
            print("   (파일 끝, 개행 없음)")
        print()

buf_off = 0
while True:
    b = f.read(CHUNK)
    if not b:
        break
    start = 0
    while True:
        i = b.find(b"\n", start)
        seg = b[start:] if i < 0 else b[start:i+1]
        if cur < 4096 and len(head) < 64:
            head += seg[:64 - len(head)]
        cur += len(seg)
        if cur < (1 << 28):          # histogram 은 256MB 까지만 (비용 절감)
            hist.update(seg)
        if i < 0:
            break
        ln += 1
        finish(True)
        off = buf_off + i + 1
        cur = 0; hist = collections.Counter(); head = b""
        start = i + 1
    buf_off += len(b)
if cur:
    ln += 1
    finish(False)
f.close()

print("-" * 70)
print("%s 바이트 중 %s 개 긴 줄(>=%s)이 %s 바이트 (%.1f%%) 차지"
      % (format(os.path.getsize(path), ","), found, format(MIN, ","),
         format(tot_long, ","), tot_long*100.0/max(1, os.path.getsize(path))))
