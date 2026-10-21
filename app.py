import time
import html
import re
import json
from datetime import datetime
from typing import Optional
from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from database import get_db

app = FastAPI(title="DC Inside Community 90-Day Archive Search")
templates = Jinja2Templates(directory="templates")

def highlight_keywords_in_text(text: str, keywords: list) -> str:
    """텍스트 내 모든 키워드에 <mark> 하이라이팅 적용 (줄바꿈 보존)"""
    if not text:
        return ""
    escaped = html.escape(text)
    valid_kws = [html.escape(k.strip()) for k in keywords if k and len(k.strip()) >= 2]
    for kw in valid_kws:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
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
    valid_kws = [html.escape(k.strip()) for k in keywords if k and len(k.strip()) >= 2]

    if not valid_kws:
        return escaped_text[:160] + ("..." if len(escaped_text) > 160 else "")

    first_pos = len(escaped_text)
    for kw in valid_kws:
        pos = escaped_text.lower().find(kw.lower())
        if pos != -1 and pos < first_pos:
            first_pos = pos

    if first_pos != len(escaped_text):
        start = max(0, first_pos - 40)
        end = min(len(escaped_text), first_pos + 120)
        snippet = ("..." if start > 0 else "") + escaped_text[start:end] + ("..." if end < len(escaped_text) else "")
    else:
        snippet = escaped_text[:160] + ("..." if len(escaped_text) > 160 else "")

    for kw in valid_kws:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
        snippet = pattern.sub(r'<mark class="bg-amber-300 text-black px-1 rounded font-bold">\g<0></mark>', snippet)

    return snippet

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
        elif period == "90d":
            where_clauses.append("created_at >= NOW() - INTERVAL '90 days'")
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
                cur.execute(f"SELECT COUNT(*) FROM dc_posts {where_sql}", params)
                total_count = cur.fetchone()[0]

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
            raw_content = "" if (not content or content.strip() == "__EMPTY__") else content
            results.append({
                "post_id": p_id,
                "url": f"https://gall.dcinside.com/mgallery/board/view/?id={gall}&no={p_id}",
                "title": highlight_keywords_in_text(title, highlight_list),
                "content_snippet": generate_snippet(raw_content, highlight_list),
                "full_content": highlight_keywords_in_text(raw_content, highlight_list),
                "has_content": bool(raw_content.strip()),
                "nickname": highlight_keywords_in_text(nick, [keyword] if target == "nickname" else []),
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

def get_recent_topics(limit: int = 8):
    """실시간 떡밥 타임라인 이벤트 조회"""
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, gallery_id, window_start, window_end,
                           topic_title, topic_summary, mood_tag, keywords,
                           post_count, sample_post_ids, created_at
                    FROM gallery_events
                    ORDER BY window_start DESC
                    LIMIT %s
                """, (limit,))
                rows = cur.fetchall()

        topics = []
        now_date = datetime.now().date()
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

            topics.append({
                "event_id": r[0],
                "gallery_id": r[1],
                "window_start": str(r[2]),
                "window_end": str(r[3]),
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
    end_date: Optional[str] = Query(None)
):
    search_data = execute_search(
        q=q, target=target, sub_q=sub_q, sub_target=sub_target,
        period=period, start_date=start_date, end_date=end_date, page=1
    )

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM dc_posts")
            db_total_posts = cur.fetchone()[0]

    recent_topics = get_recent_topics(limit=8)

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
def api_topics(limit: int = Query(10, ge=1, le=50)):
    """실시간 떡밥 타임라인 API"""
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
