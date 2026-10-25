"""
topic_engine.py — LLM 없이 100% 단어 노트(Word Note), 게시글 내 연관성(Co-occurrence),
그리고 누적 데이터 기반 규칙 학습 노트(Learning Note)로 갤러리 떡밥과 시간대를 추출하는 엔진
"""
import re
import math
import html
from collections import Counter, defaultdict
from datetime import timedelta

# 체언(명사) 뒤에 붙는 조사 및 서술격 조사('이다' 활용형) 목록 (긴 것부터 매칭)
# 주의: '-면서', '-는데' 등 동사 어미를 여기서 자르면 '맞으면서' -> '맞으', '맛있었는데' -> '맛있었' 같은 동사 어간 파편이 남으므로,
# 동사/형용사 활용형은 아래 _HADA_SUFFIX_RE 와 is_korean_predicate() 에서 전담 처리한다!
_JOSA = sorted([
    "에서는", "에서도", "으로는", "이라도", "이라서", "이라고", "이라는", "이라니", "이라네", "이라던데",
    "한테서", "에게서", "이구나", "이구만", "이잖아", "이니까", "이지만", "이라며", "인가요", "인건가",
    "에서", "으로", "에게", "한테", "까지", "부터", "보다", "처럼", "마다", "이랑", "이나", "이고",
    "이면", "이야", "이네", "인데", "이지", "인가", "인지", "인듯", "이다",
    "라고", "라는", "라서", "랑은", "만큼", "밖에", "께서",
    "이가", "이는", "이도", "이를", "이의", "이만", "이랑은",
    "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "랑", "로", "와", "과", "나", "야", "아",
], key=len, reverse=True)

# [2글자 이상 명사] + [하다/되다 활용어미]에서 명사 어근만 깔끔하게 추출하기 위한 접미 패턴 (예: 취소하고 -> 취소, 공개하네 -> 공개)
_HADA_SUFFIX_RE = re.compile(
    r'(하고|하네|하죠|하지|해서|하면|할까|할게|할래|하세요|했어|했다|했네|했지|해라|해요|하는|한데|한거|하노|하누|하냐|되고|되네|돼서|되면|됐어|됐다|되는)$'
)

# 명사가 아닌 일반 동사/형용사(용언) 활용형 필터링 정규식 (예: 맞으면서, 보는데, 하네, 와서, 먹는, 아는, 맛있어, 걸리면, 해줘 등)
_PREDICATE_ENDING_RE = re.compile(
    r'(면서|는데|던데|건데|텐데|길래|잖아|네요|구나|구만|더라|던가|고싶다|고싶어|고싶네|고있어|걸리면|'
    r'하네|하고|하죠|하지|해서|하면|할까|할게|할래|하세요|했어|했다|했네|했지|해라|해요|하는|한데|'
    r'해줘|해주네|해줘라|해달라|보여줘|알려줘|살려줘|도와줘|들어줘|봐줘|'
    r'보네|가네|오네|주네|되네|크네|많네|좋네|맞네|웃기네|귀엽네|이쁘네|예쁘네|미쳤네|지리네|'
    r'와서|가서|봐서|해서|먹는|먹고|먹어|먹자|아는|알아|보는|보고|봤어|가는|오는|쓰는|듣는|'
    r'맞으|맞아|맞는|맛있어|맛있네|맛있는|맛있던|귀여워|이뻐|예뻐|웃겨|무서워|부러워|고마워|미안해|사랑해|'
    r'싶어서|싶은데|싶으니까|싶었|하니까|되니까|있으니까|없으니까|보다가|하다가|가다가|오다가|있다가|'
    r'았어|었어|였어|됐어|았네|었네|였네|됐네|'
    r'모르겠네|모르겠어|모르겠고|모르겠음)$'
)


def is_korean_predicate(w: str) -> bool:
    """
    일반 동사·형용사(서술어) 및 동사 어간 파편('맞으', '맛있었', '미쳤', '보는데', '하네', '와서' 등) 자동 판별:
    1) 한국어 명사는 절대 쌍시옷('ㅆ', 종성 번호 20: 았/었/였/했/됐/봤/왔/갔/났/졌/쳤/렸/겼/웠/있) 받침으로 끝나지 않음!
       끝글자 받침이 'ㅆ'이면 100% 과거형/형용사 용언 어간 파편이므로 즉시 제외.
    2) 동사·형용사 활용 어미 패턴(_PREDICATE_ENDING_RE)에 매칭되면 제외.
    """
    if not w:
        return False
    last_ch = w[-1]
    if '가' <= last_ch <= '힣':
        if (ord(last_ch) - 0xAC00) % 28 == 20:
            return True
    if _PREDICATE_ENDING_RE.search(w):
        return True
    return False


def is_grammatically_valid_josa(stem: str, josa: str) -> bool:
    """
    공통 규칙 1 (한국어 음운론 받침 조화 법칙):
    앞 글자(stem[-1])의 받침(종성) 유무와 뒤따르는 조사(josa)가 한국어 문법 규칙에 부합하는지 유니코드 연산으로 자동 판별.
    문법적으로 결합 불가능한 경우(예: 무받침 뒤의 '은/이/을/과/으로', 유받침 뒤의 '는/가/를/와/로') 조사가 아닌 단어 원형으로 보존!
    """
    if not stem:
        return False
    last_ch = stem[-1]
    if not ('가' <= last_ch <= '힣'):
        return True
    batchim = (ord(last_ch) - 0xAC00) % 28  # 0: 무받침, 8: ㄹ받침, 그 외: 유받침
    has_batchim = (batchim != 0)

    # 반드시 받침이 있어야만 결합 가능한 조사
    if josa in (
        "은", "이", "을", "과", "아", "으로", "으로는", "이랑", "이랑은", "이나", "이고", "이면",
        "이야", "이라도", "이라서", "이라고", "이라는", "이라니", "이라네", "이라던데", "이라며",
        "이가", "이는", "이도", "이를", "이의", "이만", "이구나", "이구만", "이잖아", "이니까", "이지만",
    ):
        return has_batchim
    # 반드시 받침이 없어야만 결합 가능한 조사
    if josa in ("는", "가", "를", "와", "야", "랑", "랑은", "나", "라도", "라서", "라고", "라는", "라니"):
        return not has_batchim
    # '로'는 무받침이거나 'ㄹ' 받침(batchim == 8)일 때만 결합 가능
    if josa in ("로",):
        return (not has_batchim) or (batchim == 8)
    return True

