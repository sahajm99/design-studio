"""Settings, read once from environment variables.

Keys can also be saved on the Settings page (v7: every key, the agents' and the search's
too). A saved key wins over the same key in `.env`; the fields here keep what `.env` said,
and `key_in_use` answers with the saved key when there is one. The photo registry hands the
saved keys over at start-up and whenever they change, so demo mode, research and photos
follow a key saved on the page without a restart.

v7 hosting: `STUDIO_PUBLIC_HOST` and `STUDIO_PASSCODE` let the studio run on a server for
someone else, behind an HTTPS proxy and a passcode; `hosting_problem` stops a start that would
put it online without one.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, PrivateAttr

PhotoMode = Literal["cloudflare", "fake", "none"]
ResearchMode = Literal["tavily", "fake", "none"]

# v7 hosting: the shortest passcode the studio accepts, and why it will not start.
MIN_PASSCODE_CHARS = 12
NO_PASSCODE = (
    "STUDIO_PUBLIC_HOST is set but STUDIO_PASSCODE is not: the studio will not answer on a "
    "public address without a passcode."
)
SHORT_PASSCODE = f"STUDIO_PASSCODE must be at least {MIN_PASSCODE_CHARS} characters."
BAD_PUBLIC_HOST = (
    "STUDIO_PUBLIC_HOST is a host name only, with no scheme, port or path, such as "
    "34-74-245-181.sslip.io."
)


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

    # v7 hosting, both empty on the designer's own computer: the host name the studio also
    # answers to behind an HTTPS proxy, and the passcode every visitor must enter. The passcode
    # is kept out of the settings' repr.
    public_host: str = ""
    passcode: str = Field(default="", repr=False)

    # v7: the keys saved on the Settings page, by field name, set by the photo registry.
    _saved_keys: dict[str, str] = PrivateAttr(default_factory=dict)

    def use_saved_keys(self, keys: Mapping[str, str]) -> None:
        """Take the keys saved on the Settings page, by field name; an empty one is not saved."""
        self._saved_keys = {field: value for field, value in keys.items() if value}

    def key_in_use(self, field: str) -> str:
        """The key a field stands for, as the studio uses it: the one saved on the Settings
        page, else the one in `.env`, else ""."""
        return self._saved_keys.get(field) or getattr(self, field)

    @property
    def demo_mode(self) -> bool:
        """With no model key, saved or in `.env`, the studio runs on fake models."""
        return not self.key_in_use("google_api_key")

    @property
    def research_mode(self) -> ResearchMode:
        if self.research_provider != "auto":
            return self.research_provider
        if self.key_in_use("tavily_api_key"):
            return "tavily"
        return "fake" if self.demo_mode else "none"

    @property
    def photo_mode(self) -> PhotoMode:
        """Who makes the photos, by v5's rule, which v6 keeps: Cloudflare whenever its keys are
        set (saved or in `.env`), else the stand-in ("fake") in demo mode, else no free source
        ("none"). The photo registry makes every model's photos with the stand-in only while
        this is "fake"."""
        if self.photo_provider != "auto":
            return self.photo_provider
        if self.key_in_use("cloudflare_account_id") and self.key_in_use("cloudflare_api_token"):
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
            public_host=get("STUDIO_PUBLIC_HOST").lower(),
            passcode=get("STUDIO_PASSCODE"),
        )

    def hosting_problem(self) -> str:
        """Why the studio must not start as configured for hosting (v7), or "" when it may: a
        public host needs a passcode, a passcode needs 12 characters or more, and a public host
        is a bare host name. On the designer's own computer both are empty and all is well."""
        host = self.public_host
        if host and any(mark in host for mark in ("/", ":", "@", " ")):
            return BAD_PUBLIC_HOST
        if host and not self.passcode:
            return NO_PASSCODE
        if self.passcode and len(self.passcode) < MIN_PASSCODE_CHARS:
            return SHORT_PASSCODE
        return ""
