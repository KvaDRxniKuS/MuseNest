import sqlite3
import os
import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "tracks.db")


def init():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            artist TEXT,
            track TEXT,
            spotify_track_id TEXT UNIQUE,
            youtube_id TEXT,
            filepath TEXT,
            duration_ms INTEGER,
            downloaded_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def is_downloaded(spotify_track_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT 1 FROM tracks WHERE spotify_track_id=?", (spotify_track_id,))
    r = c.fetchone()
    conn.close()
    return r is not None


def add_track(artist, track, spotify_id, youtube_id, filepath, duration_ms):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT OR IGNORE INTO tracks "
        "(artist, track, spotify_track_id, youtube_id, filepath, duration_ms, downloaded_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (artist, track, spotify_id, youtube_id, filepath, duration_ms,
         datetime.datetime.now().isoformat()),
    )
    conn.commit()
    conn.close()


def get_all_tracks(limit=1000):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute(
        "SELECT artist, track, youtube_id, filepath, duration_ms, downloaded_at "
        "FROM tracks ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = [dict(r) for r in c.fetchall()]
    conn.close()
    return rows

