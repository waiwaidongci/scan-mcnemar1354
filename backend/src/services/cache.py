"""内容指纹缓存：源数据更新后沿用未受影响的结果，受影响的结果重算。

键由内容指纹决定（'batch:<批次明细指纹>'、'dist:<批次净差多重集指纹>'），
与数据版本一一对应：新报告只读取当前数据算出的键，旧版本晚到的写入
落在旧键上，永远不会并入新报告。同键并发写入内容必然相同，保留先到者。
"""
import logging
from sqlalchemy.exc import IntegrityError
from src.config.database import Session
from src.models.source import ComputeCache

log = logging.getLogger(__name__)


class CacheStore:
    """analyze 的 store 端口：get 未命中返回 None，set 冲突时保留先到结果。"""

    def __init__(self, dataset_id=None, dataset_checksum=None):
        self.dataset_id = dataset_id
        self.dataset_checksum = dataset_checksum

    def get(self, key):
        with Session() as db:
            row = db.get(ComputeCache, key)
            return None if row is None else row.payload

    def set(self, key, payload):
        with Session() as db:
            db.add(ComputeCache(cache_key=key, payload=payload,
                                dataset_id=self.dataset_id, dataset_checksum=self.dataset_checksum))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                log.debug('Cache key %s already stored by a concurrent writer', key)
