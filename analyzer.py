import json
import time
from datetime import datetime, timedelta, timezone
from database import get_db
from topic_engine import build_word_note_clusters

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
        """특정 기간의 게시글 (id, title, content, created_at, author_code) 조회"""
        query = """
            SELECT post_id, title, content, created_at, author_code
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

    def save_event(self, start_dt, end_dt, cluster_id, event_info):
        """
        gallery_events 테이블에 저장:
        - 서로 다른 토픽이 억지로 합쳐지지 않도록 45분 이내 인접 구간이면서
          '1순위 핵심 키워드(Anchor)'가 같거나 어근이 같은 경우에만 병합!
        - 병합 시 다른 토픽 키워드가 눈덩이처럼 누적되지 않도록 피크 구간의 깔끔한 키워드 세트를 유지
        """
        kw_json = json.dumps(event_info["keywords"], ensure_ascii=False)
        core_names = [k["word"] for k in event_info["keywords"] if not k.get("is_assoc")]
        if not core_names:
            core_names = [k["word"] for k in event_info["keywords"]]
        top_word = core_names[0] if core_names else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, keywords, post_count, window_start, window_end, sample_post_ids
                    FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '45 minutes'
                      AND window_start <= %s + INTERVAL '45 minutes'
                    ORDER BY event_id DESC
                """, (self.gallery_id, start_dt, end_dt))
                existing = cur.fetchall()

                for eid, old_kws, old_cnt, old_w_start, old_w_end, old_pids in existing:
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

                    # 핵심어(Core keywords)나 주요 연관어가 겹치거나, 어근 포함관계(우이은행<->우리은행)일 때 동일 토픽으로 병합
                    all_new_words = {k["word"] for k in event_info["keywords"]}
                    all_old_words = {k["word"] if isinstance(k, dict) else str(k) for k in old_kws}
                    is_same_topic = False
                    if top_word and old_top:
                        if top_word == old_top:
                            is_same_topic = True
                        elif len(top_word) >= 2 and len(old_top) >= 2 and (top_word in old_top or old_top in top_word):
                            is_same_topic = True
                        elif (top_word in all_old_words) or (old_top in all_new_words):
                            is_same_topic = True
                        elif len(set(core_names[:3]) & set(old_core[:3])) >= 2:
                            is_same_topic = True

                    if is_same_topic:
                        new_start = min(old_w_start, start_dt)
                        new_end = max(old_w_end, end_dt)
                        merged_pids = sorted(
                            set(old_pids or []) | set(event_info["sample_post_ids"] or []),
                            reverse=True,
                        )[:60]
                        new_cnt = max(old_cnt, event_info["post_count"], len(merged_pids))

                        if event_info["post_count"] >= old_cnt:
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
                                kw_json, merged_pids, eid
                            ))
                        else:
                            cur.execute("""
                                UPDATE gallery_events
                                SET window_start = %s,
                                    window_end = %s,
                                    post_count = %s,
                                    sample_post_ids = %s
                                WHERE event_id = %s
                            """, (new_start, new_end, new_cnt, merged_pids, eid))
                        conn.commit()
                        log("분석기", f"기존 토픽 #{eid} ('{top_word}') 병합 ({new_start.strftime('%H:%M')}~{new_end.strftime('%H:%M')}, {new_cnt}건)")
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

    def analyze_window(self, end_dt=None, window_minutes=90):
        """
        LLM 무사용 100% 단어 노트 & 동시출현 연관성 클러스터 분석:
        - 90분 슬라이딩 윈도우(30분 간격 중첩)로 경계에 걸친 토픽도 놓치지 않고 포착
        - 클러스터 내 실제 첫 글~마지막 글 시각(actual_start ~ actual_end)을 이벤트 시간대로 기록
        """
        if end_dt is None:
            end_dt = get_now_kst()
        start_dt = end_dt - timedelta(minutes=window_minutes)
        baseline_start = start_dt - timedelta(hours=24)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        if len(target_posts) < 12:
            return []

        baseline_posts = self.fetch_posts_between(baseline_start, start_dt)
        clusters = build_word_note_clusters(
            target_posts, baseline_posts,
            baseline_hours=24,
            target_hours=window_minutes / 60.0,
            min_posts=5,
            min_cluster_posts=7,
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
        """기존 트렌드 초기화 후 최근 24시간을 30분 간격(90분 중첩 윈도우)으로 초고속 재구축 (LLM 무사용, ~2초 소요)"""
        log("재구축", "🧹 기존 트렌드 타임라인 초기화 및 최근 24시간 단어노트 연관성 클러스터 재구축 시작...")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE gallery_events RESTART IDENTITY;")
            conn.commit()

        now_k = get_now_kst()
        total_created = 0

        # 30분 단위 슬라이딩(90분 창)으로 24시간(48개 구간) 시간순 스캔 -> 경계 잘림 완벽 해결
        for step in range(48, -1, -1):
            target_end = now_k - timedelta(minutes=30 * step)
            try:
                events = self.analyze_window(end_dt=target_end, window_minutes=90)
                total_created += len(events)
            except Exception as e:
                log("재구축오류", f"{step * 30}분 전 구간 오류: {e}")

        log("재구축", f"🎉 최근 24시간 주요 급상승 토픽 총 {total_created}건 초고속 재구축 완료!")


def run_analyzer_loop():
    """10분 간격 실시간 단어 노트 트렌드 감지 워커 (API 제한 없음)"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("트렌드감지기", "🚀 순수 단어노트 & 연관성 클러스터 트렌드 감지 워커 시작 (No-LLM)")

    analyzer.reset_and_regenerate_24h()

    while True:
        time.sleep(600)  # 10분마다 실행 (LLM 쿼터 제약이 없으므로 더 빠르게 감지)
        try:
            analyzer.analyze_window(end_dt=get_now_kst(), window_minutes=90)
        except Exception as e:
            log("트렌드감지기", f"분석 오류: {e}")


if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
