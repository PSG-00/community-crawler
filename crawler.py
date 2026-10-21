import urllib.request
import re
import html
import time
import argparse
from datetime import datetime, timedelta, timezone
from database import get_db, init_db

KST = timezone(timedelta(hours=9))
UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_M = 'Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'

def log(msg):
    now_str = datetime.now(KST).strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] {msg}", flush=True)

def fetch_html(url, ua=UA_PC, timeout=10):
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode('utf-8', 'ignore')
    except Exception as e:
        return ""

def parse_list_page(gallery_id, page=1):
    """갤러리 목록 페이지 파싱 (글 번호, 제목, 작성자, 작성일)"""
    url = f"https://gall.dcinside.com/mgallery/board/lists/?id={gallery_id}&page={page}"
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

        posts.append({
            "post_id": int(no),
            "gallery_id": gallery_id,
            "title": title,
            "nickname": nickname,
            "author_code": author_code,
            "created_at": created_at
        })
    return posts

def fetch_post_content(gallery_id, post_id):
    """모바일 상세 페이지에서 본문 텍스트 추출"""
    url = f"https://m.dcinside.com/board/{gallery_id}/{post_id}"
    h = fetch_html(url, ua=UA_M, timeout=8)
    if not h:
        return ""

    body_m = re.search(r'<div class="thum-txtin"[^>]*>(.*?)</div>', h, re.DOTALL)
    if not body_m:
        body_m = re.search(r'<div class="writing_view_box"[^>]*>(.*?)</div>', h, re.DOTALL)
    if not body_m:
        return ""

    raw_html = body_m.group(1)
    # 스크립트, 스타일 태그 제거
    clean = re.sub(r'<script.*?</script>', '', raw_html, flags=re.DOTALL)
    clean = re.sub(r'<style.*?</style>', '', clean, flags=re.DOTALL)
    # 태그 공백 변환 및 텍스트 추출
    text = html.unescape(re.sub(r'<[^>]+>', ' ', clean))
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def save_posts(posts):
    """게시글 목록 저장 (이미 존재하는 글 제외)"""
    if not posts:
        return 0

    post_ids = [p["post_id"] for p in posts]
    with get_db() as conn:
        with conn.cursor() as cur:
            # 이미 저장된 post_id 조회
            cur.execute("SELECT post_id FROM dc_posts WHERE post_id = ANY(%s)", (post_ids,))
            existing_ids = {row[0] for row in cur.fetchall()}

    new_posts = [p for p in posts if p["post_id"] not in existing_ids]
    if not new_posts:
        return 0

    # 신규 글에 대해 본문 수집
    inserted_count = 0
    with get_db() as conn:
        with conn.cursor() as cur:
            for p in new_posts:
                # 딜레이 살짝 주며 본문 가져오기
                content = fetch_post_content(p["gallery_id"], p["post_id"])
                cur.execute("""
                    INSERT INTO dc_posts (post_id, gallery_id, title, content, nickname, author_code, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (post_id) DO NOTHING;
                """, (
                    p["post_id"], p["gallery_id"], p["title"], content,
                    p["nickname"], p["author_code"], p["created_at"]
                ))
                inserted_count += 1
                time.sleep(0.05)
        conn.commit()
    return inserted_count

def cleanup_old_posts(days=60):
    """60일 지난 글 일괄 삭제"""
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM dc_posts 
                WHERE created_at < NOW() - INTERVAL '%s days';
            """, (days,))
            deleted = cur.rowcount
        conn.commit()
    if deleted > 0:
        log(f"🧹 [정리] {days}일 지난 과거 글 {deleted}건 삭제 완료")

def run_realtime(gallery_id="rescene1", interval_sec=60):
    """1분 주기 실시간 크롤링 루프"""
    log(f"🚀 [{gallery_id}] 실시간 크롤러 시작 (수집 주기: {interval_sec}초)")
    last_cleanup_date = None

    while True:
        try:
            # 1. 최신 1~2페이지 수집 (약 50~100건 확인)
            all_recent_posts = []
            for p in range(1, 3):
                page_posts = parse_list_page(gallery_id, page=p)
                all_recent_posts.extend(page_posts)
                time.sleep(0.2)

            added = save_posts(all_recent_posts)
            if added > 0:
                log(f"📥 신규 글 {added}건 DB 저장 완료")

            # 2. 매일 새벽 1회 60일 롤링 삭제
            today = datetime.now(KST).date()
            if last_cleanup_date != today:
                cleanup_old_posts(days=60)
                last_cleanup_date = today

        except Exception as e:
            log(f"⚠️ 크롤링 에러 발생: {e}")

        time.sleep(interval_sec)

def run_backfill(gallery_id="rescene1", days=60):
    """과거 N일치 데이터 백필 (천천히 역순으로 수집)"""
    log(f"⏳ [{gallery_id}] 과거 {days}일치 데이터 백필 시작...")
    target_cutoff = datetime.now() - timedelta(days=days)
    
    page = 1
    total_saved = 0
    while True:
        posts = parse_list_page(gallery_id, page=page)
        if not posts:
            log(f"페이지 {page}에 데이터가 없어 백필을 종료합니다.")
            break

        # 가장 오래된 글이 기준일보다 이전인지 확인
        oldest_on_page = min(p["created_at"] for p in posts)
        added = save_posts(posts)
        total_saved += added
        log(f"📄 [Page {page}] {len(posts)}건 중 신규 {added}건 저장 (최저 날짜: {oldest_on_page})")

        if oldest_on_page < target_cutoff:
            log(f"🎯 목표 기준일({target_cutoff}) 도달! 총 {total_saved}건 백필 완료.")
            break

        page += 1
        time.sleep(0.3) # 디시 방화벽 차단 방지용 안전 딜레이

if __name__ == "__main__":
    init_db()

    parser = argparse.ArgumentParser(description="DC Inside Gallery Crawler")
    parser.add_argument("--gallery", default="rescene1", help="갤러리 ID (기본: rescene1)")
    parser.add_argument("--backfill", type=int, default=0, help="과거 N일치 백필 실행 (예: --backfill 60)")
    parser.add_argument("--interval", type=int, default=60, help="실시간 수집 주기(초) (기본: 60)")
    args = parser.parse_args()

    if args.backfill > 0:
        run_backfill(gallery_id=args.gallery, days=args.backfill)
    else:
        run_realtime(gallery_id=args.gallery, interval_sec=args.interval)
