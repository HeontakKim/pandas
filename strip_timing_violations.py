#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
strip_timing_violations.py

SDF annotation 시뮬레이션 로그(sim.log)를 정리하고, 지워진 정보를 검색 가능한
형태로 남긴다. 한 번의 streaming pass 로 아래를 모두 처리한다.

  - Timing violation 블록(5줄) 제거 -> scope 별로 집계해서 DB 에 보관
  - (--prune-deposit 을 줬을 때만) 반복되는 xcelium deposit 줄과 PNOOBJ 에러 줄
    제거 -> 남길 정보가 없어 그냥 버림. strip 의 기본은 violation 만 지우는 것이고,
    deposit 잡음만 따로 지우려면 prune 서브커맨드를 쓴다.

산출물:
  sim.clean.log       정리된 로그
  violations.db       Scope 로 색인된 SQLite 요약 (집계가 곧 압축: 약 1/10000)
  violations.tsv.gz   전체 violation 원본 레코드 (--engine python 일 때만)

무거운 스캔은 C 판(strip_timing_violations.c)이 맡는다. strip 이 알아서 빌드해
쓰고, 컴파일러가 없으면 파이썬으로 넘어간다. 파이썬 27MB/s vs C 379MB/s.

사용법:
  # 정리 (C 자동 빌드 + 실행 + DB 적재까지 한 번에)
  python3 strip_timing_violations.py strip sim.log
  python3 strip_timing_violations.py strip *.log        # 코어 수만큼 병렬, DB 병합

  # Scope 검색 (하위 계층까지 포함)
  python3 strip_timing_violations.py query top.dut.core_a
  python3 strip_timing_violations.py query top.dut.core_a --exact
  python3 strip_timing_violations.py query top.dut --detail
  python3 strip_timing_violations.py top-scopes -n 20

  # deposit 잡음만 지우기 (timing violation 은 건드리지 않음)
  python3 strip_timing_violations.py prune sim.log --in-place
  python3 strip_timing_violations.py prune *.log --in-place   # 여러 개, 병렬

  # 사용자 규칙으로 임의의 잡음 지우기 (strip / prune 둘 다 가능)
  ... --drop-prefix 'UVM_INFO tb/env/scb.sv'  # 이 글자로 시작하는 줄 (권장)
  ... --drop '^UVM_INFO'                      # 정규식에 걸리는 줄 전부
  ... --drop-pair '^ERROR:' '^DETAIL:'        # 두 줄이 연달아 올 때만

접두사로 충분하면 --drop 보다 --drop-prefix 를 쓰는 게 좋다. 정규식이 아니라
memcmp 로 판정해서 규칙이 없을 때와 속도가 같고(800MB 기준 약 850 MB/s),
'.' 이나 '*' 가 든 접두사를 로그에서 그대로 복사해 넣어도 안전하다.
--drop 으로 같은 걸 쓰면 메타문자 때문에 리터럴 추출이 끊겨 6.6 MB/s 까지
떨어진다.

정규식은 POSIX 확장 정규식(ERE)으로 쓴다. C 엔진이 ERE 를 쓰기 때문이며,
파이썬 전용 문법(\\d, lookahead 등)을 쓰면 경고가 나온다.
'^' 뒤가 순수 리터럴인 패턴('^UVM_INFO')은 memcmp 로만 판정해서 사실상
공짜지만, '.*' 등이 섞이면 줄마다 정규식 엔진이 돌아 10배 가까이 느려진다.

메모리 사용량은 입력 파일 크기와 무관하며(스트리밍 + 주기적 flush),
10GB 이상의 로그도 상수 메모리로 처리한다. 단 한 줄이 통째로 램에 올라오므로
비정상적으로 긴 줄 하나가 그 보장을 깬다. 디스크가 가득 찬 상태로 기록된
로그에는 개행 없는 NUL 덩어리가 GB 단위로 남는 일이 있는데(write 가 부분
실패하면 파일 크기만 늘고 블록은 커밋되지 않아 그 구간이 NUL 로 읽힌다),
실측으로 300MB 짜리 줄 하나에 C 판 RSS 515MB / 파이썬 626MB 를 썼다.

  ... --max-line 1048576        1MB 넘는 줄은 버린다 (RSS 515MB -> 11MB)
  ... --max-line 1048576 --truncate-long   버리지 않고 앞 1MB 만 남긴다

기본값은 0(무제한)이라 기존 동작 그대로다. 긴 줄이 진짜 로그일 수도 있어
말없이 지우지 않는다. 대신 16MB 가 넘는 줄을 만나면 한 번 경고한다.
이미 만들어진 파일에서 NUL 만 걷어내려면 tr -d '\\0' 이 가장 빠르다.
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

# --max-line 없이도 비정상적으로 긴 줄을 만나면 한 번은 알려준다.
# 정상 로그 줄은 수백 바이트다. 이 시점엔 이미 그 줄만큼 램을 쓴 뒤라
# 늦었지만, 다음 실행 때 무엇을 줘야 하는지는 알 수 있다.
LONG_LINE_WARN = 4 << 20


