"""
AWS Secrets Manager backend.

Requires `boto3`. Region defaults to AWS_REGION env var.
"""

import os
from typing import List, Optional

from app.secrets.interface import SecretsBackend


class AWSSecretsManager(SecretsBackend):
    name = "aws_secrets_manager"

    def __init__(self, region: Optional[str] = None):
        self._region = region or os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")
        try:
            import boto3  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "AWSSecretsManager requires `boto3`. Install: pip install boto3"
            ) from e

    def _client(self):
        import boto3
        kwargs = {"region_name": self._region} if self._region else {}
        return boto3.client("secretsmanager", **kwargs)

    async def get(self, key: str) -> Optional[str]:
        client = self._client()
        try:
            resp = client.get_secret_value(SecretId=key)
            return resp.get("SecretString")
        except client.exceptions.ResourceNotFoundException:
            return None
        except Exception:
            return None

    async def set(self, key: str, value: str) -> None:
        client = self._client()
        try:
            client.create_secret(Name=key, SecretString=value)
        except client.exceptions.ResourceExistsException:
            client.put_secret_value(SecretId=key, SecretString=value)

    async def delete(self, key: str) -> bool:
        client = self._client()
        try:
            client.delete_secret(SecretId=key, ForceDeleteWithoutRecovery=True)
            return True
        except Exception:
            return False

    async def list_keys(self, prefix: str = "") -> List[str]:
        client = self._client()
        keys = []
        paginator = client.get_paginator("list_secrets")
        for page in paginator.paginate():
            for s in page.get("SecretList", []):
                name = s.get("Name", "")
                if name.startswith(prefix):
                    keys.append(name)
        return sorted(keys)
