import os
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from contextlib import contextmanager

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "dc_db")
DB_USER = os.getenv("DB_USER", "dc_user")
DB_PASS = os.getenv("DB_PASS", "dc_pass1234")

# Multi-threaded Connection Pool (Min 2, Max 30 connections)
db_pool = ThreadedConnectionPool(
    2, 30,
    host=DB_HOST,
    port=DB_PORT,
    dbname=DB_NAME,
    user=DB_USER,
    password=DB_PASS
)

@contextmanager
def get_db():
    conn = db_pool.getconn()
    try:
        yield conn
    finally:
        db_pool.putconn(conn)

def init_db():
    """테이블 및 pg_bigm 인덱스 초기화"""
    create_table_query = """
    CREATE TABLE IF NOT EXISTS dc_posts (
        post_id BIGINT PRIMARY KEY,
        gallery_id VARCHAR(50) NOT NULL,
        title VARCHAR(300) NOT NULL,
        content TEXT,
        nickname VARCHAR(100),
        author_code VARCHAR(50),
        created_at TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        crawled_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    );

    -- 2글자 이상 초고속 부분 일치 검색용 pg_bigm GIN 인덱스
    CREATE INDEX IF NOT EXISTS idx_posts_title_bigm ON dc_posts USING gin (title gin_bigm_ops);
    CREATE INDEX IF NOT EXISTS idx_posts_content_bigm ON dc_posts USING gin (content gin_bigm_ops);
    CREATE INDEX IF NOT EXISTS idx_posts_nickname_bigm ON dc_posts USING gin (nickname gin_bigm_ops);
    
    -- 식별코드(ID/IP) 완전 일치 고속 검색용 B-Tree 인덱스
    CREATE INDEX IF NOT EXISTS idx_posts_author_code ON dc_posts (author_code);
    
    -- 날짜 정렬 및 60일 롤링 삭제용 B-Tree 인덱스
    CREATE INDEX IF NOT EXISTS idx_posts_created_at ON dc_posts (created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_posts_gallery ON dc_posts (gallery_id);

    -- 실시간 갤러리 떡밥 & 토픽 타임라인 테이블
    CREATE TABLE IF NOT EXISTS gallery_events (
        event_id SERIAL PRIMARY KEY,
        gallery_id VARCHAR(50) NOT NULL,
        window_start TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        window_end TIMESTAMP WITHOUT TIME ZONE NOT NULL,
        cluster_id INT NOT NULL DEFAULT 1,
        topic_title VARCHAR(200) NOT NULL,
        topic_summary TEXT NOT NULL,
        mood_tag VARCHAR(100),
        keywords JSONB NOT NULL,
        post_count INT NOT NULL DEFAULT 0,
        sample_post_ids BIGINT[] DEFAULT '{}',
        created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
    );
    CREATE INDEX IF NOT EXISTS idx_gallery_events_start ON gallery_events (window_start DESC);
    CREATE INDEX IF NOT EXISTS idx_gallery_events_gallery ON gallery_events (gallery_id);

    -- 기준 미달(8건 미만) 소규모 잡담 이벤트 정리
    DELETE FROM gallery_events WHERE post_count < 8;
    """
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(create_table_query)
        conn.commit()
    print("✅ DB 테이블 및 pg_bigm 인덱스 초기화 완료!")

if __name__ == "__main__":
    init_db()
