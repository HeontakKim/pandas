#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strip_timing_violations.py

SDF annotation 시뮬레이션 로그(sim.log)에서 Timing violation 블록을 제거하고,
그 정보를 압축/색인된 형태로 따로 저장한다.

동작 (한 번의 streaming pass로 모두 처리):
  1) sim.log  ->  sim.clean.log        : timing violation 블록이 제거된 로그
  2)            ->  violations.tsv.gz  : 전체 violation 원본 레코드 (gzip, 무손실)
  3)            ->  violations.db      : Scope 로 색인된 SQLite 요약 DB (집계 = 압축)

사용법:
  # 추출
  python3 strip_timing_violations.py strip sim.log

  # Scope 검색 (하위 계층까지 포함)
  python3 strip_timing_violations.py query top.dut.core_a
  python3 strip_timing_violations.py query top.dut.core_a --exact
  python3 strip_timing_violations.py query top.dut --detail
  python3 strip_timing_violations.py top-scopes -n 20

메모리 사용량은 입력 파일 크기와 무관하며(스트리밍 + 주기적 flush),
2GB 이상의 로그도 상수 메모리로 처리한다.
"""

import argparse
import gzip
import io
import os
import re
import sqlite3
import sys
import time

# --------------------------------------------------------------------------
# violation 블록 포맷
#
#   Warning!  Timing violation
#              $setuphold<setup>( posedge CK:... );
#       \t    File: /path/cell.v, line = 1749      <- ", line = N" 은 없을 수 있음
#       \t   Scope: top.dut.core_a.reg_4_
#       \t    Time: 2048519871999 FS
#   (빈 줄 몇 개)
#
# 모든 처리는 bytes 로 수행한다. 2GB 로그를 str 로 디코딩하면 느릴 뿐 아니라
# 로그에 섞인 깨진 바이트에서 UnicodeDecodeError 가 날 수 있기 때문이다.
# --------------------------------------------------------------------------

HEADER_PREFIX = b"Warning!"
HEADER_KEY = b"Timing violation"

RE_CHECK = re.compile(rb"\$(\w+(?:<\w+>)?)")
RE_FILE = re.compile(rb"File:\s*(\S+?)\s*(?:,\s*line\s*=\s*(\d+)\s*)?$")
RE_SCOPE = re.compile(rb"Scope:\s*(\S+)")
RE_TIME = re.compile(rb"Time:\s*([-+]?[\d.]+)\s*([A-Za-z]*)")

# violation 헤더 뒤에서 File/Scope/Time 을 찾기 위해 살펴볼 최대 줄 수.
# 정상 블록은 4줄이므로 여유를 둔 값이다.
MAX_BLOCK_LOOKAHEAD = 8
# 블록 뒤에 따라오는 빈 줄을 최대 몇 개까지 같이 제거할지.
MAX_TRAILING_BLANK = 2

# 시간 단위 -> femtosecond 배율. 서로 다른 단위(FS/PS)가 섞여 나와도
# 비교·정렬이 가능하도록 fs 로 정규화해서 저장한다.
TIME_UNIT_FS = {
    b"": 1,
    b"FS": 1,
    b"PS": 10 ** 3,
    b"NS": 10 ** 6,
    b"US": 10 ** 9,
    b"MS": 10 ** 12,
    b"S": 10 ** 15,
}

# 메모리 상 집계 dict 가 이 크기를 넘으면 DB 로 flush 한다.
# 상수 메모리 보장을 위한 값이며, 크게 잡을수록 DB 쓰기 횟수가 준다.
FLUSH_EVERY = 500_000

IO_BUF = 1 << 22  # 4MiB


def _blank(line):
    return not line.strip()


def _parse_time_fs(raw_value, raw_unit):
    """'2048519871999', b'FS' -> femtosecond 정수."""
    mult = TIME_UNIT_FS.get(raw_unit.upper())
    if mult is None:
        return None
    try:
        if b"." in raw_value:
            return int(float(raw_value) * mult)
        return int(raw_value) * mult
    except ValueError:
        return None


class ViolationStore:
    """
    violation 을 두 가지 형태로 저장한다.

      - detail : violations.tsv.gz  (모든 레코드, gzip 무손실 압축)
      - index  : violations.db      (SQLite, Scope 로 색인된 집계)

    집계 키는 (scope, check, file, line) 이다. 동일 셀에서 반복적으로 터지는
    violation 이 수만~수백만 건이어도 DB 에는 한 행 + count 로만 남으므로
    이것이 사실상의 압축 역할을 한다.
    """

    def __init__(self, db_path, detail_path, compresslevel=6, keep_detail=True):
        self.db_path = db_path
        self.detail_path = detail_path
        self.keep_detail = keep_detail

        if os.path.exists(db_path):
            os.remove(db_path)
        self.conn = sqlite3.connect(db_path)
        self.conn.execute("PRAGMA journal_mode = OFF")
        self.conn.execute("PRAGMA synchronous = OFF")
        self.conn.execute("PRAGMA cache_size = -131072")  # 128MiB
        self.conn.executescript(
            """
            CREATE TABLE violations (
                scope       TEXT    NOT NULL,
                check_type  TEXT    NOT NULL,
                cell_file   TEXT    NOT NULL,
                cell_line   INTEGER NOT NULL,   -- 없으면 -1
                count       INTEGER NOT NULL,
                first_fs    INTEGER,
                last_fs     INTEGER,
                PRIMARY KEY (scope, check_type, cell_file, cell_line)
            ) WITHOUT ROWID;
            """
        )
        # PK 의 선두 컬럼이 scope 이므로 scope 정확검색/prefix 검색이
        # 그대로 인덱스를 탄다. 별도 인덱스는 필요 없다.

        self.pending = {}  # key -> [count, min_fs, max_fs]
        self.total = 0
        self.unparsed = 0

        if keep_detail:
            if os.path.exists(detail_path):
                os.remove(detail_path)
            self._raw = gzip.open(detail_path, "wb", compresslevel=compresslevel)
            self.detail = io.BufferedWriter(self._raw, IO_BUF)
            self.detail.write(b"#scope\tcheck\tfile\tline\ttime_fs\n")
        else:
            self._raw = None
            self.detail = None

    def add(self, scope, check, cfile, cline, time_fs):
        self.total += 1

        if self.detail is not None:
            self.detail.write(
                b"%s\t%s\t%s\t%s\t%s\n"
                % (
                    scope,
                    check,
                    cfile,
                    cline if cline is not None else b"",
                    b"%d" % time_fs if time_fs is not None else b"",
                )
            )

        key = (scope, check, cfile, int(cline) if cline is not None else -1)
        slot = self.pending.get(key)
        if slot is None:
            self.pending[key] = [1, time_fs, time_fs]
            if len(self.pending) >= FLUSH_EVERY:
                self.flush()
        else:
            slot[0] += 1
            if time_fs is not None:
                if slot[1] is None or time_fs < slot[1]:
                    slot[1] = time_fs
                if slot[2] is None or time_fs > slot[2]:
                    slot[2] = time_fs

    def flush(self):
        if not self.pending:
            return
        rows = [
            (
                k[0].decode("utf-8", "replace"),
                k[1].decode("utf-8", "replace"),
                k[2].decode("utf-8", "replace"),
                k[3],
                v[0],
                v[1],
                v[2],
            )
            for k, v in self.pending.items()
        ]
        self.conn.executemany(
            """
            INSERT INTO violations
                (scope, check_type, cell_file, cell_line, count, first_fs, last_fs)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(scope, check_type, cell_file, cell_line) DO UPDATE SET
                count    = count + excluded.count,
                first_fs = MIN(IFNULL(first_fs, excluded.first_fs),
                               IFNULL(excluded.first_fs, first_fs)),
                last_fs  = MAX(IFNULL(last_fs,  excluded.last_fs),
                               IFNULL(excluded.last_fs,  last_fs))
            """,
            rows,
        )
        self.conn.commit()
        self.pending.clear()

    def close(self):
        self.flush()
        self.conn.commit()
        self.conn.execute("VACUUM")
        self.conn.close()
        if self.detail is not None:
            self.detail.flush()
            self.detail.close()
            self._raw.close()


def strip(args):
    src = args.logfile
    dst = args.out or (os.path.splitext(src)[0] + ".clean.log")
    if os.path.abspath(src) == os.path.abspath(dst):
        sys.exit("error: 입력과 출력 경로가 같습니다. --out 으로 다른 경로를 지정하세요.")

    store = ViolationStore(
        args.db,
        args.detail,
        compresslevel=args.compresslevel,
        keep_detail=not args.no_detail,
    )

    t0 = time.time()
    nbytes = 0
    kept = 0

    fin = open(src, "rb", buffering=IO_BUF)
    fout = open(dst, "wb", buffering=IO_BUF)
    write = fout.write
    nxt = fin.readline

    try:
        line = nxt()
        while line:
            nbytes += len(line)

            # ---- fast path -----------------------------------------------
            # 전체 줄의 99% 이상은 violation 이 아니다. bytes slice 비교는
            # C 레벨이라 여기서 대부분의 줄이 즉시 통과한다.
            if line[:8] != HEADER_PREFIX or HEADER_KEY not in line:
                write(line)
                kept += 1
                line = nxt()
                continue

            # ---- violation 블록 후보 --------------------------------------
            block = []
            check = cfile = cline = scope = None
            time_fs = None
            nextline = b""

            for _ in range(MAX_BLOCK_LOOKAHEAD):
                cur = nxt()
                if not cur:
                    break
                nbytes += len(cur)

                # 다음 violation 헤더나 빈 줄을 만나면 블록 종료
                if _blank(cur) or (cur[:8] == HEADER_PREFIX and HEADER_KEY in cur):
                    nextline = cur
                    break

                block.append(cur)

                if scope is None:
                    m = RE_SCOPE.search(cur)
                    if m:
                        scope = m.group(1)
                        continue
                if cfile is None:
                    m = RE_FILE.search(cur.rstrip())
                    if m:
                        cfile, cline = m.group(1), m.group(2)
                        continue
                if time_fs is None and b"Time:" in cur:
                    m = RE_TIME.search(cur)
                    if m:
                        time_fs = _parse_time_fs(m.group(1), m.group(2))
                        # Time 은 블록의 마지막 줄이다.
                        nextline = nxt()
                        if nextline:
                            nbytes += len(nextline)
                        break
                if check is None:
                    m = RE_CHECK.search(cur)
                    if m:
                        check = m.group(1)

            if scope is None:
                # 예상한 형식이 아니다 -> 안전을 위해 원본 그대로 보존한다.
                # (파싱 실패로 진짜 로그를 잃어버리는 일이 없도록)
                write(line)
                for b in block:
                    write(b)
                kept += 1 + len(block)
                store.unparsed += 1
                line = nextline if nextline else nxt()
                continue

            store.add(
                scope,
                check if check is not None else b"?",
                cfile if cfile is not None else b"?",
                cline,
                time_fs,
            )

            # 블록 뒤의 빈 줄도 같이 제거한다 (--keep-blank 로 유지 가능)
            if not args.keep_blank:
                n = 0
                while nextline and _blank(nextline) and n < MAX_TRAILING_BLANK:
                    nextline = nxt()
                    if nextline:
                        nbytes += len(nextline)
                    n += 1

            line = nextline if nextline else nxt()
    finally:
        fout.close()
        fin.close()
        store.close()

    dt = time.time() - t0
    out_size = os.path.getsize(dst)
    db_size = os.path.getsize(args.db)
    det_size = os.path.getsize(args.detail) if not args.no_detail else 0

    sys.stderr.write(
        "\n[strip 완료] %.1fs (%.1f MB/s)\n"
        "  입력      : %-28s %12s\n"
        "  정리 로그 : %-28s %12s\n"
        "  요약 DB   : %-28s %12s\n"
        "  상세(gz)  : %-28s %12s\n"
        "  violation : %d 건  (남긴 줄 %d)\n"
        % (
            dt,
            nbytes / 1e6 / dt if dt else 0,
            src, _human(nbytes),
            dst, _human(out_size),
            args.db, _human(db_size),
            args.detail if not args.no_detail else "-", _human(det_size),
            store.total, kept,
        )
    )
    if store.unparsed:
        sys.stderr.write(
            "  주의: 형식이 다른 violation 후보 %d 건은 원본에 그대로 남겼습니다.\n"
            % store.unparsed
        )


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def _fs(v):
    if v is None:
        return "-"
    return "%d fs" % v


def query(args):
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    if args.exact:
        where, params = "scope = ?", (args.scope,)
    else:
        # prefix 검색이지만 LIKE 대신 범위 조건을 써서 PK 인덱스를 그대로 탄다.
        # scope 자기 자신 + 그 하위 계층(top.dut.core_a.*)만 잡는다.
        pre = args.scope + "."
        where = "(scope = ? OR (scope >= ? AND scope < ?))"
        params = (args.scope, pre, pre[:-1] + chr(ord(".") + 1))

    total = conn.execute(
        "SELECT IFNULL(SUM(count),0) c, COUNT(*) g, MIN(first_fs) f, MAX(last_fs) l "
        "FROM violations WHERE " + where, params
    ).fetchone()

    if total["c"] == 0:
        print("'%s' 계층에서 timing violation 이 없습니다." % args.scope)
        return

    print("Scope   : %s%s" % (args.scope, "" if args.exact else ".*  (하위 포함)"))
    print("총 건수 : %d 건 / 고유 위치 %d 곳" % (total["c"], total["g"]))
    print("시간범위: %s ~ %s" % (_fs(total["f"]), _fs(total["l"])))
    print("-" * 100)

    if args.detail:
        sql = ("SELECT scope, check_type, cell_file, cell_line, count, first_fs, last_fs "
               "FROM violations WHERE " + where + " ORDER BY count DESC LIMIT ?")
        print("%-46s %-18s %10s  %s" % ("SCOPE", "CHECK", "COUNT", "FILE:LINE"))
        for r in conn.execute(sql, params + (args.limit,)):
            loc = r["cell_file"] + ("" if r["cell_line"] < 0 else ":%d" % r["cell_line"])
            print("%-46s %-18s %10d  %s" % (r["scope"], r["check_type"], r["count"], loc))
    else:
        sql = ("SELECT scope, SUM(count) c, MIN(first_fs) f, MAX(last_fs) l "
               "FROM violations WHERE " + where +
               " GROUP BY scope ORDER BY c DESC LIMIT ?")
        print("%-56s %10s  %s" % ("SCOPE", "COUNT", "FIRST ~ LAST"))
        for r in conn.execute(sql, params + (args.limit,)):
            print("%-56s %10d  %s ~ %s" % (r["scope"], r["c"], _fs(r["f"]), _fs(r["l"])))
    conn.close()


def top_scopes(args):
    conn = sqlite3.connect(args.db)
    print("%-56s %10s  %s" % ("SCOPE", "COUNT", "CHECKS"))
    for scope, c, chk in conn.execute(
        "SELECT scope, SUM(count), GROUP_CONCAT(DISTINCT check_type) "
        "FROM violations GROUP BY scope ORDER BY SUM(count) DESC LIMIT ?",
        (args.n,),
    ):
        print("%-56s %10d  %s" % (scope, c, chk))
    conn.close()


def main():
    p = argparse.ArgumentParser(
        description="sim.log 에서 timing violation 을 분리/압축/색인한다.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("strip", help="로그에서 violation 제거 + 저장")
    s.add_argument("logfile")
    s.add_argument("--out", help="정리된 로그 (기본: <입력>.clean.log)")
    s.add_argument("--db", default="violations.db", help="SQLite 요약 DB")
    s.add_argument("--detail", default="violations.tsv.gz", help="gzip 상세 레코드")
    s.add_argument("--no-detail", action="store_true",
                   help="상세 레코드를 남기지 않고 요약 DB 만 생성 (가장 작음/빠름)")
    s.add_argument("--compresslevel", type=int, default=6,
                   help="gzip 압축 레벨 1(빠름)~9(작음), 기본 6")
    s.add_argument("--keep-blank", action="store_true",
                   help="violation 블록 뒤의 빈 줄을 지우지 않음")
    s.set_defaults(func=strip)

    q = sub.add_parser("query", help="Scope 로 검색")
    q.add_argument("scope")
    q.add_argument("--db", default="violations.db")
    q.add_argument("--exact", action="store_true", help="하위 계층 제외, 정확히 일치")
    q.add_argument("--detail", action="store_true", help="check/파일 단위까지 표시")
    q.add_argument("--limit", type=int, default=50)
    q.set_defaults(func=query)

    t = sub.add_parser("top-scopes", help="violation 이 많은 scope 순위")
    t.add_argument("-n", type=int, default=20)
    t.add_argument("--db", default="violations.db")
    t.set_defaults(func=top_scopes)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
