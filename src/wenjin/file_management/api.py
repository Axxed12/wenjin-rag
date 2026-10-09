"""兼容入口：HTTP 实现位于 wenjin.management_http 包。"""

from wenjin.management_http import create_app, validate_listen_security

__all__ = ["create_app", "validate_listen_security"]