# 조사 제거 시 끝글자('은/는/이/가/로/아/야/나')가 잘리면 안 되는 고유명사/멤버명 보호 목록
_NO_JOSA_STRIP = {
    "진경은", "경은", "원이", "정원이", "미나미", "메이", "제나", "백화요란", "동아대", "마산대",
    "떡볶이", "고양이", "강아지", "어린이", "호랑이", "원숭이", "거북이", "부엉이", "멍멍이", "냥냥이",
    "목걸이", "귀걸이", "옷걸이", "손잡이", "길잡이", "바람막이", "일요일", "월요일", "화요일", "수요일", "목요일", "금요일", "토요일",
    "바나나", "사우나", "안테나", "세미나", "카메라", "드라마", "고구마", "파자마", "시네마", "제주도", "강원도", "울릉도", "태권도",
}

# 명사 파생 접미사 패턴 (끝글자 '이'나 '은'이 조사가 아니라 단어의 일부인 경우: *경은, *둥이, *쟁이, *볶이, *걸이 등)
_NOUN_ENDING_KEEP_RE = re.compile(
    r'(경은|둥이|쟁이|뱅이|팽이|볶이|걸이|잡이|막이|받이|구이|무침|놀이|맞이|팔이|살이|보기가|바라기)$'
)

# 디시/일상 범용 기능어 (의미 없는 부사·대명사·상투어·일상 용언)
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
    "새로고침", "머야", "오늘도", "앞에서", "있는데", "어제자", "지금까지", "하는거",
    "뭔가", "좋아", "좋겠다", "좋겠네", "좋아하는", "나오네", "나온다", "하죠", "같아", "같은", "그럴", "그런", "이런", "저런",
    "사람들", "생각보다", "어쩌다", "그렇게", "이렇게", "저렇게", "많네", "맞지", "아닌가", "있나", "없나", "보네",
    "내가", "니가", "네가", "나는", "너는", "우린", "걔네", "얘네", "쟤네", "누구", "어디", "언제", "무엇",
    # 일상 호칭/대명사/지칭어
    "얘들", "얘들아", "애들아", "님들", "님들아", "형들", "형들아", "너네", "너희", "너희들", "지들",
    # 일상 부사/시간 표현/접속 부사 (새벽 벽갤 잡담 단골어)
    "한번", "한번씩", "요즘", "요즘들어", "요새", "이따", "이따가", "잠깐", "잠깐만",
    "어쩐지", "어쩌면", "어차피", "암튼", "하여튼", "어쨌든", "그냥저냥", "마침", "별로", "아무튼",
    "그나마", "오히려", "당연히", "가끔", "스스로", "혼자", "같이", "함께",
    # 일상 추상 감정/상태 명사
    "마음", "기분", "취향", "상태", "자리", "순간",
    # 구어체 용언/서술 파편
    "싶다", "싶어", "싶어서", "싶은", "싶네", "싶음", "싶은데",
    "하니까", "되니까", "있으니까", "없으니까", "좋으니까",
    "보다가", "하다가", "가다가", "오다가", "있다가",
    "모르겠", "모르겠음", "모르겠어", "모르겠네", "모르겠고", "몰라", "모름",
    "나랑", "너랑", "나한테", "너한테",
}

# 영어 기초 불용어 (영문 가사/슬로건 문구가 전치사·대명사로 쪼개져 토픽화되는 현상 방지)
_EN_STOPWORDS = {
    "with", "us", "for", "all", "the", "and", "to", "in", "of", "is", "are", "on", "at", "by",
    "my", "you", "we", "it", "be", "this", "that", "from", "but", "not", "so", "if", "or", "as",
    "do", "no", "yes", "can", "will", "just", "like", "your", "me", "our", "out", "up", "about",
    "what", "who", "how", "when", "where", "why", "stay", "thing", "dc", "official", "app", "ver",
}

# 갤러리 상시 소재 (멤버명/버블/갤벤 등): 단독이나 보조 연관어는 될 수 있어도, 서로 다른 두 떡밥을 이어주는 '다리(Bridge)'가 되면 안 되는 단어들
_BRIDGE_STOPWORDS = {
    "버블", "갤벤", "겔벤", "갸루벤", "공지", "끌올", "당발", "당첨",
    "원이", "대장", "리브", "리부", "경은", "진경은", "미나미", "미남", "메이", "예빈", "이예빈", "제나", "까엉", "가영",
}

# 접미 합성어 병합 시 너무 흔해서 서로 다른 뜻의 단어를 잘못 묶을 수 있는 일반 명사 접미어 제외 목록
_GENERIC_SUFFIXES = {
    "사람", "생각", "느낌", "시간", "오늘", "내일", "어제", "정도", "부분", "문제", "이유", "기분",
    "사진", "영상", "노래", "무대", "추천", "이야기", "얘기", "말고", "때문", "소리", "모습", "마음",
    "유저", "팬들", "분들", "애들", "형들", "누나", "언니", "오빠", "친구", "동생",
}

_HTML_TAG_RE = re.compile(r'<[^>]+>')
_URL_RE = re.compile(r'https?://\S+|www\.\S+|\S+\.(com|net|org|kr|be|me|io|ly)\S*', re.I)
# 디시 금칙어 우회용 글자 사이 점/슬래시/쉼표 결합 (우.리.은.행 -> 우리은행, 서.수 -> 서수)
_INTER_PUNCT_RE = re.compile(r'(?<=[가-힣0-9])[.,/·•_~\-]+(?=[가-힣])')
# 어절 내부에 2개 이상 연속된 ㅋㅋ/ㅎㅎ/ㅠㅠ가 끼어 단어가 붙은 경우(예: 엌ㅋㅋㅋㅋ메이김밥) 공백으로 분리
_INTERNAL_JAMO_SPLIT_RE = re.compile(r'[ㄱ-ㅎㅏ-ㅣ]{2,}')
# 어절 앞뒤에 붙은 자모/웃음(예: 예빈김밥ㅋㅋㅋㅋㅋ, 157김밥ㅌㅋㅋㅋㅋㅋ) 제거용
_EDGE_JAMO_RE = re.compile(r'^[ㄱ-ㅎㅏ-ㅣ]+|[ㅌㅋㅎㅠㅜㅡ]*[ㄱ-ㅎㅏ-ㅣ]+$')
_CLEAN_RE = re.compile(r'[^0-9A-Za-z가-힣ㄱ-ㅎ\s]')

