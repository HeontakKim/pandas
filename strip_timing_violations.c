/*
 * strip_timing_violations.c
 *
 * strip_timing_violations.py 의 C 구현. 10GB 급 로그 파일 다수를 처리하기 위한
 * 버전이다. 파이썬판과 동일한 결과(정리된 로그 + scope 집계)를 낸다.
 *
 * 설계 원칙:
 *   - libc 외 의존성 없음. zlib/sqlite3 링크 안 함 -> Termux/proot 에서 그냥 빌드된다.
 *   - 줄 단위 할당 없음. 읽기 버퍼 안에서 포인터로만 다룬다.
 *   - 메모리는 입력 크기와 무관. 집계 해시테이블만 쓰고 그건 고유 위치 수에 비례한다.
 *   - 무거운 스캔만 C 가 하고, 집계 결과(보통 수천 줄)를 SQLite 로 넣는 건
 *     기존 파이썬 스크립트의 load 서브커맨드에 맡긴다.
 *
 * 빌드:
 *   cc -O2 -o strip_tv strip_timing_violations.c
 *
 * 사용:
 *   ./strip_tv sim.log -o sim.clean.log -a sim.agg.tsv
 *   python3 strip_timing_violations.py load sim.agg.tsv --db violations.db
 *
 * 파일이 여러 개면 코어 수만큼 병렬로 돌리는 게 가장 빠르다:
 *   ls *.log | xargs -P 7 -I{} ./strip_tv {} -o {}.clean -a {}.agg
 */

#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

#ifndef RD_BUF
#define RD_BUF   (8u << 20)   /* 읽기 버퍼 8MiB (테스트용으로 -D 재정의 가능) */
#endif
#ifndef WR_BUF
#define WR_BUF   (8u << 20)   /* 쓰기 버퍼 8MiB */
#endif
#define MAX_LOOKAHEAD    8    /* violation 헤더 뒤로 몇 줄까지 살펴볼지 */
#define MAX_TRAIL_BLANK  2    /* 블록 뒤 빈 줄 몇 개까지 같이 지울지 */

/* 정리된 로그가 이 크기 이상이면 경고한다. timing violation 을 다 걷어냈는데도
 * 로그가 크다는 건 로그를 키우는 다른 메시지가 있다는 뜻이다. */
#define DEFAULT_WARN_BYTES (1ULL << 30)   /* 1 GiB */

static const char HDR[]  = "Warning!";
static const char HKEY[] = "Timing violation";
#define HDR_LEN  8

static void die(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("error: ", stderr);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
    fputc('\n', stderr);
    exit(1);
}

/* ------------------------------------------------------------------ */
/* 줄 단위 리더. 되돌리기(unget)를 지원한다.                            */
/* 반환되는 포인터는 내부 버퍼를 가리키므로 다음 read 전까지만 유효하다. */
/* ------------------------------------------------------------------ */
typedef struct {
    int            fd;
    unsigned char *buf;
    size_t         cap;   /* 버퍼 크기          */
    size_t         len;   /* 유효 데이터 끝      */
    size_t         pos;   /* 다음에 읽을 위치    */
    int            eof;
} Reader;

static void rd_init(Reader *r, int fd)
{
    r->fd = fd;
    r->cap = RD_BUF;
    r->buf = malloc(r->cap);
    if (!r->buf) die("메모리 부족");
    r->len = r->pos = 0;
    r->eof = 0;
}

/*
 * 한 줄을 돌려준다(개행 포함). 줄이 없으면 0.
 * 버퍼 경계에 걸친 줄은 앞으로 당긴 뒤 다시 채운다. 한 줄이 버퍼보다 길면
 * 버퍼를 2배로 늘린다(정상 로그에선 일어나지 않지만 방어적으로).
 */
