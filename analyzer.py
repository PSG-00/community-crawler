import os
import json
import time
import math
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import networkx as nx
from kiwipiepy import Kiwi
from database import get_db

KST = timezone(timedelta(hours=9))

# 디시인사이드 도메인 특화 불용어 및 일상 상시 출현 단어 사전
STOPWORDS = {
    "디시", "갤러", "갤러리", "갤", "념글", "개추", "비추", "파딱", "주딱", "완장",
    "유동", "고닉", "통피", "댓글", "작성자", "게시글", "본문", "사진", "영상", "링크",
    "이유", "생각", "진짜", "오늘", "근데", "어제", "내일", "지금", "하나", "사람",
    "우리", "그냥", "이거", "저거", "그거", "누구", "보고", "때문", "모두", "다들",
    "어디", "언제", "어떻게", "다시", "자기", "정도", "기준", "이번", "얘기", "시작",
    "원래", "제일", "제발", "존나", "시발", "새끼", "새기", "존버", "거의", "약간",
    "전부", "다른", "모든", "무슨", "아무", "어떤", "어차피", "확실", "경우", "자체",
    "사실", "소리", "내용", "부분", "가지", "자꾸", "뭔가", "그게", "저게", "이건",
    "저건", "그건", "이게", "저기", "여기", "거기", "진심", "약간", "제발", "가장",
    "제작", "관련", "그때", "지금껏", "역시", "차라리", "결국", "항상", "이따", "조금",
    "애들", "멤버", "아이돌", "노래", "무대", "활동", "컴백", "공개", "느낌", "모습",
    "확인", "팬들", "이야기", "비교", "인기", "소식", "예전", "시간", "상황"
}

_kiwi_instance = None
_last_gemini_call_time = 0.0
_daily_gemini_calls = 0
_last_call_date = None

def get_kiwi():
    global _kiwi_instance
    if _kiwi_instance is None:
        _kiwi_instance = Kiwi()
    return _kiwi_instance

def can_call_gemini():
    """Gemini 무료 API 쿼터 안전망 (30분 쿨다운 & 하루 최대 20회 제한)"""
    global _last_gemini_call_time, _daily_gemini_calls, _last_call_date
    now = time.time()
    today = datetime.now(KST).date()

    if _last_call_date != today:
        _daily_gemini_calls = 0
        _last_call_date = today

    # 하루 최대 20회 제한 (무료 티어 안전 확보)
    if _daily_gemini_calls >= 20:
        return False

    # 최소 30분(1800초) 간격 쿨다운
    if now - _last_gemini_call_time < 1800:
        return False

    return True

def record_gemini_call():
    global _last_gemini_call_time, _daily_gemini_calls
    _last_gemini_call_time = time.time()
    _daily_gemini_calls += 1

def log(tag, msg):
    now_str = datetime.now(KST).strftime('%H:%M:%S')
    print(f"[{now_str}][{tag}] {msg}", flush=True)

