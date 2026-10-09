"""Settings, read once from environment variables."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

PhotoMode = Literal["cloudflare", "fake", "none"]
ResearchMode = Literal["tavily", "fake", "none"]


class Settings(BaseModel):
    data_dir: Path
    db_path: Path
    brands_dir: Path
    brand_id: str = "hybridge"

    google_api_key: str = ""
    gemini_model: str = "gemini-3.5-flash-lite"

    cloudflare_account_id: str = ""
    cloudflare_api_token: str = ""

    # v6: paid image models on the designer's own keys. A key saved on the Settings page wins
    # over these; the Google image key is not GOOGLE_API_KEY, so the text calls stay free.
    openai_api_key: str = ""
    google_image_api_key: str = ""
    # The Fernet key that encrypts saved keys; without one, the studio makes one beside the
    # database on first start.
    secret_key: str = ""

    # An override for tests and demos: "fake" makes every model's photos with the stand-in,
    # "none" turns photos off, and "cloudflare" calls the real adapters for every model, the
    # paid ones included, on whatever keys are set. "auto" follows `photo_mode`.
    photo_provider: Literal["auto", "cloudflare", "fake", "none"] = "auto"
    analyse_per_minute: int = 10
    max_samples: int = 6  # the most photos one round may ask for

    # v4: web research before the draft. Tavily's free plan gives 1,000 search credits a month.
    tavily_api_key: str = ""
    research_provider: Literal["auto", "tavily", "fake", "none"] = "auto"
    research_monthly_limit: int = 900  # search credits the studio will spend in a month

    @property
    def demo_mode(self) -> bool:
        """With no model key the studio runs on fake models."""
        return not self.google_api_key

    @property
    def research_mode(self) -> ResearchMode:
        if self.research_provider != "auto":
            return self.research_provider
        if self.tavily_api_key:
            return "tavily"
        return "fake" if self.demo_mode else "none"

    @property
    def photo_mode(self) -> PhotoMode:
        """Who makes the photos, by v5's rule, which v6 keeps: Cloudflare whenever its keys are
        set, else the stand-in ("fake") in demo mode, else no free source ("none"). The photo
        registry makes every model's photos with the stand-in only while this is "fake"."""
        if self.photo_provider != "auto":
            return self.photo_provider
        if self.cloudflare_account_id and self.cloudflare_api_token:
            return "cloudflare"
        return "fake" if self.demo_mode else "none"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env

        def get(name: str, default: str = "") -> str:
            return (env.get(name) or default).strip()

        data_dir = Path(get("STUDIO_DATA_DIR", "/app/data"))
        return cls(
            data_dir=data_dir,
            db_path=Path(get("STUDIO_DB_PATH", str(data_dir / "studio.db"))),
            brands_dir=Path(get("STUDIO_BRANDS_DIR", "/app/brands")),
            brand_id=get("STUDIO_BRAND", "hybridge"),
            google_api_key=get("GOOGLE_API_KEY"),
            gemini_model=get("STUDIO_GEMINI_MODEL", "gemini-3.5-flash-lite"),
            cloudflare_account_id=get("CLOUDFLARE_ACCOUNT_ID"),
            cloudflare_api_token=get("CLOUDFLARE_API_TOKEN"),
            openai_api_key=get("OPENAI_API_KEY"),
            google_image_api_key=get("GOOGLE_IMAGE_API_KEY"),
            secret_key=get("STUDIO_SECRET_KEY"),
            photo_provider=get("STUDIO_PHOTO_PROVIDER", "auto"),  # type: ignore[arg-type]
            analyse_per_minute=int(get("STUDIO_ANALYSE_PER_MINUTE", "10")),
            max_samples=int(get("STUDIO_MAX_SAMPLES", "6")),
            tavily_api_key=get("TAVILY_API_KEY"),
            research_provider=get("STUDIO_RESEARCH_PROVIDER", "auto"),  # type: ignore[arg-type]
            research_monthly_limit=int(get("STUDIO_RESEARCH_MONTHLY_LIMIT", "900")),
        )
