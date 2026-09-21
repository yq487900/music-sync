from sqlalchemy import create_engine, Column, Integer, String, Float, JSON, Boolean
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
    data = Column(JSON)

class Track(Base):
    __tablename__ = "tracks"
    id = Column(Integer, primary_key=True)
    platform = Column(String)
    platform_track_id = Column(String)
    title = Column(String)
    artist = Column(String)
    duration = Column(Float)
    metadata = Column(JSON, default=dict)
    downloaded = Column(Boolean, default=False)

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
