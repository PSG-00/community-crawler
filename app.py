import time
import html
import re
import json
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from database import get_db, init_db
from analyzer import TopicAnalyzer
from notifier import validate_webhook_url, send_test_webhook

try:
    init_db()
except Exception as _db_err:
    print(f"⚠️ DB 스키마 초기화 건너뜀: {_db_err}")

app = FastAPI(title="DC Inside Community 90-Day Archive Search")
templates = Jinja2Templates(directory="templates")

UA_PC = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
UA_M = 'Mozilla/5.0 (Linux; Android 13; SM-G981B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36'

ADMIN_PASSWORD = "remine0326"
ADMIN_TOKEN = hashlib.sha256(f"rescene_admin_{ADMIN_PASSWORD}".encode()).hexdigest()


def verify_admin(request: Request) -> bool:
    cookie_token = request.cookies.get("admin_auth", "")
    header_pw = request.headers.get("X-Admin-Password", "")
    return cookie_token == ADMIN_TOKEN or header_pw == ADMIN_PASSWORD


def check_dc_post_deleted(gallery_id: str, post_id: int, timeout: int = 4) -> Optional[bool]:
    """
    디시인사이드 원본 게시글이 현재 삭제되었는지 실시간 확인:
    - True: 디시인사이드에서 삭제됨 (404 / 410 / 에러 리다이렉트 / 삭제 안내 문구)
    - False: 디시인사이드에 정상 존재함
    - None: 일시적 네트워크 타임아웃 또는 판단 불가 (오탐 방지)
    """
    url = f"https://gall.dcinside.com/mgallery/board/view/?id={gallery_id}&no={post_id}"
    req = urllib.request.Request(url, headers={
        'User-Agent': UA_PC,
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            final_url = resp.geturl() or ""
            if "error" in final_url or "/board/lists" in final_url:
                return True
            chunk = resp.read(16384).decode("utf-8", "ignore")
            if (
                "해당 글은 삭제되었습니다" in chunk
                or "존재하지 않는 게시물입니다" in chunk
                or "삭제된 게시물입니다" in chunk
                or " 정상적인 접근이 아닙니다" in chunk
                or "derror/deleted" in chunk
            ):
                return True
            return False
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return True
        return None
    except Exception:
        return None


def fetch_dc_post_content_live(gallery_id: str, post_id: int, timeout: int = 5) -> str:
    """DB에 본문이 아직 비어있는 과거 백필 글 열람 시 모바일 페이지에서 실시간 본문 보충"""
    url = f"https://m.dcinside.com/board/{gallery_id}/{post_id}"
    req = urllib.request.Request(url, headers={
        'User-Agent': UA_M,
        'Accept-Language': 'ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7'
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            h = resp.read().decode("utf-8", "ignore")
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
    except Exception:
        return ""


def build_dot_tolerant_regex(term: str) -> str:
    """글자 사이에 점/기호를 넣어 우회한 단어(예: 우.리.은.행, 서.수)까지 매칭하는 정규식"""
    chars = [re.escape(ch) for ch in term.strip() if ch.strip()]
    if len(chars) <= 1:
        return re.escape(term.strip())
    return r"[.,/·•_~\-\s]{0,2}".join(chars)

def highlight_keywords_in_text(text: str, keywords: list) -> str:
    """텍스트 내 모든 키워드에 <mark> 하이라이팅 적용 (줄바꿈 보존, 점 우회 표기 포함)"""
    if not text:
        return ""
    escaped = html.escape(text)
    valid_kws = [k.strip() for k in keywords if k and len(k.strip()) >= 2]
    for kw in valid_kws:
        pattern = re.compile(build_dot_tolerant_regex(html.escape(kw)), re.IGNORECASE)
        escaped = pattern.sub(r'<mark class="bg-amber-300 text-black px-1 rounded font-bold">\g<0></mark>', escaped)
    # 줄바꿈 태그 변환
    return escaped.replace("\n", "<br>")

def generate_snippet(text: str, keywords: list) -> str:
    """검색어 주변 150자 내외 발췌 스니펫 추출"""
    if not text:
        return ""
    # 연속 줄바꿈 제거 후 단일 라인으로 스니펫 생성
    single_line = re.sub(r'\s+', ' ', text).strip()
    escaped_text = html.escape(single_line)
    valid_kws = [k.strip() for k in keywords if k and len(k.strip()) >= 2]

    if not valid_kws:
        return escaped_text[:160] + ("..." if len(escaped_text) > 160 else "")

    first_pos = len(escaped_text)
    for kw in valid_kws:
        m = re.search(build_dot_tolerant_regex(html.escape(kw)), escaped_text, re.IGNORECASE)
        if m and m.start() < first_pos:
            first_pos = m.start()

    if first_pos != len(escaped_text):
        start = max(0, first_pos - 40)
        end = min(len(escaped_text), first_pos + 120)
        snippet = ("..." if start > 0 else "") + escaped_text[start:end] + ("..." if end < len(escaped_text) else "")
    else:
        snippet = escaped_text[:160] + ("..." if len(escaped_text) > 160 else "")

    for kw in valid_kws:
        pattern = re.compile(build_dot_tolerant_regex(html.escape(kw)), re.IGNORECASE)
        snippet = pattern.sub(r'<mark class="bg-amber-300 text-black px-1 rounded font-bold">\g<0></mark>', snippet)

    return snippet

def parse_search_terms(query_str: str):
    """
    검색어 문자열을 분석하여 (terms, operator) 반환
    예:
      '우리은행 OR 카드' -> (['우리은행', '카드'], 'OR')
      '우리은행 카드'    -> (['우리은행', '카드'], 'AND')
      '우리은행, 카드'   -> (['우리은행', '카드'], 'AND')
      '"우리은행 카드"'   -> (['우리은행 카드'], 'AND')
    """
    q = (query_str or "").strip()
    if not q:
        return [], "AND"
    if re.search(r"\s+OR\s+", q, flags=re.IGNORECASE):
        tokens = re.split(r"\s+OR\s+", q, flags=re.IGNORECASE)
        op = "OR"
        terms = [t.strip().strip('"\'') for t in tokens if t.strip()]
    else:
        op = "AND"
        raw_tokens = re.findall(r'"([^"]+)"|(\S+)', q)
        terms = []
        for t1, t2 in raw_tokens:
            val = (t1 or t2).strip(', \t')
            if val:
                terms.append(val)
    return terms, op

def build_search_filter(terms: list, op: str, target: str, dot_tolerant: bool = False):
    """
    다중 키워드 조건절 및 바인딩 파라미터 생성 (pg_bigm GIN 인덱스 완벽 호환, 시간대 검색 시 점 우회 단어 포함)
    """
    sub_clauses = []
    params = []

    for term in terms:
        pattern = f"%{term}%"
        if target == "title":
            if dot_tolerant:
                sub_clauses.append("(title ILIKE %s OR regexp_replace(title, '[.,/·•_~-]', '', 'g') ILIKE %s)")
                params.extend([pattern, pattern])
            else:
                sub_clauses.append("title ILIKE %s")
                params.append(pattern)
        elif target == "content":
            if dot_tolerant:
                sub_clauses.append("(content ILIKE %s OR regexp_replace(content, '[.,/·•_~-]', '', 'g') ILIKE %s)")
                params.extend([pattern, pattern])
            else:
                sub_clauses.append("content ILIKE %s")
                params.append(pattern)
        elif target == "nickname":
            sub_clauses.append("nickname ILIKE %s")
            params.append(pattern)
        elif target == "code":
            sub_clauses.append("author_code = %s")
            params.append(term)
        else: # all: 제목 또는 본문
            if dot_tolerant:
                sub_clauses.append(
                    "(title ILIKE %s OR content ILIKE %s "
                    "OR regexp_replace(title, '[.,/·•_~-]', '', 'g') ILIKE %s "
                    "OR regexp_replace(content, '[.,/·•_~-]', '', 'g') ILIKE %s)"
                )
                params.extend([pattern, pattern, pattern, pattern])
            else:
                sub_clauses.append("(title ILIKE %s OR content ILIKE %s)")
                params.extend([pattern, pattern])

    if not sub_clauses:
        return "", []

    if len(sub_clauses) == 1:
        return sub_clauses[0], params

    combined = f"({f' {op} '.join(sub_clauses)})"
    return combined, params

def execute_search(
    q: Optional[str] = None,
    target: str = "all",
    sub_q: Optional[str] = None,
    sub_target: str = "all",
    period: str = "all",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    page: int = 1,
    page_size: int = 30
):
    keyword = (q or "").strip()
    sub_keyword = (sub_q or "").strip()
    results = []
    error_msg = None
    elapsed_ms = 0.0
    total_count = 0
    offset = (page - 1) * page_size

    # 1. 다중 키워드 파싱
    terms, op = parse_search_terms(keyword) if keyword else ([], "AND")
    sub_terms, sub_op = parse_search_terms(sub_keyword) if sub_keyword else ([], "AND")

    valid_terms = [t for t in terms if len(t) >= (1 if target == "code" else 2)]
    valid_sub_terms = [t for t in sub_terms if len(t) >= (1 if sub_target == "code" else 2)]

    if keyword and not valid_terms:
        error_msg = "기본 검색어는 최소 2글자 이상 입력해야 합니다."
    elif sub_keyword and not valid_sub_terms:
        error_msg = "결과 내 검색어는 최소 2글자 이상 입력해야 합니다."
    elif valid_terms or valid_sub_terms or period != "all":
        start_time = time.perf_counter()
        # 일반 공개 검색에서는 삭제 처리된 게시글(is_deleted = TRUE)을 원천 제외
        where_clauses = ["is_deleted = FALSE"]
        params = []

        # 시간대 지정 검색(분 단위 범위)이나 오늘(1d) 검색은 스캔 행 수가 적으므로 점 우회 표기(우.리.은.행 등)도 함께 매칭
        is_time_scoped = (
            period == "1d"
            or (period == "custom" and bool(start_date or end_date))
        )

        # 1. 1차 기본 검색 조건 (다중 키워드 지원)
        if valid_terms:
            cond, p = build_search_filter(valid_terms, op, target, dot_tolerant=is_time_scoped)
            if cond:
                where_clauses.append(cond)
                params.extend(p)

        # 2. 2차 결과 내 재검색 조건 (AND 연산 결합)
        if valid_sub_terms:
            sub_cond, sub_p = build_search_filter(valid_sub_terms, sub_op, sub_target, dot_tolerant=is_time_scoped)
            if sub_cond:
                where_clauses.append(sub_cond)
                params.extend(sub_p)

        # 3. 날짜 및 시간대 필터링 조건
        if period == "1d":
            where_clauses.append("created_at >= NOW() - INTERVAL '1 day'")
        elif period == "7d":
            where_clauses.append("created_at >= NOW() - INTERVAL '7 days'")
        elif period == "30d":
            where_clauses.append("created_at >= NOW() - INTERVAL '30 days'")
        elif period == "90d":
            where_clauses.append("created_at >= NOW() - INTERVAL '90 days'")
        elif period == "custom":
            if start_date:
                s_clean = start_date.strip().replace("T", " ")
                where_clauses.append("created_at >= %s::timestamp")
                params.append(s_clean)
            if end_date:
                e_clean = end_date.strip().replace("T", " ")
                if ":" in e_clean:
                    where_clauses.append("created_at <= (%s::timestamp + INTERVAL '59 seconds')")
                    params.append(e_clean)
                else:
                    where_clauses.append("created_at < (%s::date + INTERVAL '1 day')")
                    params.append(e_clean)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT COUNT(*) FROM dc_posts {where_sql}", params)
                total_count = cur.fetchone()[0]

                # 목록 조회 시에는 본문(content)을 반환하지 않고 제목과 메타데이터만 조회 (잊힐 권리 보호)
                query = f"""
                    SELECT post_id, gallery_id, title, nickname, author_code, created_at
                    FROM dc_posts
                    {where_sql}
                    ORDER BY post_id DESC
                    LIMIT %s OFFSET %s
                """
                cur.execute(query, params + [page_size, offset])
                rows = cur.fetchall()

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        # 하이라이트할 모든 유효 단어 수집
        highlight_list = []
        if target in ("all", "title", "content"):
            highlight_list.extend(valid_terms)
        if sub_target in ("all", "title", "content"):
            highlight_list.extend(valid_sub_terms)

        for r in rows:
            p_id, gall, title, nick, code, dt = r
            results.append({
                "post_id": p_id,
                "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
                "title": highlight_keywords_in_text(title, highlight_list),
                "nickname": highlight_keywords_in_text(nick, valid_terms if target == "nickname" else []),
                "author_code": code,
                "created_at": dt.strftime("%Y-%m-%d %H:%M")
            })

    has_next = (offset + page_size) < total_count
    return {
        "results": results,
        "total_count": total_count,
        "elapsed_ms": elapsed_ms,
        "error_msg": error_msg,
        "page": page,
        "has_next": has_next
    }

KST = timezone(timedelta(hours=9))

def get_recent_topics(limit: int = 16):
    """실시간 토픽 타임라인 이벤트 조회"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, gallery_id, window_start, window_end,
                           topic_title, topic_summary, mood_tag, keywords,
                           post_count, sample_post_ids, created_at
                    FROM gallery_events
                    WHERE post_count >= 5
                    ORDER BY window_end DESC, window_start DESC
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()

        topics = []
        now_date = datetime.now(KST).date()
        for r in rows:
            kws = r[7]
            if isinstance(kws, str):
                try:
                    kws = json.loads(kws)
                except Exception:
                    kws = []
            
            w_start = r[2]
            w_end = r[3]
            if w_start.date() == now_date:
                time_display = f"오늘 {w_start.strftime('%H:%M')} ~ {w_end.strftime('%H:%M')}"
            else:
                time_display = f"{w_start.strftime('%m/%d %H:%M')} ~ {w_end.strftime('%H:%M')}"

            # 주요 키워드 리스트
            keyword_list = [k["word"] if isinstance(k, dict) else str(k) for k in kws]

            # 토픽 시간대 검색용 시작/종료 시각 (앞뒤 5분 여유 포함, datetime-local 포맷)
            search_start = (w_start - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")
            search_end = (w_end + timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M")

            topics.append({
                "event_id": r[0],
                "gallery_id": r[1],
                "window_start": str(r[2]),
                "window_end": str(r[3]),
                "search_start": search_start,
                "search_end": search_end,
                "time_display": time_display,
                "title": r[4],
                "summary": r[5],
                "mood": r[6] or "",
                "keywords": kws,
                "keyword_list": keyword_list,
                "post_count": r[8],
                "sample_post_ids": r[9] or []
            })
        return topics
    except Exception as e:
        return []

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: Optional[str] = Query(None),
    target: str = Query("all"),
    sub_q: Optional[str] = Query(None),
    sub_target: str = Query("all"),
    period: str = Query("all"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    tab: str = Query("search")
):
    search_data = execute_search(
        q=q, target=target, sub_q=sub_q, sub_target=sub_target,
        period=period, start_date=start_date, end_date=end_date, page=1
    )

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dc_posts WHERE is_deleted = FALSE")
            db_total_posts = cur.fetchone()[0]

    recent_topics = get_recent_topics(limit=24)

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "q": (q or "").strip(),
            "target": target,
            "sub_q": (sub_q or "").strip(),
            "sub_target": sub_target,
            "period": period,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "active_tab": tab if tab in ("search", "topics") else "search",
            "results": search_data["results"],
            "total_count": search_data["total_count"],
            "elapsed_ms": search_data["elapsed_ms"],
            "error_msg": search_data["error_msg"],
            "db_total_posts": db_total_posts,
            "recent_topics": recent_topics,
            "page": 1,
            "has_next": search_data["has_next"]
        }
    )

@app.get("/api/topics")
def api_topics(limit: int = Query(24, ge=1, le=60)):
    """실시간 토픽 타임라인 API"""
    return {"topics": get_recent_topics(limit)}

@app.get("/api/search")
def api_search(
    q: Optional[str] = Query(None),
    target: str = Query("all"),
    sub_q: Optional[str] = Query(None),
    sub_target: str = Query("all"),
    period: str = Query("all"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    page: int = Query(1, ge=1)
):
    """무한 스크롤 및 API용 검색 엔드포인트"""
    return execute_search(
        q=q, target=target, sub_q=sub_q, sub_target=sub_target,
        period=period, start_date=start_date, end_date=end_date, page=page
    )


@app.get("/api/post/{post_id}/content")
def api_get_post_content(
    post_id: int,
    q: Optional[str] = Query(None),
    sub_q: Optional[str] = Query(None)
):
    """
    본문 보기 요청 시 온디맨드로 디시인사이드 원본 삭제 여부를 실시간 검증:
    - 디시인사이드에서 삭제된 글이면 is_deleted=TRUE로 마킹하고 일반 뷰에서 즉시 삭제/차단(관리자 보관소로 이동)
    - 정상 존재하는 글이면 본문(검색어 하이라이트 포함)을 반환
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT post_id, gallery_id, title, content, is_deleted, last_checked_at
                FROM dc_posts
                WHERE post_id = %s
            """, (post_id,))
            row = cur.fetchone()

    if not row or row[4]:
        return {
            "status": "deleted",
            "post_id": post_id,
            "message": "디시인사이드에서 삭제된 게시글입니다."
        }

    p_id, gall, title, content, is_del, last_checked = row
    now_naive = datetime.now(KST).replace(tzinfo=None)

    # 최근 3분 이내에 이미 생존 확인된 글이 아니라면 디시인사이드에 실시간 확인
    need_live_check = True
    if last_checked and abs((now_naive - last_checked).total_seconds()) < 180:
        need_live_check = False

    if need_live_check:
        dc_deleted = check_dc_post_deleted(gall, p_id)
        if dc_deleted is True:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE dc_posts
                        SET is_deleted = TRUE,
                            deleted_at = NOW(),
                            last_checked_at = NOW()
                        WHERE post_id = %s
                    """, (p_id,))
                conn.commit()
            return {
                "status": "deleted",
                "post_id": p_id,
                "message": "디시인사이드에서 삭제된 게시글입니다. (목록에서 제외 처리됩니다)"
            }
        elif dc_deleted is False:
            # 원문이 살아있는데 DB에 본문이 비어있는 과거 백필 글이라면 즉시 보충
            if not content or content.strip() in ("", "__EMPTY__"):
                fetched_body = fetch_dc_post_content_live(gall, p_id)
                if fetched_body:
                    content = fetched_body
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        UPDATE dc_posts
                        SET content = %s,
                            last_checked_at = NOW()
                        WHERE post_id = %s
                    """, (content, p_id))
                conn.commit()

    raw_content = "" if (not content or content.strip() == "__EMPTY__") else content
    terms1, _ = parse_search_terms(q or "")
    terms2, _ = parse_search_terms(sub_q or "")
    highlight_kws = [t for t in (terms1 + terms2) if len(t) >= 2]

    return {
        "status": "ok",
        "post_id": p_id,
        "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
        "has_content": bool(raw_content.strip()),
        "full_content": highlight_keywords_in_text(raw_content, highlight_kws)
    }


@app.get("/api/stats")
def stats():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MIN(created_at), MAX(created_at) 
                FROM dc_posts
                WHERE is_deleted = FALSE
            """)
            cnt, min_dt, max_dt = cur.fetchone()
    return {
        "total_posts": cnt,
        "oldest_post": str(min_dt) if min_dt else None,
        "latest_post": str(max_dt) if max_dt else None
    }


# ================= 관리자 페이지 & 학습 노트(Learning Note) API =================

@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    is_authed = verify_admin(request)
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"is_authed": is_authed}
    )