# 기본 투표 독려 꼬리말 / 앱 서명 마스킹 정규식
_BUILTIN_FOOTER_RE = re.compile(
    r'(카뱅\s*계좌\s*또는\s*mini가\s*있으신\s*분들은[^\n]*|카카오뱅크\s*앱에서\s*매일\s*1회[^\n]*|투표\s*기간\s*동안\s*매일\s*정성으로[^\n]*|모두의\s*스타상[^\n]*투표\s*부탁[^\n]*|-\s*dc\s*official\s*App)',
    re.I
)

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


_CASE_MARKERS = ("은", "는", "이", "가", "을", "를", "도", "에", "의", "만", "로", "랑", "과", "와", "한테", "에게", "부터", "까지")
_AMBIGUOUS_1CHAR_JOSA = {"은", "는", "이", "가", "도", "만", "로", "나", "야", "아"}


def detect_corpus_3char_nouns(posts) -> set:
    """
    공통 규칙 2 (말뭉치 문맥 어근 검증 법칙 — 사전 하드코딩 없이 3글자 명사 자동 판별):
    3글자 어절(ABC)의 마지막 글자(C)가 1글자 조사('은/는/이/가/도/만/로/나/야/아')와 모양이 같을 때,
    전체 게시글 모음에서:
      - 2글자 어근(AB)이 이미 상시어/멤버명(_BRIDGE_STOPWORDS)이거나 일반 기능어(_GENERIC)면 제외하고,
      - 2글자 어근(AB)이 단독이나 다른 조사(AB+을/를/에/도 등)와 결합해 쓰인 근거(ab_evidence)가 0~1건뿐이면서
        3글자 형태(ABC + ABC조사)가 3건 이상 등장할 때만 온전한 3글자 명사로 자동 보호!
    """
    raw_counts = Counter()
    for p in posts:
        text = f"{p[1] or ''} {(p[2] or '')[:100] if p[2] and p[2] != '__EMPTY__' else ''}"
        text = _INTER_PUNCT_RE.sub("", text)
        text = _INTERNAL_JAMO_SPLIT_RE.sub(" ", text)
        for tok in re.findall(r'[가-힣]{2,8}', text):
            raw_counts[tok] += 1

    protected_3char = set()
    for w3, abc_cnt in raw_counts.items():
        if len(w3) != 3 or abc_cnt < 2:
            continue
        last1 = w3[2]
        if last1 not in _AMBIGUOUS_1CHAR_JOSA:
            continue
        stem2 = w3[:2]
        # 2글자 어근이 이미 멤버명(경은, 예빈, 까엉, 미남 등)이나 범용어면 '예빈이 -> 예빈'처럼 조사를 정상 분리해야 함!
        if stem2 in _BRIDGE_STOPWORDS or stem2 in _GENERIC:
            continue
        abc_plus_josa = sum(raw_counts[w3 + cm] for cm in _CASE_MARKERS)
        ab_standalone = raw_counts[stem2]
        ab_other_josa = sum(raw_counts[stem2 + cm] for cm in _CASE_MARKERS if cm != last1)
        ab_evidence = ab_standalone + ab_other_josa

        if (abc_cnt + abc_plus_josa) >= 3 and ab_evidence <= 1:
            protected_3char.add(w3)

    return protected_3char


def normalize_word(w, extra_stopwords=None, protected_words=None, corpus_3char_nouns=None):
    w = w.strip()
    if len(w) < 2:
        return ""
    # 영어 대소문자 구분 없이 소문자로 통일 (mvp, MVP, Mvp 등 화력 통합)
    if re.search(r'[A-Za-z]', w):
        w = w.lower()

    # 1. 어절 양끝의 ㅋㅋ/ㅎㅎ/ㅠㅠ/ㅌ 자모 선제거 (예: '예빈이김밥은ㅋㅋㅋㅋ' -> '예빈이김밥은')
    if re.search(r'[가-힣]', w):
        w = _EDGE_JAMO_RE.sub("", w)
    if len(w) < 2:
        return ""
    # '진경아', '진경이' 등 성+이름 축약 호칭은 본명 '진경은'으로 복원
    if w in ("진경", "진경아", "진경이", "진경이는", "진경이가", "진경이도", "진경이를"):
        return "진경은"

    # 보호 단어(화이트리스트)에 지정된 단어는 불용어 필터를 건너뛰고 즉시 통과
    protected_lower = {p.lower() for p in protected_words} if protected_words else None
    if protected_lower and w in protected_lower:
        return w

    # 2. [2글자 이상 명사] + [하다/되다 어미] (예: '취소하고' -> '취소', '공개하네' -> '공개') 명사 어근 복원
    m_hada = _HADA_SUFFIX_RE.search(w)
    if m_hada and len(w) - len(m_hada.group(0)) >= 2:
        w = w[: -len(m_hada.group(0))]

    # 3. 일반 동사·형용사 활용형('맞으면서', '보는데', '하네', '와서', '먹는', '걸리면', '맛있어' 등) 선제거
    if is_korean_predicate(w):
        return ""

    # 4. 체언 조사 제거 (음운 받침 조화 법칙 + 말뭉치 문맥 3글자 명사 자동 보호)
    is_protected_noun = (
        w in _NO_JOSA_STRIP
        or (corpus_3char_nouns and w in corpus_3char_nouns)
        or bool(_NOUN_ENDING_KEEP_RE.search(w))
    )
    if not is_protected_noun:
        for j in _JOSA:
            if w.endswith(j) and len(w) - len(j) >= 2:
                stem = w[: -len(j)]
                # 한국어 받침(종성) 음운 규칙에 맞지 않는 조사 결합이면 자르지 않음!
                if not is_grammatically_valid_josa(stem, j):
                    continue
                w = stem
                break
    # 복수형 접미사 '-들'(3글자 이상: 가사들 -> 가사, 노래들 -> 노래) 제거
    if len(w) >= 3 and w.endswith("들") and re.search(r'[가-힣]', w[:-1]):
        w = w[:-1]
    # 조사 제거 후 남은 끝자락 자모 및 용언 파편('맞으', '맛있었', '미쳤' 등) 한 번 더 정리
    if re.search(r'[가-힣]', w):
        w = _EDGE_JAMO_RE.sub("", w)
    if len(w) < 2 or is_korean_predicate(w):
        return ""

    if w in _GENERIC or w in _EN_STOPWORDS:
        return ""
    extra_stops_lower = {s.lower() for s in extra_stopwords} if extra_stopwords else None
    if extra_stops_lower and w in extra_stops_lower:
        return ""
    if re.fullmatch(r'[0-9]+', w):
        return ""
    if (w.endswith("시") or w.endswith("씨")) and len(w) <= 8:
        return ""
    if re.match(r'^(총공|스밍|투표|출석|음총|댓총|센모닝|센나잇|생일시|멤버시|ㄱㅂㅇㅈ|ㅌㅍㅇㅈ)', w):
        return ""
    if re.fullmatch(r'[ㄱ-ㅎㅏ-ㅣ]+', w):
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