static int rd_line(Reader *r, unsigned char **out, size_t *outlen)
{
    for (;;) {
        if (r->pos < r->len) {
            unsigned char *base = r->buf + r->pos;
            size_t avail = r->len - r->pos;
            unsigned char *nl = memchr(base, '\n', avail);
            if (nl) {
                size_t n = (size_t)(nl - base) + 1;
                *out = base;
                *outlen = n;
                r->pos += n;
                return 1;
            }
            if (r->eof) {           /* 마지막 줄에 개행이 없는 경우 */
                *out = base;
                *outlen = avail;
                r->pos = r->len;
                return 1;
            }
        } else if (r->eof) {
            return 0;
        }

        /* 남은 조각을 앞으로 당기고 더 읽는다 */
        if (r->pos > 0) {
            memmove(r->buf, r->buf + r->pos, r->len - r->pos);
            r->len -= r->pos;
            r->pos = 0;
        }
        if (r->len == r->cap) {                 /* 줄이 버퍼보다 길다 */
            size_t ncap = r->cap * 2;
            unsigned char *nb = realloc(r->buf, ncap);
            if (!nb) die("메모리 부족 (긴 줄)");
            r->buf = nb;
            r->cap = ncap;
        }
        ssize_t got = read(r->fd, r->buf + r->len, r->cap - r->len);
        if (got < 0) {
            if (errno == EINTR) continue;
            die("읽기 실패: %s", strerror(errno));
        }
        if (got == 0) r->eof = 1;
        else r->len += (size_t)got;
    }
}

/* 방금 읽은 줄을 되돌린다. 그 사이에 다른 read 가 없어야 한다. */
static inline void rd_unget(Reader *r, size_t n) { r->pos -= n; }

/* ------------------------------------------------------------------ */
/* 버퍼 라이터                                                          */
/* ------------------------------------------------------------------ */
typedef struct {
    int            fd;
    unsigned char *buf;
    size_t         cap, len;
} Writer;

static void wr_init(Writer *w, int fd)
{
    w->fd = fd;
    w->cap = WR_BUF;
    w->buf = malloc(w->cap);
    if (!w->buf) die("메모리 부족");
    w->len = 0;
}

static void wr_flush(Writer *w)
{
    size_t off = 0;
    while (off < w->len) {
        ssize_t n = write(w->fd, w->buf + off, w->len - off);
        if (n < 0) {
            if (errno == EINTR) continue;
            die("쓰기 실패: %s", strerror(errno));
        }
        off += (size_t)n;
    }
    w->len = 0;
}

static inline void wr_put(Writer *w, const unsigned char *p, size_t n)
{
    if (n >= w->cap) {                 /* 통째로 큰 덩어리는 직접 쓴다 */
        wr_flush(w);
        size_t off = 0;
        while (off < n) {
            ssize_t k = write(w->fd, p + off, n - off);
            if (k < 0) { if (errno == EINTR) continue; die("쓰기 실패"); }
            off += (size_t)k;
        }
        return;
    }
    if (w->len + n > w->cap) wr_flush(w);
    memcpy(w->buf + w->len, p, n);
    w->len += n;
}

/* ------------------------------------------------------------------ */
/* 집계 해시테이블: (scope, check, file, line) -> count/min/max         */
/* 키는 아레나에 "scope\0check\0file\0" 형태로 한 번만 복사해 둔다.     */
/* ------------------------------------------------------------------ */
typedef struct {
    uint64_t  hash;
    char     *key;          /* 아레나 포인터 */
    uint32_t  klen;
    uint32_t  scope_len, check_len, file_len;
    long      cell_line;    /* 없으면 -1 */
    uint64_t  count;
    long long tmin, tmax;   /* femtosecond, 없으면 -1 */
} Entry;

typedef struct Chunk {
    struct Chunk *next;
    size_t        used, cap;
    char          data[];
} Chunk;

typedef struct {
    Entry  *tab;
    size_t  mask, used;
    Chunk  *arena;
    size_t  uniq;
} Agg;

#define ARENA_CHUNK (1u << 20)

