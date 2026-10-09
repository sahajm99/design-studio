"""The photo registry (v6): which image models can be used now, and for each one its adapter,
key, options, price and daily limits.

Workflows ask the registry, never a provider directly: a round names a model id, and the
registry turns it into a working adapter with its key. It is built once, at start-up, from
the catalogue, the keys in `.env` and the keys saved on the Settings page (a saved key wins
over the same provider's key in `.env`), and it reads the photo settings from the store each
time, so a change on the Settings page applies to the next photo.

Free by default: a session with no model of its own uses the studio's default, FLUX.2 klein
out of the box, and nothing falls back to a paid model on its own. In demo mode the stand-in
makes every model's photos, so no image model is called and no key is used.

The daily limits are UTC days. The store counts the photos saved so far; the registry also
counts, in memory, the paid photos asked for and not answered yet (held) and those made but
not saved yet (their round is still being reviewed), so photos asked for at once cannot
together pass a limit.

v6 Part B: `input_limit` says how many product photos a model takes with the prompt, from its
catalogue entry (0: words only); in demo mode the stand-in takes three for every model, so
the whole product-photo path shows without a key. Estimates add the product photos' input
cost.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from typing import Literal

from pydantic import ValidationError

from studio.config import Settings
from studio.contracts import PhotoSettings, Sample
from studio.photos.base import (
    PROVIDER_LABELS,
    KeyCheck,
    LimitReached,
    PhotoProvider,
    PhotoUnavailable,
)
from studio.photos.catalogue import ImageModel, ProviderId
from studio.photos.cloudflare import CloudflarePhotoProvider
from studio.photos.fake import MAX_INPUT_IMAGES as STAND_IN_INPUT_IMAGES
from studio.photos.fake import FakePhotoProvider
from studio.photos.google import GooglePhotoProvider
from studio.photos.openai import OpenAIPhotoProvider
from studio.secrets import (
    ProviderKeys,
    last_four,
    read_saved_keys,
    redact,
    save_provider_keys,
)
from studio.store import Store

logger = logging.getLogger(__name__)

# The settings key that holds the photo settings, as plain JSON.
PHOTO_SETTINGS = "photo_settings"
# The spend check's allowance for rounding.
_CENT_FRACTION = 1e-9

KeySource = Literal["saved", "env", "none"]

NO_PHOTO_MODEL = "No photo model can be used. Set up a provider's key, or choose another model."
UNKNOWN_MODEL = "The studio does not know the photo model {model_id}."
MODEL_GONE = "{label} is no longer available; this round used {default}."
PHOTO_LIMIT = (
    "Today's limit for {provider} is reached ({photos}). "
    "Raise it in Settings or choose another model."
)
SPEND_LIMIT = (
    "Today's spend limit is reached (${limit:.2f}). Raise it in Settings or choose a free model."
)
# A model's price beside its name, before a photo: in the pickers and the estimates.
PRICE_FREE = "{label} · free"
PRICE_ABOUT = "{label} · about ${price:.2f} a photo"
PRICE_LISTED = "{label} · ${price:.2f} a photo"
PRICE_NOT_CHECKED = "{label} · price not checked"
# A photo's cost, after it was made: on its card and in the round's total.
COST_FREE = "free"
COST_UNKNOWN = "cost unknown"
COST_ABOUT = "about ${cost:.2f}"
COST_EXACT = "${cost:.3f}"
ROUND_TOTAL = "${total:.2f}"
BADGE = "{label} · {cost}"
SETTINGS_UNREADABLE = "The photo settings could not be read; using the defaults."


@dataclass(frozen=True)
class ResolvedModel:
    """The model a round uses, and the line saying why, when it is not the session's own."""

    model: ImageModel
    note: str = ""


@dataclass(frozen=True)
class _Pending:
    """A paid photo the store does not count yet: its provider, its cost and its UTC day."""

    provider: ProviderId
    cost: float
    day: date