@app.post("/api/admin/login")
async def admin_login(request: Request):
    body = await request.json()
    pw = (body.get("password") or "").strip()
    if pw != ADMIN_PASSWORD:
        return JSONResponse({"success": False, "message": "비밀번호가 일치하지 않습니다."}, status_code=401)
    resp = JSONResponse({"success": True})
    resp.set_cookie(
        key="admin_auth",
        value=ADMIN_TOKEN,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
    )
    return resp


@app.post("/api/admin/logout")
def admin_logout():
    resp = JSONResponse({"success": True})
    resp.delete_cookie("admin_auth")
    return resp


@app.get("/api/admin/notes")
def admin_get_notes(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    notes = []
    counts = {
        "total": 0,
        "active": 0,
        "protected": 0,
        "disabled": 0,
        "routine_word": 0,
        "signature_phrase": 0,
        "ritual_word": 0,
        "manual_stop": 0,
    }
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT note_id, pattern_type, target_text, status,
                       hit_count, hour_spread, author_count, max_lift,
                       reason, sample_text, is_manual, updated_at
                FROM learning_notes
                WHERE gallery_id = 'rescene1'
                ORDER BY
                    CASE status WHEN 'protected' THEN 0 WHEN 'active' THEN 1 ELSE 2 END,
                    is_manual DESC,
                    hit_count DESC,
                    updated_at DESC
            """)
            for r in cur.fetchall():
                p_type = r[1]
                st = r[3]
                counts["total"] += 1
                if st in counts:
                    counts[st] += 1
                if p_type in counts and st == "active":
                    counts[p_type] += 1
                notes.append({
                    "note_id": r[0],
                    "pattern_type": p_type,
                    "target_text": r[2],
                    "status": st,
                    "hit_count": r[4],
                    "hour_spread": r[5],
                    "author_count": r[6],
                    "max_lift": r[7],
                    "reason": r[8] or "",
                    "sample_text": r[9] or "",
                    "is_manual": bool(r[10]),
                    "updated_at": r[11].strftime("%m/%d %H:%M") if r[11] else "",
                })
            cur.execute("SELECT COUNT(*) FROM dc_posts WHERE is_deleted = TRUE")
            counts["deleted_posts"] = cur.fetchone()[0]
    return {"notes": notes, "counts": counts, "recent_topics": get_recent_topics(25)}


@app.post("/api/admin/notes/add")
async def admin_add_note(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    p_type = (body.get("pattern_type") or "manual_stop").strip()
    raw_text = (body.get("target_text") or "").strip()
    target_text = raw_text.lstrip("#").strip()
    reason = (body.get("reason") or "").strip()

    if not target_text:
        return JSONResponse({"success": False, "message": "단어나 문구를 입력해 주세요."}, status_code=400)

    if p_type == "protected":
        actual_type = "routine_word"
        status = "protected"
        default_reason = "관리자 지정 보호 단어 (화이트리스트 · 자동 학습 제외 방지)"
    else:
        actual_type = p_type if p_type in ("manual_stop", "signature_phrase", "routine_word", "ritual_word") else "manual_stop"
        status = "active"
        default_reason = "관리자 수동 등록 제외 규칙"

    with get_db() as conn:
        with conn.cursor() as cur:
            # 1. 이미 동일 단어/문구가 학습 노트에 존재하는지 확인
            cur.execute("""
                SELECT note_id, pattern_type, status, reason, target_text
                FROM learning_notes
                WHERE gallery_id = 'rescene1' AND LOWER(target_text) = LOWER(%s)
            """, (target_text,))
            existing = cur.fetchone()

            if existing:
                e_id, e_type, e_status, e_reason, e_orig_text = existing
                # 이미 동일한 상태(active 또는 protected)로 적용 중인 경우
                if e_status == status:
                    if reason and reason != e_reason:
                        cur.execute("""
                            UPDATE learning_notes
                            SET reason = %s, updated_at = NOW()
                            WHERE note_id = %s
                        """, (reason, e_id))
                        conn.commit()

                    if status == "protected":
                        msg = f"'{e_orig_text}'은(는) 이미 영구 보호 단어로 처리되어 있습니다."
                    elif actual_type == "signature_phrase":
                        msg = f"'{e_orig_text}'은(는) 이미 등록된 서명 문구입니다."
                    else:
                        msg = f"'{e_orig_text}'은(는) 이미 처리된 단어입니다."

                    return {
                        "success": True,
                        "already_exists": True,
                        "note_id": e_id,
                        "message": msg
                    }

                # 비활성화(disabled) 상태였던 단어를 다시 등록하여 재활성화하는 경우
                cur.execute("""
                    UPDATE learning_notes
                    SET status = %s,
                        pattern_type = %s,
                        reason = %s,
                        is_manual = TRUE,
                        updated_at = NOW()
                    WHERE note_id = %s
                """, (status, actual_type, reason or default_reason, e_id))
                conn.commit()
                return {
                    "success": True,
                    "already_exists": False,
                    "reactivated": True,
                    "note_id": e_id,
                    "message": f"'{target_text}' 규칙을 다시 활성화했습니다."
                }

            # 2. 엔진 내장 기본 불용어(_GENERIC)에 이미 포함되어 있는지 확인
            from topic_engine import _GENERIC
            if actual_type == "manual_stop" and (target_text in _GENERIC or target_text.lower() in _GENERIC):
                cur.execute("""
                    INSERT INTO learning_notes (
                        gallery_id, pattern_type, target_text, status,
                        hit_count, hour_spread, author_count, max_lift,
                        reason, sample_text, is_manual, updated_at
                    )
                    VALUES ('rescene1', %s, %s, %s, 0, 0, 0, 0.0, %s, '관리자 직접 입력', TRUE, NOW())
                    ON CONFLICT (gallery_id, pattern_type, target_text) DO NOTHING
                    RETURNING note_id;
                """, (actual_type, target_text, status, reason or default_reason))
                row = cur.fetchone()
                nid = row[0] if row else 0
                conn.commit()
                return {
                    "success": True,
                    "already_exists": True,
                    "built_in": True,
                    "note_id": nid,
                    "message": f"'{target_text}'은(는) 이미 처리된 단어입니다. (엔진 기본 불용어 내장)"
                }

            # 3. 신규 단어 정상 등록
            cur.execute("""
                INSERT INTO learning_notes (
                    gallery_id, pattern_type, target_text, status,
                    hit_count, hour_spread, author_count, max_lift,
                    reason, sample_text, is_manual, updated_at
                )
                VALUES ('rescene1', %s, %s, %s, 0, 0, 0, 0.0, %s, '관리자 직접 입력', TRUE, NOW())
                ON CONFLICT (gallery_id, pattern_type, target_text)
                DO UPDATE SET
                    status = EXCLUDED.status,
                    reason = EXCLUDED.reason,
                    is_manual = TRUE,
                    updated_at = NOW()
                RETURNING note_id;
            """, (actual_type, target_text, status, reason or default_reason))
            nid = cur.fetchone()[0]
        conn.commit()
    return {
        "success": True,
        "already_exists": False,
        "note_id": nid,
        "message": f"'{target_text}' 규칙을 등록했습니다."
    }


@app.post("/api/admin/notes/status")
async def admin_update_note_status(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    note_id = body.get("note_id")
    new_status = (body.get("status") or "").strip()
    if not note_id or new_status not in ("active", "protected", "disabled"):
        return JSONResponse({"success": False, "message": "잘못된 요청입니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE learning_notes
                SET status = %s,
                    updated_at = NOW()
                WHERE note_id = %s
            """, (new_status, int(note_id)))
        conn.commit()
    return {"success": True}