static char *arena_put(Agg *a, const char *p, size_t n)
{
    if (!a->arena || a->arena->used + n > a->arena->cap) {
        size_t cap = n > ARENA_CHUNK ? n : ARENA_CHUNK;
        Chunk *c = malloc(sizeof(Chunk) + cap);
        if (!c) die("메모리 부족 (arena)");
        c->next = a->arena; c->used = 0; c->cap = cap;
        a->arena = c;
    }
    char *dst = a->arena->data + a->arena->used;
    memcpy(dst, p, n);
    a->arena->used += n;
    return dst;
}

static void agg_init(Agg *a)
{
    size_t n = 1u << 16;
    a->tab = calloc(n, sizeof(Entry));
    if (!a->tab) die("메모리 부족");
    a->mask = n - 1;
    a->used = 0;
    a->arena = NULL;
    a->uniq = 0;
}

static inline uint64_t fnv1a(const char *p, size_t n)
{
    uint64_t h = 1469598103934665603ULL;
    for (size_t i = 0; i < n; i++) {
        h ^= (unsigned char)p[i];
        h *= 1099511628211ULL;
    }
    return h;
}

static void agg_grow(Agg *a)
{
    size_t old = a->mask + 1, nn = old * 2;
    Entry *nt = calloc(nn, sizeof(Entry));
    if (!nt) die("메모리 부족 (해시 확장)");
    size_t nmask = nn - 1;
    for (size_t i = 0; i < old; i++) {
        if (!a->tab[i].key) continue;
        size_t j = a->tab[i].hash & nmask;
        while (nt[j].key) j = (j + 1) & nmask;
        nt[j] = a->tab[i];
    }
    free(a->tab);
    a->tab = nt;
    a->mask = nmask;
}

static void agg_add(Agg *a,
                    const char *scope, size_t slen,
                    const char *check, size_t clen,
                    const char *file,  size_t flen,
                    long cell_line, long long t_fs)
{
    /* 키 조립 (스택 버퍼; 비정상적으로 길면 잘라낸다).
     * scope/check/file 최대치(1024+128+1024) + 구분자 + line 을 담을 크기. */
    char kb[2560];
    size_t need = slen + 1 + clen + 1 + flen + 1 + sizeof(long);
    if (need > sizeof(kb)) {
        if (slen > 400) slen = 400;
        if (clen > 200) clen = 200;
        if (flen > 300) flen = 300;
    }
    size_t k = 0;
    memcpy(kb + k, scope, slen); k += slen; kb[k++] = '\0';
    memcpy(kb + k, check, clen); k += clen; kb[k++] = '\0';
    memcpy(kb + k, file,  flen); k += flen; kb[k++] = '\0';
    memcpy(kb + k, &cell_line, sizeof(long)); k += sizeof(long);

    uint64_t h = fnv1a(kb, k);
    size_t i = h & a->mask;
    while (a->tab[i].key) {
        Entry *e = &a->tab[i];
        if (e->hash == h && e->klen == k && memcmp(e->key, kb, k) == 0) {
            e->count++;
            if (t_fs >= 0) {
                if (e->tmin < 0 || t_fs < e->tmin) e->tmin = t_fs;
                if (e->tmax < 0 || t_fs > e->tmax) e->tmax = t_fs;
            }
            return;
        }
        i = (i + 1) & a->mask;
    }

    Entry *e = &a->tab[i];
    e->hash = h;
    e->key = arena_put(a, kb, k);
    e->klen = (uint32_t)k;
    e->scope_len = (uint32_t)slen;
    e->check_len = (uint32_t)clen;
    e->file_len  = (uint32_t)flen;
    e->cell_line = cell_line;
    e->count = 1;
    e->tmin = e->tmax = t_fs;
    a->used++;
    a->uniq++;
    if (a->used * 10 >= (a->mask + 1) * 7) agg_grow(a);
}

