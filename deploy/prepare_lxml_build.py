"""校验离线源码输入，仅调整构建下载器，不修改解析器C源码。"""

import hashlib
import json
import tarfile
from pathlib import Path

cache = Path("/tmp/lxml-static-inputs")
sources = json.loads(Path("/tmp/lxml-sources.json").read_text())
for item in sources:
    path = cache / item["file"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
        raise RuntimeError(f"构建源码校验失败：{path.name}")

destination = Path("/tmp/lxml-source")
destination.mkdir()
with tarfile.open(cache / "lxml-6.1.3.tar.gz") as archive:
    archive.extractall(destination, filter="data")
build_script = destination / "lxml-6.1.3/buildlibxml.py"
text = build_script.read_text()
# 上游即使指定版本也会先访问远端目录；已校验本地包时跳过该枚举。
for library, version in (("libxml2", "2.11.9"), ("libxslt", "1.1.39")):
    signature = f"def download_{library}(dest_dir, version=None):\n"
    if text.count(signature) != 1:
        raise RuntimeError("lxml构建下载器结构发生变化")
    body = (
        f"    if version != {version!r}:\n"
        "        raise RuntimeError('Unexpected offline native library version')\n"
        f"    return os.path.join(dest_dir, '{library}-{version}.tar.xz')\n"
    )
    text = text.replace(signature, signature + body)
build_script.write_text(text)
print("Verified 5 source archives; offline build downloader prepared")
