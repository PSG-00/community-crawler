"""
topic_engine.py — LLM 없이 100% 단어 노트(Word Note)와 게시글 내 연관성(Co-occurrence)만으로
실제 갤러리 떡밥 덩어리와 시간대를 추출하는 엔진
"""
import re
import math
from collections import Counter, defaultdict
from datetime import timedelta

# 어절 끝 조사/어미 (긴 것부터 매칭)
_JOSA = sorted([
    "에서는", "에서도", "으로는", "이라도", "이라서", "이라고", "이라는", "한테서", "에게서",
    "에서", "으로", "에게", "한테", "까지", "부터", "보다", "처럼", "마다", "이랑", "이나", "이고",
    "이면", "이야", "이네", "인데", "이지", "라고", "라는", "라서", "랑은", "만큼", "밖에", "께서",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "랑", "로", "와", "과", "나", "야", "아",
], key=len, reverse=True)

# 디시/일상 범용 기능어 (의미 없는 부사·대명사·상투어)
_GENERIC = {
    "진짜", "근데", "그냥", "오늘", "지금", "이거", "저거", "그거", "다들", "혹시", "아니", "너무", "완전",
    "존나", "개웃", "ㅋㅋ", "ㅋㅋㅋ", "ㅋㅋㅋㅋ", "ㅎㅎ", "ㅠㅠ", "ㄹㅇ", "ㅇㅇ", "뭐임", "뭐야", "있음", "없음",
    "같은데", "같음", "하는", "하고", "해서", "하면", "했는데", "있는", "없는", "우리", "이제", "아직", "벌써",
    "갤러", "갤러리", "념글", "개추", "사진", "영상", "링크", "근황", "후기", "질문", "정보", "이번", "제발",
    "완료", "너도", "사실", "있어", "알아", "하나", "처음", "애들", "글을", "어제", "금일", "내일", "바보",
    "사람", "생각", "느낌", "그래서", "근데요", "ㅇㅈ", "인증", "센갤", "갤에", "갤러들", "있네", "나도",
    "갑자기", "솔직히", "약간", "거의", "원래", "계속", "다시", "일단", "진짜로", "급하게", "생각나네", "어떻게",
    "안녕하세요", "점심", "저녁", "아침", "출근", "퇴근", "떡밥", "벽갤", "리센느", "센둥이", "센둥이들", "본인",
    "여기에", "여기", "거기", "저기", "누가", "무슨", "어떤", "모든", "전부", "조금", "많이", "제일", "가장",
    "보니까", "봤는데", "보는", "보고", "할까", "해야", "되나", "되는", "된다", "맞음", "맞냐", "아님", "아니라",
    "총공", "스밍", "투표", "출석", "출첵", "총공자", "오이쉬", "오이쉬에", "야호",
    "생일시", "멤버시", "중간시", "대장시", "경은시", "리브시", "미남시", "미나미시", "까엉시", "제나시", "메이시", "예빈시", "원이시",
    "새로고침", "머야", "오늘도", "앞에서", "있는데", "아니라", "진짜로", "어제자", "지금까지", "하는거", "같은데",
}

# 갤러리 상시 소재 (멤버명/버블/갤벤 등): 단독이나 보조 연관어는 될 수 있어도, 서로 다른 두 떡밥을 이어주는 '다리(Bridge)'가 되면 안 되는 단어들
_BRIDGE_STOPWORDS = {
    "버블", "갤벤", "겔벤", "갸루벤", "공지", "끌올", "당발", "당첨",
    "원이", "대장", "리브", "리부", "경은", "진경은", "미나미", "미남", "메이", "예빈", "이예빈", "제나", "까엉", "가영",
}