# 파이썬 re 에만 있고 POSIX ERE(C 판)에는 없는 문법. 이걸 쓰면 두 엔진의
# 결과가 갈리므로 경고한다.
RE_PY_ONLY = re.compile(r"\\[dwsDWSbBAZ]|\(\?")


def compile_rules(args):
    """
    --drop / --drop-pair 를 컴파일한다.

    C 판은 POSIX 확장 정규식(ERE)을 쓰므로, 두 엔진에서 같은 결과를 얻으려면
    ERE 범위 안에서 써야 한다. 파이썬 전용 문법이 보이면 경고만 하고 진행한다
    (파이썬 엔진만 쓸 수도 있으므로 막지는 않는다).
    """
    drops, pairs = [], []
    for pat in getattr(args, "drop", None) or []:
        try:
            drops.append(re.compile(pat.encode()))
        except re.error as e:
            sys.exit("error: --drop 정규식이 잘못되었습니다: %s (%s)" % (pat, e))
    for a, b in getattr(args, "drop_pair", None) or []:
        try:
            pairs.append((re.compile(a.encode()), re.compile(b.encode())))
        except re.error as e:
            sys.exit("error: --drop-pair 정규식이 잘못되었습니다: %s / %s (%s)"
                     % (a, b, e))

    for pat in list(getattr(args, "drop", None) or []) + \
            [x for ab in (getattr(args, "drop_pair", None) or []) for x in ab]:
        if RE_PY_ONLY.search(pat):
            sys.stderr.write(
                "[경고] '%s' 는 파이썬 전용 정규식 문법을 씁니다.\n"
                "       C 엔진(POSIX ERE)에서는 다르게 동작하거나 실패합니다.\n"
                "       예: \\d -> [0-9], \\s -> [[:space:]], \\w -> [[:alnum:]_]\n"
                % pat)
    return drops, pairs


def prefix_rules(args):
    """
    --drop-prefix 를 bytes 튜플로 만든다.

    --drop 으로도 같은 일을 할 수 있지만, 로그에서 복사한 접두사에는
    '.'(파일명/계층)이나 '*' 같은 정규식 메타문자가 거의 항상 들어 있다.
    그러면 C 판에서 리터럴 추출이 끊겨 조용히 regexec 경로로 떨어진다
    (실측 903 MB/s -> 34 MB/s). 리터럴임을 명시하면 그럴 일이 없다.

    파이썬에서도 bytes.startswith(튜플) 한 번이면 끝나서, 정규식 경로가
    쓰는 rstrip 복사조차 필요 없다.
    """
    out = []
    for p in getattr(args, "drop_prefix", None) or []:
        if not p:
            sys.exit("error: --drop-prefix 에 빈 문자열은 쓸 수 없습니다")
        out.append(p.encode())
    return tuple(out)


def make_line_reader(fin, maxline, trunc):
    """
    fin.readline 을 감싸서 maxline 을 넘는 줄을 처리한다.

    왜 필요한가: 정상 로그 줄은 수백 바이트다. 그보다 몇 자리수 긴 줄은
    로그가 아니라, 디스크가 가득 찬 상태로 기록되다 만 NUL 덩어리이거나
    개행을 빠뜨린 덤프다. 그런 줄은 readline() 이 통째로 램에 올린다
    (실측: 300MB 짜리 줄 하나에 RSS 626MB). 10GB 로그를 병렬로 돌리면
    이런 파일 하나가 잡 전체를 죽인다.

    readline(size) 는 size 바이트까지만 읽으므로 C 판의 maxline 과 같은
    방식으로 버퍼가 커지는 걸 막는다. 결과가 딱 size 바이트인데 개행으로
    끝나지 않으면 그 줄은 한계를 넘은 것이다.

    반환: (nxt, over)
      nxt()  다음 줄. 한계를 넘은 줄은 버리거나(기본) 앞부분만 남긴다.
      over   [줄 수, 아직 nbytes 에 안 더한 바이트, 버린 총 바이트]
             maxline 이 0 이면 None (감싸지 않고 원본 readline 을 준다)
    """
    if not maxline:
        return fin.readline, None

    raw = fin.readline
    over = [0, 0, 0, 0]   # 마지막 칸은 아직 nline 에 안 더한 줄 수
    SKIP = 1 << 20

    def nxt():
        while True:
            line = raw(maxline)
            if not line:
                return b""
            if line.endswith(b"\n") or len(line) < maxline:
                return line

            # 한계 초과. 나머지를 개행까지 버린다(버퍼를 키우지 않는다).
            skipped = 0
            while True:
                c = raw(SKIP)
                if not c:
                    break
                skipped += len(c)
                if c.endswith(b"\n"):
                    break
            over[0] += 1
            over[2] += len(line) + skipped

            if trunc:
                # 호출자는 maxline+1 을 셀 텐데 실제로는 maxline+skipped 를
                # 소비했다. 그 차이를 넘긴다.
                over[1] += skipped - 1
                return line + b"\n"
            # 버린 줄은 호출자가 아예 못 보므로 전부 여기서 센다
            over[1] += len(line) + skipped
            over[3] += 1

    return nxt, over


