"""
notifier.py — 실시간 토픽 감지 시 디스코드 웹훅(Discord Webhook)으로 알림을 전송하는 모듈
"""
import os
import json
import re
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone, timedelta
from concurrent.futures import ThreadPoolExecutor
from database import get_db

KST = timezone(timedelta(hours=9))
WEBHOOK_REGEX = re.compile(r'^https://(discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+$')
SITE_BASE_URL = os.environ.get("SITE_BASE_URL", "https://archive.rescene.online").rstrip("/")


def validate_webhook_url(url: str) -> bool:
    """디스코드 공식 웹훅 URL 형식인지 검증"""
    if not url or not isinstance(url, str):
        return False
    return bool(WEBHOOK_REGEX.match(url.strip()))


def send_discord_webhook(webhook_url: str, payload: dict, timeout: int = 6) -> tuple[bool, str]:
    """urllib를 이용한 의존성 없는 초경량 디스코드 웹훅 POST 전송"""
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            webhook_url.strip(),
            data=data,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "ResceneArchive-DiscordBot/1.0"
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            if status in (200, 204):
                return True, "OK"
            return False, f"HTTP {status}"
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False, "404_NOT_FOUND"
        return False, f"HTTP Error {e.code}: {e.reason}"
    except Exception as e:
        return False, str(e)


def send_test_webhook(webhook_url: str, channel_name: str = "") -> tuple[bool, str]:
    """웹훅 등록 시 채널 연결 확인용 테스트/환영 임베드 전송"""
    if not validate_webhook_url(webhook_url):
        return False, "올바른 디스코드 웹훅 URL이 아닙니다. (https://discord.com/api/webhooks/...)"

    now_kst = datetime.now(KST)
    payload = {
        "username": "리센느 토픽 알리미",
        "avatar_url": "https://cdn-icons-png.flaticon.com/512/3669/3669986.png",
        "embeds": [{
            "title": "🎉 디스코드 실시간 토픽 알림 연동 완료!",
            "description": "앞으로 리센느 마이너 갤러리에 **새로운 화제의 토픽**이 감지될 때마다 이 채널로 즉시 알림이 전송됩니다.",
            "color": 5793266,  # #5865F2 Discord Blurple
            "fields": [
                {
                    "name": "📡 등록 채널",
                    "value": channel_name if channel_name else "디스코드 채널",
                    "inline": True
                },
                {
                    "name": "⚡ 감지 주기",
                    "value": "실시간 3분 모니터링",
                    "inline": True
                },
                {
                    "name": "🌐 웹사이트",
                    "value": f"[리센느 아카이브 바로가기]({SITE_BASE_URL})",
                    "inline": False
                }
            ],
            "footer": {
                "text": "리센느 아카이브 · 실시간 트렌드 알림봇"
            },
            "timestamp": datetime.now(timezone.utc).isoformat()
        }]
    }

    ok, msg = send_discord_webhook(webhook_url, payload)
    if ok:
        return True, "테스트 메시지가 디스코드 채널로 성공적으로 전송되었습니다."
    return False, f"디스코드 전송 실패: {msg}"


def send_new_topic_notification(event_info: dict, start_dt, end_dt, gallery_id: str = "rescene1"):
    """
    새로운 토픽이 감지(생성)되었을 때 등록된 모든 활성 디스코드 웹훅 채널로 알림 전송
    """
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT webhook_id, webhook_url, channel_name
                    FROM discord_webhooks
                    WHERE gallery_id = %s AND is_active = TRUE
                """, (gallery_id,))
                webhooks = cur.fetchall()

        if not webhooks:
            return

        # 알림 내용 구성
        title = event_info.get("title") or "새로운 화제 토픽"
        summary = event_info.get("summary") or ""
        post_count = event_info.get("post_count") or len(event_info.get("sample_post_ids") or [])
        raw_kws = event_info.get("keywords") or []

        kw_chips = []
        search_words = []
        for k in raw_kws:
            w = k.get("word") if isinstance(k, dict) else str(k)
            lift = k.get("lift") if isinstance(k, dict) else None
            search_words.append(w)
            if lift:
                kw_chips.append(f"`#{w}` ({lift}x)")
            else:
                kw_chips.append(f"`#{w}`")

        kw_display = " ".join(kw_chips[:5]) if kw_chips else "주요 키워드"
        time_display = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"

        search_query = " OR ".join(search_words[:3]) if search_words else ""
        encoded_query = urllib.parse.quote(search_query)
        if start_dt and end_dt:
            s_iso = (start_dt - timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M')
            e_iso = (end_dt + timedelta(minutes=5)).strftime('%Y-%m-%dT%H:%M')
            archive_link = (
                f"{SITE_BASE_URL}/?q={encoded_query}&target=all&period=custom&start_date={s_iso}&end_date={e_iso}"
                if search_query else f"{SITE_BASE_URL}/#topics"
            )
        else:
            archive_link = f"{SITE_BASE_URL}/?q={encoded_query}&target=all" if search_query else f"{SITE_BASE_URL}/#topics"

        embed_payload = {
            "username": "리센느 토픽 알리미",
            "avatar_url": "https://cdn-icons-png.flaticon.com/512/3669/3669986.png",
            "embeds": [{
                "title": f"🔥 {title}",
                "description": f"```text\n{summary}\n```" if summary else "새로운 토픽이 갤러리에서 활발히 논의되고 있습니다.",
                "color": 16103179,  # #F59E0B Amber
                "fields": [
                    {
                        "name": "🏷️ 주요 키워드",
                        "value": kw_display,
                        "inline": False
                    },
                    {
                        "name": "📊 화력",
                        "value": f"🔥 관련 글 **{post_count}건**",
                        "inline": True
                    },
                    {
                        "name": "⏰ 발생 시간대",
                        "value": f"`{time_display}`",
                        "inline": True
                    },
                    {
                        "name": "🔎 바로가기",
                        "value": f"[이 시간대 관련 글 모아보기 ↗]({archive_link})",
                        "inline": False
                    }
                ],
                "footer": {
                    "text": "리센느 아카이브 · 실시간 트렌드 타임라인"
                },
                "timestamp": datetime.now(timezone.utc).isoformat()
            }]
        }

        # 비동기 백그라운드 스레드풀로 전송 (메인 루프 블로킹 방지)
        def _dispatch(wh_item):
            wh_id, wh_url, wh_name = wh_item
            ok, resp_msg = send_discord_webhook(wh_url, embed_payload)
            with get_db() as conn:
                with conn.cursor() as cur:
                    if ok:
                        cur.execute("UPDATE discord_webhooks SET last_sent_at = NOW() WHERE webhook_id = %s", (wh_id,))
                    elif resp_msg == "404_NOT_FOUND":
                        # 디스코드 채널이나 웹훅이 삭제된 경우 비활성화
                        cur.execute("UPDATE discord_webhooks SET is_active = FALSE WHERE webhook_id = %s", (wh_id,))
                conn.commit()

        executor = ThreadPoolExecutor(max_workers=min(10, len(webhooks)))
        executor.map(_dispatch, webhooks)

    except Exception as e:
        print(f"⚠️ [디스코드알림오류] {e}")