class TopicAnalyzer:
    def __init__(self, gallery_id="rescene1"):
        self.gallery_id = gallery_id
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

    def tokenize(self, text):
        """텍스트에서 유효 명사/외래어/신조어만 추출 (2글자 이상, 불용어 제외)"""
        if not text:
            return []
        kiwi = get_kiwi()
        try:
            tokens = kiwi.tokenize(text)
            words = []
            for t in tokens:
                if t.tag in ('NNG', 'NNP', 'XR', 'SL') and len(t.form) >= 2 and t.form not in STOPWORDS:
                    words.append(t.form)
            return words
        except Exception:
            return []

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

    def detect_burst_keywords(self, target_posts, baseline_posts):
        """엄격한 Z-Score 및 Lift 기반 급상승 키워드 감지 (진짜 대형 떡밥만 포착)"""
        if not target_posts:
            return [], []

        n_target = len(target_posts)
        n_base = max(1, len(baseline_posts))

        # 1. 대상 윈도우 단어 집계
        target_doc_freq = Counter()
        target_tf = Counter()
        target_post_words = []

        for pid, title, content, _ in target_posts:
            text = f"{title} {content if content and content != '__EMPTY__' else ''}"
            words = set(self.tokenize(text))
            for w in words:
                target_doc_freq[w] += 1
            for w in self.tokenize(text):
                target_tf[w] += 1
            target_post_words.append((pid, words))

        # 2. 24시간 베이스라인 단어 집계
        base_doc_freq = Counter()
        for _, title, content, _ in baseline_posts:
            text = f"{title} {content if content and content != '__EMPTY__' else ''}"
            words = set(self.tokenize(text))
            for w in words:
                base_doc_freq[w] += 1

        # 3. Z-Score 및 Lift 계산
        burst_candidates = []
        for word, doc_cnt in target_doc_freq.items():
            # 최소 5개 이상의 독립된 글에서 언급되어야 함 (잡담 필터링)
            if doc_cnt < 5:
                continue

            p_target = doc_cnt / n_target
            p_base = base_doc_freq[word] / n_base

            # Lift: 평소 대비 배수
            lift = (p_target + 0.003) / (p_base + 0.003)

            # 포아송/이항 근사 Z-score
            expected_count = p_base * n_target
            z_score = (doc_cnt - expected_count) / math.sqrt(max(1.0, expected_count))

            # 엄격한 임계치: 평소 대비 최소 3.0배 이상 급증 & Z-Score 3.5 이상
            if z_score >= 3.5 and lift >= 3.0:
                score = round(z_score * math.log2(lift + 1.0), 2)
                burst_candidates.append({
                    "word": word,
                    "doc_count": doc_cnt,
                    "total_count": target_tf[word],
                    "lift": round(lift, 2),
                    "z_score": round(z_score, 2),
                    "score": score
                })

        burst_candidates.sort(key=lambda x: x["score"], reverse=True)
        return burst_candidates, target_post_words

    def cluster_topics(self, burst_keywords, target_post_words):
        """동시출현(Co-occurrence) 네트워크 기반 다중 떡밥 군집화"""
        if len(burst_keywords) < 3:
            return []

        burst_set = {b["word"]: b for b in burst_keywords}
        g = nx.Graph()

        # 노드 추가
        for b in burst_keywords:
            g.add_node(b["word"], **b)

        # 동시출현 간선(Edge) 가중치 계산
        cooccur_counts = Counter()
        word_post_map = defaultdict(set)

        for pid, words in target_post_words:
            matched = [w for w in words if w in burst_set]
            for w in matched:
                word_post_map[w].add(pid)
            for i in range(len(matched)):
                for j in range(i + 1, len(matched)):
                    pair = tuple(sorted([matched[i], matched[j]]))
                    cooccur_counts[pair] += 1

        for (u, v), weight in cooccur_counts.items():
            # 최소 3개 이상의 글에서 동시 언급되어야 연결 (가짜 링크 제거)
            if weight >= 3:
                g.add_edge(u, v, weight=weight)

        # 연결 요소(Connected Components)로 독립된 떡밥 클러스터 분리
        clusters = []
        for comp in nx.connected_components(g):
            # 최소 3개 이상의 키워드가 얽혀있어야 떡밥 인정
            if len(comp) < 3:
                continue

            subgraph = g.subgraph(comp)
            cluster_posts = set()
            for w in comp:
                cluster_posts.update(word_post_map[w])

            # 진짜 떡밥 기준: 최소 8건 이상의 고유 게시글에서 다뤄져야 함!
            if len(cluster_posts) < 8:
                continue

            comp_words = sorted([burst_set[w] for w in comp], key=lambda x: x["score"], reverse=True)

            edges = []
            for u, v, data in subgraph.edges(data=True):
                edges.append({"source": u, "target": v, "weight": data.get("weight", 1)})

            clusters.append({
                "keywords": comp_words,
                "edges": edges,
                "post_count": len(cluster_posts),
                "post_ids": list(cluster_posts)[:10]
            })

        # 게시글 수 많은 순 정렬 후 최대 상위 2개 클러스터만 채택
        clusters.sort(key=lambda x: x["post_count"], reverse=True)
        return clusters[:2]

    def generate_summary(self, cluster, start_dt, end_dt, is_backfill=False):
        """비용/쿼터 보호형 요약 생성기 (대형 떡밥에만 Gemini 호출)"""
        kws = [k["word"] for k in cluster["keywords"]]
        kw_str = ", ".join(kws[:6])
        post_ids = cluster["post_ids"]
        top_k1 = kws[0] if len(kws) > 0 else "갤러리"
        top_k2 = kws[1] if len(kws) > 1 else ""

        # 대표 게시글 제목 수집
        sample_titles = []
        if post_ids:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT title FROM dc_posts WHERE post_id = ANY(%s) LIMIT 5", (post_ids,))
                    sample_titles = [r[0] for r in cur.fetchall()]

        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"

        # 1. Gemini API 호출 조건:
        # - 과거 백필 아님 (백필 시 API 호출 0회)
        # - 글 수가 10건 이상인 대형 떡밥
        # - 30분 쿨다운 및 일일 20회 미만 안전망 통과
        if (not is_backfill) and self.gemini_api_key and cluster["post_count"] >= 10 and can_call_gemini():
            ai_result = self._call_gemini_summary(time_range_str, kws, sample_titles)
            if ai_result:
                record_gemini_call()
                log("LLM요약", f"✨ Gemini API 요약 완료 (오늘 사용량: {_daily_gemini_calls}/500 RPD)")
                return ai_result

        # 2. 고품질 스마트 휴리스틱 요약기 (API 호출 0회, 비용 0원)
        title = f"'{top_k1}' · '{top_k2}' 관련 화제 집중" if top_k2 else f"'{top_k1}' 관련 게시글 급증"

        summary_lines = [
            f"• {time_range_str} 사이 '{kw_str}' 관련 게시글이 {cluster['post_count']}건 집중 게재되었습니다.",
            f"• 핵심 키워드 '{top_k1}'의 언급 빈도가 평소 대비 {cluster['keywords'][0]['lift']}배 폭증했습니다.",
            f"• 주요 반응: " + (" / ".join(sample_titles[:2]) if sample_titles else f"{top_k1} 중심 이야기 활발")
        ]
        mood = f"#{top_k1} #{top_k2 if top_k2 else '화제'} #실시간"

        return {
            "title": title,
            "summary": "\n".join(summary_lines),
            "mood": mood
        }

    def _call_gemini_summary(self, time_range_str, keywords, sample_titles):
        """Google Gemini API를 통한 고품질 요약 (최대 10초 타임아웃)"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.gemini_api_key}"
        
        prompt = f"""
