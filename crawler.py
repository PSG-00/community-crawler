import urllib.request
import re
import html
import time
import threading
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
from psycopg2.extras import execute_values
from database import get_db, init_db
from analyzer import run_analyzer_loop

KST = timezone(timedelta(hours=9))
UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_M = 'Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'

GALLERY_ID = "rescene1"
DAYS_RETENTION = 90

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
    """모바일 상세 페이지 본문 텍스트 추출 (이미지 뒤 본문 누락 방지 & 줄바꿈 보존)"""
    url = f"https://m.dcinside.com/board/{gallery_id}/{post_id}"
    h = fetch_html(url, ua=UA_M, timeout=6)
    if not h:
        return ""

    # 중요: thum-txtin 안의 중첩 <div>(이미지/광고)에 낚여 본문이 잘리지 않도록, 다음 섹션 직전까지 전체 블록 추출
    body_m = re.search(
        r'<div class="thum-txtin"[^>]*>(.*?)(?:<div class="reco-search"|<div class="comment|<div class="gallview-btn|<div class="user-info|$)',
        h, re.DOTALL
    )
    if not body_m:
        body_m = re.search(r'<div class="writing_view_box"[^>]*>(.*?)</div>', h, re.DOTALL)
    if not body_m:
        return ""

    raw_html = body_m.group(1)
    clean = re.sub(r'<script.*?</script>', '', raw_html, flags=re.DOTALL)
    clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
    clean = re.sub(r'<br\s*/?>', '\n', clean, flags=re.IGNORECASE)
    clean = re.sub(r'</?(p|div)[^>]*>', '\n', clean, flags=re.IGNORECASE)
    text = html.unescape(re.sub(r'<[^>]+>', ' ', clean))
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()

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

# ================= 1. 초고속 백필 스레드 (과거 90일치 목록 57만 건 수집) =================
def metadata_backfill_worker():
    log("백필", "🚀 과거 90일치 메타데이터(제목/작성자/IP) 초고속 수집 시작!")
    target_cutoff = datetime.now(KST).replace(tzinfo=None) - timedelta(days=DAYS_RETENTION)

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
    log("본문워커", "🛠️ 본문(Content) 고속 멀티스레드 보충 워커 시작 (워커 수: 10개)")
    total_filled = 0

    while True:
        try:
            # 본문이 비어있거나 기존 파서 오류로 공백(' ') 처리된 최근 60일 글 100건 가져오기
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT post_id FROM dc_posts
                        WHERE (content IS NULL OR content = '' OR content = ' ')
                          AND created_at >= NOW() - INTERVAL '90 days'
                        ORDER BY post_id DESC
                        LIMIT 100
                    """)
                    target_ids = [row[0] for row in cur.fetchall()]

            if not target_ids:
                time.sleep(10)
                continue

            # 10개 멀티스레드로 동시 본문 수집
            def fetch_single(pid):
                cnt = fetch_post_content(GALLERY_ID, pid)
                # 빈 텍스트면 다음 번에 중복 시도 방지를 위해 특별 마커 "__EMPTY__" 저장
                return pid, (cnt if cnt else "__EMPTY__")

            with ThreadPoolExecutor(max_workers=10) as executor:
                results = list(executor.map(fetch_single, target_ids))

            # DB 일괄 업데이트
            with get_db() as conn:
                with conn.cursor() as cur:
                    for pid, text in results:
                        cur.execute("UPDATE dc_posts SET content = %s WHERE post_id = %s", (text, pid))
                conn.commit()

            total_filled += len(results)
            if total_filled % 500 == 0 or total_filled <= 100:
                log("본문워커", f"📥 본문 보충 진행 중: 누적 {total_filled}건 완료")

            time.sleep(0.3)

        except Exception as e:
            time.sleep(5)

# ================= 3. 실시간 모니터링 루프 (최신 글 즉시 수집) =================
def realtime_worker():
    log("실시간", "👀 실시간 15초 모니터링 가동 (갭 감지 동적 페이징 탑재)")
    last_cleanup = None
    consecutive_empty = 0

    # DB에 보관된 가장 최신 post_id 로드
    last_seen_id = None
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(post_id) FROM dc_posts WHERE gallery_id = %s", (GALLERY_ID,))
                row = cur.fetchone()
                if row and row[0]:
                    last_seen_id = int(row[0])
                    log("실시간", f"📍 최근 기준 글 번호(post_id): {last_seen_id}")
    except Exception as e:
        log("실시간", f"초기 글 번호 조회 실패: {e}")
        last_seen_id = None

    while True:
        try:
            # 기본 1페이지부터 시작하되, 직전 수집 시점 글 번호(last_seen_id)와 갭이 생기면 2, 3페이지로 동적 확장
            recent_posts = []
            page = 1
            max_pages = 5  # 도배 폭발 시 최대 500건까지 한 번에 추적

            while page <= max_pages:
                page_posts = parse_list_page(GALLERY_ID, page=page)
                if not page_posts:
                    break
                recent_posts.extend(page_posts)

                # 만약 이미 알고 있는 최신 글 번호(last_seen_id) 이하의 글이 현재 페이지에 포함되어 있다면,
                # 직전 수집 시점과 완벽하게 연결된 것이므로 다음 페이지를 볼 필요 없이 즉시 완료!
                oldest_in_page = min(p[0] for p in page_posts)
                if last_seen_id is not None and oldest_in_page <= last_seen_id:
                    break

                # 최초 실행이거나 화력이 폭발하여 1페이지 100건이 모두 신규 글인 경우 다음 페이지 추가 확인
                page += 1
                time.sleep(0.1)

            if not recent_posts:
                consecutive_empty += 1
                if consecutive_empty >= 3:
                    log("실시간", "⚠️ 디시인사이드 응답 지연/차단 징후 감지: 45초 쿨다운 대기")
                    time.sleep(45)
                    consecutive_empty = 0
                    continue
                time.sleep(5)
                continue

            consecutive_empty = 0

            # 이번 루프에서 확인한 가장 최신 글 번호 갱신
            current_max_id = max(p[0] for p in recent_posts)
            if last_seen_id is None or current_max_id > last_seen_id:
                last_seen_id = current_max_id

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

            # 매일 새벽 90일 롤링 삭제
            today = datetime.now(KST).date()
            if last_cleanup != today:
                with get_db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("DELETE FROM dc_posts WHERE created_at < NOW() - INTERVAL '90 days'")
                        deleted = cur.rowcount
                    conn.commit()
                if deleted > 0:
                    log("청소", f"🧹 90일 지난 글 {deleted}건 삭제 완료")
                last_cleanup = today

        except Exception as e:
            log("실시간", f"에러: {e}")
            time.sleep(5)

        time.sleep(15)

if __name__ == "__main__":
    init_db()

    # 1. 과거 데이터 백필 스레드 시작
    t_backfill = threading.Thread(target=metadata_backfill_worker, daemon=True)
    t_backfill.start()

    # 2. 본문 보충 워커 스레드 시작
    t_content = threading.Thread(target=content_fill_worker, daemon=True)
    t_content.start()

    # 3. 실시간 토픽 타임라인 감지 및 AI 요약 워커 시작
    t_analyzer = threading.Thread(target=run_analyzer_loop, daemon=True)
    t_analyzer.start()

    # 4. 메인 스레드에서 실시간 모니터링 실행
    realtime_worker()
