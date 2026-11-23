"""Environment-only configuration. Secrets never live in the repo."""

from __future__ import annotations

import os
from dataclasses import dataclass


class ConfigError(RuntimeError):
    pass


def _first(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value
    return default


@dataclass(frozen=True, slots=True)
class Config:
    endpoint: str
    instance: str
    bucket: str
    oss_endpoint: str
    region: str
    ak_id: str
    ak_secret: str
    weflow_api: str
    weflow_token: str
    source: str

    @classmethod
    def from_env(cls) -> Config:
        instance = _first("TABLESTORE_INSTANCE", default="wechat-vault")
        region = _first("OSS_REGION", "TABLESTORE_REGION", default="cn-beijing")
        endpoint = _first(
            "TABLESTORE_ENDPOINT",
            default=f"https://{instance}.{region}.ots.aliyuncs.com",
        )
        return cls(
            endpoint=endpoint,
            instance=instance,
            bucket=_first("OSS_BUCKET", default="wechat-vault"),
            oss_endpoint=_first(
                "OSS_ENDPOINT", default=f"https://oss-{region}.aliyuncs.com"
            ),
            region=region,
            ak_id=_first(
                "ALIBABA_CLOUD_ACCESS_KEY_ID",
                "TABLESTORE_ACCESS_KEY_ID",
                "OSS_ACCESS_KEY_ID",
            ),
            ak_secret=_first(
                "ALIBABA_CLOUD_ACCESS_KEY_SECRET",
                "TABLESTORE_ACCESS_KEY_SECRET",
                "OSS_ACCESS_KEY_SECRET",
            ),
            weflow_api=_first("WEFLOW_API", default="http://127.0.0.1:5031"),
            weflow_token=_first("WEFLOW_ACCESS_TOKEN"),
            source=_first("WECHAT_VAULT_SOURCE", default="mac"),
        )

    def require_cloud(self) -> None:
        missing = []
        if not self.ak_id:
            missing.append("ALIBABA_CLOUD_ACCESS_KEY_ID")
        if not self.ak_secret:
            missing.append("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
        if missing:
            raise ConfigError(
                "缺少云凭据: " + ", ".join(missing) + "（从 Infisical tools/dev /wechat-vault 注入）"
            )

    def require_weflow(self) -> None:
        if not self.weflow_token:
            raise ConfigError("缺少 WEFLOW_ACCESS_TOKEN（Infisical platform-accounts/dev /weflow）")

    def summary(self) -> str:
        return (
            f"endpoint={self.endpoint} instance={self.instance} "
            f"bucket={self.bucket} region={self.region} source={self.source} "
            f"ak={'set' if self.ak_id else 'missing'}"
        )