def warn_long_line(n, lineno):
    sys.stderr.write(
        "\n  [경고] %s 바이트짜리 줄을 만났습니다 (줄 %s).\n"
        "         정상 로그 줄이 아닙니다. 디스크가 가득 찬 상태로\n"
        "         기록된 로그에는 개행 없는 NUL 덩어리가 GB 단위로\n"
        "         남기도 하며, 그런 줄은 통째로 램에 올라옵니다.\n"
        "         --max-line 1048576 을 주면 그런 줄을 버립니다.\n"
        % (format(n, ","), format(lineno, ",")))


def _probe(line):
    """정규식 판정용으로 줄 끝 개행을 떼어낸다 (C 판과 동일하게)."""
    return line.rstrip(b"\r\n")


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
        self.pruned_dep = 0
        self.pruned_err = 0
        self.user_dropped = 0
        self.long_lines = 0
        self.long_bytes = 0

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
    prune_dep = getattr(args, "prune_deposit", False)
    drops, pairs = compile_rules(args)
    prefixes = prefix_rules(args)
    has_rules = bool(drops or pairs)
    rule_hits = [0] * len(drops)
    pair_hits = [0] * len(pairs)
    prefix_hits = [0] * len(prefixes)
    pairs_only = getattr(args, "deposit_pairs_only", False)
    every_line = INF if args.no_progress else args.progress_lines
    every_viol = INF if args.no_progress else args.progress_violations
    line_mark = every_line
    viol_mark = every_viol

    fin = open(src, "rb", buffering=IO_BUF)
    fout = open(dst, "wb", buffering=IO_BUF)
    write = fout.write
    nxt, over = make_line_reader(
        fin, getattr(args, "max_line", 0), getattr(args, "truncate_long", False))
    warn_at = LONG_LINE_WARN

    try:
        line = nxt()
        while line:
            nbytes += len(line)
            nline += 1
            # 한계를 넘어 버려진 줄은 호출자가 볼 수 없으므로 여기서
            # 합쳐준다. 안 그러면 진행률이 뒤처지고, C 판과 줄 수가 어긋난다.
            if over is not None and over[1]:
                nbytes += over[1]; nline += over[3]
                over[1] = over[3] = 0
            if len(line) >= warn_at:
                warn_long_line(len(line), nline)
                warn_at = 1 << 62      # 한 번만 알린다

            # 진행률은 줄을 읽은 직후에 확인한다. 아래 분기 안쪽에 두면
            # 버려지는 줄(deposit/PNOOBJ, violation 블록)에서 continue 로
            # 빠져나가 검사를 건너뛴다. 잡음이 한 구간에 몰려 있는 실제
            # 로그에서는 그 구간 내내 화면이 멈춘 것처럼 보인다.
            # mark 를 더하지 않고 현재 위치 기준으로 다시 잡는 것도 같은 이유다.
            if nline >= line_mark:
                line_mark = nline + every_line
                report()

            # ---- fast path -----------------------------------------------
            # 전체 줄의 99% 이상은 violation 이 아니다. bytes slice 비교는
            # C 레벨이라 여기서 대부분의 줄이 즉시 통과한다.
            if line[:8] != HEADER_PREFIX or HEADER_KEY not in line:

                # 리터럴 접두사 규칙. startswith(튜플)은 C 레벨 호출
                # 한 번이라, 정규식 경로가 하는 rstrip 복사가 없다.
                if prefixes and line.startswith(prefixes):
                    for pi, pf in enumerate(prefixes):
                        if line.startswith(pf):
                            prefix_hits[pi] += 1
                            break
                    store.user_dropped += 1
                    line = nxt()
                    continue

                # 사용자 정규식 규칙. 규칙이 없으면 비용이 0 이다.
                if has_rules:
                    probe = _probe(line)
                    matched = False
                    for i, rx in enumerate(drops):
                        if rx.search(probe):
                            rule_hits[i] += 1
                            store.user_dropped += 1
                            matched = True
                            break
                    if matched:
                        line = nxt()
                        continue

                    for j, (ra, rb) in enumerate(pairs):
                        if not ra.search(probe):
                            continue
                        nl2 = nxt()
                        if nl2 and rb.search(_probe(nl2)):
                            nbytes += len(nl2)
                            nline += 1
                            pair_hits[j] += 1
                            store.user_dropped += 2
                            line = nxt()
                        else:
                            # 짝이 아니다 -> 첫 줄은 살리고 다음 줄을 새로 판정
                            write(line)
                            kept += 1
                            line = nl2 if nl2 else nxt()
                        matched = True
                        break        # A 가 맞았으면 이 줄 판정은 끝
                    if matched:
                        continue

                # deposit 줄 / PNOOBJ 줄 제거. 둘 다 'x' 로 시작한다.
                if prune_dep and line[:1] == b"x":
                    if line[:8] == PRUNE_P1_PREFIX and RE_DEPOSIT.match(line):
                        if not pairs_only:
                            store.pruned_dep += 1   # 짝이든 아니든 버린다
                            line = nxt()
                            continue
                        nl2 = nxt()
                        if nl2:
                            if nl2[:6] == PRUNE_P2_PREFIX and RE_PNOOBJ.match(nl2):
                                nbytes += len(nl2)
                                nline += 1
                                store.pruned_dep += 1
                                store.pruned_err += 1
                                line = nxt()
                                continue
                            write(line)
                            kept += 1
                            # 짝이 아니다 -> 다음 줄을 새로 판정한다.
                            # 세는 건 루프 상단이 하므로 여기서 세면 이중 계산이 된다.
                            line = nl2
                            continue
                    elif (not pairs_only and line[:6] == PRUNE_P2_PREFIX
                            and RE_PNOOBJ.match(line)):
                        store.pruned_err += 1
                        line = nxt()
                        continue

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

    # 마지막 줄이 버려졌으면 루프가 한 번 더 돌지 않아 위 flush 를 못 탄다.
    if over is not None and over[1]:
        nbytes += over[1]; nline += over[3]
        over[1] = over[3] = 0

    if not args.no_progress:
        report(final=True)   # 마지막 진행률을 확정 출력하고 '\r' 줄을 닫는다

    if over is not None and over[0]:
        store.long_lines += over[0]
        store.long_bytes += over[2]

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
    if args.prune_deposit:
        cmdbase.append("--prune-deposit")
    if args.max_line:
        cmdbase += ["--max-line", str(args.max_line)]
        if args.truncate_long:
            cmdbase.append("--truncate-long")
    for pat in args.drop_prefix or []:
        cmdbase += ["--drop-prefix", pat]
    for pat in args.drop or []:
        cmdbase += ["--drop", pat]
    for a, b in args.drop_pair or []:
        cmdbase += ["--drop-pair", a, b]
    if args.deposit_pairs_only:
        cmdbase.append("--deposit-pairs-only")
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
    # 어느 엔진을 쓰든 정규식은 여기서 먼저 검증한다. C 로 넘긴 뒤에
    # regcomp 가 실패하면 "C 엔진 실패" 같은 불친절한 메시지만 남는다.
    compile_rules(args)
    check_max_line(args)

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
    if store.pruned_dep or store.pruned_err:
        sys.stderr.write(
            "  deposit   : %s 줄 + PNOOBJ %s 줄 = %s 줄 삭제\n"
            % (format(store.pruned_dep, ","), format(store.pruned_err, ","),
               format(store.pruned_dep + store.pruned_err, ","))
        )
    if store.user_dropped:
        sys.stderr.write("  규칙 삭제 : %s 줄\n"
                         % format(store.user_dropped, ","))
    if store.long_lines:
        sys.stderr.write(
            "  긴 줄     : %s 줄 %s %s (--max-line %d)\n"
            % (format(store.long_lines, ","), _human(store.long_bytes),
               "잘라냄" if args.truncate_long else "삭제", args.max_line))
    if store.unparsed:
        sys.stderr.write(
            "  주의: 형식이 다른 violation 후보 %d 건은 원본에 그대로 남겼습니다.\n"
            % store.unparsed
        )
    warn_big_clean(outs, args.warn_bytes)


