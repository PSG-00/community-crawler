"""
topic_engine.py — 사전/형태소분석기 없이 동작하는 떡밥 후보 추출 엔진 (순수 함수, DB 의존 없음)

핵심 아이디어
1. 띄어쓰기(어절) 기준으로 단어를 자르고, 끝에 붙은 조사만 떼어낸다. (형태소 분석기 X → '공완' 같은 파편 원천 차단)
2. '평소에도 매일 나오는 말'은 떡밥이 아니다.
   → 지난 며칠간 매일 등장한 단어(총공, 스밍, 센모닝, ㅌㅍㅇㅈ ...)는 자동으로 '루틴'으로 분류 (사전 등록 불필요)
3. 진짜 떡밥 = '여러 사람이 서로 다른 문장으로' 같은 단어를 말하는 것.
   → 같은 제목 복붙/초단문 도배(미남시, 까엉시 ...)는 제목 다양성 점수로 자동 배제
4. 이렇게 걸러낸 '급상승 단어 + 대표 글'만 LLM에 보내서 묶고 요약하게 한다. (토큰 절약 + 정확도↑)
"""
import re
import math
import json
import time
import threading
import urllib.request
import urllib.error
from collections import Counter, defaultdict, deque

# 어절 끝 조사/어미 (긴 것부터 매칭)
_JOSA = sorted([
    "에서는", "에서도", "으로는", "이라도", "이라서", "이라고", "이라는", "한테서", "에게서",
    "에서", "으로", "에게", "한테", "까지", "부터", "보다", "처럼", "마다", "이랑", "이나", "이고",
    "이면", "이야", "이네", "인데", "이지", "라고", "라는", "라서", "랑은", "만큼", "밖에", "께서",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "랑", "로", "와", "과", "나", "야", "아",
], key=len, reverse=True)

# 디시/일상 범용어 (아주 소수만 유지 — 대부분은 '루틴 자동 판정'이 처리)
_GENERIC = {
    "진짜", "근데", "그냥", "오늘", "지금", "이거", "저거", "그거", "다들", "혹시", "아니", "너무", "완전",
    "존나", "개웃", "ㅋㅋ", "ㅋㅋㅋ", "ㅋㅋㅋㅋ", "ㅎㅎ", "ㅠㅠ", "ㄹㅇ", "ㅇㅇ", "뭐임", "뭐야", "있음", "없음",
    "같은데", "같음", "하는", "하고", "해서", "하면", "했는데", "있는", "없는", "우리", "이제", "아직", "벌써",
    "갤러", "갤러리", "념글", "개추", "사진", "영상", "링크", "근황", "후기", "질문", "정보", "이번", "제발",
    "완료", "너도", "사실", "있어", "알아", "하나", "처음", "애들", "글을", "어제", "금일", "내일", "바보",
    "사람", "생각", "느낌", "그래서", "근데요", "ㅇㅈ", "인증", "센갤", "갤에", "갤러들", "있네", "나도",
    "갑자기", "솔직히", "약간", "거의", "원래", "계속", "다시", "일단", "그냥", "진짜로", "급하게", "생각나네",
    "안녕하세요", "점심", "저녁", "아침", "출근", "퇴근", "떡밥", "벽갤", "리센느", "센둥이", "센둥이들",
    # 사용자가 명시적으로 떡밥에서 제외 요청한 팬 루틴 (나머지는 '매일 나오는 말' 자동 판정이 처리)
    "총공", "스밍", "투표", "출석", "출첵", "총공자", "오이쉬", "오이쉬에", "야호",
}