static void agg_dump(Agg *a, const char *path)
{
    FILE *f = fopen(path, "w");
    if (!f) die("집계 파일을 열 수 없습니다: %s", path);
    fputs("#scope\tcheck\tfile\tline\tcount\tfirst_fs\tlast_fs\n", f);
    for (size_t i = 0; i <= a->mask; i++) {
        Entry *e = &a->tab[i];
        if (!e->key) continue;
        const char *s = e->key;
        const char *c = s + e->scope_len + 1;
        const char *fl = c + e->check_len + 1;
        fprintf(f, "%.*s\t%.*s\t%.*s\t%ld\t%llu\t%lld\t%lld\n",
                (int)e->scope_len, s, (int)e->check_len, c,
                (int)e->file_len, fl, e->cell_line,
                (unsigned long long)e->count, e->tmin, e->tmax);
    }
    if (fclose(f)) die("집계 파일 쓰기 실패");
}

/* ------------------------------------------------------------------ */
/* 파싱 도우미                                                          */
/* ------------------------------------------------------------------ */
static inline int is_blank(const unsigned char *p, size_t n)
{
    for (size_t i = 0; i < n; i++)
        if (p[i] != ' ' && p[i] != '\t' && p[i] != '\r' && p[i] != '\n')
            return 0;
    return 1;
}

static inline int is_header(const unsigned char *p, size_t n)
{
    return n >= HDR_LEN && memcmp(p, HDR, HDR_LEN) == 0 &&
           memmem(p, n, HKEY, sizeof(HKEY) - 1) != NULL;
}

/* "라벨: 값" 에서 값의 시작을 찾는다. 없으면 NULL. */
static const unsigned char *after_label(const unsigned char *p, size_t n,
                                        const char *label, size_t llen)
{
    const unsigned char *q = memmem(p, n, label, llen);
    if (!q) return NULL;
    q += llen;
    const unsigned char *end = p + n;
    while (q < end && (*q == ' ' || *q == '\t')) q++;
    return q;
}

/*
 * 리더 버퍼의 토큰을 지역 버퍼로 복사한다.
 *
 * 중요: rd_line() 은 리필할 때 memmove/realloc 로 버퍼를 옮길 수 있다.
 * 따라서 블록의 앞줄에서 얻은 포인터를 뒷줄을 읽은 뒤까지 들고 있으면
 * 엉뚱한 데이터를 가리키게 된다(파일이 버퍼보다 클 때만 발생).
 * 값을 찾는 즉시 복사해 두는 이유다.
 */
static inline size_t copy_tok(char *dst, size_t cap,
                              const unsigned char *src, size_t n)
{
    if (n >= cap) n = cap - 1;
    memcpy(dst, src, n);
    return n;
}

static inline size_t token_len(const unsigned char *p, const unsigned char *end)
{
    const unsigned char *q = p;
    while (q < end && *q != ' ' && *q != '\t' && *q != '\r' && *q != '\n' &&
           *q != ',')
        q++;
    return (size_t)(q - p);
}

/* 단위를 femtosecond 배율로 */
static long long unit_mult(const unsigned char *u, size_t n)
{
    if (n == 0) return 1;
    if (n == 2 && (u[0]=='F'||u[0]=='f') && (u[1]=='S'||u[1]=='s')) return 1LL;
    if (n == 2 && (u[0]=='P'||u[0]=='p') && (u[1]=='S'||u[1]=='s')) return 1000LL;
    if (n == 2 && (u[0]=='N'||u[0]=='n') && (u[1]=='S'||u[1]=='s')) return 1000000LL;
    if (n == 2 && (u[0]=='U'||u[0]=='u') && (u[1]=='S'||u[1]=='s')) return 1000000000LL;
    if (n == 2 && (u[0]=='M'||u[0]=='m') && (u[1]=='S'||u[1]=='s')) return 1000000000000LL;
    if (n == 1 && (u[0]=='S'||u[0]=='s')) return 1000000000000000LL;
    return -1;
}

/* ------------------------------------------------------------------ */
/* 진행률                                                              */
/* ------------------------------------------------------------------ */
static void human(double n, char *out, size_t cap)
{
    const char *u[] = {"B","KB","MB","GB","TB"};
    int i = 0;
    while (n >= 1024.0 && i < 4) { n /= 1024.0; i++; }
    snprintf(out, cap, "%.1f %s", n, u[i]);
}

