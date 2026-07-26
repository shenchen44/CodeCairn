from openai import OpenAI

from app.core.config import get_settings


class OpenAIChatClient:
    def __init__(self) -> None:
        settings = get_settings()
        self.model = settings.openai_model
        self.provider = settings.openai_provider.lower()
        client_kwargs = {"api_key": settings.openai_api_key}
        if settings.openai_base_url:
            client_kwargs["base_url"] = settings.openai_base_url
        self.client = OpenAI(
            timeout=settings.openai_timeout_seconds,
            max_retries=settings.openai_max_retries,
            **client_kwargs,
        )

    def create_completion(self, *, messages: list[dict], tools: list[dict] | None = None):
        extra_body = {"reasoning_split": True} if self.provider == "minimax" else None
        return self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools or [],
            extra_body=extra_body,
        )
