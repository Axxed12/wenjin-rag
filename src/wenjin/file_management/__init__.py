"""统一文件管理核心。"""

from wenjin.file_management.backends import IndexAdapter, QdrantBm25Adapter
from wenjin.file_management.models import FileRecord, FileSnapshot, JobRecord
from wenjin.file_management.service import FileManagementService

__all__ = [
    "FileManagementService",
    "FileRecord",
    "FileSnapshot",
    "JobRecord",
    "IndexAdapter",
    "QdrantBm25Adapter",
]
