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
        """gallery_events 테이블에 저장 (인접/중첩 구간 동일 떡밥 자동 병합 및 실제 발생 시간대 반영)"""
        kw_json = json.dumps(event_info["keywords"], ensure_ascii=False)
        kw_names = {k["word"] for k in event_info["keywords"]}
        top_word = event_info["keywords"][0]["word"] if event_info["keywords"] else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, keywords, post_count, window_start, window_end, sample_post_ids
                    FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '3 hours'
                      AND window_start <= %s + INTERVAL '3 hours'
                    ORDER BY event_id DESC
                """, (self.gallery_id, start_dt, end_dt))
                existing = cur.fetchall()

                for eid, old_kws, old_cnt, old_w_start, old_w_end, old_pids in existing:
                    if isinstance(old_kws, str):
                        try:
                            old_kws = json.loads(old_kws)
                        except Exception:
                            old_kws = []
                    old_names = {k["word"] if isinstance(k, dict) else str(k) for k in old_kws}
                    intersect = len(kw_names & old_names)
                    old_top = (
                        old_kws[0]["word"]
                        if (old_kws and isinstance(old_kws[0], dict))
                        else (old_kws[0] if old_kws else "")
                    )

                    is_same_topic = (
                        (top_word and top_word in old_names)
                        or (old_top and old_top in kw_names)
                        or intersect >= 2
                        or any(
                            len(w1) >= 2 and len(w2) >= 2 and (w1 in w2 or w2 in w1)
                            for w1 in list(kw_names)[:2]
                            for w2 in list(old_names)[:2]
                        )
                    )

                    if is_same_topic:
                        new_start = min(old_w_start, start_dt)
                        new_end = max(old_w_end, end_dt)
                        merged_pids = sorted(
                            set(old_pids or []) | set(event_info["sample_post_ids"] or []),
                            reverse=True,
                        )[:60]
                        new_cnt = max(old_cnt, event_info["post_count"], len(merged_pids))

                        # 키워드 병합 (피크 구간의 키워드 우선 + 누락된 연관 키워드 추가)
                        if event_info["post_count"] >= old_cnt:
                            base_kws, extra_kws = event_info["keywords"], old_kws
                        else:
                            base_kws, extra_kws = old_kws, event_info["keywords"]

                        merged_kws = list(base_kws)
                        seen_w = {k["word"] if isinstance(k, dict) else str(k) for k in merged_kws}
                        for k in extra_kws:
                            w = k["word"] if isinstance(k, dict) else str(k)
                            if w not in seen_w and len(merged_kws) < 6:
                                seen_w.add(w)
                                merged_kws.append(k)
                        merged_kw_json = json.dumps(merged_kws, ensure_ascii=False)

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
                                merged_kw_json, merged_pids, eid
                            ))
                        else:
                            cur.execute("""
                                UPDATE gallery_events
                                SET window_start = %s,
                                    window_end = %s,
                                    post_count = %s,
                                    keywords = %s,
                                    sample_post_ids = %s
                                WHERE event_id = %s
                            """, (new_start, new_end, new_cnt, merged_kw_json, merged_pids, eid))
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