_URL_RE = re.compile(r'https?://\S+|www\.\S+|\S+\.(com|net|org|kr|be|me|io|ly)\S*', re.I)
# 디시 금칙어 우회용 글자 사이 점/슬래시/쉼표 결합 (우.리.은.행 -> 우리은행, 은.행 -> 은행)
_INTER_PUNCT_RE = re.compile(r'(?<=[가-힣0-9])[.,/·•_~\-]+(?=[가-힣])')
_CLEAN_RE = re.compile(r'[^0-9A-Za-z가-힣ㄱ-ㅎ\s]')
# 시간 맞춤 놀이(미남시, 까엉시, 경은씨+2, 중간시 ...): 짧은 제목이 '시/씨'로 끝나는 패턴
_TIMECALL_RE = re.compile(r'^[^\s]{0,14}[시씨](간)?\s*([+@\-]\s*\d*)?[!~.ㅋㅎㅠㅜ\s]*$')
# 총공/스밍/투표인증/인사 등 루틴성 게시글 제목 패턴
_RITUAL_TITLE_RE = re.compile(
    r'(ㅌㅍㅇㅈ|투표\s*인증|스밍\s*인증|총공\s*(완|완료|\d+일차|오이쉬|다녀왔)|오이쉬에?\s*~(?!.*무대)|오이쉬에?\s*(총공|완|완료)|^센모닝|^센나잇|^출첵|^출석)',
    re.I
)


def normalize_word(w):
    w = w.strip()
    if len(w) < 2:
        return ""
    for j in _JOSA:
        if w.endswith(j) and len(w) - len(j) >= 2:
            w = w[: -len(j)]
            break
    if len(w) < 2 or w in _GENERIC:
        return ""
    if re.fullmatch(r'[0-9]+', w):
        return ""
    # '미남시', '경은시', '경은씨', '중간시' 같은 시간 맞춤 놀이 단어
    if (w.endswith("시") or w.endswith("씨")) and len(w) <= 5:
        return ""
    # 총공완, 총공완료, 스밍완료 등 루틴 변형
    if re.match(r'^(총공|스밍|투표|출석|음총|댓총|센모닝|센나잇)', w):
        return ""
    if re.fullmatch(r'[ㄱ-ㅎ]+', w) and len(set(w)) <= 2:  # ㅋㅋㅋ, ㅎㅎ 류
        return ""
    return w


def is_ritual_title(title):
    """시간 맞춤 놀이 / 총공·투표·스밍 인증 / 초단문 인사 같은 루틴성 제목"""
    t = (title or "").strip()
    if len(t) <= 2:
        return True
    compact = re.sub(r'\s+', '', t)
    if _TIMECALL_RE.match(compact) and len(compact) <= 16:
        return True
    if _RITUAL_TITLE_RE.search(t):
        return True
    return False


def extract_words(title, content=""):
    """제목(가중) + 본문 앞부분에서 어절 단위 단어 집합 추출"""
    text = f"{title or ''} {(content or '')[:200] if content and content != '__EMPTY__' else ''}"
    text = _URL_RE.sub(" ", text)
    text = _INTER_PUNCT_RE.sub("", text)
    text = _CLEAN_RE.sub(" ", text)
    words = set()
    for raw in text.split():
        w = normalize_word(raw)
        if w:
            words.add(w)
    return words


def merge_prefix_family(cands):
    """
    포함관계(접두사) 묶기:
    - '무아'(34건) + '무아짱'(4건) → 더 많이 쓰인 '무아'(38건)로 대표어 통합
    - '검색'(6건) + '검색기록'(21건) → 더 구체적이고 많이 쓰인 '검색기록'(27건)으로 대표어 통합
    """
    words = sorted(cands.keys(), key=len)
    absorbed = set()
    for i, short in enumerate(words):
        if short in absorbed or short not in cands:
            continue
        for long in words[i + 1:]:
            if long in absorbed or long not in cands:
                continue
            if long.startswith(short) and len(short) >= 2:
                # 더 많은 글에서 쓰인 쪽을 대표 단어(keeper)로 삼는다
                if len(cands[long]["post_ids"]) > len(cands[short]["post_ids"]):
                    keeper, other = long, short
                else:
                    keeper, other = short, long
                cands[keeper]["post_ids"] |= cands[other]["post_ids"]
                cands[keeper]["variants"] |= cands[other]["variants"] | {other}
                cands[keeper]["base_cnt"] = max(cands[keeper]["base_cnt"], cands[other]["base_cnt"])
                absorbed.add(other)
                if other == short:
                    break
    for w in absorbed:
        cands.pop(w, None)
    return cands