static void hms(double s, char *out, size_t cap)
{
    if (s < 0 || s > 359999) { snprintf(out, cap, "--:--:--"); return; }
    long t = (long)s;
    snprintf(out, cap, "%02ld:%02ld:%02ld", t/3600, t%3600/60, t%60);
}

static double now_sec(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec / 1e9;
}

/*
 * timing violation 을 전부 걷어냈는데도 정리된 로그가 여전히 크다면,
 * 로그를 부풀리는 다른 메시지가 있다는 뜻이다. 그대로 두면 사용자는
 * "왜 아직도 크지?" 하고 헤매게 되므로 무엇을 확인해야 할지까지 알려준다.
 */
static void warn_big_clean(const char *path, const char *size_str)
{
    fprintf(stderr,
        "\n  [경고] timing violation 을 제거한 뒤에도 정리된 로그가 %s 입니다.\n"
        "         timing violation 외에 로그를 키우는 메시지가 더 있습니다.\n"
        "         (테스트벤치 $display/$monitor, UVM_INFO, SDF annotation 경고,\n"
        "          assertion 메시지 등이 흔한 원인입니다)\n"
        "         어떤 메시지가 반복되는지 앞부분만 표본으로 확인해 보세요\n"
        "         (숫자를 # 로 바꿔 같은 종류의 메시지를 묶습니다):\n"
        "           head -n 2000000 %s \\\n"
        "             | sed 's/[0-9][0-9]*/#/g' | cut -c1-60 \\\n"
        "             | sort | uniq -c | sort -rn | head -20\n",
        size_str, path);
}

/* ------------------------------------------------------------------ */
int main(int argc, char **argv)
{
    const char *in_path = NULL, *out_path = NULL, *agg_path = NULL;
    unsigned long long prog_lines = 1000000, prog_viol = 1000;
    unsigned long long warn_bytes = DEFAULT_WARN_BYTES;
    int no_progress = 0, keep_blank = 0;

    for (int i = 1; i < argc; i++) {
        const char *a = argv[i];
        if (!strcmp(a, "-o") && i + 1 < argc)       out_path = argv[++i];
        else if (!strcmp(a, "-a") && i + 1 < argc)  agg_path = argv[++i];
        else if (!strcmp(a, "--progress-lines") && i + 1 < argc)
            prog_lines = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(a, "--progress-violations") && i + 1 < argc)
            prog_viol = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(a, "--warn-bytes") && i + 1 < argc)
            warn_bytes = strtoull(argv[++i], NULL, 10);
        else if (!strcmp(a, "--no-progress"))       no_progress = 1;
        else if (!strcmp(a, "--keep-blank"))        keep_blank = 1;
        else if (a[0] == '-' && a[1])
            die("알 수 없는 옵션: %s", a);
        else if (!in_path)                          in_path = a;
        else die("입력 파일은 하나만 지정하세요");
    }
    if (!in_path)
        die("사용법: %s <sim.log> -o <clean.log> -a <agg.tsv> [--no-progress]", argv[0]);

    char defo[4096], defa[4096];
    if (!out_path) { snprintf(defo, sizeof defo, "%s.clean", in_path); out_path = defo; }
    if (!agg_path) { snprintf(defa, sizeof defa, "%s.agg",   in_path); agg_path = defa; }

    int fin = open(in_path, O_RDONLY);
    if (fin < 0) die("입력을 열 수 없습니다: %s", in_path);
    struct stat st;
    if (fstat(fin, &st)) die("stat 실패");
    double total_bytes = (double)st.st_size;
#ifdef POSIX_FADV_SEQUENTIAL
    posix_fadvise(fin, 0, 0, POSIX_FADV_SEQUENTIAL);