class PhotoRegistry:
    """The studio's image models, their keys, options, prices and limits."""

    def __init__(
        self,
        catalogue: Iterable[ImageModel],
        settings: Settings,
        store: Store,
        *,
        secret: bytes | None = None,
    ) -> None:
        self._catalogue = list(catalogue)
        self._by_id = {model.id: model for model in self._catalogue}
        self._settings = settings
        self._store = store
        self._secret = secret
        self._saved_keys, self._keys_unreadable = read_saved_keys(store, secret)
        self._held: dict[str, _Pending] = {}  # by sample id: asked for, not answered yet
        self._unsaved: dict[str, _Pending] = {}  # by sample id: made, not saved yet
        self._stand_in = FakePhotoProvider()

    # ------------------------------------------------------------ the catalogue

    @property
    def catalogue(self) -> list[ImageModel]:
        """Every model the studio knows, in the catalogue's order."""
        return list(self._catalogue)

    def model(self, model_id: str) -> ImageModel | None:
        """The catalogue's model with this id, or None."""
        return self._by_id.get(model_id)

    @property
    def stand_in(self) -> bool:
        """Whether the stand-in makes every model's photos: demo mode, or the tests' override."""
        mode = self._settings.photo_provider
        return mode == "fake" or (mode == "auto" and self._settings.demo_mode)

    def input_limit(self, model: ImageModel) -> int:
        """How many product photos the model takes with the prompt (v6 Part B): its catalogue
        entry's `max_input_images`, 0 for a words-only model; the stand-in's three for every
        model in demo mode, since the stand-in makes the photo."""
        return STAND_IN_INPUT_IMAGES if self.stand_in else model.max_input_images

    def takes_photos(self, model: ImageModel) -> bool:
        """Whether the model can see product photos (v6 Part B)."""
        return self.input_limit(model) > 0

    # ------------------------------------------------------------ settings

    def photo_settings(self) -> PhotoSettings:
        """The photo settings as saved, or the defaults when none are, or they cannot be read."""
        data = self._store.get_setting(PHOTO_SETTINGS)
        if data is None:
            return PhotoSettings()
        try:
            return PhotoSettings.model_validate(data)
        except ValidationError:
            logger.warning(SETTINGS_UNREADABLE)
            return PhotoSettings()

    def save_photo_settings(self, photo_settings: PhotoSettings) -> None:
        """Save the photo settings; the next photo follows them."""
        self._store.set_setting(PHOTO_SETTINGS, photo_settings)

    # ------------------------------------------------------------ keys

    def keys(self) -> ProviderKeys:
        """Each key in use: the saved one where there is one, else the one in `.env`."""
        env = self._env_keys()
        saved = self._saved_keys
        return ProviderKeys(
            **{
                field: getattr(saved, field) or getattr(env, field)
                for field in ProviderKeys.model_fields
            }
        )

    @property
    def keys_unreadable(self) -> bool:
        """Whether keys were saved that this secret cannot read (the `.env` keys still work)."""
        return self._keys_unreadable

    def has_key(self, provider: ProviderId) -> bool:
        """Whether the provider has every key it needs, saved or in `.env`."""
        return all(_provider_fields(self.keys(), provider))

    def key_source(self, provider: ProviderId) -> KeySource:
        """Where the provider's key in use comes from: saved here, `.env`, or nowhere."""
        if any(_provider_fields(self._saved_keys, provider)):
            return "saved"
        if all(_provider_fields(self._env_keys(), provider)):
            return "env"
        return "none"

    def key_ending(self, provider: ProviderId) -> str:
        """The last four characters of the provider's saved key, the only part ever shown."""
        fields = _provider_fields(self._saved_keys, provider)
        return last_four(fields[-1]) if fields else ""

    def saved_keys(self) -> ProviderKeys:
        """The keys saved on the Settings page alone (none of `.env`), for the page to change one
        provider's and save them all again. Never sent to a page."""
        return self._saved_keys.model_copy()

    def set_saved_keys(self, keys: ProviderKeys) -> None:
        """Encrypt and save these as the saved keys (all of them, empty ones removed), and use
        them from the next photo. Raises SecretUnavailable without a secret."""
        save_provider_keys(self._store, self._secret, keys)
        self._saved_keys, self._keys_unreadable = keys, False

    def redact(self, text: str) -> str:
        """The text with every key the studio knows removed, for a log line or a card."""
        return redact(text, [*self._env_keys().values(), *self._saved_keys.values()])

    async def test_key(self, provider: ProviderId, typed: ProviderKeys | None = None) -> KeyCheck:
        """The provider's free key check; in demo mode, no call at all. It checks the key in use,
        or, when `typed` holds keys typed on the Settings page and not saved, those instead,
        field by field (a field left empty keeps the key in use). Nothing is saved."""
        if self.stand_in:
            adapter: PhotoProvider = self._stand_in
        else:
            keys = self.keys()
            if typed is not None:
                keys = ProviderKeys(
                    **{
                        field: getattr(typed, field) or getattr(keys, field)
                        for field in ProviderKeys.model_fields
                    }
                )
            adapter = self._adapter(provider, keys)
        check = await adapter.test_key()
        known = [*self._env_keys().values(), *self._saved_keys.values()]
        if typed is not None:
            known += typed.values()
        return check.model_copy(update={"message": redact(check.message, known)})

    # ------------------------------------------------------------ which models

    def usable_models(self) -> list[ImageModel]:
        """The models a round may use now: those offered on the Settings page (all of them
        while none is ticked) whose provider has a key. In demo mode the free ones need none."""
        if self._settings.photo_provider == "none":
            return []
        enabled = set(self.photo_settings().enabled_model_ids)
        return [
            model
            for model in self._catalogue
            if (not enabled or model.id in enabled)
            and (self.has_key(model.provider) or (self.stand_in and not model.paid))
        ]

    def has_photo_source(self) -> bool:
        """Whether any model can make a photo now."""
        return bool(self.usable_models())

    def default_model(self) -> ImageModel | None:
        """The studio's default model while it can be used, else the first free one that can;
        never a paid model it was not set to. None when no such model can be used."""
        usable = self.usable_models()
        chosen = self.photo_settings().default_model_id
        return next((m for m in usable if m.id == chosen), None) or _first_free(usable)

    def fallback_model(self) -> ImageModel | None:
        """The free model an auto session finishes on when its paid model stops: the default
        when it is free, else the first free model that can be used."""
        default = self.default_model()
        if default is not None and not default.paid:
            return default
        return _first_free(self.usable_models())

    def resolve(self, model_id: str) -> ResolvedModel:
        """The model a round asked for with `model_id` uses: that model while it can be used;
        else the default, with the line saying so (no line when it asked for the default).
        Raises PhotoUnavailable when no model can be used."""
        usable = self.usable_models()
        if model_id:
            asked = next((model for model in usable if model.id == model_id), None)
            if asked is not None:
                return ResolvedModel(asked)
        default = self.default_model()
        if default is None:
            raise PhotoUnavailable(NO_PHOTO_MODEL)
        if not model_id or model_id == default.id:
            return ResolvedModel(default)
        gone = self.model(model_id)
        label = gone.label if gone is not None else model_id
        return ResolvedModel(default, MODEL_GONE.format(label=label, default=default.label))

    def adapter_for(self, model_id: str) -> PhotoProvider:
        """The adapter that makes the model's photos, with its provider's key in use; the
        stand-in for every model in demo mode. Raises PhotoUnavailable for an unknown id."""
        model = self.model(model_id)
        if model is None:
            raise PhotoUnavailable(UNKNOWN_MODEL.format(model_id=model_id))
        return self._stand_in if self.stand_in else self._adapter(model.provider)

    def options_for(self, model: ImageModel) -> dict[str, str]:
        """The options the model's photos are asked for with: its preset, with the Settings
        page's choices."""
        return model.preset(self.photo_settings().options.get(model.id))

    def describe(self, model: ImageModel, options: dict[str, str]) -> str:
        """The model as a run step names it: "openai:gpt-image-2.5-flare (high)"; the stand-in
        with the model it stands in for."""
        if self.stand_in:
            return f"fake ({model.label})"
        name = f"{model.provider}:{model.short_id}"
        values = ", ".join(options.values())
        return f"{name} ({values})" if values else name

    # ------------------------------------------------------------ prices and costs

    def estimate(
        self, model: ImageModel, options: dict[str, str] | None = None, input_images: int = 0
    ) -> float | None:
        """What one photo is expected to cost, from the catalogue; None when not known. With
        product photos (v6 Part B), as many as the model takes of `input_images` add their
        input cost."""
        price = model.price_for(options if options is not None else self.options_for(model))
        if price is None:
            return None
        return price + model.input_cost(min(input_images, self.input_limit(model)))

    def price_label(self, model: ImageModel) -> str:
        """The model with its price, for a picker: "GPT Image 2.5 Flare · about $0.05 a photo",
        "Nano Banana 2.1 · $0.05 a photo", "FLUX.2 klein · free"."""
        if not model.paid:
            return PRICE_FREE.format(label=model.label)
        price = self.estimate(model)
        if price is None or not model.checked:
            return PRICE_NOT_CHECKED.format(label=model.label)
        template = PRICE_ABOUT if model.price_basis == "usage" else PRICE_LISTED
        return template.format(label=model.label, price=price)

    def cost_label(self, sample: Sample) -> str:
        """What the sample's photo cost: "$0.048", "free", or "about $0.05" for a usage-priced
        model that reported no usage; "cost unknown" when it is not known."""
        if sample.cost_usd is None:
            return COST_UNKNOWN
        if sample.cost_usd == 0:
            return COST_FREE
        model = self.model(sample.model_id)
        if sample.cost_basis == "list_price" and model is not None and model.price_basis == "usage":
            return COST_ABOUT.format(cost=sample.cost_usd)
        return COST_EXACT.format(cost=sample.cost_usd)

    def badge(self, sample: Sample) -> str:
        """The sample card's badge: the model's label and the photo's cost, the label alone for
        a photo that was not made, "" for a sample no model made (an upload, an old sample)."""
        if not sample.model_id:
            return ""
        model = self.model(sample.model_id)
        label = model.label if model is not None else sample.model_id
        if not sample.image_path:
            return label
        return BADGE.format(label=label, cost=self.cost_label(sample))

    def round_cost(self, samples: Iterable[Sample]) -> str:
        """The total cost of a round's photos: "$0.14", "free", or "cost unknown" when a photo's
        cost is not known."""
        costs = [sample.cost_usd for sample in samples if sample.image_path]
        if any(cost is None for cost in costs):
            return COST_UNKNOWN
        total = sum(cost for cost in costs if cost is not None)
        return COST_FREE if total == 0 else ROUND_TOTAL.format(total=total)

    # ------------------------------------------------------------ daily limits

    def check_limits(
        self, model: ImageModel, options: dict[str, str] | None = None, input_images: int = 0
    ) -> None:
        """Raise LimitReached when one more photo from the model would pass today's photo limit
        for its provider, or the studio's spend limit with this photo's estimate (its product
        photos' input cost included). Free models and the stand-in have no limit."""
        if self.stand_in or not model.paid:
            return
        photo_settings = self.photo_settings()
        since = _midnight_utc()
        pending = self._pending_today()
        limit = photo_settings.daily_photo_limit.get(model.provider)
        if limit is not None:
            made = self._store.count_photos_since(since, model.provider)
            made += sum(1 for item in pending if item.provider == model.provider)
            if made + 1 > limit:
                provider = PROVIDER_LABELS[model.provider]
                photos = f"{limit} photo" if limit == 1 else f"{limit} photos"
                raise LimitReached(PHOTO_LIMIT.format(provider=provider, photos=photos))
        spent = self._store.spend_since(since) + sum(item.cost for item in pending)
        estimate = self.estimate(model, options, input_images) or 0.0
        if spent + estimate > photo_settings.daily_spend_limit_usd + _CENT_FRACTION:
            raise LimitReached(SPEND_LIMIT.format(limit=photo_settings.daily_spend_limit_usd))

    def hold(
        self, sample: Sample, model: ImageModel, options: dict[str, str], input_images: int = 0
    ) -> None:
        """Check the limits for the sample's photo and count it as asked for, until `record`.
        Raises LimitReached, holding nothing, when a limit is reached."""
        self.check_limits(model, options, input_images)
        if self.stand_in or not model.paid:
            return
        cost = self.estimate(model, options, input_images) or 0.0
        self._held[sample.id] = _Pending(model.provider, cost, _today())

    def record(self, sample: Sample) -> None:
        """Count the sample's photo as made, at its cost, until the store has it; or drop what
        `hold` counted when no photo was made."""
        held = self._held.pop(sample.id, None)
        if held is None or not sample.image_path:
            return
        cost = sample.cost_usd if sample.cost_usd is not None else held.cost
        self._unsaved[sample.id] = replace(held, cost=cost)

    def _pending_today(self) -> list[_Pending]:
        """The paid photos of today the store does not count yet; saved ones are let go."""
        today = _today()
        for sample_id, item in list(self._unsaved.items()):
            if item.day != today or self._store.get_sample(sample_id) is not None:
                del self._unsaved[sample_id]
        held = [item for item in self._held.values() if item.day == today]
        return [*held, *self._unsaved.values()]

    # ------------------------------------------------------------ helpers

    def _env_keys(self) -> ProviderKeys:
        settings = self._settings
        return ProviderKeys(
            cloudflare_account_id=settings.cloudflare_account_id,
            cloudflare_api_token=settings.cloudflare_api_token,
            openai_api_key=settings.openai_api_key,
            google_image_api_key=settings.google_image_api_key,
        )

    def _adapter(self, provider: ProviderId, keys: ProviderKeys | None = None) -> PhotoProvider:
        """A real adapter for the provider, with the key in use now, or with `keys`."""
        keys = keys if keys is not None else self.keys()
        if provider == "cloudflare":
            return CloudflarePhotoProvider(
                account_id=keys.cloudflare_account_id,
                api_token=keys.cloudflare_api_token,
                model=self._settings.cloudflare_image_model,
            )
        if provider == "openai":
            return OpenAIPhotoProvider(keys.openai_api_key)
        if provider == "google":
            return GooglePhotoProvider(keys.google_image_api_key)
        return self._stand_in


def _provider_fields(keys: ProviderKeys, provider: ProviderId) -> list[str]:
    """The provider's key fields in `keys`: Cloudflare's account id and token, one key for the
    others, none for the stand-in."""
    if provider == "cloudflare":
        return [keys.cloudflare_account_id, keys.cloudflare_api_token]
    if provider == "openai":
        return [keys.openai_api_key]
    if provider == "google":
        return [keys.google_image_api_key]
    return []


def _first_free(models: Iterable[ImageModel]) -> ImageModel | None:
    return next((model for model in models if not model.paid), None)


def _midnight_utc() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)


def _today() -> date:
    return datetime.now(timezone.utc).date()