def strip_learned_signatures(content, learned_signatures=None):
    """
    본문 앞 80자를 자르기 전에 HTML 태그·URL·투표 독려 꼬리말·복붙 서명문을 먼저 제거!
    (URL이 80자 경계에서 잘려 'https'가 단어로 남거나, 긴 링크가 본문 글자 수를 잡아먹는 버그 원천 차단)
    """
    if not content or content == "__EMPTY__":
        return ""
    text = _HTML_TAG_RE.sub(" ", content)
    text = html.unescape(text)
    text = _BUILTIN_FOOTER_RE.sub(" ", text)
    if learned_signatures:
        for sig in learned_signatures:
            if sig and len(sig) >= 8 and sig in text:
                text = text.replace(sig, " ")
    text = _URL_RE.sub(" ", text)
    return re.sub(r'\s+', ' ', text).strip()


def extract_words(title, content="", learned_rules=None, corpus_3char_nouns=None):
    """게시글 1개의 [단어 노트] 생성: 복붙 꼬리말·URL 선제거 + 점 찍힌 단어 복원 + 자모 분리 + 조사 제거"""
    sigs = learned_rules.get("signatures") if learned_rules else None
    extra_stops = learned_rules.get("stopwords") if learned_rules else None
    protected = learned_rules.get("protected") if learned_rules else None

    cleaned_body = strip_learned_signatures(content, sigs)
    clean_content = cleaned_body[:80] if cleaned_body else ""
    clean_title = _URL_RE.sub(" ", title or "")
    text = f"{clean_title} {clean_content}"
    text = _INTER_PUNCT_RE.sub("", text)
    text = _INTERNAL_JAMO_SPLIT_RE.sub(" ", text)
    text = _CLEAN_RE.sub(" ", text)
    words = set()
    for raw in text.split():
        w = normalize_word(
            raw,
            extra_stopwords=extra_stops,
            protected_words=protected,
            corpus_3char_nouns=corpus_3char_nouns,
        )
        if w:
            words.add(w)
    return words


def merge_word_families(cands):
    """
    접두사(Prefix) 및 접미사(Suffix) 합성어 패밀리 통합:
    1) 접두사 포함관계: '검색'(6) + '검색기록'(21) → '검색기록'(27)
    2) 접미사 합성어 포함관계: '김밥'(5) + '예빈이김밥'(6) + '예빈김밥'(5) + '메이김밥'(4) + '157김밥'(4)
       → '김밥' 패밀리로 게시글·작성자 풀을 통합하고, 세부 변형어 빈도(variant_counts)를 보존하여
       토픽 제목에 [김밥 · 예빈이김밥 · 메이김밥] 형태로 풍부하게 노출!
    """
    # 고유 토픽어(비-상시어)가 멤버 이름(_BRIDGE_STOPWORDS)보다 먼저 합성어를 흡수하도록 정렬
    words = sorted(cands.keys(), key=lambda w: (len(w), w in _BRIDGE_STOPWORDS, w))
    absorbed = set()

    def _absorb(keeper, other):
        cands[keeper]["post_ids"] |= cands[other]["post_ids"]
        cands[keeper]["authors"] |= cands[other]["authors"]
        for u, cnt in cands[other]["author_counts"].items():
            cands[keeper]["author_counts"][u] += cnt
        cands[keeper]["title_hits"] += cands[other]["title_hits"]
        cands[keeper]["titles"].extend(cands[other]["titles"])
        cands[keeper]["variants"] |= cands[other]["variants"] | {other}
        for vw, vcnt in cands[other]["variant_counts"].items():
            cands[keeper]["variant_counts"][vw] += vcnt
        cands[keeper]["base_cnt"] = max(cands[keeper]["base_cnt"], cands[other]["base_cnt"])
        absorbed.add(other)

    # 1차: 짧은 어근 단어(short, 2글자 이상)가 존재할 때 접미(endswith) 또는 접두(startswith) 합성어 통합
    for i, short in enumerate(words):
        if short in absorbed or short not in cands:
            continue
        if len(short) < 2 or not re.search(r'[가-힣]', short):
            continue
        for long in words[i + 1:]:
            if long in absorbed or long not in cands:
                continue
            # 멤버 이름(예빈, 메이 등)이 4글자 이상 합성 명사(예빈김밥, 메이김밥)를 가로채지 않도록 방지
            is_prefix = long.startswith(short) and not (short in _BRIDGE_STOPWORDS and len(long) >= 4)
            is_suffix = (
                long.endswith(short)
                and len(short) >= 2
                and short not in _GENERIC_SUFFIXES
                and short not in _BRIDGE_STOPWORDS
            )
            if is_prefix or is_suffix:
                if is_suffix and not is_prefix:
                    keeper, other = short, long
                else:
                    # 2글자 축약어(short)와 3글자 이상 온전한 단어(long)가 접두 관계일 때,
                    # 3글자 이상 단어가 최소 3건 이상(또는 2글자 단어의 40% 이상) 쓰였다면 잘린 2글자 대신 온전한 3글자+ 단어를 대표어로 채택!
                    long_cnt = len(cands[long]["post_ids"])
                    short_cnt = len(cands[short]["post_ids"])
                    if len(short) == 2 and len(long) >= 3 and (long_cnt >= short_cnt * 0.45 or long_cnt >= 4):
                        keeper, other = long, short
                    elif long_cnt >= short_cnt:
                        keeper, other = long, short
                    else:
                        keeper, other = short, long
                _absorb(keeper, other)
                if other == short:
                    break

    # 2차: 단독 짧은 어근이 없더라도, 3개 이상의 합성어가 동일한 2글자 이상 한글 접미사를 공유하는 경우 (예: 예빈김밥, 메이김밥, 157김밥)
    remaining = [w for w in cands.keys() if w not in absorbed and len(w) >= 3 and re.search(r'[가-힣]{2}$', w)]
    suffix_groups = defaultdict(list)
    for w in remaining:
        suf = w[-2:]
        if suf not in _GENERIC_SUFFIXES and suf not in _BRIDGE_STOPWORDS and suf not in _GENERIC:
            suffix_groups[suf].append(w)

    for suf, group in suffix_groups.items():
        if len(group) >= 3:
            group.sort(key=lambda w: len(cands[w]["post_ids"]), reverse=True)
            keeper = group[0]
            for other in group[1:]:
                if other not in absorbed and other in cands:
                    _absorb(keeper, other)

    for w in absorbed:
        cands.pop(w, None)
    return cands


