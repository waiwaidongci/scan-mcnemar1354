from datetime import datetime
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from src.config.database import Base
from src.utils.time import now

class Dataset(Base):
    __tablename__ = 'datasets'
    __table_args__ = (UniqueConstraint('name', 'checksum'),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    filename: Mapped[str] = mapped_column(String(160))
    checksum: Mapped[str] = mapped_column(String(64))
    rows: Mapped[list] = mapped_column(JSON)
    row_count: Mapped[int]
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)

class ComputeCache(Base):
    """按内容指纹缓存的计算结果。

    键为 'batch:<指纹>' 或 'dist:<指纹>'：内容不变则沿用，内容改动即换键重算。
    报告只读取当前数据指纹对应的键，旧版本晚到的写入落在旧键上，不会并入新报告。
    """
    __tablename__ = 'compute_cache'

    cache_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    dataset_id: Mapped[int | None] = mapped_column(ForeignKey('datasets.id'))
    dataset_checksum: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(default=now)
