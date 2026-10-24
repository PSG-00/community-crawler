import json
import time
from datetime import datetime, timedelta, timezone
from database import get_db
from topic_engine import build_word_note_clusters, extract_learning_candidates_from_24h

KST = timezone(timedelta(hours=9))


def log(tag, msg):
    now_str = datetime.now(KST).strftime('%H:%M:%S')
    print(f"[{now_str}][{tag}] {msg}", flush=True)


def get_now_kst():
    """한국 표준시(KST) 기준 naive datetime 반환 (DB 쿼리 및 타임라인 동기화용)"""
    return datetime.now(KST).replace(tzinfo=None)


class TopicAnalyzer:
    def __init__(self, gallery_id="rescene1"):
        self.gallery_id = gallery_id

    def fetch_posts_between(self, start_dt, end_dt):
        """특정 기간의 게시글 (id, title, content, created_at, author_key) 조회"""
        query = """
            SELECT post_id, title, content, created_at,
                   COALESCE(NULLIF(author_code, ''), NULLIF(nickname, ''), 'anon_' || post_id::text) AS author_key
            FROM dc_posts
            WHERE gallery_id = %s
              AND created_at >= %s
              AND created_at < %s
            ORDER BY created_at ASC
        """
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute(query, (self.gallery_id, start_dt, end_dt))
                return cur.fetchall()

    def load_learned_rules(self):
        """DB의 learning_notes 테이블에서 현재 활성화된 학습 규칙과 보호 단어(화이트리스트) 로드"""
        rules = {
            "signatures": [],
            "stopwords": set(),
            "routine_words": set(),
            "protected": set(),
        }
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT pattern_type, target_text, status
                        FROM learning_notes
                        WHERE gallery_id = %s
                    """, (self.gallery_id,))
                    for p_type, text, status in cur.fetchall():
                        if not text:
                            continue
                        if status == "protected":
                            rules["protected"].add(text)
                        elif status == "active":
                            if p_type == "signature_phrase":
                                rules["signatures"].append(text)
                            elif p_type in ("ritual_word", "manual_stop"):
                                rules["stopwords"].add(text)
                            elif p_type == "routine_word":
                                rules["routine_words"].add(text)
        except Exception as e:
            log("학습노트", f"학습 규칙 로드 오류: {e}")
        return rules

    def sync_learning_notes(self, end_dt=None):
        """최근 24시간 게시글에서 반복 복붙 꼬리말·상시 반복어·루틴어를 자동 학습하여 DB에 동기화"""
        if end_dt is None:
            end_dt = get_now_kst()
        start_dt = end_dt - timedelta(hours=24)
        posts_24h = self.fetch_posts_between(start_dt, end_dt)
        if len(posts_24h) < 30:
            return 0

        current_rules = self.load_learned_rules()
        candidates = extract_learning_candidates_from_24h(
            posts_24h, protected_words=current_rules["protected"]
        )
        if not candidates:
            return 0

        upserted = 0
        valid_keys = []
        with get_db() as conn:
            with conn.cursor() as cur:
                for item in candidates:
                    valid_keys.append((item["pattern_type"], item["target_text"]))
                    # 이미 관리자가 protected 나 disabled 로 지정한 항목은 status를 덮어쓰지 않고 통계만 갱신!
                    cur.execute("""
                        INSERT INTO learning_notes (
                            gallery_id, pattern_type, target_text, status,
                            hit_count, hour_spread, author_count, max_lift,
                            reason, sample_text, is_manual, updated_at
                        )
                        VALUES (%s, %s, %s, 'active', %s, %s, %s, %s, %s, %s, FALSE, NOW())
                        ON CONFLICT (gallery_id, pattern_type, target_text)
                        DO UPDATE SET
                            hit_count = EXCLUDED.hit_count,
                            hour_spread = EXCLUDED.hour_spread,
                            author_count = EXCLUDED.author_count,
                            max_lift = EXCLUDED.max_lift,
                            reason = CASE WHEN learning_notes.is_manual THEN learning_notes.reason ELSE EXCLUDED.reason END,
                            sample_text = EXCLUDED.sample_text,
                            updated_at = NOW();
                    """, (
                        self.gallery_id,
                        item["pattern_type"],
                        item["target_text"],
                        item["hit_count"],
                        item["hour_spread"],
                        item["author_count"],
                        item["max_lift"],
                        item["reason"],
                        item["sample_text"],
                    ))
                    upserted += 1

                # 최신 24시간 스캔에서 더 이상 조건을 충족하지 않는 오래된 자동학습(is_manual=FALSE, status='active') 잔여물 정리
                cur.execute("""
                    DELETE FROM learning_notes
                    WHERE gallery_id = %s
                      AND is_manual = FALSE
                      AND status = 'active'
                      AND updated_at < NOW() - INTERVAL '5 minutes';
                """, (self.gallery_id,))
            conn.commit()
        log("학습노트", f"📒 최근 24시간 패턴 학습 완료 ({upserted}개 항목 갱신)")
        return upserted

    def save_event(self, start_dt, end_dt, cluster_id, event_info):
        """
        gallery_events 테이블에 저장:
        - 60분 이내 인접 구간에서 동일 토픽으로 판정되는 기존 이벤트들을 모두 찾아 하나로 통합(중복 파편 카드 제거)!
        """
        kw_json = json.dumps(event_info["keywords"], ensure_ascii=False)
        core_names = [k["word"] for k in event_info["keywords"] if not k.get("is_assoc")]
        if not core_names:
            core_names = [k["word"] for k in event_info["keywords"]]
        top_word = core_names[0] if core_names else ""
        new_pids_set = set(event_info.get("sample_post_ids") or [])

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, keywords, post_count, window_start, window_end, sample_post_ids
                    FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '60 minutes'
                      AND window_start <= %s + INTERVAL '60 minutes'
                    ORDER BY post_count DESC, event_id DESC
                """, (self.gallery_id, start_dt, end_dt))
                existing = cur.fetchall()

                matched_rows = []
                for row in existing:
                    eid, old_kws, old_cnt, old_w_start, old_w_end, old_pids = row
                    if isinstance(old_kws, str):
                        try:
                            old_kws = json.loads(old_kws)
                        except Exception:
                            old_kws = []
                    old_core = [
                        (k["word"] if isinstance(k, dict) else str(k))
                        for k in old_kws
                        if not (isinstance(k, dict) and k.get("is_assoc"))
                    ]
                    if not old_core:
                        old_core = [k["word"] if isinstance(k, dict) else str(k) for k in old_kws]
                    old_top = old_core[0] if old_core else ""

                    all_new_words = {k["word"].lower() for k in event_info["keywords"]}
                    all_old_words = {(k["word"] if isinstance(k, dict) else str(k)).lower() for k in old_kws}
                    old_pids_set = set(old_pids or [])
                    min_pid_len = max(1, min(len(new_pids_set), len(old_pids_set)))
                    pid_overlap = len(new_pids_set & old_pids_set) / min_pid_len if (new_pids_set and old_pids_set) else 0.0

                    is_same_topic = False
                    top_word_l = top_word.lower() if top_word else ""
                    old_top_l = old_top.lower() if old_top else ""
                    if top_word_l and old_top_l:
                        if top_word_l == old_top_l:
                            is_same_topic = True
                        elif len(top_word_l) >= 2 and len(old_top_l) >= 2 and (top_word_l in old_top_l or old_top_l in top_word_l):
                            is_same_topic = True
                        elif (top_word_l in all_old_words) or (old_top_l in all_new_words):
                            is_same_topic = True
                        elif len({w.lower() for w in core_names[:3]} & {w.lower() for w in old_core[:3]}) >= 2:
                            is_same_topic = True
                        elif len({w.lower() for w in core_names[:3]} & {w.lower() for w in old_core[:3]}) >= 1 and pid_overlap >= 0.12:
                            is_same_topic = True
                        elif pid_overlap >= 0.25:
                            is_same_topic = True

                    if is_same_topic:
                        matched_rows.append((eid, old_cnt, old_w_start, old_w_end, old_pids_set))

                if matched_rows:
                    primary_eid, best_old_cnt, _, _, _ = matched_rows[0]
                    new_start = min([start_dt] + [r[2] for r in matched_rows])
                    new_end = max([end_dt] + [r[3] for r in matched_rows])
                    union_pids = set(new_pids_set)
                    for r in matched_rows:
                        union_pids |= r[4]
                    merged_pids = sorted(union_pids, reverse=True)[:60]
                    new_cnt = max([event_info["post_count"], len(merged_pids)] + [r[1] for r in matched_rows])

                    if event_info["post_count"] >= best_old_cnt:
                        cur.execute("""
                            UPDATE gallery_events
                            SET window_start = %s,
                                window_end = %s,
                                post_count = %s,
                                topic_title = %s,
                                topic_summary = %s,
                                mood_tag = %s,
                                keywords = %s,
                                sample_post_ids = %s
                            WHERE event_id = %s
                        """, (
                            new_start, new_end, new_cnt,
                            event_info["title"], event_info["summary"], event_info["mood"],
                            kw_json, merged_pids, primary_eid
                        ))
                    else:
                        cur.execute("""
                            UPDATE gallery_events
                            SET window_start = %s,
                                window_end = %s,
                                post_count = %s,
                                sample_post_ids = %s
                            WHERE event_id = %s
                        """, (new_start, new_end, new_cnt, merged_pids, primary_eid))

                    # 만약 기존에 둘 이상으로 나뉘어 있던 중복 토픽 카드가 함께 매칭되었다면 하나로 흡수 후 정리
                    if len(matched_rows) > 1:
                        dup_ids = [r[0] for r in matched_rows[1:]]
                        cur.execute("DELETE FROM gallery_events WHERE event_id = ANY(%s)", (dup_ids,))

                    conn.commit()
                    log("분석기", f"기존 토픽 #{primary_eid} ('{top_word}') 병합 ({new_start.strftime('%H:%M')}~{new_end.strftime('%H:%M')}, {new_cnt}건)")
                    return None

                cur.execute("""
                    INSERT INTO gallery_events (
                        gallery_id, window_start, window_end, cluster_id,
                        topic_title, topic_summary, mood_tag, keywords,
                        post_count, sample_post_ids
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING event_id;
                """, (
                    self.gallery_id,
                    start_dt,
                    end_dt,
                    cluster_id,
                    event_info["title"],
                    event_info["summary"],
                    event_info["mood"],
                    kw_json,
                    event_info["post_count"],
                    event_info["sample_post_ids"],
                ))
                new_id = cur.fetchone()[0]
            conn.commit()
        return new_id

    def analyze_window(self, end_dt=None, window_minutes=90, learned_rules=None):
        """
        LLM 무사용 100% 단어 노트 & 동시출현 연관성 클러스터 분석 (학습 노트 연동)
        """
        if end_dt is None:
            end_dt = get_now_kst()
        start_dt = end_dt - timedelta(minutes=window_minutes)
        baseline_start = start_dt - timedelta(hours=24)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        if len(target_posts) < 12:
            return []

        if learned_rules is None:
            learned_rules = self.load_learned_rules()

        baseline_posts = self.fetch_posts_between(baseline_start, start_dt)
        clusters = build_word_note_clusters(
            target_posts, baseline_posts,
            baseline_hours=24,
            target_hours=window_minutes / 60.0,
            min_posts=5,
            min_cluster_posts=7,
            learned_rules=learned_rules,
        )
        if not clusters:
            return []

        saved_events = []
        for idx, ev in enumerate(clusters, 1):
            ev_start = ev.get("actual_start") or start_dt
            ev_end = ev.get("actual_end") or end_dt
            eid = self.save_event(ev_start, ev_end, idx, ev)
            if eid:
                saved_events.append((eid, ev["title"]))
                kw_summary = [f"{k['word']} {k['lift']}x" for k in ev["keywords"][:4]]
                log("트렌드포착", f"🔥 [토픽 #{eid}] {ev['title']} ({ev_start.strftime('%H:%M')}~{ev_end.strftime('%H:%M')}, {ev['post_count']}건 / {kw_summary})")

        return saved_events

    def reset_and_regenerate_24h(self):
        """24시간 데이터로부터 학습 노트(Learning Note)를 먼저 갱신한 뒤, 최근 24시간 토픽을 초고속 재구축"""
        log("재구축", "📒 1단계: 최근 24시간 데이터 패턴 자동 학습(Learning Note) 동기화 시작...")
        try:
            self.sync_learning_notes()
        except Exception as e:
            log("학습노트오류", f"패턴 학습 중 오류: {e}")

        learned_rules = self.load_learned_rules()

        log("재구축", "🧹 2단계: 기존 트렌드 타임라인 초기화 및 최근 24시간 클러스터 재구축 시작...")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE gallery_events RESTART IDENTITY;")
            conn.commit()

        now_k = get_now_kst()
        total_created = 0

        for step in range(48, -1, -1):
            target_end = now_k - timedelta(minutes=30 * step)
            try:
                events = self.analyze_window(
                    end_dt=target_end, window_minutes=90, learned_rules=learned_rules
                )
                total_created += len(events)
            except Exception as e:
                log("재구축오류", f"{step * 30}분 전 구간 오류: {e}")

        log("재구축", f"🎉 최근 24시간 주요 급상승 토픽 총 {total_created}건 초고속 재구축 완료!")
        return total_created


def run_analyzer_loop():
    """3분 간격 실시간 단어 노트 트렌드 감지 및 주기적 학습 노트 갱신 워커"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("트렌드감지기", "🚀 단어노트 & 학습노트(Learning Note) 기반 실시간 3분 트렌드 감지 워커 시작")

    analyzer.reset_and_regenerate_24h()

    cycle = 0
    while True:
        time.sleep(180)  # 3분마다 초고속 실시간 트렌드 감지
        cycle += 1
        try:
            # 30분(10사이클)마다 최근 24시간 학습 노트 자동 업데이트
            if cycle % 10 == 0:
                analyzer.sync_learning_notes()
            analyzer.analyze_window(end_dt=get_now_kst(), window_minutes=90)
        except Exception as e:
            log("트렌드감지기", f"분석 오류: {e}")


if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
