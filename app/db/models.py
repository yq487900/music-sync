from sqlalchemy import (Boolean, Column, Float, Integer, JSON, String,
                        create_engine, inspect, text)
from sqlalchemy.orm import declarative_base, sessionmaker

engine = create_engine("sqlite:////data/musicsync.db", connect_args={"check_same_thread": False})
Base = declarative_base()
SessionLocal = sessionmaker(bind=engine)


class Playlist(Base):
    __tablename__ = "playlists"
    id = Column(Integer, primary_key=True)
    platform = Column(String)
    playlist_id = Column(String)
    title = Column(String)
    track_count = Column(Integer, default=0)
    last_sync = Column(String, default="")
    data = Column(JSON, default=dict)


class Track(Base):
    __tablename__ = "tracks"
    id = Column(Integer, primary_key=True)
    platform = Column(String)
    platform_track_id = Column(String)
    title = Column(String)
    artist = Column(String)
    album = Column(String, default="")
    album_id = Column(Integer)
    duration = Column(Float, default=0)
    track_no = Column(Integer, default=0)
    disc = Column(Integer, default=0)
    pic_url = Column(String, default="")
    # 下载结果
    file_path = Column(String, default="")
    ext = Column(String, default="")
    level = Column(String, default="")
    br = Column(Integer, default=0)
    size = Column(Integer, default=0)
    md5 = Column(String, default="")
    status = Column(String, default="new")        # new / ok / failed
    fail_count = Column(Integer, default=0)
    last_error = Column(String, default="")
    downloaded_at = Column(String, default="")
    downloaded = Column(Boolean, default=False)   # 兼容旧字段
    # 用户选择
    selected = Column(Boolean, default=True)      # 是否勾选参与批量下载
    source_pref = Column(String, default="")      # "" 自动 / netease 官方 / 音源 id
    source_used = Column(String, default="")      # 实际成功使用的音源名
    # 云盘
    cloud_sid = Column(String, default="")
    cloud_state = Column(String, default="")      # "" / uploaded / failed
    cloud_at = Column(String, default="")
    cloud_error = Column(String, default="")
    track_metadata = Column(JSON, default=dict)


class LocalFile(Base):
    __tablename__ = "local_files"
    id = Column(Integer, primary_key=True)
    path = Column(String, unique=True)
    md5 = Column(String)
    sha256 = Column(String)
    duration = Column(Float)
    title = Column(String)
    artist = Column(String)


Base.metadata.create_all(engine)


def _migrate() -> None:
    """老库补列（SQLite 不支持一次加多列，逐列 ALTER）"""
    want = {
        "playlists": {"track_count": "INTEGER", "last_sync": "VARCHAR"},
        "tracks": {
            "album": "VARCHAR", "album_id": "INTEGER", "track_no": "INTEGER", "disc": "INTEGER",
            "pic_url": "VARCHAR", "file_path": "VARCHAR", "ext": "VARCHAR", "level": "VARCHAR",
            "br": "INTEGER", "size": "INTEGER", "md5": "VARCHAR", "status": "VARCHAR",
            "fail_count": "INTEGER", "last_error": "VARCHAR", "downloaded_at": "VARCHAR",
            "selected": "BOOLEAN", "source_pref": "VARCHAR", "source_used": "VARCHAR",
            "cloud_sid": "VARCHAR", "cloud_state": "VARCHAR", "cloud_at": "VARCHAR",
            "cloud_error": "VARCHAR",
        },
    }
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, cols in want.items():
            if table not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for name, typ in cols.items():
                if name not in have:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {typ}"))
        # 老数据的 selected 为空 → 默认勾选
        if "tracks" in tables:
            conn.execute(text("UPDATE tracks SET selected = 1 WHERE selected IS NULL"))


_migrate()