def _are_words_strongly_related(c1, c2):
    """
    두 급상승 단어가 '정말로 같은 토픽'인지 판정:
    1) 글자 뿌리 / 은어 변형
    2) 높은 게시글 동시출현 비율
    """
    w1, w2 = c1["word"], c2["word"]
    pids1, pids2 = c1["post_ids"], c2["post_ids"]
    inter = len(pids1 & pids2)
    min_len = max(1, min(len(pids1), len(pids2)))
    union_len = max(1, len(pids1 | pids2))

    short, long = (w1, w2) if len(w1) <= len(w2) else (w2, w1)
    if len(short) >= 2 and (long.startswith(short) or long.endswith(short)):
        return True
    if len(w1) == len(w2) and len(w1) >= 4 and sum(a != b for a, b in zip(w1, w2)) == 1:
        return True
    if len(w1) >= 3 and len(w2) >= 3 and w1[-2:] == w2[-2:] and inter >= 1:
        return True
    if inter >= 1 and (short[0] in long or short[-1] in long):
        return True

    if (w1 in _BRIDGE_STOPWORDS) != (w2 in _BRIDGE_STOPWORDS):
        if (inter / min_len) < 0.55 or inter < 4:
            return False

    if inter >= 3 and ((inter / min_len) >= 0.30 or (inter / union_len) >= 0.18):
        return True

    return False


def find_burst_candidates(target_posts, baseline_posts, baseline_hours=24, target_hours=1,
                          routine_days=None, min_posts=5, max_words=15, learned_rules=None):
    """
    1단계: 각 게시글의 단어 노트를 만들고 평소(24h) 대비 급상승한 단어 후보들을 추출
    - 접두사/접미사 합성어 패밀리 통합을 먼저 수행하여 파편화된 신조어(예: 예빈김밥/메이김밥/157김밥) 누락 방지
    - 학습 노트(learned_rules)의 상시 반복어(routine_words)는 일반 변동 시 제외하되, 6.5배 이상 대폭발 시 안전장치로 통과!
    """
    post_map = {}
    post_notes = {}
    target_word_posts = defaultdict(set)
    target_word_title_hits = Counter()
    target_word_titles = defaultdict(list)
    target_word_authors = defaultdict(set)
    target_word_author_counts = defaultdict(Counter)

    routine_words = (learned_rules or {}).get("routine_words", set())
    protected_words = (learned_rules or {}).get("protected", set())

    # 말뭉치 문맥 기반 3글자 고유 명사 자동 감지 (24시간 전체 말뭉치 기준으로 판별하여 단편 구간 오판 방지)
    corpus_3char_nouns = detect_corpus_3char_nouns(list(target_posts) + list(baseline_posts[:3000]))

    for p in target_posts:
        pid, title, content, created_at = p[0], p[1], p[2], p[3]
        author = (p[4] or "").strip() if len(p) > 4 else ""
        if is_ritual_title(title):
            continue
        post_map[pid] = p
        title_note = extract_words(title, "", learned_rules=learned_rules, corpus_3char_nouns=corpus_3char_nouns)
        full_note = extract_words(title, content, learned_rules=learned_rules, corpus_3char_nouns=corpus_3char_nouns)
        post_notes[pid] = full_note
        t_clean = re.sub(r'\s+', '', title or '')

        for w in full_note:
            if author and not author.startswith("anon_"):
                if target_word_author_counts[w][author] >= 2:
                    continue
                target_word_author_counts[w][author] += 1
                target_word_authors[w].add(author)

            target_word_posts[w].add(pid)
            target_word_titles[w].append(t_clean)
            if w in title_note:
                target_word_title_hits[w] += 1

    base_df = Counter()
    for p in baseline_posts:
        if is_ritual_title(p[1]):
            continue
        for w in extract_words(p[1], p[2], learned_rules=learned_rules, corpus_3char_nouns=corpus_3char_nouns):
            base_df[w] += 1

    n_target = max(1, len(post_map))
    n_base = max(1, sum(1 for p in baseline_posts if not is_ritual_title(p[1])))

    # 합성어 파편화 방지를 위해 2건 이상 등장한 단어들을 후보로 올린 뒤 패밀리 병합 수행
    raw_cands = {}
    for w, pids in target_word_posts.items():
        df = len(pids)
        if df < 2:
            continue
        raw_cands[w] = {
            "word": w,
            "post_ids": set(pids),
            "authors": set(target_word_authors.get(w, set())),
            "author_counts": Counter(target_word_author_counts[w]),
            "title_hits": target_word_title_hits[w],
            "titles": list(target_word_titles[w]),
            "variants": set(),
            "variant_counts": Counter({w: df}),
            "base_cnt": base_df[w],
        }

    cands = merge_word_families(raw_cands)

    final_cands = []
    for w, c in cands.items():
        pids = c["post_ids"]
        df = len(pids)
        if df < min_posts:
            continue
        # 실제 떡밥은 글 제목으로 올라와야 함 (제목 언급 최소 3건 이상 및 제목 언급 비율 40% 이상, 단 6건 이상 대규모 제목 언급 시 예외)
        title_ratio = c["title_hits"] / max(1, df)
        if c["title_hits"] < 3 or (c["title_hits"] < 6 and title_ratio < 0.40):
            continue
        titles = c["titles"]
        diversity = len(set(titles)) / max(1, len(titles))
        if diversity < 0.55:
            continue
        authors = c["authors"]
        if authors:
            if len(authors) < 4:
                continue
            top_author_cnt = c["author_counts"].most_common(1)[0][1]
            if (top_author_cnt / df) > 0.45:
                continue

        # 패밀리 내 변형어들의 baseline 합산 (과대 추정 방지용 상한 적용)
        family_words = {w} | c["variants"]
        base_cnt = min(sum(base_df[fw] for fw in family_words), max(c["base_cnt"], int(sum(base_df[fw] for fw in family_words) * 0.75)))

        p_t = df / n_target
        p_b = base_cnt / n_base
        lift = (p_t + 0.002) / (p_b + 0.002)
        expected = p_b * n_target
        z = (df - expected) / math.sqrt(max(1.0, expected))

        # 학습 노트의 24시간 상시 반복어(routine_words) 필터링 (단, 6.5배 이상 대폭발 시 안전장치로 허용!)
        if w in routine_words and w not in protected_words:
            if lift < 6.5 or z < 5.0:
                continue

        if routine_days and routine_days.get(w, 0) >= 4 and lift < 6.0:
            continue
        if lift < 2.8 or z < 3.2:
            continue

        c["doc_count"] = df
        c["lift"] = round(lift, 1)
        c["z"] = round(z, 1)
        c["score"] = round(z * math.log2(lift + 1), 2)
        final_cands.append(c)

    ranked = sorted(
        final_cands,
        key=lambda x: (x["word"] not in _BRIDGE_STOPWORDS, x["score"]),
        reverse=True,
    )[:max(max_words, 20)]
    return ranked, post_map, post_notes, base_df, n_target, n_base


