import urllib.request
import re
import html
import time
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from psycopg2.extras import execute_values
from database import get_db, init_db

KST = timezone(timedelta(hours=9))
UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_M = 'Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'

GALLERY_ID = "rescene1"
DAYS_RETENTION = 60

def log(tag, msg):
    now_str = datetime.now(KST).strftime('%H:%M:%S')
    print(f"[{now_str}][{tag}] {msg}", flush=True)

def fetch_html(url, ua=UA_PC, timeout=8):
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', 'ignore')
    except Exception:
        return ""

def parse_list_page(gallery_id, page=1):
    """목록 페이지 파싱 (list_num=100으로 1회 요청당 100건 수집)"""
    url = f"https://gall.dcinside.com/mgallery/board/lists/?id={gallery_id}&list_num=100&page={page}"
    h = fetch_html(url, ua=UA_PC)
    if not h:
        return []

    posts = []
    rows = re.findall(r'<tr class="ub-content us-post[^"]*" data-no="(\d+)"(.*?)</tr>', h, re.DOTALL)
    for no, row in rows:
        if 'icon_notice' in row:
            continue
        tit_m = re.search(r'<td class="gall_tit[^"]*".*?<a[^>]*>(.*?)</a>', row, re.DOTALL)
        if not tit_m:
            continue
        title = html.unescape(re.sub(r'<[^>]+>', '', tit_m.group(1))).strip()

        date_m = re.search(r'class="gall_date"[^>]*title="([^"]*)"', row)
        if not date_m:
            continue
        try:
            created_at = datetime.strptime(date_m.group(1).strip(), "%Y-%m-%d %H:%M:%S")
        except Exception:
            continue

        writer_m = re.search(r'class="gall_writer[^"]*"(.*?)</td>', row, re.DOTALL)
        writer_raw = writer_m.group(1) if writer_m else ''
        nick_m = re.search(r'data-nick="([^"]*)"', writer_raw)
        uid_m = re.search(r'data-uid="([^"]*)"', writer_raw)
        ip_m = re.search(r'data-ip="([^"]*)"', writer_raw)

        nickname = nick_m.group(1) if nick_m else ''
        uid = uid_m.group(1) if uid_m else ''
        ip = ip_m.group(1) if ip_m else ''
        author_code = uid if uid else ip

        posts.append((int(no), gallery_id, title, "", nickname, author_code, created_at))
    return posts

def fetch_post_content(gallery_id, post_id):
    """모바일 상세 페이지 본문 텍스트 추출"""
    url = f"https://m.dcinside.com/board/{gallery_id}/{post_id}"
    h = fetch_html(url, ua=UA_M, timeout=6)
    if not h:
        return ""

    body_m = re.search(r'<div class="thum-txtin"[^>]*>(.*?)</div>', h, re.DOTALL)
    if not body_m:
        body_m = re.search(r'<div class="writing_view_box"[^>]*>(.*?)</div>', h, re.DOTALL)
    if not body_m:
        return ""

    raw_html = body_m.group(1)
    clean = re.sub(r'<script.*?</script>', '', raw_html, flags=re.DOTALL)
    clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', clean))
    return re.sub(r'\s+', ' ', text).strip()

