"""检索请求可能遇到的稳定领域错误。"""


class StaleSnapshotError(RuntimeError):
    """请求所持版本快照已失效，需要刷新目录后重试。"""