def restore_canonical_casing(word: str, titles: list) -> str:
    """영어 단어의 경우 게시글 제목들에서 가장 많이 쓰인 대소문자 형태(MVP, mvp, iPhone 등) 복원"""
    if not word or not re.search(r'[A-Za-z]', word):
        return word
    pattern = re.compile(r'(?<![A-Za-z0-9])' + re.escape(word) + r'(?![A-Za-z0-9])', re.I)
    counter = Counter()
    for t in titles:
        for m in pattern.finditer(t or ""):
            counter[m.group(0)] += 1
    if counter:
        return counter.most_common(1)[0][0]
    sub_pat = re.compile(re.escape(word), re.I)
    for t in titles:
        for m in sub_pat.finditer(t or ""):
            counter[m.group(0)] += 1
    if counter:
        return counter.most_common(1)[0][0]
    if len(word) <= 4 and re.fullmatch(r'[A-Za-z]+', word):
        return word.upper()
    return word


def _build_cluster_representation(comp, union_pids, assoc_kws, post_map, post_notes):
    """클러스터의 대표 키워드(합성어 인기 변형 포함), 대표 게시글 요약, 제목 문자열 생성"""
    titles_pool = [post_map[pid][1] for pid in union_pids if pid in post_map]

    # 핵심어 및 패밀리 내 주요 변형어(3건 이상 등장한 구체 합성어: 예빈이김밥, 메이김밥 등) 펼치기
    display_core = []
    seen_words = set()
    for c in comp:
        # 패밀리 변형어 중 어근과 2글자 이상 차이 나는 구체적 합성어(예: 김밥 -> 예빈이김밥, 메이김밥, 157김밥)만 함께 노출
        # (1글자 조사/어미 파생어인 '가사인' 등이 '가사' 옆에 중복 노출되는 현상 방지)
        compound_vars = [
            vw for vw, vcnt in c.get("variant_counts", Counter()).most_common(4)
            if vw != c["word"] and vcnt >= 3 and abs(len(vw) - len(c["word"])) >= 2
        ]
        top_vars = [c["word"]] + compound_vars[:2]

        for tw in top_vars:
            tw_display = restore_canonical_casing(tw, titles_pool)
            if tw_display not in seen_words:
                seen_words.add(tw_display)
                v_cnt = c.get("variant_counts", {}).get(tw, c["doc_count"])
                display_core.append({
                    "word": tw_display,
                    "lift": c["lift"],
                    "count": v_cnt if len(top_vars) > 1 and tw != c["word"] else c["doc_count"],
                })

    for a in assoc_kws:
        a["word"] = restore_canonical_casing(a["word"], titles_pool)

    keywords = display_core[:4] + assoc_kws[:2]
    all_variants = {v for c in comp for v in c.get("variants", ())} | {c["word"] for c in comp}

    cluster_times = [post_map[pid][3] for pid in union_pids if pid in post_map]
    actual_start = min(cluster_times)
    actual_end = max(cluster_times)

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

    core_label = " · ".join(k["word"] for k in display_core[:3])
    assoc_label = " · ".join(a["word"] for a in assoc_kws[:2] if a["word"] not in seen_words)
    if assoc_label:
        title = f"[{core_label}] + {assoc_label} 관련 토픽"
    else:
        title = f"[{core_label}] 급상승 토픽"

    mood = " ".join(f"#{k['word']}" for k in keywords[:4])
    return {
        "title": title,
        "summary": "\n".join(rep_titles),
        "mood": mood,
        "keywords": keywords,
        "post_count": len(union_pids),
        "sample_post_ids": sorted(union_pids, reverse=True)[:50],
        "actual_start": actual_start,
        "actual_end": actual_end,
        "score": sum(c["score"] for c in comp[:2]),
        "_comp": comp,
        "_union_pids": union_pids,
        "_assoc_kws": assoc_kws,
        "_rep_titles": set(rep_titles),
    }


