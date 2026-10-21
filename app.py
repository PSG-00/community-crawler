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

def highlight_keyword(text: str, keyword: str) -> str:
    """검색어에 <mark> 태그 적용 및 스니펫 추출"""
    if not text:
        return ""
    escaped_text = html.escape(text)
    if not keyword:
        return escaped_text[:200]
    
    escaped_kw = html.escape(keyword)
    # 대소문자 무시 하이라이팅
    pattern = re.compile(re.escape(escaped_kw), re.IGNORECASE)
    
    # 키워드 주변 150자 정도 자르기
    match = pattern.search(escaped_text)
    if match:
        start = max(0, match.start() - 60)
        end = min(len(escaped_text), match.end() + 100)
        snippet = ("..." if start > 0 else "") + escaped_text[start:end] + ("..." if end < len(escaped_text) else "")
    else:
        snippet = escaped_text[:160] + ("..." if len(escaped_text) > 160 else "")

    return pattern.sub(r'<mark class="bg-amber-300 text-black px-1 rounded font-bold">\g<0></mark>', snippet)

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    q: Optional[str] = Query(None, description="검색어"),
    target: str = Query("all", description="검색 대상: all, title, content, nickname, code"),
    page: int = Query(1, ge=1)
):
    keyword = (q or "").strip()
    results = []
    error_msg = None
    elapsed_ms = 0.0
    total_count = 0
    page_size = 30
    offset = (page - 1) * page_size

    if keyword:
        # 1. 두 글자 미만 차단
        if len(keyword) < 2 and target != "code":
            error_msg = "검색어는 최소 2글자 이상 입력해야 합니다."
        else:
            start_time = time.perf_counter()
            with get_db() as conn:
                with conn.cursor() as cur:
                    # 검색 대상별 쿼리 분기 (pg_bigm GIN 인덱스 자동 활용)
                    pattern = f"%{keyword}%"
                    if target == "title":
                        where_sql = "WHERE title LIKE %s"
                        params = [pattern]
                    elif target == "content":
                        where_sql = "WHERE content LIKE %s"
                        params = [pattern]
                    elif target == "nickname":
                        where_sql = "WHERE nickname LIKE %s"
                        params = [pattern]
                    elif target == "code":
                        where_sql = "WHERE author_code = %s"
                        params = [keyword]
                    else: # all: 제목 또는 본문
                        where_sql = "WHERE title LIKE %s OR content LIKE %s"
                        params = [pattern, pattern]

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

            for r in rows:
                p_id, gall, title, content, nick, code, dt = r
                results.append({
                    "post_id": p_id,
                    "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
                    "title": highlight_keyword(title, keyword if target in ("all", "title") else ""),
                    "content_snippet": highlight_keyword(content, keyword if target in ("all", "content") else ""),
                    "nickname": highlight_keyword(nick, keyword if target == "nickname" else ""),
                    "author_code": code,
                    "created_at": dt.strftime("%Y-%m-%d %H:%M")
                })

    # 전체 DB 통계
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dc_posts")
            db_total_posts = cur.fetchone()[0]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "q": keyword,
        "target": target,
        "results": results,
        "total_count": total_count,
        "elapsed_ms": elapsed_ms,
        "error_msg": error_msg,
        "db_total_posts": db_total_posts,
        "page": page,
        "has_next": (offset + page_size) < total_count,
        "has_prev": page > 1
    })

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
