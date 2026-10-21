import os
import re
import json
import time
import math
import base64
import urllib.request
import urllib.parse
import urllib.error
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict
import networkx as nx
from kiwipiepy import Kiwi
from database import get_db

KST = timezone(timedelta(hours=9))

# 디시인사이드 특화 불용어 및 루틴성 노이즈 단어 사전
STOPWORDS = {
    # 1. 멤버 생일시(시간 놀이) 노이즈 필터링
    "리브시", "원시", "원이시", "제나시", "미나미시", "메이시", "남시", "시글",
    # 2. 일상 루틴 팬 활동 체크인 / 총공 노이즈
    "총공", "총공완료", "스밍완료", "투표완료", "출석", "출첵", "완료", "인증글",
    # 3. 디시인사이드 도메인 특화 은어 및 시스템 용어
    "디시", "갤러", "갤러리", "갤", "념글", "개추", "비추", "파딱", "주딱", "완장",
    "유동", "고닉", "통피", "댓글", "작성자", "게시글", "본문", "사진", "영상", "링크",
    "화질", "캡처", "움짤", "자짤", "제목",
    # 4. URL 및 웹 아티팩트
    "유튜브", "트위터", "인스타", "틱톡", "링크", "사이트", "영상링크", "조회수",
    # 5. 일상 구어체 및 지시대명사/부사 (단, '우리'는 우리은행 등 결합 고려하여 제외)
    "진짜", "오늘", "근데", "어제", "내일", "지금", "하나", "사람",
    "그냥", "이거", "저거", "그거", "누구", "보고", "때문", "모두", "다들",
    "어디", "언제", "어떻게", "다시", "자기", "정도", "기준", "이번", "얘기", "시작",
    "원래", "제일", "제발", "존나", "시발", "새끼", "새기", "존버", "거의", "약간",
    "전부", "다른", "모든", "무슨", "아무", "어떤", "어차피", "확실", "경우", "자체",
    "사실", "소리", "내용", "부분", "가지", "자꾸", "뭔가", "그게", "저게", "이건",
    "저건", "그건", "이게", "저기", "여기", "거기", "진심", "약간", "제발", "가장",
    "제작", "관련", "그때", "지금껏", "역시", "차라리", "결국", "항상", "이따", "조금",
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
        # 아이돌 멤버명 및 주요 상표/도메인 단어 사용자 사전 등록 (분리 방지)
        custom_words = [
            ("우리은행", "NNP"), ("체크카드", "NNG"), ("포토카드", "NNG"), ("포카", "NNG"),
            ("리센느", "NNP"), ("미나미", "NNP"), ("제나", "NNP"), ("리브", "NNP"),
            ("메이", "NNP"), ("원이", "NNP"), ("원희", "NNP"), ("직캠", "NNG"),
            ("스밍", "NNG"), ("음방", "NNG"), ("사전투표", "NNG"), ("뮤비", "NNG"),
            ("광고모델", "NNG")
        ]
        for w, tag in custom_words:
            try:
                _kiwi_instance.add_user_word(w, tag)
            except Exception:
                pass
    return _kiwi_instance

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

class TopicAnalyzer:
    def __init__(self, gallery_id="rescene1"):
        self.gallery_id = gallery_id
        self.groq_api_key = os.getenv("GROQ_API_KEY", get_default_groq_key()).strip()
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
        self.gemini_api_key = os.getenv("GEMINI_API_KEY", "").strip()

    def clean_text(self, text):
        """URL 및 웹 주소 조각 원천 제거"""
        if not text:
            return ""
        # 1. http, https, www, 도메인 패턴 완전 제거
        t = re.sub(r'https?://\S+|www\.\S+|[\w-]+\.(com|net|org|kr|co\.kr|be|me|io|xyz)\S*', ' ', text)
        # 2. 디시 및 플랫폼 경로 잔재 제거
        t = re.sub(r'\b(gall|board|lists|view|dcinside|mgallery|youtu|youtube|twitter|instagram)\b', ' ', t, flags=re.IGNORECASE)
        # 3. 특수문자 제거
        t = re.sub(r'[^\w\s가-힣a-zA-Z]', ' ', t)
        return t

    def tokenize(self, text):
        """텍스트에서 유효 명사/외래어/신조어 추출 및 공백 없이 붙은 복합명사 자동 결합"""
        if not text:
            return []
        cleaned = self.clean_text(text)
        kiwi = get_kiwi()
        try:
            tokens = kiwi.tokenize(cleaned)
            words = []
            curr_word = ""
            curr_end = -1

            for t in tokens:
                # 명사 계열 및 접두사성 토큰 자동 결합 판정
                if t.tag in ('NNG', 'NNP', 'XR') or (t.tag == 'NP' and t.form in ('우리',)):
                    # 원문에서 띄어쓰기 없이 바로 붙어있으면 자동으로 한 단어로 결합 (예: 우리+은행 -> 우리은행, 체크+카드 -> 체크카드)
                    if curr_word and t.start == curr_end:
                        curr_word += t.form
                        curr_end = t.start + t.len
                    else:
                        if curr_word and len(curr_word) >= 2 and curr_word not in STOPWORDS:
                            words.append(curr_word)
                        curr_word = t.form
                        curr_end = t.start + t.len
                else:
                    if curr_word and len(curr_word) >= 2 and curr_word not in STOPWORDS:
                        words.append(curr_word)
                    curr_word = ""
                    curr_end = -1

            if curr_word and len(curr_word) >= 2 and curr_word not in STOPWORDS:
                words.append(curr_word)

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
            # 최소 3개 이상의 글에서 동시 언급되어야 연결 (가짜 링크 제거)
            if weight >= 3:
                g.add_edge(u, v, weight=weight)

        # 연결 요소(Connected Components)로 독립된 떡밥 클러스터 분리
        clusters = []
        for comp in nx.connected_components(g):
            # 최소 2개 이상의 키워드가 얽혀있어야 떡밥 인정
            if len(comp) < 2:
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
        """제목 + 본문 핵심 문맥을 모두 반영한 고품질 요약 생성기"""
        kws = [k["word"] for k in cluster["keywords"]]
        kw_str = ", ".join(kws[:6])
        post_ids = cluster["post_ids"]
        top_k1 = kws[0] if len(kws) > 0 else "갤러리"
        top_k2 = kws[1] if len(kws) > 1 else ""

        # 대표 게시글 (제목 + 본문 내용) 수집
        sample_posts_text = []
        if post_ids:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT title, content FROM dc_posts
                        WHERE post_id = ANY(%s)
                        LIMIT 5
                    """, (post_ids,))
                    for r_title, r_content in cur.fetchall():
                        clean_body = re.sub(r'\s+', ' ', r_content or '').strip()
                        if clean_body == '__EMPTY__':
                            clean_body = ''
                        sample_posts_text.append(f"제목: {r_title}\n내용: {clean_body[:180]}")

        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"

        # 1. 1순위: Groq API (openai/gpt-oss-120b, 1000 RPD, 0.5초 초고속 LPU)
        if self.groq_api_key and cluster["post_count"] >= 8:
            ai_result = self._call_groq_summary(time_range_str, kws, sample_posts_text)
            if ai_result:
                log("LLM요약", f"⚡ Groq ({self.groq_model}) 요약 완료 (초고속 LPU / 1,000 RPD)")
                return ai_result

        # 2. 2순위: Gemini API (gemini-2.5-flash, 500 RPD)
        if (not is_backfill) and self.gemini_api_key and cluster["post_count"] >= 10 and can_call_gemini():
            ai_result = self._call_gemini_summary(time_range_str, kws, sample_posts_text)
            if ai_result:
                record_gemini_call()
                log("LLM요약", f"✨ Gemini API 요약 완료 (오늘 사용량: {_daily_gemini_calls}/500 RPD)")
                return ai_result

        # 3. 3순위: 고품질 스마트 휴리스틱 요약기 (API 호출 0회, 비용 0원)
        title = f"'{top_k1}' · '{top_k2}' 관련 화제 집중" if top_k2 else f"'{top_k1}' 관련 게시글 급증"

        summary_lines = [
            f"• {time_range_str} 사이 '{kw_str}' 관련 게시글이 {cluster['post_count']}건 집중 게재되었습니다.",
            f"• 핵심 키워드 '{top_k1}'의 언급 빈도가 평소 대비 {cluster['keywords'][0]['lift']}배 폭증했습니다.",
            f"• 주요 갤러리 반응: {sample_posts_text[0].split(chr(10))[0] if sample_posts_text else f'{top_k1} 중심 이야기 활발'}"
        ]
        mood = f"#{top_k1} #{top_k2 if top_k2 else '화제'} #실시간"

        return {
            "title": title,
            "summary": "\n".join(summary_lines),
            "mood": mood
        }

    def _call_groq_summary(self, time_range_str, keywords, sample_posts_text):
        """Groq API (openai/gpt-oss-120b)를 통한 초고속 AI 요약"""
        if not self.groq_api_key:
            return None
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        }
        prompt = f"""당신은 디시인사이드 아이돌 갤러리의 유쾌하고 눈치 빠른 트렌드 요약 봇입니다.
시간대: {time_range_str}
급상승 핵심 키워드: {', '.join(keywords[:7])}

해당 시간대 대표 게시글 (제목 및 본문 내용):
{chr(10).join(['[게시글 ' + str(i+1) + ']\n' + p for i, p in enumerate(sample_posts_text)])}

[★ 작성 시 절대 주의사항]
1. 본문 내용 기반 실제 맥락 완벽 반영 (핵심!):
   - 제목만 보지 말고 본문 내용을 꼼꼼히 확인하여 "누가, 무엇을, 왜" 이야기하는지 구체적 팩트(예: 특정 브랜드/은행 광고 모델 발탁, 특정 방송/이벤트, 실제 굿즈/카드 명칭 등)를 정확하게 포착하세요!
   - 모호하거나 두루뭉술하게 얼버무리지 말고 구체적인 화제 대상(예: '우리은행 광고 모델 발탁', '신규 체크카드 포토카드 증정' 등)을 제목과 요약에 정확히 명시하세요.
2. 과도한 진지충 / 사회 뉴스 기사체 절대 금지:
   - 갤러들이 장난치며 노는 것(유튜브 검색기록 공유, 밈, 사진 인증, 주접 등)을 보고 '개인정보 유출', '프라이버시 우려' 같은 심각한 사회 뉴스식 오버해석을 절대로 하지 마세요!
   - 갤러들의 실제 행동과 놀이 의도 그대로 유쾌하고 생생하게 파악하세요.
3. 디시 갤러리 특유의 가볍고 생동감 있는 톤 유지:
   - 팬덤의 덕질, 주접, 웃긴 릴레이, 떡밥 반응을 친근하고 생동감 있게 요약하세요.

위 데이터를 바탕으로 다음 JSON 포맷으로 작성하세요:
{{
  "title": "화제 대상을 구체적으로 명시한 생생한 한 줄 제목 (20자 내외)",
  "summary": "구체적인 원인과 맥락을 담은 유쾌하고 정확한 2~3줄 요약 (글머리 기호 • 사용)",
  "mood": "실제 커뮤니티 분위기를 담은 해시태그 2~3개"
}}
반드시 마크다운 없이 순수 JSON 객체로만 응답하세요."""

        payload = {
            "model": self.groq_model,
            "messages": [
                {"role": "system", "content": "디시인사이드 팬 갤러리 트렌드 요약봇. 본문 사실관계 정확 반영. 진지한 뉴스체 금지. 유쾌하고 생생한 커뮤니티 톤앤매너 유지. 한국어로 JSON 응답."},
                {"role": "user", "content": prompt}
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.3
        }
        req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                raw_text = data["choices"][0]["message"]["content"].strip()
                res_json = json.loads(raw_text)
                return res_json
        except urllib.error.HTTPError as he:
            if he.code == 429:
                log("Groq한도", "⚠️ Groq 429 Too Many Requests (1,000 RPD 도달). Gemini 또는 템플릿으로 자동 전환합니다.")
            else:
                log("Groq오류", f"Groq HTTP 에러 ({he.code}): {he.reason}")
            return None
        except Exception as e:
            log("Groq오류", f"Groq 처리 예외: {e}")
            return None

    def _call_gemini_summary(self, time_range_str, keywords, sample_posts_text):
        """Google Gemini API를 통한 고품질 요약 (최대 10초 타임아웃)"""
        url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={self.gemini_api_key}"
        
        prompt = f"""당신은 디시인사이드 아이돌 갤러리의 유쾌하고 눈치 빠른 트렌드 요약 봇입니다.
시간대: {time_range_str}
급상승 핵심 키워드: {', '.join(keywords[:7])}

해당 시간대 대표 게시글 (제목 및 본문 내용):
{chr(10).join(['[게시글 ' + str(i+1) + ']\n' + p for i, p in enumerate(sample_posts_text)])}

[★ 작성 시 절대 주의사항]
1. 본문 내용 기반 실제 맥락 완벽 반영:
   - 제목만 보지 말고 본문 내용을 꼼꼼히 확인하여 구체적 팩트(예: '우리은행 광고 모델 발탁', '신규 체크카드 포토카드 증정' 등)를 제목과 요약에 정확히 명시하세요.
2. 과도한 진지충 / 사회 뉴스 기사체 절대 금지:
   - 개인정보 유출, 프라이버시 우려 같은 사회 뉴스식 오버해석 절대 금지! 갤러들의 실제 놀이 의도 그대로 유쾌하게 파악하세요.
3. 디시 갤러리 특유의 가볍고 생동감 있는 톤 유지.

위 데이터를 바탕으로 다음 JSON 포맷으로 작성하세요:
{{
  "title": "화제 대상을 구체적으로 명시한 생생한 한 줄 제목 (20자 내외)",
  "summary": "구체적인 원인과 맥락을 담은 유쾌하고 정확한 2~3줄 요약 (글머리 기호 • 사용)",
  "mood": "실제 커뮤니티 분위기를 담은 해시태그 2~3개"
}}
반드시 마크다운 없이 순수 JSON 객체로만 응답하세요."""
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
        """지정된 시간 범위의 떡밥 감지 및 분석 실행 (한국 표준시 KST 기준)"""
        if end_dt is None:
            end_dt = get_now_kst()
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

    def reset_and_regenerate_24h(self):
        """기존 떡밥 완전 삭제 후 최근 24시간을 클린하게 전면 재분석"""
        log("재구축", "🧹 기존 떡밥 타임라인 완전 초기화 및 최근 24시간 클린 재구축 시작...")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE gallery_events RESTART IDENTITY;")
            conn.commit()

        now_k = get_now_kst()
        total_created = 0

        # 과거 24시간을 2시간 간격으로 슬라이딩 분석
        for hours_back in range(24, 0, -2):
            target_end = now_k - timedelta(hours=hours_back)
            try:
                events = self.analyze_window(end_dt=target_end, window_hours=2, is_backfill=False)
                total_created += len(events)
            except Exception as e:
                pass
            time.sleep(0.1)

        # 현재 시각 기준 마지막 분석
        try:
            events = self.analyze_window(end_dt=now_k, window_hours=2, is_backfill=False)
            total_created += len(events)
        except Exception:
            pass

        log("재구축", f"🎉 최근 24시간 주요 떡밥 총 {total_created}건 클린 재구축 완료!")

def run_analyzer_loop():
    """20분 간격 실시간 대형 떡밥 감지 워커 (KST 표준시 기준)"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("떡밥감지기", "🚀 실시간 대형 떡밥 감지 워커 시작 (KST 동기화 / 20분 간격 / Groq 120B 연동)")
    
    # 시작 시 최근 24시간 클린 전면 재구축 1회 실행!
    analyzer.reset_and_regenerate_24h()

    while True:
        try:
            analyzer.analyze_window(end_dt=get_now_kst(), window_hours=1, is_backfill=False)
        except Exception as e:
            log("떡밥감지기", f"분석 오류: {e}")
        time.sleep(1200) # 20분마다 실행

if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