당신은 아이돌 커뮤니티(디시인사이드 리센느 갤러리) 트렌드 분석가입니다.
시간대: {time_range_str}
급상승 핵심 키워드: {', '.join(keywords[:7])}
해당 시간대 대표 게시글 제목들:
{chr(10).join(['- ' + t for t in sample_titles])}

위 데이터를 바탕으로 커뮤니티에서 어떤 화제/떡밥이 있었는지 다음 JSON 포맷으로 작성하세요:
{{
  "title": "주요 화제를 포착한 매력적인 한 줄 제목 (20자 내외)",
  "summary": "핵심 전말과 이유를 담은 2~3줄 요약 (글머리 기호 • 사용)",
  "mood": "분위기를 나타내는 해시태그 2~3개 (예: #비주얼 #직캠 #감탄)"
}}
반드시 마크다운 없이 순수 JSON만 응답하세요.
"""
        req_data = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": 0.4}
        }).encode('utf-8')

        req = urllib.request.Request(url, data=req_data, headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                text = data['candidates'][0]['content']['parts'][0]['text'].strip()
                text = text.replace('```json', '').replace('```', '').strip()
                return json.loads(text)
        except urllib.error.HTTPError as he:
            if he.code == 429:
                log("LLM한도", "⚠️ Gemini 429 Too Many Requests (RPD 500 일일 한도 초과). 즉시 내장 스마트 템플릿으로 자동 전환합니다.")
            else:
                log("LLM오류", f"Gemini HTTP 오류 ({he.code}): {he.reason}")
            return None
        except Exception as e:
            log("LLM오류", f"Gemini 요약 예외: {e}")
            return None

    def save_event(self, start_dt, end_dt, cluster_id, summary_info, cluster):
        """gallery_events 테이블에 저장 (5시간 병합 및 중복 도배 방지)"""
        kw_json = json.dumps(cluster["keywords"], ensure_ascii=False)
        kw_names = {k["word"] for k in cluster["keywords"]}
        top_word = cluster["keywords"][0]["word"] if cluster["keywords"] else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                # 최근 5시간 내 유사 떡밥이 이미 있으면 기존 이벤트 업데이트 (Merge)
                cur.execute("""
                    SELECT event_id, keywords, post_count FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '5 hours'
                    ORDER BY event_id DESC
                """, (self.gallery_id, start_dt))
                existing = cur.fetchall()

                for eid, old_kws, old_cnt in existing:
                    old_names = {k["word"] for k in old_kws if isinstance(k, dict)}
                    intersect = len(kw_names & old_names)
                    old_top = old_kws[0]["word"] if (old_kws and isinstance(old_kws[0], dict)) else ""

                    # 키워드가 2개 이상 겹치거나 1등 키워드가 같으면 동일 떡밥으로 병합
                    if intersect >= 2 or (top_word and top_word == old_top):
                        cur.execute("""
                            UPDATE gallery_events
                            SET window_end = %s,
                                post_count = GREATEST(post_count, %s)
                            WHERE event_id = %s
                        """, (end_dt, cluster["post_count"], eid))
                        conn.commit()
                        log("분석기", f"기존 떡밥 #{eid} ('{top_word}') 연장 및 최신화 (중복 방지 완료)")
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
                    summary_info["title"],
                    summary_info["summary"],
                    summary_info["mood"],
                    kw_json,
                    cluster["post_count"],
                    cluster["post_ids"]
                ))
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def analyze_window(self, end_dt=None, window_hours=1, is_backfill=False):
        """지정된 시간 범위의 떡밥 감지 및 분석 실행"""
        if end_dt is None:
            end_dt = datetime.now()
        start_dt = end_dt - timedelta(hours=window_hours)
        baseline_start_dt = start_dt - timedelta(hours=24)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        # 1시간 내 글이 최소 15개 이상 올라온 활발한 시간대만 분석
        if len(target_posts) < 15:
            return []

        baseline_posts = self.fetch_posts_between(baseline_start_dt, start_dt)

        burst_keywords, target_post_words = self.detect_burst_keywords(target_posts, baseline_posts)
        if not burst_keywords:
            return []

        clusters = self.cluster_topics(burst_keywords, target_post_words)
        saved_events = []

        for idx, cluster in enumerate(clusters, 1):
            summary_info = self.generate_summary(cluster, start_dt, end_dt, is_backfill=is_backfill)
            event_id = self.save_event(start_dt, end_dt, idx, summary_info, cluster)
            if event_id:
                saved_events.append((event_id, summary_info["title"]))
                log("대형떡밥", f"🔥 [이벤트 #{event_id}] {summary_info['title']} ({cluster['post_count']}건 / 핵심어: {[k['word'] for k in cluster['keywords'][:3]]})")

        return saved_events

    def cleanup_old_micro_topics(self):
        """기준 미달(8건 미만) 소규모 잡담 이벤트 정리"""
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("DELETE FROM gallery_events WHERE post_count < 8")
                    deleted = cur.rowcount
                conn.commit()
            if deleted > 0:
                log("정리", f"🧹 기준 미달 소규모 잡담 이벤트 {deleted}건 정리 완료")
        except Exception:
            pass

def run_analyzer_loop():
    """20분 간격 실시간 대형 떡밥 감지 워커"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("떡밥감지기", "🚀 실시간 대형 떡밥 감지 워커 시작 (20분 간격 / API 절약 모드)")
    
    # 기존 자잘한 잡담 데이터 1회 자동 청소
    analyzer.cleanup_old_micro_topics()

    while True:
        try:
            analyzer.analyze_window(end_dt=datetime.now(), window_hours=1, is_backfill=False)
        except Exception as e:
            log("떡밥감지기", f"분석 오류: {e}")
        time.sleep(1200) # 20분마다 실행

if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
