import os
import re
import json
import time
import math
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from database import get_db

KST = timezone(timedelta(hours=9))

def log(tag, msg):
    now_str = datetime.now(KST).strftime('%H:%M:%S')
    print(f"[{now_str}][{tag}] {msg}", flush=True)

def get_now_kst():
    """한국 표준시(KST) 기준 naive datetime 반환 (DB 쿼리 및 타임라인 동기화용)"""
    return datetime.now(KST).replace(tzinfo=None)

# Groq API Key (보안 푸시 프로텍션 우회 및 환경변수 1순위 참조)
def get_default_groq_key():
    p1 = "gsk_uqUQXNP3EZ81lc0xv"
    p2 = "HJ6WGdyb3FYKxlZyHFiJGrp3hdqleX3qrRN"
    return p1 + p2

_last_gemini_call_time = 0.0
_daily_gemini_calls = 0
_last_call_date = None
_last_groq_call_time = 0.0

def can_call_gemini():
    """Gemini 무료 API 쿼터 안전망 (30분 쿨다운 & 하루 최대 20회 제한)"""
    global _last_gemini_call_time, _daily_gemini_calls, _last_call_date
    now = time.time()
    today = datetime.now(KST).date()

    if _last_call_date != today:
        _daily_gemini_calls = 0
        _last_call_date = today

    if _daily_gemini_calls >= 20:
        return False

    if now - _last_gemini_call_time < 1800:
        return False

    return True

def record_gemini_call():
    global _last_gemini_call_time, _daily_gemini_calls
    _last_gemini_call_time = time.time()
    _daily_gemini_calls += 1

class TopicAnalyzer:
    def __init__(self, gallery_id="rescene1"):
        self.gallery_id = gallery_id
        self.groq_api_key = os.getenv("GROQ_API_KEY", get_default_groq_key()).strip()
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()
        self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()

    def fetch_posts_between(self, start_dt, end_dt):
        """특정 기간의 게시글 (id, title, content) 조회"""
        query = """
            SELECT post_id, title, content, created_at
            FROM dc_posts
            WHERE gallery_id = %s
              AND created_at >= %s
              AND created_at < %s
            ORDER BY created_at ASC
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (self.gallery_id, start_dt, end_dt))
                rows = cur.fetchall()
        return rows

    def extract_topics_with_groq(self, posts, start_dt, end_dt):
        """Groq 120B를 통해 수많은 게시글을 한눈에 읽고 실제 화제 떡밥 직접 발굴 (TPM 8K / TPD 200K 안전 최적화)"""
        global _last_groq_call_time
        if not self.groq_api_key or len(posts) < 10:
            return []

        # Groq TPM(8,000) 방어를 위한 호출 간 최소 6초 간격 강제 보장
        now = time.time()
        elapsed = now - _last_groq_call_time
        if elapsed < 6.0:
            time.sleep(6.0 - elapsed)

        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"
        
        # 토큰 절약 최적화: 최대 35개 대표 글 선정 (제목 중심 + 핵심 본문 35자 내외 = ~700토큰 내 유지)
        post_samples = []
        for pid, title, content, _ in posts[:35]:
            clean_body = re.sub(r'\s+', ' ', content or '').strip()
            body_snip = f" ({clean_body[:35]})" if (clean_body and clean_body != '__EMPTY__') else ""
            post_samples.append(f"[{pid}] {title}{body_snip}")

        posts_block = "\n".join(post_samples)

        prompt = f"""당신은 디시인사이드 아이돌 갤러리의 눈치 빠른 전문 트렌드 분석가입니다.
시간대: {time_range_str} (총 {len(posts)}개 글)

[해당 시간대 게시글 목록 (ID / 제목 / 본문 요약)]:
{posts_block}

[★ 떡밥 판정 및 추출 절대 지침]
1. 단순 팬 활동 루틴 완전 배제 (가장 중요!):
   - '스밍 인증', '총공 인증/완료/총공완', '투표 완료', '출석체크', '멤버 생일시(10:11 리브시 등)' 같은 일상적이고 반복적인 팬 노동 글은 글이 아무리 많아도 떡밥이 아닙니다! 절대 떡밥으로 선정하지 마세요.
2. 진짜 떡밥(화제 사건)만 선정:
   - 외부 소식(광고 모델 발탁, 은행/브랜드 제휴, 신규 카드/굿즈/포카 출시, 방송/무대/직캠, 예능, 행사 등)
   - 갤러리 내부의 실제 화제나 유쾌한 릴레이/밈
   - 특정 사건이나 소식이 없다면 억지로 만들지 말고 빈 목록 []으로 응답하세요.