def find_burst_candidates(target_posts, baseline_posts, baseline_hours, target_hours,
                          routine_days=None, min_posts=5, max_words=12):
    """
    target_posts / baseline_posts: [(post_id, title, content, created_at, author_code)]
    routine_days: {word: 과거 N일 중 등장한 일수} — 매일 나오는 말 자동 배제용 (없으면 생략)
    반환: (후보 단어 리스트, post_id→post 매핑)
    """
    post_map = {}
    target_word_posts = defaultdict(set)
    target_word_titles = defaultdict(list)
    target_word_authors = defaultdict(set)

    for p in target_posts:
        pid, title, content, created_at = p[0], p[1], p[2], p[3]
        author = p[4] if len(p) > 4 else None
        if is_ritual_title(title):
            continue
        post_map[pid] = p
        for w in extract_words(title, content):
            target_word_posts[w].add(pid)
            target_word_titles[w].append(re.sub(r'\s+', '', title or ''))
            if author:
                target_word_authors[w].add(author)

    base_df = Counter()
    for p in baseline_posts:
        if is_ritual_title(p[1]):
            continue
        for w in extract_words(p[1], p[2]):
            base_df[w] += 1

    n_target = max(1, len(post_map))
    n_base = max(1, sum(1 for p in baseline_posts if not is_ritual_title(p[1])))

    # 1차 후보 수집 (접두사 병합 전에는 3건 이상부터 열어둠)
    cands = {}
    for w, pids in target_word_posts.items():
        df = len(pids)
        if df < max(3, min_posts - 1):
            continue
        titles = target_word_titles[w]
        diversity = len(set(titles)) / len(titles)
        if diversity < 0.6:
            continue
        if target_word_authors.get(w) and len(target_word_authors[w]) < 3:
            continue
        cands[w] = {
            "word": w,
            "post_ids": set(pids),
            "variants": set(),
            "base_cnt": base_df[w],
        }

    # 접두사 가족 병합 후 통합 빈도로 Lift / Z-score 정확히 재계산
    cands = merge_prefix_family(cands)
    final_cands = []
    for w, c in cands.items():
        df = len(c["post_ids"])
        if df < min_posts:
            continue
        p_t = df / n_target
        p_b = c["base_cnt"] / n_base
        lift = (p_t + 0.002) / (p_b + 0.002)
        expected = p_b * n_target
        z = (df - expected) / math.sqrt(max(1.0, expected))

        if routine_days and routine_days.get(w, 0) >= 4 and lift < 6:
            continue
        if lift < 2.8 or z < 3.2:
            continue

        c["doc_count"] = df
        c["lift"] = round(lift, 1)
        c["z"] = round(z, 1)
        c["score"] = round(z * math.log2(lift + 1), 2)
        final_cands.append(c)

    ranked = sorted(final_cands, key=lambda x: x["score"], reverse=True)[:max_words]
    return ranked, post_map


