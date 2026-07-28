"""GitHub integration services."""

import httpx

from codecairn.config import get_settings


class GitHubApiService:
    """Base class for GitHub API services with common HTTP client functionality."""

    def __init__(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        api_base: str | None = None,
    ) -> None:
        self.settings = get_settings()
        self.token = token
        self.client = client
        self.api_base = api_base or self.settings.github_api_base

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    async def _post(self, url: str, json: dict | None = None) -> dict:
        if self.client is not None:
            response = await self.client.post(
                url, headers=self._headers(), json=json
            )
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(url, headers=self._headers(), json=json)
            response.raise_for_status()
            return response.json()

    async def _get(self, url: str) -> dict:
        if self.client is not None:
            response = await self.client.get(url, headers=self._headers())
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.get(url, headers=self._headers())
            response.raise_for_status()
            return response.json()

    async def _put(self, url: str, json: dict | None = None) -> dict:
        if self.client is not None:
            response = await self.client.put(
                url, headers=self._headers(), json=json
            )
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.put(url, headers=self._headers(), json=json)
            response.raise_for_status()
            return response.json()

    async def _patch(self, url: str, json: dict | None = None) -> dict:
        if self.client is not None:
            response = await self.client.patch(
                url, headers=self._headers(), json=json
            )
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.patch(
                url, headers=self._headers(), json=json
            )
            response.raise_for_status()
            return response.json()