def save_metadata_batch(records):
    """100건 단위 초고속 배치 저장"""
    if not records:
        return 0
    query = """
        INSERT INTO dc_posts (post_id, gallery_id, title, content, nickname, author_code, created_at)
        VALUES %s
        ON CONFLICT (post_id) DO NOTHING;
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            execute_values(cur, query, records)
            count = cur.rowcount
        conn.commit()
    return count

# ================= 1. 초고속 백필 스레드 (과거 60일치 목록 40만 건 수집) =================
def metadata_backfill_worker():
    log("백필", "🚀 과거 60일치 메타데이터(제목/작성자/IP) 초고속 수집 시작!")
    target_cutoff = datetime.now() - timedelta(days=DAYS_RETENTION)

    # 이미 수집된 건수가 있으면 그 지점부터 스마트 이어받기
    start_page = 1
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM dc_posts")
                cnt = cur.fetchone()[0]
                if cnt > 1000:
                    start_page = max(1, (cnt // 100) - 15)
                    log("백필", f"📍 기존 수집량({cnt}건) 확인. Page {start_page}부터 바로 이어받기 시작합니다!")
    except Exception:
        start_page = 1

    page = start_page
    consecutive_empty = 0
    total_saved = 0

    while True:
        try:
            posts = parse_list_page(GALLERY_ID, page=page)
            if not posts:
                consecutive_empty += 1
                if consecutive_empty >= 5:
                    log("백필", f"페이지 {page}에서 연속 5회 빈 응답. 백필 정상 종료.")
                    break
                time.sleep(1)
                continue
            consecutive_empty = 0

            # 저장
            saved = save_metadata_batch(posts)
            total_saved += saved

            # 끌올 글 방지: 해당 페이지의 "가장 최신 글(Top post)"이 60일 전으로 내려갔는지로 완벽 판정
            top_dt = posts[0][6]
            oldest_dt = min(p[6] for p in posts)

            if page % 10 == 0 or page == start_page:
                log("백필", f"📄 [Page {page}] {len(posts)}건 중 {saved}건 신규 저장 (페이지 상단: {top_dt} / 최저: {oldest_dt})")

            # 페이지 최상단 글까지 60일 기준일보다 오래되었을 때만 진짜 종료!
            if top_dt < target_cutoff:
                log("백필", f"🎯 60일 기준일 도달! (페이지 최신글: {top_dt}) 총 {total_saved}건 추가 색인 완료.")
                break

            page += 1
            time.sleep(0.08) # 방화벽 차단 방지용 안전 딜레이

        except Exception as e:
            log("백필", f"⚠️ Page {page} 처리 중 일시적 오류: {e} (재시도 중...)")
            time.sleep(1)

# ================= 2. 백그라운드 본문 수집 워커 (멀티스레드) =================
def content_fill_worker():
    log("본문워커", "🛠️ 본문(Content) 백그라운드 보충 워커 시작")
    while True:
        try:
            # 본문이 비어있는 최근 60일 이내 글 100건 가져오기
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT post_id FROM dc_posts
                        WHERE (content IS NULL OR content = '')
                          AND created_at >= NOW() - INTERVAL '60 days'
                        ORDER BY post_id DESC
                        LIMIT 100
                    """)
                    target_ids = [row[0] for row in cur.fetchall()]

            if not target_ids:
                time.sleep(10)
                continue

            # 5개 멀티스레드로 동시 본문 수집
            def fetch_single(pid):
                cnt = fetch_post_content(GALLERY_ID, pid)
                return pid, (cnt if cnt else " ")

            with ThreadPoolExecutor(max_workers=5) as executor:
                results = list(executor.map(fetch_single, target_ids))

            # DB 일괄 업데이트
            with get_db() as conn:
                with conn.cursor() as cur:
                    for pid, text in results:
                        cur.execute("UPDATE dc_posts SET content = %s WHERE post_id = %s", (text, pid))
                conn.commit()

            time.sleep(0.5)

        except Exception as e:
            time.sleep(5)

# ================= 3. 실시간 모니터링 루프 (최신 글 즉시 수집) =================
def realtime_worker():
    log("실시간", "👀 실시간 1분 모니터링 가동 (새 글 즉시 반영)")
    last_cleanup = None

    while True:
        try:
            # 1~2페이지 확인
            recent_posts = []
            for p in range(1, 3):
                recent_posts.extend(parse_list_page(GALLERY_ID, page=p))
                time.sleep(0.1)

            # 신규 글만 필터링
            post_ids = [p[0] for p in recent_posts]
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT post_id FROM dc_posts WHERE post_id = ANY(%s)", (post_ids,))
                    existing = {row[0] for row in cur.fetchall()}

            new_posts = [p for p in recent_posts if p[0] not in existing]
            if new_posts:
                # 신규 글은 본문까지 바로 긁어서 저장
                records_to_insert = []
                for p in new_posts:
                    body = fetch_post_content(p[1], p[0])
                    records_to_insert.append((p[0], p[1], p[2], body, p[4], p[5], p[6]))
                    time.sleep(0.05)

                saved = save_metadata_batch(records_to_insert)
                if saved > 0:
                    log("실시간", f"⚡ 최신 글 {saved}건 즉시 본문 포함 색인 완료!")

            # 매일 새벽 60일 롤링 삭제
            today = datetime.now(KST).date()
            if last_cleanup != today:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM dc_posts WHERE created_at < NOW() - INTERVAL '60 days'")
                        deleted = cur.rowcount
                    conn.commit()
                if deleted > 0:
                    log("청소", f"🧹 60일 지난 글 {deleted}건 삭제 완료")
                last_cleanup = today

        except Exception as e:
            log("실시간", f"에러: {e}")

        time.sleep(60)

if __name__ == "__main__":
    init_db()

    # 1. 과거 데이터 백필 스레드 시작
    t_backfill = threading.Thread(target=metadata_backfill_worker, daemon=True)
    t_backfill.start()

    # 2. 본문 보충 워커 스레드 시작
    t_content = threading.Thread(target=content_fill_worker, daemon=True)
    t_content.start()

    # 3. 메인 스레드에서 실시간 모니터링 실행
    realtime_worker()
