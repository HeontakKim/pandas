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
import shutil
import sqlite3
import subprocess
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


UPSERT_SQL = """
    INSERT INTO violations
        (scope, check_type, cell_file, cell_line, count, first_fs, last_fs)
    VALUES (?,?,?,?,?,?,?)
    ON CONFLICT(scope, check_type, cell_file, cell_line) DO UPDATE SET
        count    = count + excluded.count,
        first_fs = MIN(IFNULL(first_fs, excluded.first_fs),
                       IFNULL(excluded.first_fs, first_fs)),
        last_fs  = MAX(IFNULL(last_fs,  excluded.last_fs),
                       IFNULL(excluded.last_fs,  last_fs))
"""


def open_db(path, fresh=False):
    """요약 DB 를 연다. fresh=True 면 새로 만든다."""
    if fresh and os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = OFF")
    conn.execute("PRAGMA synchronous = OFF")
    conn.execute("PRAGMA cache_size = -131072")  # 128MiB
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS violations (
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
    return conn


def load(args):
    """
    C 판(strip_tv)이 만든 집계 TSV 를 SQLite 로 넣는다.
    여러 개를 한꺼번에 주면 전부 하나의 DB 로 병합된다. 로그 파일이 많을 때
    각 파일을 병렬로 strip 한 뒤 결과를 여기서 합치는 흐름이다.
    """
    conn = open_db(args.db, fresh=args.fresh)
    files = rows = 0
    batch = []

    for path in args.aggfile:
        with open(path, encoding="utf-8", errors="replace") as f:
            for ln in f:
                if not ln or ln[0] == "#":
                    continue
                p = ln.rstrip("\n").split("\t")
                if len(p) != 7:
                    continue
                try:
                    batch.append((
                        p[0], p[1], p[2], int(p[3]), int(p[4]),
                        None if p[5] == "-1" else int(p[5]),
                        None if p[6] == "-1" else int(p[6]),
                    ))
                except ValueError:
                    continue
                rows += 1
                if len(batch) >= 50000:
                    conn.executemany(UPSERT_SQL, batch)
                    batch.clear()
        files += 1

    if batch:
        conn.executemany(UPSERT_SQL, batch)
    conn.commit()

    tot, uniq, scopes = conn.execute(
        "SELECT IFNULL(SUM(count),0), COUNT(*), COUNT(DISTINCT scope) FROM violations"
    ).fetchone()
    conn.execute("VACUUM")
    conn.close()

    print("적재 완료: 집계파일 %d개 / %s행 -> %s" % (files, format(rows, ","), args.db))
    print("  누적 violation %s 건 / 고유 위치 %s 곳 / scope %s 개"
          % (format(tot, ","), format(uniq, ","), format(scopes, ",")))


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

        self.conn = open_db(db_path, fresh=True)

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
        self.conn.executemany(UPSERT_SQL, rows)
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


def _strip_python_file(src, dst, store, args):
    """파일 하나를 파이썬으로 처리한다. (nbytes, nline, kept) 를 돌려준다."""
    t0 = time.time()
    nbytes = 0
    nline = 0
    kept = 0

    # ---- 진행률 표시 ---------------------------------------------------
    # stderr 가 터미널이면 한 줄을 '\r' 로 덮어써서 갱신하고, 파일로
    # 리다이렉트했으면 줄바꿈으로 쌓는다. (로그 파일에 '\r' 이 섞이면 지저분함)
    total_bytes = os.path.getsize(src)
    eol = "\r" if sys.stderr.isatty() else "\n"
    tag = os.path.basename(src)[:20]

    def report(final=False):
        el = time.time() - t0
        rate = nbytes / el if el else 0.0
        pct = nbytes * 100.0 / total_bytes if total_bytes else 0.0
        eta = (total_bytes - nbytes) / rate if rate else float("inf")
        sys.stderr.write(
            "%-20s [%5.1f%%] %13s lines %11s viol %9s %6.1f MB/s ETA %s   %s"
            % (tag, pct, format(nline, ","), format(store.total, ","),
               _human(nbytes), rate / 1e6, _hms(eta),
               "\n" if final else eol)
        )
        sys.stderr.flush()

    # 임계값에 도달했을 때만 report() 를 부른다. 비활성화 시 inf 를 넣어두면
    # 루프 안에 별도의 'if enabled' 분기를 두지 않아도 된다.
    INF = float("inf")
    every_line = INF if args.no_progress else args.progress_lines
    every_viol = INF if args.no_progress else args.progress_violations
    line_mark = every_line
    viol_mark = every_viol

    fin = open(src, "rb", buffering=IO_BUF)
    fout = open(dst, "wb", buffering=IO_BUF)
    write = fout.write
    nxt = fin.readline

    try:
        line = nxt()
        while line:
            nbytes += len(line)
            nline += 1

            # ---- fast path -----------------------------------------------
            # 전체 줄의 99% 이상은 violation 이 아니다. bytes slice 비교는
            # C 레벨이라 여기서 대부분의 줄이 즉시 통과한다.
            if line[:8] != HEADER_PREFIX or HEADER_KEY not in line:
                write(line)
                kept += 1
                if nline >= line_mark:          # 나눗셈(%)보다 비교가 싸다
                    line_mark += every_line
                    report()
                line = nxt()
                continue

            if nline >= line_mark:
                line_mark += every_line
                report()

            # ---- violation 블록 후보 --------------------------------------
            block = []
            check = cfile = cline = scope = None
            time_fs = None
            nextline = b""

            for _ in range(MAX_BLOCK_LOOKAHEAD):
                cur = nxt()
                if not cur:
                    break

                # 다음 violation 헤더나 빈 줄을 만나면 블록 종료.
                # 이 줄은 nextline 으로 되돌려져 루프 상단에서 다시 처리되므로
                # 여기서 세면 이중 계산이 된다. (모든 줄은 정확히 한 번만 센다)
                if _blank(cur) or (cur[:8] == HEADER_PREFIX and HEADER_KEY in cur):
                    nextline = cur
                    break

                nbytes += len(cur)
                nline += 1
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
                        # Time 은 블록의 마지막 줄이다. 그 다음 줄은 되돌려
                        # 보내므로 여기서 세지 않는다.
                        nextline = nxt()
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

            if store.total >= viol_mark:
                viol_mark += every_viol
                report()

            # 블록 뒤의 빈 줄도 같이 제거한다 (--keep-blank 로 유지 가능)
            if not args.keep_blank:
                n = 0
                while nextline and _blank(nextline) and n < MAX_TRAILING_BLANK:
                    # 이 빈 줄은 여기서 버려지므로(되돌아가지 않으므로) 센다
                    nbytes += len(nextline)
                    nline += 1
                    nextline = nxt()
                    n += 1

            line = nextline if nextline else nxt()
    finally:
        fout.close()
        fin.close()

    if not args.no_progress:
        report(final=True)   # 마지막 진행률을 확정 출력하고 '\r' 줄을 닫는다

    return nbytes, nline, kept


# ---------------------------------------------------------------------------
# C 엔진 연동
# ---------------------------------------------------------------------------
C_EXE = "strip_tv"
C_SRC = "strip_timing_violations.c"


def find_or_build_c(quiet=False):
    """
    C 실행파일을 찾고, 없거나 소스보다 오래됐으면 컴파일한다.
    컴파일러가 없거나 실패하면 None 을 돌려준다(파이썬으로 폴백).
    """
    here = os.path.dirname(os.path.abspath(__file__))
    exe = os.path.join(here, C_EXE)
    src = os.path.join(here, C_SRC)

    if os.path.exists(exe):
        if not os.path.exists(src) or os.path.getmtime(exe) >= os.path.getmtime(src):
            return exe
    if not os.path.exists(src):
        return None

    for cc in ("cc", "gcc", "clang"):
        if not shutil.which(cc):
            continue
        if not quiet:
            sys.stderr.write("[빌드] %s -O2 -o %s\n" % (cc, C_EXE))
        r = subprocess.run([cc, "-O2", "-o", exe, src],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if r.returncode == 0:
            return exe
        sys.stderr.write("[빌드 실패] %s\n" % r.stderr.decode("utf-8", "replace")[:500])
        return None
    return None


def _strip_with_c(args, exe):
    """C 엔진으로 전부 처리한 뒤 집계를 DB 로 적재한다."""
    jobs = []
    for src in args.logfile:
        dst = args.out or (os.path.splitext(src)[0] + ".clean.log")
        agg = src + ".agg"
        if os.path.abspath(src) == os.path.abspath(dst):
            sys.exit("error: 입력과 출력 경로가 같습니다: %s" % src)
        jobs.append((src, dst, agg))

    cmdbase = [exe]
    if args.no_progress:
        cmdbase.append("--no-progress")
    if args.keep_blank:
        cmdbase.append("--keep-blank")
    cmdbase += ["--progress-lines", str(args.progress_lines),
                "--progress-violations", str(args.progress_violations),
                "--warn-bytes", "0"]
    # C 쪽 경고는 끈다(0). 파일마다 찍으면 마지막 요약에 묻히므로,
    # 아래에서 파이썬이 전체를 모아 맨 끝에 한 번만 출력한다.

    t0 = time.time()
    par = max(1, args.jobs)
    running = []
    pending = list(jobs)
    failed = []

    # 파일 여러 개를 코어 수만큼 동시에 돌린다.
    while pending or running:
        while pending and len(running) < par:
            src, dst, agg = pending.pop(0)
            p = subprocess.Popen(cmdbase + [src, "-o", dst, "-a", agg])
            running.append((p, src))
        p, src = running.pop(0)
        rc = p.wait()
        if rc != 0:
            failed.append(src)

    if failed:
        sys.exit("error: C 엔진 처리 실패: %s" % ", ".join(failed))

    scan_dt = time.time() - t0

    # 집계를 DB 로 적재
    class _A:
        pass
    la = _A()
    la.aggfile = [j[2] for j in jobs]
    la.db = args.db
    la.fresh = True
    load(la)
    sys.stdout.flush()   # load() 는 stdout, 아래 요약은 stderr -> 순서 보장

    if not args.keep_agg:
        for _, _, agg in jobs:
            try:
                os.remove(agg)
            except OSError:
                pass

    total_in = sum(os.path.getsize(s) for s, _, _ in jobs)
    outs = [d for _, d, _ in jobs]
    total_out = sum(os.path.getsize(d) for d in outs if os.path.exists(d))
    sys.stderr.write(
        "\n[strip 완료 / C 엔진] 스캔 %.1fs (%.1f MB/s), 전체 %.1fs\n"
        "  입력      : %d개 파일 %s\n"
        "  정리 로그 : %-28s %s\n"
        "  요약 DB   : %-28s %s\n"
        % (scan_dt, total_in / 1e6 / scan_dt if scan_dt else 0,
           time.time() - t0,
           len(jobs), _human(total_in),
           ", ".join(os.path.basename(d) for d in outs[:3])
           + (" ..." if len(outs) > 3 else ""), _human(total_out),
           args.db, _human(os.path.getsize(args.db)))
    )
    if not args.no_detail:
        sys.stderr.write(
            "  참고: C 엔진은 gzip 상세 레코드를 만들지 않습니다.\n"
            "        개별 타임스탬프까지 필요하면 --engine python 을 쓰세요.\n"
        )
    warn_big_clean(outs, args.warn_bytes)


def strip(args):
    """
    엔진을 고르고 전체 흐름을 한 번에 돌린다.
      auto   : C 가 쓸 수 있으면 C, 아니면 파이썬
      c      : C 강제 (없으면 오류)
      python : 파이썬 강제 (gzip 상세 레코드까지 생성)
    """
    exe = None
    if args.engine in ("auto", "c"):
        exe = find_or_build_c(quiet=args.no_progress)
        if exe is None and args.engine == "c":
            sys.exit("error: C 엔진을 쓸 수 없습니다 (컴파일러 또는 %s 없음)" % C_SRC)
    if exe is not None:
        return _strip_with_c(args, exe)

    if args.engine == "auto":
        sys.stderr.write("[안내] C 엔진을 쓸 수 없어 파이썬으로 처리합니다 (10배 이상 느림).\n")

    if args.out and len(args.logfile) > 1:
        sys.exit("error: --out 은 입력이 하나일 때만 쓸 수 있습니다.")

    store = ViolationStore(
        args.db, args.detail,
        compresslevel=args.compresslevel,
        keep_detail=not args.no_detail,
    )
    t0 = time.time()
    tb = tl = tk = 0
    outs = []
    try:
        for src in args.logfile:
            dst = args.out or (os.path.splitext(src)[0] + ".clean.log")
            if os.path.abspath(src) == os.path.abspath(dst):
                sys.exit("error: 입력과 출력 경로가 같습니다: %s" % src)
            b, l, k = _strip_python_file(src, dst, store, args)
            tb += b; tl += l; tk += k
            outs.append(dst)
    finally:
        store.close()

    dt = time.time() - t0
    sys.stderr.write(
        "\n[strip 완료 / 파이썬 엔진] %.1fs (%.1f MB/s)\n"
        "  입력      : %d개 파일 %s\n"
        "  요약 DB   : %-28s %s\n"
        "  상세(gz)  : %-28s %s\n"
        "  violation : %s 건  (남긴 줄 %s)\n"
        % (dt, tb / 1e6 / dt if dt else 0,
           len(args.logfile), _human(tb),
           args.db, _human(os.path.getsize(args.db)),
           args.detail if not args.no_detail else "-",
           _human(os.path.getsize(args.detail)) if not args.no_detail else "-",
           format(store.total, ","), format(tk, ","))
    )
    if store.unparsed:
        sys.stderr.write(
            "  주의: 형식이 다른 violation 후보 %d 건은 원본에 그대로 남겼습니다.\n"
            % store.unparsed
        )
    warn_big_clean(outs, args.warn_bytes)


def _human(n):
    for u in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or u == "TB":
            return "%.1f %s" % (n, u)
        n /= 1024.0


def warn_big_clean(paths, warn_bytes):
    """
    timing violation 을 전부 걷어냈는데도 정리된 로그가 여전히 크면 알려준다.
    그대로 두면 "왜 아직도 크지?" 하고 헤매게 되므로, 무엇을 확인해야 할지까지
    같이 알려준다.
    """
    big = []
    for p in paths:
        try:
            n = os.path.getsize(p)
        except OSError:
            continue
        if n >= warn_bytes:
            big.append((p, n))
    if not big:
        return

    sys.stderr.write(
        "\n  [경고] timing violation 을 제거한 뒤에도 정리된 로그가 큽니다:\n")
    for p, n in big:
        sys.stderr.write("         %-40s %s\n" % (p, _human(n)))
    sys.stderr.write(
        "         timing violation 외에 로그를 키우는 메시지가 더 있습니다.\n"
        "         (테스트벤치 $display/$monitor, UVM_INFO, SDF annotation 경고,\n"
        "          assertion 메시지 등이 흔한 원인입니다)\n"
        "         어떤 메시지가 반복되는지 앞부분만 표본으로 확인해 보세요\n"
        "         (숫자를 # 로 바꿔 같은 종류의 메시지를 묶습니다):\n"
        "           head -n 2000000 %s \\\n"
        "             | sed 's/[0-9][0-9]*/#/g' | cut -c1-60 \\\n"
        "             | sort | uniq -c | sort -rn | head -20\n"
        % big[0][0]
    )


def _hms(sec):
    if sec < 0 or sec != sec or sec == float("inf"):
        return "--:--:--"
    sec = int(sec)
    return "%02d:%02d:%02d" % (sec // 3600, sec % 3600 // 60, sec % 60)


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

    s = sub.add_parser("strip", help="로그에서 violation 제거 + 저장 (C 엔진 자동 사용)")
    s.add_argument("logfile", nargs="+", help="sim.log (여러 개 지정 가능)")
    s.add_argument("--engine", choices=("auto", "c", "python"), default="auto",
                   help="auto=C 우선(기본), c=C 강제, python=파이썬 강제")
    s.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                   metavar="N", help="C 엔진에서 동시 처리할 파일 수 (기본: 코어 수)")
    s.add_argument("--keep-agg", action="store_true",
                   help="C 엔진의 중간 집계 파일(.agg)을 지우지 않음")
    s.add_argument("--out", help="정리된 로그 (기본: <입력>.clean.log, 입력 1개일 때만)")
    s.add_argument("--db", default="violations.db", help="SQLite 요약 DB")
    s.add_argument("--detail", default="violations.tsv.gz", help="gzip 상세 레코드")
    s.add_argument("--no-detail", action="store_true",
                   help="상세 레코드를 남기지 않고 요약 DB 만 생성 (가장 작음/빠름)")
    s.add_argument("--compresslevel", type=int, default=6,
                   help="gzip 압축 레벨 1(빠름)~9(작음), 기본 6")
    s.add_argument("--keep-blank", action="store_true",
                   help="violation 블록 뒤의 빈 줄을 지우지 않음")
    s.add_argument("--progress-lines", type=int, default=1_000_000,
                   metavar="N", help="N 줄마다 진행률 출력 (기본: 1000000)")
    s.add_argument("--progress-violations", type=int, default=1000,
                   metavar="N", help="violation N 건마다 진행률 출력 (기본: 1000)")
    s.add_argument("--no-progress", action="store_true", help="진행률 출력 끄기")
    s.add_argument("--warn-bytes", type=int, default=1 << 30, metavar="N",
                   help="정리된 로그가 N 바이트 이상이면 경고 (기본: 1GiB)")
    s.set_defaults(func=strip)

    ld = sub.add_parser("load", help="C 판이 만든 집계 TSV 를 DB 로 적재/병합")
    ld.add_argument("aggfile", nargs="+", help="집계 TSV (여러 개 지정 시 병합)")
    ld.add_argument("--db", default="violations.db")
    ld.add_argument("--fresh", action="store_true", help="기존 DB 를 지우고 새로 만듦")
    ld.set_defaults(func=load)

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
