"""The model catalogue (v6): every image model the studio can offer, read from data.

Providers are adapters; models are data. `catalogue.yaml`, beside this file, holds one entry
per model, and `load_catalogue` reads and checks it once, at start-up. An entry that does not
fit stops the studio, with the entry and the field named in the log, so a typo in a price
never reaches a photo.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.contracts import PriceBasis, ProviderId

__all__ = [
    "CATALOGUE_PATH",
    "CatalogueInvalid",
    "ImageModel",
    "PriceBasis",
    "ProviderId",
    "load_catalogue",
]

logger = logging.getLogger(__name__)

CATALOGUE_PATH = Path(__file__).with_name("catalogue.yaml")
# v6 Part B: the input tokens one product photo is expected to cost, for the estimate before a
# round. A photo of at most 2048 pixels comes to about a thousand on both paid providers; the
# cost after the round uses the tokens each provider reports.
INPUT_TOKENS_PER_PHOTO = 1000

CATALOGUE_INVALID = "The model catalogue is invalid: {entry}, field {field}: {problem}"
NOT_A_LIST = "The model catalogue is invalid: it needs a list of models under `models`."
DUPLICATE_ID = "the same id as an earlier entry"
NOT_A_CHOICE = "{value!r} is not one of {choices}"
NOT_AN_ENTRY = "an entry must be a mapping of fields"


class CatalogueInvalid(ValueError):
    """The catalogue file does not fit; the message names the entry and the field."""


class ImageModel(BaseModel):
    """One image model of one provider, with its preset options and its price."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)  # the provider's own model id
    provider: ProviderId
    label: str = Field(min_length=1)  # "GPT Image 2.5 Flare"
    price_basis: PriceBasis
    list_price_usd: float = Field(default=0.0, ge=0)  # a photo at the preset; 0 for free
    output_token_price_usd: float = Field(default=0.0, ge=0)  # a token, for usage-priced models
    options: dict[str, str] = Field(default_factory=dict)  # the preset: {"quality": "high"}
    option_choices: dict[str, list[str]] = Field(default_factory=dict)  # what Settings may pick
    # The list price for each choice of an option that changes it: {"image_size": {"1K": ...}}.
    option_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    max_parallel: int = Field(default=3, ge=1, le=12)  # photos asked for at once on this model
    checked: str = ""  # "2026-10-08": when the price was checked; empty when it was not
    # v6 Part B: how many product photos the model takes with the prompt (0: words only), and
    # what an input token costs, for the product photos it is sent.
    max_input_images: int = Field(default=0, ge=0, le=16)
    input_token_price_usd: float = Field(default=0.0, ge=0)

    @property
    def paid(self) -> bool:
        """Whether a photo from this model costs money."""
        return self.price_basis != "free_allowance"

    @property
    def short_id(self) -> str:
        """The id without its path, as the run page names it: "flux-2-klein-4b"."""
        return self.id.rsplit("/", 1)[-1]

    def preset(self, overrides: dict[str, str] | None = None) -> dict[str, str]:
        """The options a photo is asked for with: the preset, with each override that is one of
        the option's choices (an override for an option without choices is ignored)."""
        options = dict(self.options)
        for name, value in (overrides or {}).items():
            if value in self.option_choices.get(name, []):
                options[name] = value
        return options

    def price_for(self, options: dict[str, str] | None = None) -> float | None:
        """The list price of one photo with these options: 0 for a free model, the option's own
        price where the catalogue gives one, else the list price; None when it is not known."""
        if not self.paid:
            return 0.0
        chosen = options if options is not None else self.options
        for name, prices in self.option_prices.items():
            value = chosen.get(name)
            if value in prices:
                return prices[value]
        return self.list_price_usd or None

    def input_cost(self, photos: int) -> float:
        """What sending `photos` product photos with one request is expected to cost, from the
        catalogue's input token price: 0 for a free model or for none."""
        if not self.paid or photos <= 0:
            return 0.0
        return photos * INPUT_TOKENS_PER_PHOTO * self.input_token_price_usd


def load_catalogue(path: Path = CATALOGUE_PATH) -> list[ImageModel]:
    """Every model in the catalogue, in its order. Raises `CatalogueInvalid`, after logging the
    same message, when an entry does not fit: the studio does not start on a bad catalogue."""
    try:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise CatalogueInvalid(f"The model catalogue could not be read: {error}") from error
        entries = data.get("models") if isinstance(data, dict) else None
        if not isinstance(entries, list) or not entries:
            raise CatalogueInvalid(NOT_A_LIST)
        models: list[ImageModel] = []
        for number, entry in enumerate(entries, start=1):
            model = _checked_entry(number, entry)
            if any(known.id == model.id for known in models):
                raise CatalogueInvalid(_problem(number, entry, "id", DUPLICATE_ID))
            models.append(model)
        return models
    except CatalogueInvalid as error:
        logger.error("%s", error)
        raise


def _checked_entry(number: int, entry: Any) -> ImageModel:
    """One entry as a model, or `CatalogueInvalid` naming its first problem."""
    if not isinstance(entry, dict):
        raise CatalogueInvalid(_problem(number, entry, "(all)", NOT_AN_ENTRY))
    try:
        model = ImageModel.model_validate(entry)
    except ValidationError as error:
        first = error.errors()[0]
        field = ".".join(str(part) for part in first["loc"]) or "(all)"
        raise CatalogueInvalid(_problem(number, entry, field, first["msg"])) from None
    for name, value in model.options.items():
        choices = model.option_choices.get(name)
        if choices and value not in choices:
            problem = NOT_A_CHOICE.format(value=value, choices=", ".join(choices))
            raise CatalogueInvalid(_problem(number, entry, f"options.{name}", problem))
    return model


def _problem(number: int, entry: Any, field: str, problem: str) -> str:
    """The catalogue's error line: the entry by its number and id, the field and the problem."""
    model_id = entry.get("id") if isinstance(entry, dict) else None
    name = f"entry {number} ({model_id})" if model_id else f"entry {number}"
    return CATALOGUE_INVALID.format(entry=name, field=field, problem=problem)