# ---------------------------------------------------------------------------
# 반복되는 잡음 메시지 제거 (prune)
#
# Xcelium 이 존재하지 않는 계층에 deposit 을 시도하면 아래 두 줄이 짝으로 남는다.
#
#   xcelium> deposit top.dut.core_aaa.l2_cache.ff_0.Q 0
#   xmsim: *SE,PNOOBJ: Path element could not be found: l2_cache.
#
# 계층 이름만 매번 달라서 줄 단위로는 전부 다른 문자열이지만, 형태는 같다.
# timing violation 과 달리 남겨둘 정보가 없으므로 그냥 지운다.
#
# deposit 줄은 '프롬프트 + deposit + 인자 하나 이상' 까지만 확인한다.
# 인자의 개수나 형태(계층만, 계층+값 0, 1'b0, = 붙은 형태 ...)는 보지 않는다.
# 시뮬레이터/스크립트마다 다르고, 거기에 맞춰 정규식을 조이면 형태가 조금만
# 달라져도 조용히 매칭에 실패해서 아무것도 지워지지 않기 때문이다.
# (C 판 is_deposit_cmd() 와 같은 규칙이다. 두 엔진이 어긋나면 안 된다.)
#
# deposit 줄과 PNOOBJ 줄은 서로 독립적으로 지운다. deposit 이 성공해서 에러
# 줄이 안 붙은 경우까지 전부 지운다. 짝을 맞출 필요가 없으니 앞뒤를 살펴볼
# 일도 없어서 더 단순하고 빠르다.
# --deposit-pairs-only 를 주면 'deposit + 바로 뒤 PNOOBJ' 짝일 때만 지운다.
#
# prune 서브커맨드에서는 이게 존재 이유라 항상 켜져 있고, strip 에서는
# --prune-deposit 을 명시해야 켜진다 (strip 의 기본은 violation 만 지우는 것).
# ---------------------------------------------------------------------------
PRUNE_P1_PREFIX = b"xcelium>"
PRUNE_P2_PREFIX = b"xmsim:"
RE_DEPOSIT = re.compile(rb"^xcelium>\s*deposit\s+\S")
RE_PNOOBJ = re.compile(
    rb"^xmsim:\s*\*SE,PNOOBJ:\s*Path element could not be found:")


