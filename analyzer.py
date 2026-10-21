import os
import json
import time
import math
import urllib.request
import urllib.parse
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
    "제작", "관련", "그때", "지금껏", "역시", "차라리", "결국", "항상", "이따", "조금"
}

_kiwi_instance = None

def get_kiwi():
    global _kiwi_instance
    if _kiwi_instance is None:
        _kiwi_instance = Kiwi()
    return _kiwi_instance

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
        """Z-Score 및 Lift 기반 급상승 키워드 감지"""
        if not target_posts:
            return []

        n_target = len(target_posts)
        n_base = max(1, len(baseline_posts))

        # 1. 대상 윈도우 단어 집계 (게시글 단위 Doc Frequency 및 Total Frequency)
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
            # 최소 3개 이상의 독립된 글에서 언급되어야 노이즈 방지
            if doc_cnt < 3:
                continue

            p_target = doc_cnt / n_target
            p_base = base_doc_freq[word] / n_base

            # Lift: 평소 출현 비율 대비 현재 출현 비율의 배수
            lift = (p_target + 0.003) / (p_base + 0.003)

            # 포아송/이항 근사 Z-score:
            expected_count = p_base * n_target
            z_score = (doc_cnt - expected_count) / math.sqrt(max(1.0, expected_count))

            # 임계치 검증: 평소 대비 2.2배 이상 급증 & Z-Score 2.5 이상
            if z_score >= 2.5 and lift >= 2.0:
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
        if len(burst_keywords) < 2:
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
            if weight >= 2:  # 최소 2개 이상의 글에서 동시 언급되어야 연결
                g.add_edge(u, v, weight=weight)

        # 연결 요소(Connected Components)로 독립된 떡밥 클러스터 분리
        clusters = []
        for comp in nx.connected_components(g):
            if len(comp) < 2:
                continue

            subgraph = g.subgraph(comp)
            # 클러스터 내 단어들이 언급된 고유 게시글 합집합
            cluster_posts = set()
            for w in comp:
                cluster_posts.update(word_post_map[w])

            if len(cluster_posts) < 3:
                continue

            comp_words = sorted([burst_set[w] for w in comp], key=lambda x: x["score"], reverse=True)

            edges = []
            for u, v, data in subgraph.edges(data=True):
                edges.append({"source": u, "target": v, "weight": data.get("weight", 1)})

            clusters.append({
                "keywords": comp_words,
                "edges": edges,
                "post_count": len(cluster_posts),
                "post_ids": list(cluster_posts)[:10]  # 대표 글 최대 10개
            })

        # 게시글 수 많은 순 정렬
        clusters.sort(key=lambda x: x["post_count"], reverse=True)
        return clusters

    def generate_summary(self, cluster, start_dt, end_dt):
        """Gemini LLM 또는 스마트 휴리스틱 요약기 생성"""
        kws = [k["word"] for k in cluster["keywords"]]
        kw_str = ", ".join(kws[:6])
        post_ids = cluster["post_ids"]

        # 대표 게시글 제목 수집
        sample_titles = []
        if post_ids:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT title FROM dc_posts WHERE post_id = ANY(%s) LIMIT 5", (post_ids,))
                    sample_titles = [r[0] for r in cur.fetchall()]

        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"

        # 1. Gemini API 호출 시도 (API 키가 있을 경우)
        if self.gemini_api_key:
            try:
                ai_result = self._call_gemini_summary(time_range_str, kws, sample_titles)
                if ai_result:
                    return ai_result
            except Exception as e:
                log("LLM오류", f"Gemini 요약 실패: {e}")

        # 2. Fallback 규칙 기반 요약기
        top_k1 = kws[0] if len(kws) > 0 else "갤러리"
        top_k2 = kws[1] if len(kws) > 1 else ""
        title = f"{top_k1} · {top_k2} 관련 화제 집중" if top_k2 else f"{top_k1} 관련 게시글 급증"

        summary_lines = [
            f"• {time_range_str} 사이 '{kw_str}' 관련 게시글이 {cluster['post_count']}건 집중 작성되었습니다.",
            f"• 대표 키워드 '{top_k1}'의 출현 빈도가 24시간 평균 대비 {cluster['keywords'][0]['lift']}배 급상승했습니다.",
            f"• 갤러리 내 주요 언급: " + (" / ".join(sample_titles[:2]) if sample_titles else f"{top_k1} 중심 이야기 활발")
        ]
        mood = f"#{top_k1} #{top_k2 if top_k2 else '화제'} #실시간"

        return {
            "title": title,
            "summary": "\n".join(summary_lines),
            "mood": mood
        }

    def _call_gemini_summary(self, time_range_str, keywords, sample_titles):
        """Google Gemini API를 통한 고품질 떡밥 요약 (의존성 없이 urllib로 호출)"""
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
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            text = data['candidates'][0]['content']['parts'][0]['text'].strip()
            # 마크다운 코드블록 제거
            text = text.replace('```json', '').replace('```', '').strip()
            return json.loads(text)

    def save_event(self, start_dt, end_dt, cluster_id, summary_info, cluster):
        """gallery_events 테이블에 저장 (중복 방지 로직 포함)"""
        kw_json = json.dumps(cluster["keywords"], ensure_ascii=False)
        kw_names = {k["word"] for k in cluster["keywords"]}

        with get_db() as conn:
            with conn.cursor() as cur:
                # 최근 2시간 내 동일 키워드 유사도가 50% 이상인 이벤트가 있으면 중복 삽입 방지
                cur.execute("""
                    SELECT event_id, keywords FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '2 hours'
                """, (self.gallery_id, start_dt))
                existing = cur.fetchall()

                for eid, old_kws in existing:
                    old_names = {k["word"] for k in old_kws}
                    intersect = len(kw_names & old_names)
                    if intersect >= max(2, len(kw_names) * 0.5):
                        log("분석기", f"기존 이벤트 #{eid}와 유사 키워드({intersect}개 일치)로 중복 스킵.")
                        return None

                # 신규 이벤트 저장
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

    def analyze_window(self, end_dt=None, window_hours=1):
        """지정된 시간 범위(기본 1시간)의 떡밥 감지 및 분석 실행"""
        if end_dt is None:
            end_dt = datetime.now()
        start_dt = end_dt - timedelta(hours=window_hours)
        baseline_start_dt = start_dt - timedelta(hours=24)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        if len(target_posts) < 5:
            return []

        baseline_posts = self.fetch_posts_between(baseline_start_dt, start_dt)

        burst_keywords, target_post_words = self.detect_burst_keywords(target_posts, baseline_posts)
        if not burst_keywords:
            return []

        clusters = self.cluster_topics(burst_keywords, target_post_words)
        saved_events = []

        for idx, cluster in enumerate(clusters, 1):
            summary_info = self.generate_summary(cluster, start_dt, end_dt)
            event_id = self.save_event(start_dt, end_dt, idx, summary_info, cluster)
            if event_id:
                saved_events.append((event_id, summary_info["title"]))
                log("떡밥포착", f"🔥 [이벤트 #{event_id}] {summary_info['title']} ({cluster['post_count']}개 글 / 키워드: {[k['word'] for k in cluster['keywords'][:4]]})")

        return saved_events

    def backfill_history(self, days=3):
        """과거 N일간의 떡밥 타임라인을 슬라이딩 윈도우로 일괄 분석"""
        log("타임라인백필", f"⏳ 최근 {days}일간의 주요 떡밥 이벤트 히스토리 추출 시작...")
        now = datetime.now()
        start = now - timedelta(days=days)

        curr = start + timedelta(hours=1)
        total_created = 0
        while curr <= now:
            try:
                events = self.analyze_window(end_dt=curr, window_hours=1)
                total_created += len(events)
            except Exception as e:
                pass
            curr += timedelta(hours=1)
            time.sleep(0.05)

        log("타임라인백필", f"🎉 과거 {days}일간 총 {total_created}개의 주요 떡밥 이벤트 복원 완료!")

def run_analyzer_loop():
    """10분마다 최근 1시간 갤러리 글을 분석하여 떡밥 감지"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("떡밥감지기", "🚀 실시간 떡밥 타임라인 감지 워커 가동 (10분 간격)")
    
    # 최초 실행 시 최근 2일치 떡밥 히스토리 자동 추출
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM gallery_events")
                cnt = cur.fetchone()[0]
                if cnt == 0:
                    analyzer.backfill_history(days=2)
    except Exception as e:
        log("떡밥감지기", f"초기 백필 확인 오류: {e}")

    while True:
        try:
            analyzer.analyze_window(end_dt=datetime.now(), window_hours=1)
        except Exception as e:
            log("떡밥감지기", f"분석 오류: {e}")
        time.sleep(600)

if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