@app.post("/api/admin/notes/delete")
async def admin_delete_note(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    note_id = body.get("note_id")
    if not note_id:
        return JSONResponse({"success": False, "message": "note_id가 필요합니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM learning_notes WHERE note_id = %s", (int(note_id),))
        conn.commit()
    return {"success": True}


@app.post("/api/admin/rebuild")
def admin_rebuild_topics(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    rebuilt_cnt = analyzer.reset_and_regenerate_24h()
    return {"success": True, "rebuilt_topics": rebuilt_cnt}


@app.post("/api/admin/deep-audit/start")
def admin_start_deep_audit(request: Request, days: int = Query(7, ge=1, le=30)):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    from crawler import run_deep_audit
    state = run_deep_audit(days=days)
    return {"success": True, "state": state}


@app.get("/api/admin/deep-audit/status")
def admin_get_deep_audit_status(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    from crawler import get_deep_audit_state
    return {"success": True, "state": get_deep_audit_state()}


@app.post("/api/admin/reconcile")
def admin_reconcile_posts(request: Request, pages: int = Query(30, ge=5, le=100)):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    from crawler import reconcile_recent_posts
    res = reconcile_recent_posts(max_pages=pages)
    return {"success": True, **res}


@app.post("/api/admin/topic/delete")
async def admin_delete_topic(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    event_id = body.get("event_id")
    if not event_id:
        return JSONResponse({"success": False, "message": "event_id가 필요합니다."}, status_code=400)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM gallery_events WHERE event_id = %s", (int(event_id),))
        conn.commit()
    return {"success": True}


@app.get("/api/admin/top-authors")
def admin_top_authors(request: Request, days: int = Query(0, ge=0, le=90)):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")

    where_clause = "gallery_id = 'rescene1' AND author_code IS NOT NULL AND author_code != ''"
    params = []
    if days > 0:
        where_clause += " AND created_at >= NOW() - (%s * INTERVAL '1 day')"
        params.append(days)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"""
                SELECT 
                    author_code,
                    COALESCE(MODE() WITHIN GROUP (ORDER BY nickname), 'ㅇㅇ') as main_nickname,
                    COUNT(*) as post_count,
                    MAX(created_at) as last_active,
                    MIN(created_at) as first_active
                FROM dc_posts
                WHERE {where_clause}
                GROUP BY author_code
                ORDER BY post_count DESC
                LIMIT 30;
            """, tuple(params))
            rows = cur.fetchall()

            top_authors = []
            max_cnt = rows[0][2] if rows else 1
            for rank, r in enumerate(rows, 1):
                acode = r[0]
                is_ip = bool(re.match(r'^\d+\.\d+', acode))
                top_authors.append({
                    "rank": rank,
                    "author_code": acode,
                    "is_ip": is_ip,
                    "nickname": r[1] or "ㅇㅇ",
                    "post_count": r[2],
                    "ratio": round((r[2] / max_cnt) * 100, 1),
                    "last_active": r[3].strftime("%m/%d %H:%M") if r[3] else "",
                    "first_active": r[4].strftime("%m/%d %H:%M") if r[4] else "",
                })
    return {"top_authors": top_authors, "days": days}


# ==================== Discord Webhook Endpoints ====================

@app.post("/api/discord/subscribe")
async def discord_subscribe(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "잘못된 요청 형식입니다."}, status_code=400)

    url = (body.get("webhook_url") or "").strip()
    channel_name = (body.get("channel_name") or "").strip()

    if not url:
        return JSONResponse({"success": False, "message": "디스코드 웹훅 URL을 입력해주세요."}, status_code=400)

    if not validate_webhook_url(url):
        return JSONResponse({
            "success": False,
            "message": "올바른 디스코드 웹훅 URL 형식이 아닙니다.\n(https://discord.com/api/webhooks/...)"
        }, status_code=400)

    # 1. 즉시 디스코드로 연결 테스트 전송
    ok, test_msg = send_test_webhook(url, channel_name)
    if not ok:
        return JSONResponse({
            "success": False,
            "message": f"웹훅 전송에 실패했습니다: {test_msg}\nURL이 올바른지 확인해주세요."
        }, status_code=400)

    # 2. DB 저장 (이미 등록된 URL이면 활성화 및 채널명 업데이트)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO discord_webhooks (gallery_id, webhook_url, channel_name, is_active, created_at, last_sent_at)
                VALUES ('rescene1', %s, %s, TRUE, NOW(), NOW())
                ON CONFLICT (webhook_url)
                DO UPDATE SET
                    is_active = TRUE,
                    channel_name = COALESCE(NULLIF(EXCLUDED.channel_name, ''), discord_webhooks.channel_name),
                    last_sent_at = NOW()
                RETURNING webhook_id;
            """, (url, channel_name))
            wh_id = cur.fetchone()[0]
        conn.commit()

    return {
        "success": True,
        "message": "디스코드 웹훅이 성공적으로 등록되었습니다! 디스코드 채널에서 환영 메시지를 확인해보세요."
    }


@app.post("/api/discord/unsubscribe")
async def discord_unsubscribe(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "잘못된 요청 형식입니다."}, status_code=400)

    url = (body.get("webhook_url") or "").strip()
    if not url:
        return JSONResponse({"success": False, "message": "해제할 디스코드 웹훅 URL을 입력해주세요."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM discord_webhooks WHERE webhook_url = %s", (url,))
            deleted = cur.rowcount
        conn.commit()

    if deleted > 0:
        return {"success": True, "message": "디스코드 웹훅 알림 연동이 성공적으로 해제(삭제)되었습니다."}
    return JSONResponse({"success": False, "message": "등록 내역에 존재하지 않는 웹훅 URL입니다."}, status_code=404)



@app.get("/api/admin/webhooks")
def admin_get_webhooks(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT webhook_id, webhook_url, channel_name, is_active, created_at, last_sent_at
                FROM discord_webhooks
                WHERE gallery_id = 'rescene1'
                ORDER BY created_at DESC;
            """)
            rows = cur.fetchall()

    webhooks = []
    for r in rows:
        wh_url = r[1]
        parts = wh_url.split("/")
        masked_url = "/".join(parts[:-1]) + "/••••••••" if len(parts) >= 6 else wh_url

        webhooks.append({
            "webhook_id": r[0],
            "masked_url": masked_url,
            "channel_name": r[2] or "이름 없음",
            "is_active": bool(r[3]),
            "created_at": r[4].strftime("%m/%d %H:%M") if r[4] else "",
            "last_sent_at": r[5].strftime("%m/%d %H:%M") if r[5] else "전송 기록 없음",
        })

    return {"webhooks": webhooks}


@app.post("/api/admin/webhooks/status")
async def admin_update_webhook_status(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    wh_id = body.get("webhook_id")
    is_active = body.get("is_active")
    if wh_id is None or is_active is None:
        return JSONResponse({"success": False, "message": "잘못된 요청입니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE discord_webhooks SET is_active = %s WHERE webhook_id = %s", (bool(is_active), int(wh_id)))
        conn.commit()
    return {"success": True}


@app.post("/api/admin/webhooks/delete")
async def admin_delete_webhook(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    wh_id = body.get("webhook_id")
    if not wh_id:
        return JSONResponse({"success": False, "message": "webhook_id가 필요합니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM discord_webhooks WHERE webhook_id = %s", (int(wh_id),))
        conn.commit()
    return {"success": True}


@app.post("/api/admin/webhooks/test")
async def admin_test_webhook(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    wh_id = body.get("webhook_id")
    if not wh_id:
        return JSONResponse({"success": False, "message": "webhook_id가 필요합니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT webhook_url, channel_name FROM discord_webhooks WHERE webhook_id = %s", (int(wh_id),))
            row = cur.fetchone()

    if not row:
        return JSONResponse({"success": False, "message": "해당 웹훅을 찾을 수 없습니다."}, status_code=404)

    ok, msg = send_test_webhook(row[0], row[1])
    if ok:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE discord_webhooks SET last_sent_at = NOW() WHERE webhook_id = %s", (int(wh_id),))
            conn.commit()
        return {"success": True, "message": "테스트 알림을 성공적으로 발송했습니다."}
    return JSONResponse({"success": False, "message": msg}, status_code=400)


# ==================== 관리자 삭제글 증거 보관함 (Deleted Posts Archive) ====================

@app.get("/api/admin/deleted-posts")
def admin_get_deleted_posts(
    request: Request,
    q: Optional[str] = Query(None),
    target: str = Query("all"),
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100)
):
    """관리자 전용: 디시인사이드에서 삭제 감지되거나 숨김 처리된 게시글 원문 증거 보관함 조회"""
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")

    keyword = (q or "").strip()
    offset = (page - 1) * page_size
    where_clauses = ["is_deleted = TRUE"]
    params = []

    if keyword:
        if keyword.isdigit() and target in ("all", "post_id"):
            where_clauses.append("(post_id = %s OR title ILIKE %s OR content ILIKE %s OR nickname ILIKE %s OR author_code = %s)")
            params.extend([int(keyword), f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", keyword])
        else:
            terms, op = parse_search_terms(keyword)
            valid_terms = [t for t in terms if len(t) >= 1]
            if valid_terms:
                cond, p = build_search_filter(valid_terms, op, target, dot_tolerant=True)
                if cond:
                    where_clauses.append(cond)
                    params.extend(p)

    where_sql = "WHERE " + " AND ".join(where_clauses)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SELECT COUNT(*) FROM dc_posts {where_sql}", params)
            total_count = cur.fetchone()[0]

            cur.execute(f"""
                SELECT post_id, gallery_id, title, content, nickname, author_code, created_at, deleted_at
                FROM dc_posts
                {where_sql}
                ORDER BY COALESCE(deleted_at, created_at) DESC, post_id DESC
                LIMIT %s OFFSET %s
            """, params + [page_size, offset])
            rows = cur.fetchall()

    terms, _ = parse_search_terms(keyword) if keyword else ([], "AND")
    highlight_kws = [t for t in terms if len(t) >= 2]

    items = []
    for r in rows:
        p_id, gall, title, content, nick, code, created_dt, deleted_dt = r
        raw_content = "" if (not content or content.strip() == "__EMPTY__") else content
        items.append({
            "post_id": p_id,
            "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
            "title": highlight_keywords_in_text(title, highlight_kws),
            "raw_title": title,
            "full_content": highlight_keywords_in_text(raw_content, highlight_kws),
            "has_content": bool(raw_content.strip()),
            "nickname": nick or "ㅇㅇ",
            "author_code": code or "",
            "created_at": created_dt.strftime("%Y-%m-%d %H:%M:%S") if created_dt else "",
            "deleted_at": deleted_dt.strftime("%Y-%m-%d %H:%M:%S") if deleted_dt else "수동/감지됨",
        })

    return {
        "results": items,
        "total_count": total_count,
        "page": page,
        "has_next": (offset + page_size) < total_count
    }


@app.post("/api/admin/deleted-posts/mark")
async def admin_mark_post_deleted(request: Request):
    """관리자 전용: 특정 글 번호(post_id)를 즉시 삭제 보관함으로 이동(블라인드)하거나 디시 삭제 여부 실시간 점검"""
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    raw_id = str(body.get("post_id") or "").strip()
    mode = (body.get("mode") or "force").strip()  # 'force' (즉시 보관함 이동) or 'check' (디시 확인 후 삭제시 이동)

    if not raw_id.isdigit():
        return JSONResponse({"success": False, "message": "올바른 게시글 번호(숫자)를 입력해 주세요."}, status_code=400)

    pid = int(raw_id)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT post_id, gallery_id, title, is_deleted FROM dc_posts WHERE post_id = %s", (pid,))
            row = cur.fetchone()
            if not row:
                return JSONResponse({"success": False, "message": f"DB에 #{pid}번 게시글이 존재하지 않습니다."}, status_code=404)

            _, gall, title, already_del = row
            if already_del:
                return {"success": True, "already_deleted": True, "message": f"#{pid}번 글은 이미 삭제글 보관함에 있습니다."}

            if mode == "check":
                dc_del = check_dc_post_deleted(gall, pid)
                if dc_del is False:
                    return {"success": False, "alive": True, "message": f"#{pid}번 글은 현재 디시인사이드에 정상 존재합니다."}
                elif dc_del is None:
                    return {"success": False, "message": "디시인사이드 서버 응답 지연으로 확인하지 못했습니다."}

            cur.execute("""
                UPDATE dc_posts
                SET is_deleted = TRUE,
                    deleted_at = NOW(),
                    last_checked_at = NOW()
                WHERE post_id = %s
            """, (pid,))
        conn.commit()

    return {
        "success": True,
        "post_id": pid,
        "message": f"#{pid} ('{title[:20]}') 게시글을 일반 뷰에서 숨기고 삭제글 보관함으로 이동했습니다."
    }


@app.post("/api/admin/deleted-posts/restore")
async def admin_restore_deleted_post(request: Request):
    """관리자 전용: 삭제글 보관함에 있는 게시글을 일반 검색 목록으로 복구"""
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    pid = body.get("post_id")
    if not pid:
        return JSONResponse({"success": False, "message": "post_id가 필요합니다."}, status_code=400)

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE dc_posts
                SET is_deleted = FALSE,
                    deleted_at = NULL
                WHERE post_id = %s
            """, (int(pid),))
        conn.commit()
    return {"success": True, "message": f"#{pid}번 게시글을 일반 검색 목록으로 복구했습니다."}