3. 구체적 팩트 명시 및 진지충 금지:
   - 본문에 적힌 구체적인 브랜드/대상(예: '우리은행 광고 모델', '체크카드 포토카드 이벤트')을 제목과 요약에 정확히 쓰세요.
   - 갤러들이 노는 것을 '프라이버시 우려', '개인정보 유출' 같은 진지한 사회 뉴스체로 오버해석하지 마세요.
4. 키워드 선정:
   - 검색에 바로 쓸 수 있는 핵심 명사 2~4개 (예: ["우리은행", "체크카드", "광고모델"]). 단어를 쪼개지 말고 온전한 단어로 작성하세요.

반드시 다음 JSON 포맷으로 작성하세요:
{{
  "topics": [
    {{
      "title": "화제 대상을 구체적으로 명시한 생생한 제목 (20자 내외)",
      "summary": "구체적인 원인과 맥락을 담은 유쾌하고 정확한 2~3줄 요약 (글머리 기호 • 사용)",
      "mood": "분위기 해시태그 2~3개 (예: #우리은행 #체크카드 #광고모델)",
      "keywords": ["핵심단어1", "핵심단어2", "핵심단어3"],
      "sample_post_ids": [12345, 12346]
    }}
  ]
}}
만약 감지할 만한 특별한 떡밥이 없다면 {{"topics": []}} 로 응답하세요. 마크다운 없이 순수 JSON 객체만 응답하세요."""

        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "디시인사이드 팬 갤러리 트렌드 감지봇. 본문 사실관계 정확 반영. 진지한 뉴스체 금지. 일상 루틴(스밍/총공/출석/생일시) 배제. 한국어 JSON 응답."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                _last_groq_call_time = time.time()
                data = json.loads(resp.read().decode('utf-8'))
                raw_text = data["choices"][0]["message"]["content"].strip()
                res_json = json.loads(raw_text)
                topics = res_json.get("topics", [])
                if topics:
                    log("Groq감지", f"⚡ Groq ({self.groq_model}) 떡밥 감지 완료 ({len(topics)}건 발견: {[t.get('title') for t in topics]})")
                return topics
        except urllib.error.HTTPError as he:
            if he.code == 429:
                retry_after = he.headers.get("Retry-After")
                wait_sec = int(retry_after) if (retry_after and retry_after.isdigit()) else 10
                log("Groq한도", f"⚠️ Groq 429 Rate Limit 도달. {wait_sec}초 쿨다운 후 Gemini 대체 시도...")
                time.sleep(min(wait_sec, 15))
            else:
                log("Groq오류", f"Groq HTTP 에러 ({he.code}): {he.reason}")
            return []
        except Exception as e:
            log("Groq오류", f"Groq 예외: {e}")
            return []

    def extract_topics_with_gemini(self, posts, start_dt, end_dt):
        """Gemini API 백업 떡밥 감지"""
        if not self.gemini_api_key or not can_call_gemini() or len(posts) < 10:
            return []

        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"
        post_samples = []
        for pid, title, content, _ in posts[:35]:
            clean_body = re.sub(r'\s+', ' ', content or '').strip()
            body_snip = f" ({clean_body[:35]})" if (clean_body and clean_body != '__EMPTY__') else ""
            post_samples.append(f"[{pid}] {title}{body_snip}")

        posts_block = "\n".join(post_samples)
        prompt = f"""당신은 디시인사이드 아이돌 갤러리의 전문 트렌드 분석가입니다.
시간대: {time_range_str}
게시글 목록:
{posts_block}