def _prune_dst(args, src):
    if args.dry_run:
        # dry-run 은 /dev/null 로 흘려보내면 개수만 세는 것과 같다.
        return os.devnull
    if args.in_place:
        return src + ".prune.tmp"
    dst = args.out or (os.path.splitext(src)[0] + ".pruned.log")
    if os.path.abspath(src) == os.path.abspath(dst):
        sys.exit("error: 입력과 출력 경로가 같습니다: %s" % src)
    return dst


def check_max_line(args):
    """--max-line / --truncate-long 조합을 C 판과 같은 규칙으로 검증한다."""
    if args.max_line and args.max_line < 1024:
        sys.exit("error: --max-line 은 1024 이상이어야 합니다 (받은 값: %d)"
                 % args.max_line)
    if args.truncate_long and not args.max_line:
        sys.exit("error: --truncate-long 은 --max-line 과 같이 써야 합니다")


def check_inputs(paths):
    """입력이 전부 읽을 수 있는 일반 파일인지 먼저 확인한다."""
    for p in paths:
        if not os.path.exists(p):
            sys.exit("error: 파일이 없습니다: %s" % p)
        if not os.path.isfile(p):
            sys.exit("error: 일반 파일이 아닙니다: %s" % p)
        if not os.access(p, os.R_OK):
            sys.exit("error: 읽을 수 없습니다: %s" % p)


def _prune_with_c(args, exe):
    """C 의 --prune-only 모드로 처리한다. strip 과 같은 엔진을 쓴다."""
    check_inputs(args.logfile)
    jobs = [(src, _prune_dst(args, src)) for src in args.logfile]
    # --in-place 는 원본을 덮어쓰므로 입력 크기를 미리 재둬야 한다.
    total_in = sum(os.path.getsize(s) for s, _ in jobs)

    base = [exe, "--prune-only", "--warn-bytes", "0"]
    if args.deposit_pairs_only:
        base.append("--deposit-pairs-only")
    if args.keep_deposit:
        base.append("--keep-deposit")   # 내장 규칙 끄고 --drop 만 쓰는 경우
    if args.max_line:
        base += ["--max-line", str(args.max_line)]
        if args.truncate_long:
            base.append("--truncate-long")
    for pat in args.drop_prefix or []:
        base += ["--drop-prefix", pat]
    for pat in args.drop or []:
        base += ["--drop", pat]
    for a, b in args.drop_pair or []:
        base += ["--drop-pair", a, b]
    if args.no_progress:
        base.append("--no-progress")
    else:
        base += ["--progress-lines", str(args.progress_lines)]

    t0 = time.time()
    par = max(1, args.jobs)
    running = []
    pending = list(jobs)
    failed = []
    done = 0
    # 아직 제자리를 못 찾은 산출물. 중간에 실패하거나 Ctrl-C 로 끊겨도
    # 여기 남은 것들을 지워서 .prune.tmp 찌꺼기가 남지 않게 한다.
    orphan = set()

    try:
        # 파일 여러 개를 코어 수만큼 동시에 돌린다.
        while pending or running:
            while pending and len(running) < par:
                src, dst = pending.pop(0)
                orphan.add(dst)
                running.append(
                    (subprocess.Popen(base + [src, "-o", dst]), src, dst))
            # running 에 남겨둔 채로 기다린다. 미리 pop 하면 대기 중에
            # Ctrl-C 가 들어왔을 때 이 프로세스만 종료 대상에서 빠져서,
            # 파이썬이 끝난 뒤에도 혼자 계속 도는 고아 프로세스가 된다.
            p, src, dst = running[0]
            rc = p.wait()
            running.pop(0)
            if rc != 0:
                failed.append(src)
                continue          # 실패한 산출물은 orphan 에 남겨 지운다
            done += 1
            if args.dry_run:
                orphan.discard(dst)          # /dev/null
            elif args.in_place:
                # 끝나는 즉시 교체한다. 마지막에 몰아서 하면 모든 임시 파일이
                # 동시에 존재해서, 10GB 짜리가 여러 개면 그만큼 여유 공간이
                # 더 필요해진다.
                os.replace(dst, src)         # 같은 파일시스템이면 원자적
                orphan.discard(dst)
            else:
                orphan.discard(dst)          # 사용자가 원한 출력이므로 남긴다
    finally:
        # Ctrl-C 등으로 빠져나온 경우: 자식을 확실히 죽인 뒤에 정리한다.
        # 죽기 전에 파일을 지우면 남은 자식이 계속 써대며 디스크를 먹는다.
        for p, _, _ in running:
            if p.poll() is None:
                p.terminate()
        for p, _, _ in running:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        for path in orphan:
            try:
                os.remove(path)
            except OSError:
                pass

    if failed:
        sys.exit("error: C 엔진 prune 실패: %s\n"
                 "       (실패한 파일의 산출물은 지웠습니다. "
                 "성공한 %d개는 그대로 반영되었습니다.)"
                 % (", ".join(failed), done))

    if args.dry_run:
        sys.stderr.write("\n  (--dry-run: 파일을 만들지 않았습니다)\n")
        return
    if args.in_place:
        sys.stderr.write("\n  원본 %d개를 교체했습니다.\n" % len(jobs))

    if len(jobs) > 1:
        finals = [s if args.in_place else d for s, d in jobs]
        total_out = sum(os.path.getsize(f) for f in finals if os.path.exists(f))
        sys.stderr.write(
            "\n[prune 전체 완료] %.1fs, %d개 파일  %s -> %s (%s 절감)\n"
            % (time.time() - t0, len(jobs), _human(total_in), _human(total_out),
               _human(total_in - total_out)))