def consolidate_overlapping_clusters(clusters, post_map, post_notes):
    """
    동시간대 중복/파편화 클러스터 병합:
    - 같은 시간대 윈도우에서 게시글 풀이 22% 이상 겹치거나,
    - 대표 요약 게시글(rep_titles)이 1건이라도 겹치거나,
    - 핵심/연관 키워드가 서로 겹치는 하위 클러스터를 상위 클러스터로 흡수!
    """
    if len(clusters) <= 1:
        return clusters

    clusters.sort(key=lambda x: (x["post_count"], x["score"]), reverse=True)
    merged_flags = set()
    result = []

    for i in range(len(clusters)):
        if i in merged_flags:
            continue
        main_c = clusters[i]
        comp = list(main_c["_comp"])
        union_pids = set(main_c["_union_pids"])
        assoc_kws = list(main_c["_assoc_kws"])
        rep_set = set(main_c["_rep_titles"])
        kw_words = {k["word"] for k in main_c["keywords"]}

        for j in range(i + 1, len(clusters)):
            if j in merged_flags:
                continue
            sub_c = clusters[j]
            sub_pids = sub_c["_union_pids"]
            inter_pids = len(union_pids & sub_pids)
            min_pids = max(1, min(len(union_pids), len(sub_pids)))
            sub_kw_words = {k["word"] for k in sub_c["keywords"]}

            overlap_ratio = inter_pids / min_pids
            shares_summary = bool(rep_set & sub_c["_rep_titles"])
            shares_keyword = bool(kw_words & sub_kw_words)

            if overlap_ratio >= 0.22 or shares_summary or shares_keyword:
                # 하위 클러스터의 핵심어를 메인 클러스터에 편입
                comp.extend(sub_c["_comp"])
                union_pids |= sub_pids
                for ak in sub_c["_assoc_kws"]:
                    if ak["word"] not in kw_words:
                        assoc_kws.append(ak)
                rep_set |= sub_c["_rep_titles"]
                kw_words |= sub_kw_words
                merged_flags.add(j)

        rebuilt = _build_cluster_representation(comp, union_pids, assoc_kws, post_map, post_notes)
        # 내부 임시 키 제거
        for k in ("_comp", "_union_pids", "_assoc_kws", "_rep_titles"):
            rebuilt.pop(k, None)
        result.append(rebuilt)

    return result


def build_word_note_clusters(target_posts, baseline_posts, baseline_hours=24, target_hours=1.5,
                             min_posts=5, min_cluster_posts=7, learned_rules=None):
    """
    2단계 (메인 엔진 — LLM 무사용):
    급상승 단어 후보들을 연관성 고리로 묶어 '떡밥 단어 덩어리(Cluster)'를 만들고,
    동시간대 중복 클러스터까지 깔끔하게 병합한다.
    """
    cands, post_map, post_notes, base_df, n_target, n_base = find_burst_candidates(
        target_posts, baseline_posts,
        baseline_hours=baseline_hours, target_hours=target_hours,
        min_posts=min_posts, max_words=15, learned_rules=learned_rules,
    )
    if not cands:
        return []

    routine_words = (learned_rules or {}).get("routine_words", set())
    protected_words = (learned_rules or {}).get("protected", set())

    cands.sort(key=lambda x: (x["word"] not in _BRIDGE_STOPWORDS, x["score"], x["doc_count"]), reverse=True)
    n = len(cands)
    used = set()
    raw_clusters = []

    for i in range(n):
        if i in used:
            continue
        anchor = cands[i]
        if anchor["word"] in _BRIDGE_STOPWORDS and anchor["lift"] < 12.0:
            continue

        used.add(i)
        comp = [anchor]

        for j in range(i + 1, n):
            if j in used:
                continue
            if _are_words_strongly_related(anchor, cands[j]):
                comp.append(cands[j])
                used.add(j)

        comp.sort(key=lambda x: (x["word"] not in _BRIDGE_STOPWORDS, x["doc_count"], x["score"]), reverse=True)
        union_pids = set()
        union_authors = set()
        for c in comp:
            union_pids |= c["post_ids"]
            union_authors |= c.get("authors", set())

        if len(union_pids) < min_cluster_posts:
            continue
        if union_authors and len(union_authors) < 4:
            continue

        core_words = {c["word"] for c in comp}
        all_variants = {v for c in comp for v in c.get("variants", ())}
        assoc_counter = Counter()
        assoc_author_cnt = defaultdict(Counter)
        for pid in union_pids:
            p_author = (post_map[pid][4] or "").strip() if (pid in post_map and len(post_map[pid]) > 4) else ""
            for w in post_notes.get(pid, ()):
                if w in core_words or w in all_variants:
                    continue
                if w in routine_words and w not in protected_words:
                    continue
                if any(w.startswith(cw) or cw.startswith(w) or w.endswith(cw) or cw.endswith(w) for cw in core_words):
                    continue
                if p_author and not p_author.startswith("anon_"):
                    if assoc_author_cnt[w][p_author] >= 2:
                        continue
                    assoc_author_cnt[w][p_author] += 1
                assoc_counter[w] += 1

        assoc_kws = []
        min_assoc_cnt = max(3, int(len(union_pids) * 0.16))
        for w, cnt in assoc_counter.most_common(10):
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

        # 단독 단어 클러스터(동시 출현 연관어가 전혀 없는 단독 키워드)의 엄격한 자격 검증
        # 새벽/벽갤 시간대 일상어 및 본문 파편이 단독 토픽으로 올라오는 것을 방지
        if len(comp) == 1 and not assoc_kws:
            core_cand = comp[0]
            core_title_ratio = core_cand["title_hits"] / max(1, core_cand["doc_count"])
            # 단독 키워드 토픽은 최소 4개 이상의 제목에서 직접 언급되어야 하며, 제목 비중이 50% 이상이어야 함
            if core_cand["title_hits"] < 4 or core_title_ratio < 0.50 or core_cand["lift"] < 3.2:
                continue

        raw_clusters.append(_build_cluster_representation(comp, union_pids, assoc_kws, post_map, post_notes))

    consolidated = consolidate_overlapping_clusters(raw_clusters, post_map, post_notes)
    consolidated.sort(key=lambda x: (x["post_count"], x["score"]), reverse=True)
    return consolidated[:3]