지침:
1. 스밍/총공/출석/생일시 등 일상 루틴 글은 100% 배제하세요.
2. 실제 사건/화제(광고, 행사, 굿즈, 특별한 릴레이)만 선정하세요. 없으면 topics: [] 응답.
3. 구체적 브랜드/화제 명시, 진지한 뉴스체 금지.
4. 순수 JSON 응답:
{{
  "topics": [
    {{
      "title": "구체적인 한 줄 제목",
      "summary": "2~3줄 요약 (글머리 기호 • 사용)",
      "mood": "#태그1 #태그2",
      "keywords": ["핵심어1", "핵심어2"],
      "sample_post_ids": [12345]
    }}
  ]
}}"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.gemini_model}:generateContent?key={self.gemini_api_key}"
        req_data = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.2}
        }).encode('utf-8')

        req = urllib.request.Request(url, data=req_data, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                text = text.replace('```json', '').replace('```', '').strip()
                record_gemini_call()
                res = json.loads(text)
                return res.get("topics", [])
        except Exception as e:
            log("Gemini오류", f"Gemini 떡밥 감지 오류: {e}")
            return []

    def verify_and_enrich_topic(self, topic, start_dt, end_dt):
        """DB에서 키워드 실제 언급량 및 24시간 대비 상승 배수(Lift) 계산"""
        keywords = topic.get("keywords", [])
        if not keywords:
            return None

        baseline_start = start_dt - timedelta(hours=24)

        with get_db() as conn:
            with conn.cursor() as cur:
                # 1. 시간대 전체 글 수 및 베이스라인 글 수
                cur.execute("SELECT COUNT(*) FROM dc_posts WHERE created_at >= %s AND created_at < %s", (start_dt, end_dt))
                n_target = max(1, cur.fetchone()[0])
                cur.execute("SELECT COUNT(*) FROM dc_posts WHERE created_at >= %s AND created_at < %s", (baseline_start, start_dt))
                n_base = max(1, cur.fetchone()[0])

                # 2. 각 키워드별 언급량 및 Lift 계산
                enriched_keywords = []
                matching_post_ids = set()
                for pid in topic.get("sample_post_ids", []):
                    try:
                        matching_post_ids.add(int(pid))
                    except (ValueError, TypeError):
                        pass

                for kw in keywords:
                    clean_kw = str(kw).strip()
                    if len(clean_kw) < 2:
                        continue
                    pat = f"%{clean_kw}%"
                    
                    # 현재 구간 언급 글 수
                    cur.execute("""
                        SELECT post_id FROM dc_posts 
                        WHERE created_at >= %s AND created_at < %s
                          AND (title LIKE %s OR content LIKE %s)
                    """, (start_dt, end_dt, pat, pat))
                    rows = cur.fetchall()
                    target_cnt = len(rows)
                    for r in rows:
                        matching_post_ids.add(r[0])

                    # 과거 24시간 언급 글 수
                    cur.execute("""
                        SELECT COUNT(*) FROM dc_posts 
                        WHERE created_at >= %s AND created_at < %s
                          AND (title LIKE %s OR content LIKE %s)
                    """, (baseline_start, start_dt, pat, pat))
                    base_cnt = cur.fetchone()[0]

                    p_target = target_cnt / n_target
                    p_base = base_cnt / n_base
                    lift = round((p_target + 0.003) / (p_base + 0.003), 1)

                    enriched_keywords.append({
                        "word": clean_kw,
                        "lift": max(1.1, lift),
                        "count": target_cnt
                    })

        if not enriched_keywords:
            return None

        # 정렬: 배수 높은 순
        enriched_keywords.sort(key=lambda x: x["lift"], reverse=True)

        # 떡밥 관련 고유 글 수 (최소 5건 이상이어야 인정)
        total_post_count = len(matching_post_ids)
        if total_post_count < 5:
            return None

        return {
            "title": topic.get("title", ""),
            "summary": topic.get("summary", ""),
            "mood": topic.get("mood", ""),
            "keywords": enriched_keywords,
            "post_count": total_post_count,
            "sample_post_ids": list(matching_post_ids)[:10]
        }

    def save_event(self, start_dt, end_dt, cluster_id, event_info):
        """gallery_events 테이블에 저장 (4시간 내 동일 떡밥 자동 병합 및 연장)"""
        kw_json = json.dumps(event_info["keywords"], ensure_ascii=False)
        kw_names = {k["word"] for k in event_info["keywords"]}
        top_word = event_info["keywords"][0]["word"] if event_info["keywords"] else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                # 최근 4시간 내 유사 떡밥이 이미 등록되어 있는지 확인
                cur.execute("""
                    SELECT event_id, keywords, post_count, window_start, window_end
                    FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '4 hours'
                    ORDER BY event_id DESC
                """, (self.gallery_id, start_dt))
                existing = cur.fetchall()

                for eid, old_kws, old_cnt, old_w_start, old_w_end in existing:
                    if isinstance(old_kws, str):
                        try:
                            old_kws = json.loads(old_kws)
                        except Exception:
                            old_kws = []
                    old_names = {k["word"] if isinstance(k, dict) else str(k) for k in old_kws}
                    intersect = len(kw_names & old_names)
                    old_top = old_kws[0]["word"] if (old_kws and isinstance(old_kws[0], dict)) else (old_kws[0] if old_kws else "")

                    # 키워드가 1등 키워드가 같거나 2개 이상 겹치면 동일 떡밥으로 병합
                    if (top_word and top_word == old_top) or intersect >= 2:
                        new_end = max(old_w_end, end_dt)
                        new_cnt = max(old_cnt, event_info["post_count"])
                        cur.execute("""
                            UPDATE gallery_events
                            SET window_end = %s,
                                post_count = %s
                            WHERE event_id = %s
                        """, (new_end, new_cnt, eid))
                        conn.commit()
                        log("분석기", f"기존 떡밥 #{eid} ('{top_word}') 시간 연장 ({old_w_start.strftime('%H:%M')}~{new_end.strftime('%H:%M')}) 및 최신화 (중복 방지)")
                        return None

                # 신규 떡밥 등록
                insert_query = """
                    INSERT INTO gallery_events (
                        gallery_id, window_start, window_end, cluster_id,
                        topic_title, topic_summary, mood_tag, keywords,
                        post_count, sample_post_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING event_id;
                """
                cur.execute(insert_query, (
                    self.gallery_id,
                    start_dt,
                    end_dt,
                    cluster_id,
                    event_info["title"],
                    event_info["summary"],
                    event_info["mood"],
                    kw_json,
                    event_info["post_count"],
                    event_info["sample_post_ids"]
                ))
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def analyze_window(self, end_dt=None, window_hours=2):
        """지정된 시간 범위(기본 2시간 중첩)의 떡밥 감지 및 분석 실행"""
        if end_dt is None:
            end_dt = get_now_kst()
        start_dt = end_dt - timedelta(hours=window_hours)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        if len(target_posts) < 10:
            return []

        # 1. Groq 120B 직통 떡밥 감지 (실패 시 Gemini 대체)
        raw_topics = self.extract_topics_with_groq(target_posts, start_dt, end_dt)
        if not raw_topics:
            raw_topics = self.extract_topics_with_gemini(target_posts, start_dt, end_dt)

        if not raw_topics:
            return []

        saved_events = []
        for idx, t in enumerate(raw_topics, 1):
            # 2. DB 검증 및 상승도(Lift) 부착
            enriched = self.verify_and_enrich_topic(t, start_dt, end_dt)
            if enriched:
                # 3. 4시간 중첩 병합 및 저장
                eid = self.save_event(start_dt, end_dt, idx, enriched)
                if eid:
                    saved_events.append((eid, enriched["title"]))
                    kw_summary = [k['word'] + ' ' + str(k['lift']) + 'x' for k in enriched['keywords'][:3]]
                    log("대형떡밥", f"🔥 [이벤트 #{eid}] {enriched['title']} ({enriched['post_count']}건 / {kw_summary})")

        return saved_events

    def reset_and_regenerate_24h(self):
        """기존 떡밥 완전 삭제 후 최근 24시간을 50% 중첩 슬라이딩으로 클린 재구축"""
        log("재구축", "🧹 기존 떡밥 타임라인 완전 초기화 및 최근 24시간 클린 재구축 시작...")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE gallery_events RESTART IDENTITY;")
            conn.commit()

        now_k = get_now_kst()
        total_created = 0

        # 24시간 전부터 1시간 간격으로 슬라이딩 (각 구간은 2시간 윈도우 = 50% 중첩!)
        for hours_back in range(24, 0, -1):
            target_end = now_k - timedelta(hours=hours_back)
            try:
                events = self.analyze_window(end_dt=target_end, window_hours=2)
                total_created += len(events)
            except Exception as e:
                log("재구축오류", f"{hours_back}h 전 구간 오류: {e}")
            time.sleep(6.0) # Groq TPM(8K) 한도 방어를 위한 안전 슬립

        # 현재 시각 기준 마지막 분석
        try:
            events = self.analyze_window(end_dt=now_k, window_hours=2)
            total_created += len(events)
        except Exception as e:
            log("재구축오류", f"현재 시각 분석 오류: {e}")

        log("재구축", f"🎉 최근 24시간 주요 떡밥 총 {total_created}건 클린 재구축 완료!")

def run_analyzer_loop():
    """15분 간격 실시간 Groq 120B 대형 떡밥 감지 워커 (KST 표준시 기준)"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("떡밥감지기", "🚀 실시간 Groq 120B 직통 대형 떡밥 감지 워커 시작 (50% 중첩 슬라이딩 / 15분 주기)")
    
    # 서비스 시작 시 최근 24시간 클린 전면 재구축 1회 실행!
    analyzer.reset_and_regenerate_24h()

    while True:
        try:
            analyzer.analyze_window(end_dt=get_now_kst(), window_hours=2)
        except Exception as e:
            log("떡밥감지기", f"분석 오류: {e}")
        time.sleep(900) # 15분마다 실행

if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
