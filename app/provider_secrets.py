"""供应商 API 密钥的服务端加密存储。

密钥只从环境变量 ``DWELL_CONFIG_KEY`` 读取，绝不进数据库明文、日志或 API 响应。
部署时应在 Zeabur 的环境变量中配置一个 Fernet key；它不应提交到 GitHub。
"""

import os

from cryptography.fernet import Fernet, InvalidToken


class SecretConfigurationError(RuntimeError):
    """部署没有配置可用的加密主密钥。"""


def _fernet() -> Fernet:
    raw = os.environ.get("DWELL_CONFIG_KEY", "").strip()
    if not raw:
        raise SecretConfigurationError("服务器尚未配置 DWELL_CONFIG_KEY")
    try:
        return Fernet(raw.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretConfigurationError("DWELL_CONFIG_KEY 不是有效的 Fernet key") from exc


def encryption_ready() -> bool:
    try:
        _fernet()
        return True
    except SecretConfigurationError:
        return False


def encrypt_api_key(api_key: str) -> str:
    return _fernet().encrypt(api_key.encode("utf-8")).decode("ascii")


def decrypt_api_key(api_key_box: str) -> str:
    try:
        return _fernet().decrypt(api_key_box.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, UnicodeEncodeError) as exc:
        raise SecretConfigurationError("已保存的供应商密钥无法解密") from exc