def extract_learning_candidates_from_24h(posts_24h, protected_words=None):
    """
    최근 24시간 데이터로부터 자동 학습 노트(Learning Note) 규칙 후보 추출:
    1) signature_phrase: 본문에서 8회 이상 똑같이 반복 복붙되는 12자 이상 고정 문구/꼬리말
    2) routine_word: 24시간 중 10개 이상의 시간대에 걸쳐 고르게 등장(피크 시간 비중 <= 22%)하는 일상 상시어
    3) ritual_word: 시보/출첵/인증 등 루틴성 게시글에서 60% 이상의 비율로 반복 등장하는 상투 어휘
    """
    protected = protected_words or set()
    learned_items = []

    # 1. 반복 복붙 꼬리말 / 서명문 (signature_phrase) 감지
    line_counter = Counter()
    line_authors = defaultdict(set)
    line_sample = {}

    for p in posts_24h:
        pid, title, content, created_at = p[0], p[1], p[2], p[3]
        author = (p[4] or "").strip() if len(p) > 4 else ""
        if not content or content == "__EMPTY__":
            continue
        text = re.sub(r'<br\s*/?>', '\n', content, flags=re.I)
        text = re.sub(r'<[^>]+>', '', text)
        text = html.unescape(text)
        for line in text.split('\n'):
            line_s = line.strip()
            if len(line_s) >= 12 and not line_s.startswith("http"):
                line_counter[line_s] += 1
                if author:
                    line_authors[line_s].add(author)
                if line_s not in line_sample:
                    line_sample[line_s] = f"[{created_at.strftime('%H:%M')}] {title}"

    detected_sigs = []
    for line_s, cnt in line_counter.most_common(40):
        if cnt < 8:
            break
        u_cnt = max(1, len(line_authors[line_s]))
        detected_sigs.append(line_s)
        learned_items.append({
            "pattern_type": "signature_phrase",
            "target_text": line_s,
            "hit_count": cnt,
            "hour_spread": 24,
            "author_count": u_cnt,
            "max_lift": 1.0,
            "reason": f"최근 24시간 내 동일 문구 {cnt}회 반복 복붙 감지 ({u_cnt}명 사용)",
            "sample_text": line_sample.get(line_s, ""),
        })

    # 2. 24시간 전 시간대 분산 상시어 (routine_word) 및 루틴/인증어 (ritual_word) 감지
    word_total_df = Counter()
    word_ritual_df = Counter()
    word_hour_counts = defaultdict(Counter)
    word_authors = defaultdict(set)
    word_sample = {}
    hour_author_cap = defaultdict(Counter)

    temp_rules = {"signatures": detected_sigs, "stopwords": set(), "protected": protected}
    corpus_3char_nouns = detect_corpus_3char_nouns(posts_24h)

    for p in posts_24h:
        pid, title, content, created_at = p[0], p[1], p[2], p[3]
        author = (p[4] or "").strip() if len(p) > 4 else ""
        hr = created_at.hour
        ritual_flag = is_ritual_title(title)

        words = extract_words(title, content, learned_rules=temp_rules, corpus_3char_nouns=corpus_3char_nouns)
        for w in words:
            if w in protected or w in _BRIDGE_STOPWORDS:
                continue
            # 시간당 동일 유저 최대 2회 캡핑
            if author and not author.startswith("anon_"):
                if hour_author_cap[(w, hr)][author] >= 2:
                    continue
                hour_author_cap[(w, hr)][author] += 1
                word_authors[w].add(author)

            word_total_df[w] += 1
            word_hour_counts[w][hr] += 1
            if ritual_flag:
                word_ritual_df[w] += 1
            if w not in word_sample:
                word_sample[w] = f"[{created_at.strftime('%H:%M')}] {title}"

    total_hours_present = max(1, len({p[3].hour for p in posts_24h}))
    min_spread = max(6, min(10, int(total_hours_present * 0.55)))

    for w, total_cnt in word_total_df.most_common(300):
        if total_cnt < 15:
            break
        u_cnt = len(word_authors[w])
        hr_buckets = word_hour_counts[w]
        spread = len(hr_buckets)
        peak_cnt = max(hr_buckets.values()) if hr_buckets else total_cnt
        peak_ratio = peak_cnt / max(1, total_cnt)
        ritual_cnt = word_ritual_df[w]
        ritual_ratio = ritual_cnt / max(1, total_cnt)

        # 규칙 A: 루틴/인증성 게시글에서 절반 이상 등장하는 상투어
        if ritual_cnt >= 8 and ritual_ratio >= 0.55:
            learned_items.append({
                "pattern_type": "ritual_word",
                "target_text": w,
                "hit_count": total_cnt,
                "hour_spread": spread,
                "author_count": u_cnt,
                "max_lift": round(peak_ratio * 10, 1),
                "reason": f"시보·인증·루틴성 글에서 집중 반복 ({total_cnt}건 중 {int(ritual_ratio * 100)}%가 루틴 문맥)",
                "sample_text": word_sample.get(w, ""),
            })
            continue

        # 규칙 B: 특정 시간 피크 없이 하루 종일(min_spread개 이상 시간대) 평평하게 깔리는 일상 상시어
        if total_cnt >= 22 and spread >= min_spread and peak_ratio <= 0.22:
            learned_items.append({
                "pattern_type": "routine_word",
                "target_text": w,
                "hit_count": total_cnt,
                "hour_spread": spread,
                "author_count": u_cnt,
                "max_lift": round(peak_ratio * 10, 1),
                "reason": f"24시간 중 {spread}개 시간대에 고르게 분산 등장 (총 {total_cnt}건, 피크 시간 비중 {int(peak_ratio * 100)}%로 일상 상시어 판정 · 단 6.5x 폭발 시 예외 통과)",
                "sample_text": word_sample.get(w, ""),
            })

    return learned_items