def build_llm_digest(cands, post_map, per_word=5, max_posts=45, body_chars=60):
    """LLM에 보낼 압축 요약본: 급상승 단어 표 + 단어별 대표 글 (중복 제목 제거)"""
    lines_kw = []
    chosen = []
    seen_ids, seen_titles = set(), set()
    for c in cands:
        var = f" (변형: {', '.join(sorted(c['variants'])[:3])})" if c["variants"] else ""
        lines_kw.append(f"- {c['word']}{var}: {c['doc_count']}개 글, 평소 대비 {c['lift']}배")
        picked = 0
        # 시간 순으로 고르게 분포되도록 정렬
        for pid in sorted(c["post_ids"]):
            if picked >= per_word or len(chosen) >= max_posts:
                break
            p = post_map.get(pid)
            if not p:
                continue
            tkey = re.sub(r'\s+', '', p[1] or '')
            if pid in seen_ids or tkey in seen_titles:
                continue
            seen_ids.add(pid)
            seen_titles.add(tkey)
            chosen.append(p)
            picked += 1

    post_lines = []
    for p in sorted(chosen, key=lambda x: x[3]):
        body = re.sub(r'\s+', ' ', p[2] or '').strip()
        body = "" if body == "__EMPTY__" else body
        body = _URL_RE.sub("", body)[:body_chars]
        ts = p[3].strftime('%H:%M') if hasattr(p[3], 'strftime') else str(p[3])[11:16]
        post_lines.append(f"[{p[0]}] {ts} {p[1]}" + (f" | {body}" if body else ""))
    return "\n".join(lines_kw), "\n".join(post_lines)


# ============================================================
# LLM (Groq) 호출 — 무료 플랜 한도(분당 8K 토큰 / 일 200K 토큰 / 분당 30회 / 일 1K회) 엄수
# ============================================================
class GroqRateLimiter:
    """최근 60초 토큰 사용량 + 일일 토큰 사용량을 직접 추적해서, 한도에 닿기 전에 미리 기다린다."""

    def __init__(self, tpm=8000, tpd=200000, rpm=30, safety=0.8):
        self.tpm = int(tpm * safety)        # 6,400 토큰/분까지만 사용
        self.tpd = int(tpd * safety)        # 160,000 토큰/일까지만 사용
        self.rpm = int(rpm * safety)
        self.window = deque()               # (ts, tokens)
        self.day_key = None
        self.day_tokens = 0
        self.cooldown_until = 0.0
        self.lock = threading.Lock()

    def _roll(self, now):
        while self.window and now - self.window[0][0] > 60:
            self.window.popleft()
        dk = time.strftime("%Y-%m-%d", time.localtime(now))
        if dk != self.day_key:
            self.day_key, self.day_tokens = dk, 0

    def acquire(self, est_tokens, max_wait=180):
        """est_tokens 만큼 쓸 수 있을 때까지 대기. 일일 한도 초과 시 False."""
        deadline = time.time() + max_wait
        while True:
            with self.lock:
                now = time.time()
                self._roll(now)
                if self.day_tokens + est_tokens > self.tpd:
                    return False
                used = sum(t for _, t in self.window)
                if now >= self.cooldown_until and used + est_tokens <= self.tpm and len(self.window) < self.rpm:
                    self.window.append((now, est_tokens))   # 예약
                    return True
                wait = max(self.cooldown_until - now, (60 - (now - self.window[0][0]) + 0.5) if self.window else 1)
            if time.time() + wait > deadline:
                return False
            time.sleep(min(max(wait, 1), 30))

    def settle(self, est_tokens, real_tokens):
        """예약해둔 추정치를 실제 사용량으로 교정"""
        with self.lock:
            for i in range(len(self.window) - 1, -1, -1):
                if self.window[i][1] == est_tokens:
                    self.window[i] = (self.window[i][0], real_tokens)
                    break
            self.day_tokens += real_tokens

    def penalize(self, seconds):
        with self.lock:
            self.cooldown_until = max(self.cooldown_until, time.time() + seconds)


GROQ_LIMITER = GroqRateLimiter()

SYSTEM_PROMPT = (
    "너는 아이돌 그룹 '리센느' 디시인사이드 마이너 갤러리(센갤)의 떡밥 분석가다. "
    "갤러리 은어: 멤버=원이·리브·미나미·메이·제나, 팬덤=리마인, 센모닝/센나잇=인사, ㅌㅍㅇㅈ=투표인증, "
    "'○○시'=멤버 생일 숫자 시각에 글 쓰는 놀이, 총공/스밍=정기 팬활동. "
    "인사·시간 맞춤 놀이·총공/스밍/투표 인증 같은 매일 반복되는 루틴은 절대 떡밥이 아니다. "
    "사실에 근거해서 짧고 구체적으로, 디시 특유의 가벼운 톤으로 쓴다. 과장·뉴스체·추측 금지. 반드시 JSON으로만 답한다."
)