_URL_RE = re.compile(r'https?://\S+|www\.\S+|\S+\.(com|net|org|kr|be|me|io|ly)\S*', re.I)
# 디시 금칙어 우회용 글자 사이 점/슬래시/쉼표 결합 (우.리.은.행 -> 우리은행, 서.수 -> 서수)
_INTER_PUNCT_RE = re.compile(r'(?<=[가-힣0-9])[.,/·•_~\-]+(?=[가-힣])')
_CLEAN_RE = re.compile(r'[^0-9A-Za-z가-힣ㄱ-ㅎ\s]')
# 시간 맞춤 놀이(미남시, 까엉시, 경은씨+2, 진경은짱짱걸시, 수원왕발가락시 ...): 짧은 제목이 '시/씨'로 끝나는 패턴
_TIMECALL_RE = re.compile(r'^[^\s]{0,16}[시씨](간)?\s*([+@\-]\s*\d*)?[!~.?ㅋㅎㅠㅜ\s]*$')
# 문장형 생일시 언급 (예: "아 경은시 놓쳤어", "까엉시 +1", "미남시 막타", "생일시 보내줘야지")
_TIMECALL_MENTION_RE = re.compile(
    r'(생일시|멤버시|중간시|낑긴시|(리브|리부|리뿌|경은|미나미|미남|미냠|오이데|원이|오이|대장|메이|예빈|큣걸|핫걸|제나|까엉|가영|가엉|무아)[가-힣]{0,4}[시씨](?![가-힣])|[가-힣]{1,8}[시씨]\s*(\+\s*\d+|\+@|놓쳤|놓친|실패|성공|막타|지난시))'
)
# 총공/스밍/투표인증/인사 등 루틴성 게시글 제목 패턴
_RITUAL_TITLE_RE = re.compile(
    r'(ㅌㅍㅇㅈ|ㄱㅂㅇㅈ|투표\s*인증|스밍\s*인증|총공[^\n]{0,18}(완|완료|인증|일차|다녀왔|끝|등반)|총공\s*(완|완료|\d+일차|오이쉬|다녀왔|끝)|오이쉬에?\s*~(?!.*무대)|오이쉬에?\s*(총공|완|완료)|^센모닝|^센나잇|^출첵|^출석)',
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
    if (w.endswith("시") or w.endswith("씨")) and len(w) <= 8:
        return ""
    if re.match(r'^(총공|스밍|투표|출석|음총|댓총|센모닝|센나잇|생일시|멤버시)', w):
        return ""
    if re.fullmatch(r'[ㄱ-ㅎ]+', w) and len(set(w)) <= 2:
        return ""
    return w


def is_ritual_title(title):
    """시간 맞춤 놀이 / 총공·투표·스밍 인증 / 초단문 인사 같은 루틴성 제목"""
    t = (title or "").strip()
    if len(t) <= 2:
        return True
    compact = re.sub(r'\s+', '', t)
    if _TIMECALL_RE.match(compact) and len(compact) <= 18:
        return True
    if _TIMECALL_MENTION_RE.search(t):
        return True
    if _RITUAL_TITLE_RE.search(t):
        return True
    return False


def extract_words(title, content=""):
    """게시글 1개의 [단어 노트] 생성: 점 찍힌 단어 복원 + 어절 분리 + 조사 제거 (본문 하단 잡담 오염 방지를 위해 제목+본문 첫 80자만 사용)"""
    clean_content = (content or "")[:80] if content and content != "__EMPTY__" else ""
    text = f"{title or ''} {clean_content}"
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
    접두사 포함관계 묶기:
    - '무아'(34건) + '무아짱'(4건) → '무아'(38건)
    - '검색'(6건) + '검색기록'(21건) → '검색기록'(27건)
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
                if len(cands[long]["post_ids"]) >= len(cands[short]["post_ids"]):
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


def _are_words_strongly_related(c1, c2):
    """
    두 급상승 단어가 '정말로 같은 토픽'인지 엄격하게 판정 (억지 병합 및 다리 연결 방지):
    1) 글자 뿌리 / 은어 변형 (예: 우이은행 <-> 우리은행 <-> 은행, 검색기록 <-> 기록, 1호선 <-> 2호선, 슈붕 <-> 팥붕 <-> 붕어빵)
    2) 높은 게시글 동시출현 비율:
       - 두 단어가 같은 게시글에 최소 3건 이상 함께 등장하면서,
       - 작은 쪽 게시글의 30% 이상(또는 Jaccard >= 20%)이 겹칠 때만 같은 토픽으로 묶음!
       - 단, 멤버 이름이나 '버블/갤벤' 같은 상시어(_BRIDGE_STOPWORDS)는 어근 변형이 아닌 이상 다른 고유 토픽과 합치지 않음.
    """
    w1, w2 = c1["word"], c2["word"]
    pids1, pids2 = c1["post_ids"], c2["post_ids"]
    inter = len(pids1 & pids2)
    min_len = max(1, min(len(pids1), len(pids2)))
    union_len = max(1, len(pids1 | pids2))

    # 고리 1: 글자 뿌리 / 은어 변형
    short, long = (w1, w2) if len(w1) <= len(w2) else (w2, w1)
    if len(short) >= 2 and (long.startswith(short) or long.endswith(short)):
        return True
    if len(w1) == len(w2) and len(w1) >= 4 and sum(a != b for a, b in zip(w1, w2)) == 1:
        return True
    # 공통 접미사 2글자 이상 (예: 1호선/2호선/4호선 -> '호선', 핫걸단/큣걸단 -> '걸단')
    if len(w1) >= 3 and len(w2) >= 3 and w1[-2:] == w2[-2:] and inter >= 1:
        return True
    # '붕어빵' <-> '슈붕'/'팥붕' 처럼 특수 줄임말이 같이 쓰이면서 글자를 공유할 때
    if inter >= 1 and (short[0] in long or short[-1] in long):
        return True

    # 상시어(멤버 이름, 버블, 갤벤 등)가 다른 독립 토픽(반바지, 성대모사, 방탈출 등)을 흡수하지 못하도록 차단
    if (w1 in _BRIDGE_STOPWORDS) != (w2 in _BRIDGE_STOPWORDS):
        if (inter / min_len) < 0.55 or inter < 4:
            return False

    # 고리 2: 엄격한 직접 게시글 동시출현 (최소 3건 이상 + 작은 쪽의 32% 이상 또는 전체의 20% 이상 공유)
    if inter >= 3 and ((inter / min_len) >= 0.32 or (inter / union_len) >= 0.20):
        return True

    return False


def find_burst_candidates(target_posts, baseline_posts, baseline_hours=24, target_hours=1,
                          routine_days=None, min_posts=5, max_words=15):
    """
    1단계: 각 게시글의 단어 노트를 만들고 평소(24h) 대비 급상승한 단어 후보들을 추출
    """
    post_map = {}
    post_notes = {}
    target_word_posts = defaultdict(set)
    target_word_titles = defaultdict(list)
    target_word_authors = defaultdict(set)

    for p in target_posts:
        pid, title, content, created_at = p[0], p[1], p[2], p[3]
        author = p[4] if len(p) > 4 else None
        if is_ritual_title(title):
            continue
        post_map[pid] = p
        note = extract_words(title, content)
        post_notes[pid] = note
        t_clean = re.sub(r'\s+', '', title or '')
        for w in note:
            target_word_posts[w].add(pid)
            target_word_titles[w].append(t_clean)
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
    return ranked, post_map, post_notes, base_df, n_target, n_base


def build_word_note_clusters(target_posts, baseline_posts, baseline_hours=24, target_hours=1.5,
                             min_posts=5, min_cluster_posts=7):
    """
    2단계 (메인 엔진 — LLM 무사용):
    급상승 단어 후보들을 3가지 연관성 고리로 묶어 '떡밥 단어 덩어리(Cluster)'를 만들고,
    그 덩어리에 속한 게시글들의 단어 노트에서 함께 등장한 보조 연관어(모델, 광고, 커피차, 유튜브 등)를 추출한다.
    """
    cands, post_map, post_notes, base_df, n_target, n_base = find_burst_candidates(
        target_posts, baseline_posts,
        baseline_hours=baseline_hours, target_hours=target_hours,
        min_posts=min_posts, max_words=15,
    )
    if not cands:
        return []

    # Star-Clustering (중심축 직접 연결 방식):
    # A-B가 연결되고 B-C가 연결되었다고 해서 A-B-C를 다리 건너 합치지 않고,
    # 점수가 가장 높은 중심 단어(Anchor)와 '직접' 강하게 연결된 단어들만 한 클러스터로 묶는다!
    cands.sort(key=lambda x: (x["word"] not in _BRIDGE_STOPWORDS, x["score"], x["doc_count"]), reverse=True)
    n = len(cands)
    used = set()
    clusters = []

    for i in range(n):
        if i in used:
            continue
        anchor = cands[i]
        # 상시어(버블, 갤벤, 멤버 이름 등)는 단독 클러스터의 중심축(Anchor)이 될 수 없도록 스킵!
        # (예: '버블 100일' 토픽이 있을 때 '버블' 단독 토픽이 또 생기는 현상 방지)
        if anchor["word"] in _BRIDGE_STOPWORDS and anchor["lift"] < 12.0:
            continue

        used.add(i)
        comp = [anchor]

        for j in range(i + 1, n):
            if j in used:
                continue
            # 중심축(anchor)과 직접 강하게 연결되어 있을 때만 편입
            if _are_words_strongly_related(anchor, cands[j]):
                comp.append(cands[j])
                used.add(j)

        # 점수/빈도 순 정렬 (단, 상시어보다 고유 토픽어가 앞으로 오도록 정렬)
        comp.sort(key=lambda x: (x["word"] not in _BRIDGE_STOPWORDS, x["doc_count"], x["score"]), reverse=True)
        union_pids = set()
        for c in comp:
            union_pids |= c["post_ids"]

        if len(union_pids) < min_cluster_posts:
            continue

        # 덩어리 내 게시글들의 단어 노트에서 함께 자주 쓰인 연관어(Co-occurring words) 추출
        core_words = {c["word"] for c in comp}
        all_variants = {v for c in comp for v in c.get("variants", ())}
        assoc_counter = Counter()
        for pid in union_pids:
            for w in post_notes.get(pid, ()):
                if w in core_words or w in all_variants:
                    continue
                if any(w.startswith(cw) or cw.startswith(w) for cw in core_words):
                    continue
                assoc_counter[w] += 1

        assoc_kws = []
        min_assoc_cnt = max(3, int(len(union_pids) * 0.16))
        for w, cnt in assoc_counter.most_common(10):
            # 클러스터 게시글의 최소 16% 이상(최소 3건)에서 함께 등장하고, 평소 대비 의미 있는 단어만 채택
            if cnt < min_assoc_cnt:
                continue
            p_t = cnt / n_target
            p_b = base_df[w] / n_base
            lift = round((p_t + 0.002) / (p_b + 0.002), 1)
            if w in _BRIDGE_STOPWORDS:
                if (cnt / len(union_pids)) < 0.35 or lift < 1.5:
                    continue
            elif lift < 2.2:
                continue
            assoc_kws.append({
                "word": w,
                "lift": lift,
                "count": cnt,
                "is_assoc": True,
            })
            if len(assoc_kws) >= 2:
                break

        # 전체 키워드 리스트 (핵심 급상승어 + 단어 노트 동반 연관어)
        keywords = [
            {"word": c["word"], "lift": c["lift"], "count": c["doc_count"]}
            for c in comp[:4]
        ] + assoc_kws

        # 이 떡밥 덩어리의 실제 발생 시작~종료 시간 계산
        cluster_times = [post_map[pid][3] for pid in union_pids if pid in post_map]
        actual_start = min(cluster_times)
        actual_end = max(cluster_times)

        # 대표 게시글 선정: 핵심어/연관어를 가장 많이 포함한 게시글 순으로 중복 제목 없이 4건 추출
        kw_set = {k["word"] for k in keywords} | all_variants
        scored_pids = sorted(
            union_pids,
            key=lambda pid: (
                len(post_notes.get(pid, set()) & kw_set),
                len((post_map[pid][1] or "").strip()),
                pid,
            ),
            reverse=True,
        )
        rep_titles = []
        seen_t = set()
        for pid in scored_pids:
            t_str = (post_map[pid][1] or "").strip()
            t_key = re.sub(r'\s+', '', t_str)
            if not t_str or t_key in seen_t:
                continue
            seen_t.add(t_key)
            ts_str = post_map[pid][3].strftime("%H:%M")
            rep_titles.append(f"• [{ts_str}] {t_str}")
            if len(rep_titles) >= 4:
                break

        core_label = " · ".join(c["word"] for c in comp[:3])
        assoc_label = " · ".join(a["word"] for a in assoc_kws[:2])
        if assoc_label:
            title = f"[{core_label}] + {assoc_label} 관련 토픽"
        else:
            title = f"[{core_label}] 급상승 토픽"

        mood = " ".join(f"#{k['word']}" for k in keywords[:4])

        clusters.append({
            "title": title,
            "summary": "\n".join(rep_titles),
            "mood": mood,
            "keywords": keywords,
            "post_count": len(union_pids),
            "sample_post_ids": sorted(union_pids, reverse=True)[:50],
            "actual_start": actual_start,
            "actual_end": actual_end,
            "score": sum(c["score"] for c in comp[:2]),
        })

    clusters.sort(key=lambda x: (x["post_count"], x["score"]), reverse=True)
    return clusters[:3]
