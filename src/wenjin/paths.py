"""把语料内相对路径安全地解析到指定根目录之下。

入库落盘和原文取件都要做同一件事："调用方给一个相对路径，我要确定它没有跑出根目录"。
放在这里而不是各自实现，是为了只有一处需要审计：绝对路径、`..`、符号链接逃逸都在这里挡掉。

不依赖任何其他模块，方便被 ingestion、检索服务和MCP端点共同复用。
"""

from pathlib import Path


class UnsafePathError(ValueError):
    """路径超出允许的根目录，或本身就是不安全的形式。"""


def resolve_within(root: Path, relative: str, *, allow_symlink: bool = False) -> Path:
    """把相对路径解析到 root 之下；越界、绝对路径、符号链接逃逸都直接拒绝。

    返回值可能不存在（调用方自己决定要不要要求存在）。
    `allow_symlink=False` 时会拒绝解析结果经过符号链接的情况：仓库里的软链接可能指向
    语料目录之外，复制或返回它都会破坏"这份内容属于该语料"的溯源契约。
    """
    if not relative or not relative.strip():
        raise UnsafePathError("路径不能为空")
    candidate = Path(relative)
    if candidate.is_absolute():
        raise UnsafePathError(f"必须是相对路径：{relative}")
    if any(part == ".." for part in candidate.parts):
        raise UnsafePathError(f"路径不能包含 ..：{relative}")
    if candidate.drive or candidate.root:
        raise UnsafePathError(f"路径不能带盘符或根：{relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and not resolved.is_relative_to(resolved_root):
        raise UnsafePathError(f"路径超出根目录：{relative}")
    if not allow_symlink and resolved != resolved_root:
        if _has_symlink(resolved_root, candidate):
            raise UnsafePathError(f"路径经过符号链接，拒绝访问：{relative}")
    return resolved


def _has_symlink(resolved_root: Path, candidate: Path) -> bool:
    """逐段检查原始路径（不是解析后的路径）中是否出现符号链接。"""
    current = resolved_root
    for part in candidate.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def validate_relative_path(value: str, *, field: str = "路径") -> str:
    """校验一个**语料内相对路径**的写法，返回统一的 POSIX 形式。

    只看前导斜杠和 `..` 是不够的：Windows 上 `C:/outside/a.txt` 是绝对路径，
    `C:a.txt` 是盘符相对路径，`\\\\server\\share\\x` 是 UNC 路径，都必须在进入
    任何路径拼接之前就拒绝。这里不碰文件系统，纯写法校验。
    """
    text = (value or "").strip()
    if not text:
        raise UnsafePathError(f"{field}不能为空")
    normalized = text.replace("\\", "/")
    if normalized.startswith("/"):
        raise UnsafePathError(f"{field}必须是相对路径：{value}")
    # 盘符形式（C:/x 与 C:x），以及UNC前缀。
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        raise UnsafePathError(f"{field}不能带盘符：{value}")
    if normalized.startswith("//"):
        raise UnsafePathError(f"{field}不能是UNC路径：{value}")
    parts = normalized.split("/")
    if any(part == ".." for part in parts):
        raise UnsafePathError(f"{field}不能包含 ..：{value}")
    if any(part in {"", "."} for part in parts):
        raise UnsafePathError(f"{field}包含空路径段：{value}")
    return normalized


def is_within(root: Path, path: Path) -> bool:
    """判断一个已解析的路径是否位于根目录内。"""
    resolved_root = root.resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == resolved_root or resolved.is_relative_to(resolved_root)
