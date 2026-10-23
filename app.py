import time
import html
import re
import json
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Optional
from fastapi import FastAPI, Request, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from database import get_db
from analyzer import TopicAnalyzer

app = FastAPI(title="DC Inside Community 90-Day Archive Search")
templates = Jinja2Templates(directory="templates")

ADMIN_PASSWORD = "remine0326"
ADMIN_TOKEN = hashlib.sha256(f"rescene_admin_{ADMIN_PASSWORD}".encode()).hexdigest()


def verify_admin(request: Request) -> bool:
    cookie_token = request.cookies.get("admin_auth", "")
    header_pw = request.headers.get("X-Admin-Password", "")
    return cookie_token == ADMIN_TOKEN or header_pw == ADMIN_PASSWORD


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
                sub_clauses.append("(title LIKE %s OR regexp_replace(title, '[.,/·•_~-]', '', 'g') LIKE %s)")
                params.extend([pattern, pattern])
            else:
                sub_clauses.append("title LIKE %s")
                params.append(pattern)
        elif target == "content":
            if dot_tolerant:
                sub_clauses.append("(content LIKE %s OR regexp_replace(content, '[.,/·•_~-]', '', 'g') LIKE %s)")
                params.extend([pattern, pattern])
            else:
                sub_clauses.append("content LIKE %s")
                params.append(pattern)
        elif target == "nickname":
            sub_clauses.append("nickname LIKE %s")
            params.append(pattern)
        elif target == "code":
            sub_clauses.append("author_code = %s")
            params.append(term)
        else: # all: 제목 또는 본문
            if dot_tolerant:
                sub_clauses.append(
                    "(title LIKE %s OR content LIKE %s "
                    "OR regexp_replace(title, '[.,/·•_~-]', '', 'g') LIKE %s "
                    "OR regexp_replace(content, '[.,/·•_~-]', '', 'g') LIKE %s)"
                )
                params.extend([pattern, pattern, pattern, pattern])
            else:
                sub_clauses.append("(title LIKE %s OR content LIKE %s)")
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
        where_clauses = []
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

        # 하이라이트할 모든 유효 단어 수집
        highlight_list = []
        if target in ("all", "title", "content"):
            highlight_list.extend(valid_terms)
        if sub_target in ("all", "title", "content"):
            highlight_list.extend(valid_sub_terms)

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
    """실시간 떡밥 타임라인 이벤트 조회"""
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

            # 떡밥 시간대 검색용 시작/종료 시각 (앞뒤 5분 여유 포함, datetime-local 포맷)
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
            cur.execute("SELECT COUNT(*) FROM dc_posts")
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
    return {"notes": notes, "counts": counts, "recent_topics": get_recent_topics(25)}


@app.post("/api/admin/notes/add")
async def admin_add_note(request: Request):
    if not verify_admin(request):
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")
    body = await request.json()
    p_type = (body.get("pattern_type") or "manual_stop").strip()
    target_text = (body.get("target_text") or "").strip()
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
    return {"success": True, "note_id": nid}


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


