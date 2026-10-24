"""First release policy. Re-enabling collection requires a separately reviewed release."""
COLLECTION_ENABLED = False
COLLECTION_MESSAGE = '采集暂未启用；已有数量、时间、错误和任务记录保留'


def require_collection():
    raise ValueError(COLLECTION_MESSAGE)