def prune(args):
    """deposit/PNOOBJ 짝을 지운다. 1패스 스트리밍이라 파일 크기와 무관하다."""
    compile_rules(args)          # 엔진과 무관하게 먼저 검증
    check_max_line(args)
    exe = None
    if args.engine in ("auto", "c"):
        exe = find_or_build_c(quiet=args.no_progress)
        if exe is None and args.engine == "c":
            sys.exit("error: C 엔진을 쓸 수 없습니다 (컴파일러 또는 %s 없음)" % C_SRC)
    if args.out and len(args.logfile) > 1:
        sys.exit("error: --out 은 입력이 하나일 때만 쓸 수 있습니다.")
    if exe is not None:
        return _prune_with_c(args, exe)
    if args.engine == "auto":
        sys.stderr.write("[안내] C 엔진을 쓸 수 없어 파이썬으로 처리합니다.\n")

    check_inputs(args.logfile)
    t_all = time.time()
    tot_in = tot_out = tot_dep = tot_err = 0
    for src in args.logfile:
        dst = _prune_dst(args, src)
        try:
            d_in, d_out, d_dep, d_err = _prune_python_file(src, dst, args)
        except BaseException:
            # 중간에 끊기면 반쯤 쓰인 산출물이 남는다. 특히 .prune.tmp 는
            # 순전히 내부용이라 남겨둘 이유가 없다.
            if not args.dry_run and os.path.exists(dst):
                try:
                    os.remove(dst)
                except OSError:
                    pass
            raise
        tot_in += d_in; tot_out += d_out
        tot_dep += d_dep; tot_err += d_err

    if len(args.logfile) > 1 and not args.dry_run:
        sys.stderr.write(
            "\n[prune 전체 완료] %.1fs, %d개 파일  %s -> %s (%s 절감)\n"
            "  제거      : deposit %s 줄 + PNOOBJ %s 줄\n"
            % (time.time() - t_all, len(args.logfile), _human(tot_in),
               _human(tot_out), _human(tot_in - tot_out),
               format(tot_dep, ","), format(tot_err, ",")))


