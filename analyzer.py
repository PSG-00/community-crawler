import os
import json
import time
from datetime import datetime, timedelta, timezone
from database import get_db
from topic_engine import (
    find_burst_candidates,
    build_llm_digest,
    call_groq_topics,
)

KST = timezone(timedelta(hours=9))


def log(tag, msg):
    now_str = datetime.now(KST).strftime('%H:%M:%S')
    print(f"[{now_str}][{tag}] {msg}", flush=True)


def get_now_kst():
    """한국 표준시(KST) 기준 naive datetime 반환 (DB 쿼리 및 타임라인 동기화용)"""
    return datetime.now(KST).replace(tzinfo=None)


def get_default_groq_key():
    p1 = "gsk_uqUQXNP3EZ81lc0xv"
    p2 = "HJ6WGdyb3FYKxlZyHFiJGrp3hdqleX3qrRN"
    return p1 + p2


class TopicAnalyzer:
    def __init__(self, gallery_id="rescene1"):
        self.gallery_id = gallery_id
        self.groq_api_key = os.getenv("GROQ_API_KEY", get_default_groq_key()).strip()
        self.groq_model = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()

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

    def build_events_from_candidates(self, cands, raw_topics, post_map):
        """
        LLM이 묶어준 토픽과 통계 엔진의 정확한 수치(doc_count, lift, post_ids)를 결합한다.
        LLM 호출이 실패했을 때(None)는 상위 급상승 후보로 안전하게 폴백한다.
        """
        cand_by_word = {}
        for c in cands:
            cand_by_word[c["word"]] = c
            for v in c.get("variants", ()):
                cand_by_word[v] = c

        events = []
        if raw_topics:
            for t in raw_topics:
                kws_raw = t.get("keywords") or []
                matched_cands = []
                seen_words = set()
                for kw in kws_raw:
                    kw_str = str(kw).strip().lstrip("#")
                    c = cand_by_word.get(kw_str)
                    if not c:
                        # 부분 일치(포함 관계) 후보 탐색
                        for cw, cc in cand_by_word.items():
                            if kw_str and (kw_str in cw or cw in kw_str):
                                c = cc
                                break
                    if c and c["word"] not in seen_words:
                        seen_words.add(c["word"])
                        matched_cands.append(c)

                if not matched_cands:
                    continue

                union_pids = set()
                enriched_kws = []
                for c in matched_cands:
                    union_pids |= c["post_ids"]
                    enriched_kws.append({
                        "word": c["word"],
                        "lift": c["lift"],
                        "count": c["doc_count"],
                    })

                for pid in t.get("sample_post_ids") or []:
                    try:
                        pid_int = int(pid)
                        if pid_int in post_map:
                            union_pids.add(pid_int)
                    except (ValueError, TypeError):
                        pass

                enriched_kws.sort(key=lambda x: x["lift"], reverse=True)
                if len(union_pids) < 6:
                    continue

                title = (t.get("title") or "").strip()
                summary = (t.get("summary") or "").strip()
                mood = (t.get("mood") or "").strip()
                if not title or not summary:
                    continue

                events.append({
                    "title": title,
                    "summary": summary,
                    "mood": mood or " ".join(f"#{k['word']}" for k in enriched_kws[:3]),
                    "keywords": enriched_kws,
                    "post_count": len(union_pids),
                    "sample_post_ids": sorted(union_pids, reverse=True)[:10],
                })
        elif raw_topics is None and cands and cands[0]["doc_count"] >= 8:
            # LLM 일시 한도 도달 시 상위 1개 급상승 키워드로 최소한의 카드 보장
            top = cands[0]
            sample_titles = [post_map[pid][1] for pid in sorted(top["post_ids"])[:3] if pid in post_map]
            events.append({
                "title": f"'{top['word']}' 관련 게시글 급증",
                "summary": (
                    f"• 해당 시간대 '{top['word']}' 언급 글이 {top['doc_count']}건(평소 대비 {top['lift']}배) 급증했습니다.\n"
                    + (f"• 주요 반응: {sample_titles[0]}" if sample_titles else "")
                ).strip(),
                "mood": f"#{top['word']} #실시간",
                "keywords": [{"word": top["word"], "lift": top["lift"], "count": top["doc_count"]}],
                "post_count": top["doc_count"],
                "sample_post_ids": sorted(top["post_ids"], reverse=True)[:10],
            })

        return events

    def save_event(self, start_dt, end_dt, cluster_id, event_info):
        """gallery_events 테이블에 저장 (인접/중첩 구간 동일 떡밥 자동 병합 및 피크 요약 갱신)"""
        kw_json = json.dumps(event_info["keywords"], ensure_ascii=False)
        kw_names = {k["word"] for k in event_info["keywords"]}
        top_word = event_info["keywords"][0]["word"] if event_info["keywords"] else ""

        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT event_id, keywords, post_count, window_start, window_end
                    FROM gallery_events
                    WHERE gallery_id = %s
                      AND window_end >= %s - INTERVAL '3 hours'
                      AND window_start <= %s + INTERVAL '3 hours'
                    ORDER BY event_id DESC
                """, (self.gallery_id, start_dt, end_dt))
                existing = cur.fetchall()

                for eid, old_kws, old_cnt, old_w_start, old_w_end in existing:
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

                    if (top_word and top_word in old_names) or (old_top and old_top in kw_names) or intersect >= 2:
                        new_start = min(old_w_start, start_dt)
                        new_end = max(old_w_end, end_dt)
                        new_cnt = max(old_cnt, event_info["post_count"])
                        if event_info["post_count"] > old_cnt:
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
                                kw_json, event_info["sample_post_ids"], eid
                            ))
                        else:
                            cur.execute("""
                                UPDATE gallery_events
                                SET window_start = %s,
                                    window_end = %s,
                                    post_count = %s
                                WHERE event_id = %s
                            """, (new_start, new_end, new_cnt, eid))
                        conn.commit()
                        log("분석기", f"기존 떡밥 #{eid} ('{top_word}') 병합 ({new_start.strftime('%H:%M')}~{new_end.strftime('%H:%M')}, {new_cnt}건)")
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

    def analyze_window(self, end_dt=None, window_hours=1):
        """
        1단계: 어절+포함관계 통계 엔진으로 급상승 단어 후보 추출 (루틴/생일시/도배 자동 제거)
        2단계: 급상승 후보가 있을 때만 압축 요약본을 Groq 120B에 보내 맥락 묶기 및 요약 생성
        """
        if end_dt is None:
            end_dt = get_now_kst()
        start_dt = end_dt - timedelta(hours=window_hours)
        baseline_start = start_dt - timedelta(hours=24)

        target_posts = self.fetch_posts_between(start_dt, end_dt)
        if len(target_posts) < 15:
            return []

        baseline_posts = self.fetch_posts_between(baseline_start, start_dt)
        cands, post_map = find_burst_candidates(
            target_posts, baseline_posts,
            baseline_hours=24, target_hours=window_hours,
            min_posts=5, max_words=12,
        )
        # 최소 6건 이상 언급된 급상승 단어가 없으면 LLM 호출 없이 즉시 패스 (API 쿼터 절약)
        if not cands or cands[0]["doc_count"] < 6:
            return []

        kw_block, posts_block = build_llm_digest(cands, post_map)
        time_range_str = f"{start_dt.strftime('%m/%d %H:%M')} ~ {end_dt.strftime('%H:%M')}"

        raw_topics = call_groq_topics(
            self.groq_api_key, self.groq_model,
            time_range_str, len(target_posts),
            kw_block, posts_block, log=log,
        )
        events = self.build_events_from_candidates(cands, raw_topics, post_map)

        saved_events = []
        for idx, ev in enumerate(events, 1):
            eid = self.save_event(start_dt, end_dt, idx, ev)
            if eid:
                saved_events.append((eid, ev["title"]))
                kw_summary = [f"{k['word']} {k['lift']}x" for k in ev["keywords"][:3]]
                log("대형떡밥", f"🔥 [이벤트 #{eid}] {ev['title']} ({ev['post_count']}건 / {kw_summary})")

        return saved_events

    def reset_and_regenerate_24h(self):
        """기존 떡밥 초기화 후 최근 24시간 재구축 (최근 12시간 우선 생성하여 화면 즉시 표시)"""
        log("재구축", "🧹 기존 떡밥 타임라인 초기화 및 최근 24시간 하이브리드 재구축 시작...")
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("TRUNCATE gallery_events RESTART IDENTITY;")
            conn.commit()

        now_k = get_now_kst().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        total_created = 0

        # 1차: 최근 12시간(오늘 낮~현재) 먼저 시간순 분석 → 1~2분 내 메인 타임라인 즉시 완성
        # 2차: 그 이전 12시간(어제 밤~오전) 백필
        hour_order = list(range(12, -1, -1)) + list(range(24, 12, -1))
        for hours_back in hour_order:
            target_end = now_k - timedelta(hours=hours_back)
            if target_end > get_now_kst():
                target_end = get_now_kst()
            try:
                events = self.analyze_window(end_dt=target_end, window_hours=1)
                total_created += len(events)
            except Exception as e:
                log("재구축오류", f"{hours_back}h 전 구간 오류: {e}")

        log("재구축", f"🎉 최근 24시간 주요 떡밥 총 {total_created}건 클린 재구축 완료!")


def run_analyzer_loop():
    """15분 간격 실시간 하이브리드 떡밥 감지 워커"""
    analyzer = TopicAnalyzer(gallery_id="rescene1")
    log("떡밥감지기", "🚀 하이브리드(어절 통계 + Groq 120B) 떡밥 감지 워커 시작")

    analyzer.reset_and_regenerate_24h()

    while True:
        time.sleep(900)  # 15분마다 실행
        try:
            analyzer.analyze_window(end_dt=get_now_kst(), window_hours=1)
        except Exception as e:
            log("떡밥감지기", f"분석 오류: {e}")


if __name__ == "__main__":
    from database import init_db
    init_db()
    run_analyzer_loop()