def build_user_prompt(time_range_str, n_posts, kw_block, posts_block):
    return f"""[분석 구간] {time_range_str} / 이 구간 전체 글 {n_posts}개

[평소 대비 급상승한 단어] (통계로 미리 계산됨)
{kw_block}

[급상승 단어가 들어간 대표 글] ([글번호] 시각 제목 | 본문 앞부분)
{posts_block}

할 일:
1. 위 단어들 중 같은 화제를 가리키는 것끼리 묶어서 '떡밥'을 만든다. 서로 무관한 화제는 별도 떡밥으로 나눈다. (최대 3개)
2. 루틴(인사, ○○시, 총공/스밍/투표 인증, 출석), 의미 없는 단어, 근거 글이 2개 미만인 화제는 버린다. 진짜 떡밥이 없으면 빈 배열.
3. keywords는 반드시 위 '급상승 단어' 목록에 있는 단어를 그대로 복사해서 2~4개 (검색에 그대로 쓰임). 중요도 순.
4. title: 무슨 일인지 바로 알 수 있는 구체적 제목 (25자 이내). 글에 없는 내용 지어내지 말 것.
5. summary: 글 내용에 근거한 2~3줄, 각 줄은 '• '로 시작.
6. sample_post_ids: 근거가 된 글번호 3~6개.

출력 형식:
{{"topics":[{{"title":"","summary":"• ...\\n• ...","mood":"#태그 #태그","keywords":["",""],"sample_post_ids":[0]}}]}}"""


def call_groq_topics(api_key, model, time_range_str, n_posts, kw_block, posts_block, log=print):
    """급상승 요약본을 Groq에 보내 떡밥 JSON을 받는다. 실패 시 None."""
    user_prompt = build_user_prompt(time_range_str, n_posts, kw_block, posts_block)
    # 한국어는 대략 1글자 ≈ 1토큰으로 넉넉히 추정 + 출력/추론 여유 1,500
    est = len(SYSTEM_PROMPT) + len(user_prompt) + 1500
    if not GROQ_LIMITER.acquire(est):
        log("Groq한도", f"일일/분당 토큰 예산 부족으로 이번 구간 LLM 분석 건너뜀 (추정 {est} 토큰)")
        return None

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0.2,
        "max_completion_tokens": 1500,
    }
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"   # 추론 토큰 낭비 방지 (TPM 절약)

    req = urllib.request.Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        usage = data.get("usage", {}) or {}
        real = int(usage.get("total_tokens") or est)
        GROQ_LIMITER.settle(est, real)
        content = data["choices"][0]["message"]["content"].strip()
        content = content.replace("```json", "").replace("```", "").strip()
        res = json.loads(content)
        log("Groq", f"응답 OK (토큰 {real}, 오늘 누적 {GROQ_LIMITER.day_tokens})")
        return res.get("topics", []) if isinstance(res, dict) else []
    except urllib.error.HTTPError as he:
        GROQ_LIMITER.settle(est, est)
        body = ""
        try:
            body = he.read().decode("utf-8")[:300]
        except Exception:
            pass
        if he.code == 429:
            ra = he.headers.get("retry-after") or "30"
            try:
                wait = float(ra)
            except ValueError:
                wait = 30.0
            GROQ_LIMITER.penalize(min(max(wait, 10), 600))
            log("Groq한도", f"429 수신 → {wait:.0f}초 쿨다운. {body}")
        else:
            log("Groq오류", f"HTTP {he.code}: {body}")
        return None
    except Exception as e:
        GROQ_LIMITER.settle(est, est)
        log("Groq오류", f"{type(e).__name__}: {e}")
        return None
