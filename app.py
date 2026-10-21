import time
import html
import re
from typing import Optional
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from database import get_db

app = FastAPI(title="DC Inside Community Search Engine")
templates = Jinja2Templates(directory="templates")

def highlight_keywords(text: str, keywords: list) -> str:
    """검색어들에 <mark> 태그 적용 및 스니펫 추출"""
    if not text:
        return ""
    escaped_text = html.escape(text)
    
    valid_kws = [html.escape(k.strip()) for k in keywords if k and len(k.strip()) >= 2]
    if not valid_kws:
        return escaped_text[:180] + ("..." if len(escaped_text) > 180 else "")

    # 첫 번째 매칭 키워드 위치 기준 스니펫 추출
    first_pos = len(escaped_text)
    for kw in valid_kws:
        pos = escaped_text.lower().find(kw.lower())
        if pos != -1 and pos < first_pos:
            first_pos = pos

    if first_pos != len(escaped_text):
        start = max(0, first_pos - 50)
        end = min(len(escaped_text), first_pos + 120)
        snippet = ("..." if start > 0 else "") + escaped_text[start:end] + ("..." if end < len(escaped_text) else "")
    else:
        snippet = escaped_text[:180] + ("..." if len(escaped_text) > 180 else "")

    # 키워드 하이라이팅
    for kw in valid_kws:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        snippet = pattern.sub(r'<mark class="bg-amber-300 text-black px-1 rounded font-bold">\g<0></mark>', snippet)

    return snippet

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: Optional[str] = Query(None, description="기본 검색어"),
    target: str = Query("all", description="기본 검색 대상: all, title, content, nickname, code"),
    sub_q: Optional[str] = Query(None, description="결과 내 재검색어"),
    sub_target: str = Query("all", description="재검색 대상: all (제목+본문), title (제목만)"),
    period: str = Query("all", description="날짜 필터: all, 1d, 7d, 30d, custom"),
    start_date: Optional[str] = Query(None, description="시작일 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, description="종료일 (YYYY-MM-DD)"),
    page: int = Query(1, ge=1)
):
    keyword = (q or "").strip()
    sub_keyword = (sub_q or "").strip()
    results = []
    error_msg = None
    elapsed_ms = 0.0
    total_count = 0
    page_size = 30
    offset = (page - 1) * page_size

    # 유효성 검사
    if keyword and target != "code" and len(keyword) < 2:
        error_msg = "기본 검색어는 최소 2글자 이상 입력해야 합니다."
    elif sub_keyword and len(sub_keyword) < 2:
        error_msg = "결과 내 검색어는 최소 2글자 이상 입력해야 합니다."
    elif keyword or sub_keyword or period != "all":
        start_time = time.perf_counter()
        where_clauses = []
        params = []

        # 1. 1차 기본 검색 조건
        if keyword:
            pattern = f"%{keyword}%"
            if target == "title":
                where_clauses.append("title LIKE %s")
                params.append(pattern)
            elif target == "content":
                where_clauses.append("content LIKE %s")
                params.append(pattern)
            elif target == "nickname":
                where_clauses.append("nickname LIKE %s")
                params.append(pattern)
            elif target == "code":
                where_clauses.append("author_code = %s")
                params.append(keyword)
            else: # all: 제목 또는 본문
                where_clauses.append("(title LIKE %s OR content LIKE %s)")
                params.extend([pattern, pattern])

        # 2. 2차 결과 내 재검색 조건 (AND 연산)
        if sub_keyword:
            sub_pattern = f"%{sub_keyword}%"
            if sub_target == "title":
                where_clauses.append("title LIKE %s")
                params.append(sub_pattern)
            else: # all: 제목 또는 본문
                where_clauses.append("(title LIKE %s OR content LIKE %s)")
                params.extend([sub_pattern, sub_pattern])

        # 3. 날짜 필터링 조건
        if period == "1d":
            where_clauses.append("created_at >= NOW() - INTERVAL '1 day'")
        elif period == "7d":
            where_clauses.append("created_at >= NOW() - INTERVAL '7 days'")
        elif period == "30d":
            where_clauses.append("created_at >= NOW() - INTERVAL '30 days'")
        elif period == "custom":
            if start_date:
                where_clauses.append("created_at >= %s")
                params.append(start_date)
            if end_date:
                where_clauses.append("created_at < (%s::date + INTERVAL '1 day')")
                params.append(end_date)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                # 총 개수 조회
                cur.execute(f"SELECT COUNT(*) FROM dc_posts {where_sql}", params)
                total_count = cur.fetchone()[0]

                # 결과 리스트 조회 (최신순)
                query = f"""
                    SELECT post_id, gallery_id, title, content, nickname, author_code, created_at
                    FROM dc_posts
                    {where_sql}
                    ORDER BY post_id DESC
                    LIMIT %s OFFSET %s
                """
                cur.execute(query, params + [page_size, offset])
                rows = cur.fetchall()

        elapsed_ms = round((time.perf_counter() - start_time) * 1000, 2)

        highlight_list = []
        if keyword and target in ("all", "title", "content"):
            highlight_list.append(keyword)
        if sub_keyword:
            highlight_list.append(sub_keyword)

        for r in rows:
            p_id, gall, title, content, nick, code, dt = r
            results.append({
                "post_id": p_id,
                "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
                "title": highlight_keywords(title, highlight_list),
                "content_snippet": highlight_keywords(content, highlight_list),
                "nickname": highlight_keywords(nick, [keyword] if target == "nickname" else []),
                "author_code": code,
                "created_at": dt.strftime("%Y-%m-%d %H:%M")
            })

    # 전체 DB 통계
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dc_posts")
            db_total_posts = cur.fetchone()[0]

    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "q": keyword,
            "target": target,
            "sub_q": sub_keyword,
            "sub_target": sub_target,
            "period": period,
            "start_date": start_date or "",
            "end_date": end_date or "",
            "results": results,
            "total_count": total_count,
            "elapsed_ms": elapsed_ms,
            "error_msg": error_msg,
            "db_total_posts": db_total_posts,
            "page": page,
            "has_next": (offset + page_size) < total_count,
            "has_prev": page > 1
        }
    )

@app.get("/api/stats")
def stats():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*), MIN(created_at), MAX(created_at) 
                FROM dc_posts
            """)
            cnt, min_dt, max_dt = cur.fetchone()
    return {
        "total_posts": cnt,
        "oldest_post": str(min_dt) if min_dt else None,
        "latest_post": str(max_dt) if max_dt else None
    }