#endif

    int fout = open(out_path, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fout < 0) die("출력을 열 수 없습니다: %s", out_path);

    Reader r; rd_init(&r, fin);
    Writer w; wr_init(&w, fout);
    Agg agg;  agg_init(&agg);

    unsigned long long nline = 0, nviol = 0, kept = 0, unparsed = 0;
    unsigned long long nbytes = 0;
    unsigned long long line_mark = prog_lines, viol_mark = prog_viol;
    double t0 = now_sec();
    int tty = isatty(STDERR_FILENO);

    /* 형식이 어긋난 블록을 원본 그대로 되살리기 위한 임시 버퍼 */
    size_t blk_cap = 1u << 16, blk_len = 0;
    unsigned char *blk = malloc(blk_cap);
    if (!blk) die("메모리 부족");

#define REPORT(final) do {                                                    \
        double el = now_sec() - t0;                                           \
        double rate = el > 0 ? (double)nbytes / el : 0;                       \
        double pct = total_bytes > 0 ? nbytes * 100.0 / total_bytes : 0;      \
        char hb[32], eb[16];                                                  \
        human((double)nbytes, hb, sizeof hb);                                 \
        hms(rate > 0 ? (total_bytes - nbytes) / rate : -1, eb, sizeof eb);    \
        fprintf(stderr, "[%5.1f%%] %13llu lines %11llu viol %9s %6.1f MB/s "  \
                        "ETA %s   %s",                                        \
                pct, nline, nviol, hb, rate / 1e6, eb,                        \
                (final) ? "\n" : (tty ? "\r" : "\n"));                        \
        fflush(stderr);                                                       \
    } while (0)

    unsigned char *p; size_t l;
    while (rd_line(&r, &p, &l)) {
        nline++; nbytes += l;

        /* ---- fast path: 대부분의 줄은 여기서 끝난다 ---- */
        if (l < HDR_LEN || memcmp(p, HDR, HDR_LEN) != 0 ||
            !memmem(p, l, HKEY, sizeof(HKEY) - 1)) {
            wr_put(&w, p, l);
            kept++;
            if (!no_progress && nline >= line_mark) {
                line_mark += prog_lines;
                REPORT(0);
            }
            continue;
        }
        if (!no_progress && nline >= line_mark) {
            line_mark += prog_lines;
            REPORT(0);
        }

        /* ---- violation 블록 후보 ---- */
        char scope_buf[1024], check_buf[128], file_buf[1024];
        size_t scope_len = 0, check_len = 0, file_len = 0;
        int has_scope = 0, has_check = 0, has_file = 0;
        long cell_line = -1;
        long long t_fs = -1;
        /* 형식이 어긋났을 때 원본 그대로 되살리기 위해 헤더 줄부터 보관한다.
         * (고정 크기 배열을 쓰면 긴 줄이 잘려 데이터가 손실된다) */
        blk_len = 0;
        if (l > blk_cap) {
            while (l > blk_cap) blk_cap *= 2;
            blk = realloc(blk, blk_cap);
            if (!blk) die("메모리 부족");
        }
        memcpy(blk, p, l);
        blk_len = l;

        for (int k = 0; k < MAX_LOOKAHEAD; k++) {
            if (!rd_line(&r, &p, &l)) break;
            if (is_blank(p, l) || is_header(p, l)) {
                rd_unget(&r, l);        /* 되돌리므로 여기선 세지 않는다 */
                break;
            }
            nline++; nbytes += l;

            /* 원본 보존용 사본 */
            if (blk_len + l > blk_cap) {
                while (blk_len + l > blk_cap) blk_cap *= 2;
                blk = realloc(blk, blk_cap);
                if (!blk) die("메모리 부족");
            }
            memcpy(blk + blk_len, p, l);
            blk_len += l;

            const unsigned char *end = p + l;
            const unsigned char *q;

            if (!has_scope && (q = after_label(p, l, "Scope:", 6))) {
                scope_len = copy_tok(scope_buf, sizeof scope_buf, q,
                                     token_len(q, end));
                has_scope = 1;
                continue;
            }
            if (!has_file && (q = after_label(p, l, "File:", 5))) {
                file_len = copy_tok(file_buf, sizeof file_buf, q,
                                    token_len(q, end));
                has_file = 1;
                const unsigned char *ln = after_label(p, l, "line", 4);
                if (ln) {
                    while (ln < end && (*ln == '=' || *ln == ' ')) ln++;
                    if (ln < end && *ln >= '0' && *ln <= '9')
                        cell_line = strtol((const char *)ln, NULL, 10);
                }
                continue;
            }
            if (t_fs < 0 && (q = after_label(p, l, "Time:", 5))) {
                char *ep = NULL;
                double v = strtod((const char *)q, &ep);
                if (ep && ep != (const char *)q) {
                    const unsigned char *u = (const unsigned char *)ep;
                    while (u < end && (*u == ' ' || *u == '\t')) u++;
                    size_t ul = token_len(u, end);
                    long long m = unit_mult(u, ul);
                    if (m > 0) t_fs = (long long)(v * (double)m);
                }
                break;                  /* Time 은 블록의 마지막 줄 */
            }
            if (!has_check) {
                const unsigned char *d = memchr(p, '$', l);
                if (d) {
                    d++;
                    const unsigned char *e2 = d;
                    while (e2 < end && (*e2 == '_' || *e2 == '<' || *e2 == '>' ||
                           (*e2 >= 'a' && *e2 <= 'z') || (*e2 >= 'A' && *e2 <= 'Z') ||
                           (*e2 >= '0' && *e2 <= '9')))
                        e2++;
                    check_len = copy_tok(check_buf, sizeof check_buf, d,
                                         (size_t)(e2 - d));
                    has_check = 1;
                }
            }
        }

        if (!has_scope) {
            /* 예상 형식이 아니다 -> 헤더까지 포함해 원본 그대로 살려 둔다 */
            wr_put(&w, blk, blk_len);
            kept += 1;
            unparsed++;
            continue;
        }

        agg_add(&agg, scope_buf, scope_len,
                has_check ? check_buf : "?", has_check ? check_len : 1,
                has_file  ? file_buf  : "?", has_file  ? file_len  : 1,
                cell_line, t_fs);
        nviol++;

        if (!no_progress && nviol >= viol_mark) {
            viol_mark += prog_viol;
            REPORT(0);
        }

        if (!keep_blank) {
            for (int n = 0; n < MAX_TRAIL_BLANK; n++) {
                if (!rd_line(&r, &p, &l)) break;
                if (!is_blank(p, l)) { rd_unget(&r, l); break; }
                nline++; nbytes += l;   /* 여기서 버려지므로 센다 */
            }
        }
    }

    wr_flush(&w);
    if (close(fout)) die("출력 닫기 실패");
    close(fin);
    agg_dump(&agg, agg_path);

    if (!no_progress) REPORT(1);

    unsigned long long out_size = 0;
    struct stat ost;
    if (stat(out_path, &ost) == 0) out_size = (unsigned long long)ost.st_size;

    double dt = now_sec() - t0;
    char hb[32], ob[32];
    human((double)nbytes, hb, sizeof hb);
    human((double)out_size, ob, sizeof ob);
    fprintf(stderr,
            "\n[strip 완료] %.1fs (%.1f MB/s)\n"
            "  입력      : %-28s %12s\n"
            "  정리 로그 : %-28s %12s\n"
            "  집계      : %-28s %zu 곳\n"
            "  violation : %llu 건  (남긴 줄 %llu)\n",
            dt, dt > 0 ? nbytes / 1e6 / dt : 0,
            in_path, hb, out_path, ob, agg_path, agg.uniq, nviol, kept);
    if (unparsed)
        fprintf(stderr,
                "  주의: 형식이 다른 violation 후보 %llu 건은 원본에 남겼습니다.\n",
                unparsed);
    /* warn_bytes==0 이면 경고를 끈다. 파이썬이 여러 파일을 조율할 때는
     * 여기서 파일마다 찍으면 마지막 요약에 묻히므로, 파이썬이 맨 끝에
     * 한 번만 모아서 출력하도록 넘긴다. */
    if (warn_bytes > 0 && out_size >= warn_bytes)
        warn_big_clean(out_path, ob);
    return 0;
}
