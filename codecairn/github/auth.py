from datetime import datetime, timedelta, timezone

import httpx
import jwt

from codecairn.config import get_settings


class GitHubAuthService:
    def __init__(self) -> None:
        self.settings = get_settings()

    def build_app_jwt(self) -> str:
        now = datetime.now(timezone.utc)
        payload = {
            "iat": int((now - timedelta(seconds=60)).timestamp()),
            "exp": int((now + timedelta(minutes=10)).timestamp()),
            "iss": self.settings.github_app_id,
        }
        return jwt.encode(payload, self.settings.private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        token = self.build_app_jwt()
        url = f"{self.settings.github_api_base}/app/installations/{installation_id}/access_tokens"
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )
            response.raise_for_status()
            return response.json()["token"]