def _prune_python_file(src, dst, args):
    """파일 하나를 파이썬으로 처리한다. (입력크기, 출력크기, dep, err)."""
    n_dep = n_err = n_user = nbytes = nline = 0
    pairs_only = args.deposit_pairs_only
    prune_dep = not args.keep_deposit
    drops, pairs = compile_rules(args)
    prefixes = prefix_rules(args)
    has_rules = bool(drops or pairs)
    rule_hits = [0] * len(drops)
    pair_hits = [0] * len(pairs)
    prefix_hits = [0] * len(prefixes)
    total = os.path.getsize(src)
    t0 = time.time()
    eol = "\r" if sys.stderr.isatty() else "\n"
    mark = args.progress_lines if not args.no_progress else float("inf")

    def report(final=False):
        el = time.time() - t0
        sys.stderr.write(
            "[prune] %-16s %5.1f%% %13s lines %9s 삭제 %9s %6.1f MB/s   %s"
            % (os.path.basename(src)[:16],
               nbytes * 100.0 / total if total else 0.0,
               format(nline, ","), format(n_dep + n_err + n_user, ","),
               _human(nbytes),
               (nbytes / el if el else 0) / 1e6, "\n" if final else eol))
        sys.stderr.flush()

    fin = open(src, "rb", buffering=IO_BUF)
    fout = None if args.dry_run else open(dst, "wb", buffering=IO_BUF)
    nxt, over = make_line_reader(fin, args.max_line, args.truncate_long)
    warn_at = LONG_LINE_WARN
    write = fout.write if fout is not None else None
    try:
        line = nxt()
        while line:
            nbytes += len(line)
            nline += 1
            if over is not None and over[1]:
                nbytes += over[1]; nline += over[3]
                over[1] = over[3] = 0
            if len(line) >= warn_at:
                warn_long_line(len(line), nline)
                warn_at = 1 << 62      # 한 번만 알린다

            # 진행률은 줄을 읽은 직후에 확인한다(버려지는 줄 포함).
            if nline >= mark:
                mark = nline + args.progress_lines
                report()

            # 리터럴 접두사 규칙. startswith(튜플)은 C 레벨 호출 한 번이라,
            # 정규식 경로가 하는 rstrip 복사가 없다.
            if prefixes and line.startswith(prefixes):
                for pi, pf in enumerate(prefixes):
                    if line.startswith(pf):
                        prefix_hits[pi] += 1
                        break
                n_user += 1
                line = nxt()
                continue

            # 사용자 정규식 규칙. 규칙이 없으면 비용이 0 이다.
            if has_rules:
                probe = _probe(line)
                matched = False
                for i, rx in enumerate(drops):
                    if rx.search(probe):
                        rule_hits[i] += 1
                        n_user += 1
                        matched = True
                        break
                if matched:
                    line = nxt()
                    continue
                for j, (ra, rb) in enumerate(pairs):
                    if not ra.search(probe):
                        continue
                    nl2 = nxt()
                    if nl2 and rb.search(_probe(nl2)):
                        nbytes += len(nl2)
                        nline += 1
                        pair_hits[j] += 1
                        n_user += 2
                        line = nxt()
                    else:
                        if write is not None:
                            write(line)
                        line = nl2 if nl2 else nxt()
                    matched = True
                    break
                if matched:
                    continue

            # fast path: 대부분의 줄은 1~8바이트 비교로 통과한다
            if not prune_dep or line[:1] != b"x":
                if write is not None:
                    write(line)
                line = nxt()
                continue

            is_dep = line[:8] == PRUNE_P1_PREFIX and RE_DEPOSIT.match(line)

            if is_dep and not pairs_only:
                n_dep += 1                # 짝이든 아니든 버린다
                line = nxt()
                continue

            if not is_dep:
                if (not pairs_only and line[:6] == PRUNE_P2_PREFIX
                        and RE_PNOOBJ.match(line)):
                    n_err += 1
                    line = nxt()
                    continue
                if write is not None:
                    write(line)
                line = nxt()
                continue

            # 여기부터는 pairs_only 모드의 deposit 줄
            nxt_line = nxt()
            if not nxt_line:
                if write is not None:
                    write(line)
                break

            if nxt_line[:6] == PRUNE_P2_PREFIX and RE_PNOOBJ.match(nxt_line):
                nbytes += len(nxt_line)
                nline += 1
                n_dep += 1
                n_err += 1
                line = nxt()          # 짝을 통째로 버린다
                continue

            # 짝이 아니다 -> deposit 줄은 살리고, 다음 줄을 새로 판정한다
            if write is not None:
                write(line)
            line = nxt_line
    finally:
        fin.close()
        if fout is not None:
            fout.close()

    if over is not None and over[1]:
        nbytes += over[1]; nline += over[3]
        over[1] = over[3] = 0

    if not args.no_progress:
        report(final=True)

    if args.dry_run:
        sys.stderr.write("\n[prune / dry-run] %s: deposit %s 줄 + PNOOBJ %s 줄 "
                         "= %s 줄 발견. 파일은 만들지 않았습니다.\n"
                         % (src, format(n_dep, ","), format(n_err, ","),
                            format(n_dep + n_err, ",")))
        return nbytes, 0, n_dep, n_err

    if args.in_place:
        os.replace(dst, src)          # 같은 파일시스템이면 원자적으로 교체된다
        final_path = src
    else:
        final_path = dst

    out_size = os.path.getsize(final_path)
    sys.stderr.write(
        "\n[prune 완료] %.1fs\n"
        "  입력      : %-28s %12s\n"
        "  결과      : %-28s %12s\n"
        "  제거      : deposit %s 줄 + PNOOBJ %s 줄 + 규칙 %s 줄 = %s 줄"
        "  (%s 절감)\n"
        % (time.time() - t0, src, _human(nbytes), final_path, _human(out_size),
           format(n_dep, ","), format(n_err, ","), format(n_user, ","),
           format(n_dep + n_err + n_user, ","), _human(nbytes - out_size))
    )
    if over is not None and over[0]:
        sys.stderr.write(
            "  긴 줄     : %s 줄 %s %s (--max-line %d)\n"
            % (format(over[0], ","), _human(over[2]),
               "잘라냄" if args.truncate_long else "삭제", args.max_line))
    return nbytes, out_size, n_dep, n_err


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
    s.add_argument("--max-line", type=int, default=0, metavar="N",
                   help="N 바이트가 넘는 줄을 삭제 (0=무제한). 디스크가 가득 찬 "
                        "상태로 기록된 로그의 NUL 덩어리 대응. 권장: 1048576")
    s.add_argument("--truncate-long", action="store_true",
                   help="--max-line 초과 줄을 버리지 않고 앞부분만 남김")
    s.add_argument("--drop-prefix", action="append", metavar="STR",
                   help="이 글자로 시작하는 줄을 삭제 (정규식 아님, 가장 빠름). "
                        "여러 번 지정 가능")
    s.add_argument("--drop", action="append", metavar="RE",
                    help="이 정규식에 걸리는 줄을 삭제 (여러 번 지정 가능)")
    s.add_argument("--drop-pair", action="append", nargs=2,
                    metavar=("RE1", "RE2"),
                    help="RE1 줄 바로 뒤에 RE2 줄이 올 때만 두 줄 삭제 "
                         "(여러 번 지정 가능)")
    s.add_argument("--prune-deposit", action="store_true",
                   help="xcelium deposit / PNOOBJ 줄까지 함께 삭제 "
                        "(기본: timing violation 만 삭제)")
    s.add_argument("--deposit-pairs-only", action="store_true",
                   help="--prune-deposit 과 함께: deposit + 바로 뒤 PNOOBJ "
                        "짝일 때만 지움 (성공한 deposit 은 보존)")
    s.add_argument("--progress-lines", type=int, default=1_000_000,
                   metavar="N", help="N 줄마다 진행률 출력 (기본: 1000000)")
    s.add_argument("--progress-violations", type=int, default=1000,
                   metavar="N", help="violation N 건마다 진행률 출력 (기본: 1000)")
    s.add_argument("--no-progress", action="store_true", help="진행률 출력 끄기")
    s.add_argument("--warn-bytes", type=int, default=1 << 30, metavar="N",
                   help="정리된 로그가 N 바이트 이상이면 경고 (기본: 1GiB)")
    s.set_defaults(func=strip)

    pr = sub.add_parser(
        "prune", help="반복되는 deposit/PNOOBJ 잡음 두 줄짜리 짝을 삭제 (DB 없음)")
    pr.add_argument("logfile", nargs="+", help="sim.log (여러 개 지정 가능)")
    pr.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    metavar="N",
                    help="C 엔진에서 동시 처리할 파일 수 (기본: 코어 수)")
    pr.add_argument("--engine", choices=("auto", "c", "python"), default="auto",
                    help="auto=C 우선(기본), c=C 강제, python=파이썬 강제")
    pr.add_argument("--out",
                    help="결과 파일 (기본: <입력>.pruned.log, 입력 1개일 때만)")
    pr.add_argument("--in-place", action="store_true",
                    help="임시 파일에 쓴 뒤 원본을 교체 (원자적)")
    pr.add_argument("--drop-prefix", action="append", metavar="STR",
                    help="이 글자로 시작하는 줄을 삭제 (정규식 아님, 가장 빠름). "
                         "여러 번 지정 가능")
    pr.add_argument("--drop", action="append", metavar="RE",
                    help="이 정규식에 걸리는 줄을 삭제 (여러 번 지정 가능)")
    pr.add_argument("--drop-pair", action="append", nargs=2,
                    metavar=("RE1", "RE2"),
                    help="RE1 줄 바로 뒤에 RE2 줄이 올 때만 두 줄 삭제 "
                         "(여러 번 지정 가능)")
    pr.add_argument("--keep-deposit", action="store_true",
                    help="내장 deposit/PNOOBJ 규칙을 끔 (--drop 만 쓸 때)")
    pr.add_argument("--deposit-pairs-only", action="store_true",
                    help="deposit + 바로 뒤 PNOOBJ 짝일 때만 지움 "
                         "(성공한 deposit 은 보존)")
    pr.add_argument("--max-line", type=int, default=0, metavar="N",
                    help="N 바이트가 넘는 줄을 삭제 (0=무제한). 디스크가 가득 찬 "
                         "상태로 기록된 로그의 NUL 덩어리 대응. 권장: 1048576")
    pr.add_argument("--truncate-long", action="store_true",
                    help="--max-line 초과 줄을 버리지 않고 앞부분만 남김")
    pr.add_argument("--dry-run", action="store_true",
                    help="개수만 세고 파일은 만들지 않음")
    pr.add_argument("--progress-lines", type=int, default=1_000_000, metavar="N",
                    help="N 줄마다 진행률 출력 (기본: 1000000)")
    pr.add_argument("--no-progress", action="store_true", help="진행률 출력 끄기")
    pr.set_defaults(func=prune)

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
