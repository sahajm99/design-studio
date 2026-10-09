"""Tests for the photo providers: the deterministic fake and the Cloudflare adapter.

No network: the Cloudflare tests run against an httpx.MockTransport.
"""

from __future__ import annotations

import base64
import io
from pathlib import Path

import httpx
import pytest
from PIL import Image

from studio.config import Settings
from studio.contracts import DEFAULT_PHOTO_MODEL_ID
from studio.photos.base import PhotoUnavailable
from studio.photos.catalogue import load_catalogue
from studio.photos.cloudflare import CloudflarePhotoProvider
from studio.photos.fake import FakePhotoProvider
from studio.photos.registry import PhotoRegistry
from studio.store import Store

SECRET_TOKEN = "super-secret-token-value"
SECRET_ACCOUNT = "super-secret-account-id"


def _encoded_png(size: tuple[int, int] = (64, 64), colour: tuple[int, int, int] = (10, 20, 30)) -> str:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def _cloudflare_envelope(result: dict | None, *, success: bool = True, errors: list | None = None) -> dict:
    return {"result": result, "success": success, "errors": errors or [], "messages": []}


# ------------------------------------------------------------------------- fake


async def test_fake_photo_size_and_determinism(tmp_path: Path) -> None:
    provider = FakePhotoProvider()
    out_path = tmp_path / "nested" / "photo.png"

    result = await provider.generate("a calm minimal studio shot", 300, 200, out_path)

    assert result.provider == "fake"
    assert result.prompt == "a calm minimal studio shot"
    assert result.path == str(out_path)
    assert out_path.exists()

    with Image.open(out_path) as image:
        assert image.size == (300, 200)
        assert image.format == "PNG"
    first_bytes = out_path.read_bytes()

    second_path = tmp_path / "second.png"
    await provider.generate("a calm minimal studio shot", 300, 200, second_path)
    assert second_path.read_bytes() == first_bytes

    third_path = tmp_path / "third.png"
    await provider.generate("a bold loud busy photo", 300, 200, third_path)
    assert third_path.read_bytes() != first_bytes


async def test_fake_photo_follows_the_backdrop_colour_in_the_prompt(tmp_path: Path) -> None:
    provider = FakePhotoProvider()
    cases = [("light", "#F3F4F7"), ("dark", "#18181B")]

    for mode, hex_colour in cases:
        prompt = (
            "a calm, confident headshot for a dental brand. Set on a seamless, evenly lit "
            f"{mode} backdrop close to the colour {hex_colour}, with the subject centred and "
            "empty space around it."
        )
        out_path = tmp_path / f"{mode}.png"

        await provider.generate(prompt, 300, 200, out_path)

        expected = tuple(int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
        with Image.open(out_path) as image:
            width, height = image.size
            corners = [
                image.getpixel((0, 0)),
                image.getpixel((width - 1, 0)),
                image.getpixel((0, height - 1)),
                image.getpixel((width - 1, height - 1)),
            ]
            centre = image.getpixel((width // 2, height // 2))

        for corner in corners:
            for channel, expected_channel in zip(corner, expected):
                assert abs(channel - expected_channel) <= 6

        assert any(abs(centre[channel] - corners[0][channel]) >= 12 for channel in range(3))


async def test_fake_photo_without_a_colour_is_dark(tmp_path: Path) -> None:
    provider = FakePhotoProvider()
    out_path = tmp_path / "no_colour.png"

    await provider.generate("a calm, confident headshot for a dental brand", 300, 200, out_path)

    with Image.open(out_path) as image:
        width, height = image.size
        corners = [
            image.getpixel((0, 0)),
            image.getpixel((width - 1, 0)),
            image.getpixel((0, height - 1)),
            image.getpixel((width - 1, height - 1)),
        ]

    for corner in corners:
        assert all(channel < 60 for channel in corner)


# ------------------------------------------------------------------- cloudflare


async def test_cloudflare_success_writes_an_image(tmp_path: Path) -> None:
    encoded = _encoded_png()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_cloudflare_envelope({"image": encoded}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = CloudflarePhotoProvider(
            account_id="acct-123",
            api_token="token-secret-xyz",
            model="@cf/black-forest-labs/flux-2-klein-4b",
            client=client,
        )
        out_path = tmp_path / "photo.png"
        result = await provider.generate("a minimal studio product shot", 1080, 1350, out_path)

    assert result.provider == "cloudflare:flux-2-klein-4b"
    assert result.path == str(out_path)
    with Image.open(out_path) as image:
        assert image.format == "PNG"

    assert len(seen) == 1
    request = seen[0]
    assert request.url.path == "/client/v4/accounts/acct-123/ai/run/@cf/black-forest-labs/flux-2-klein-4b"
    assert request.headers["authorization"] == "Bearer token-secret-xyz"
    assert request.headers["content-type"].startswith("multipart/form-data")
    body = request.content
    assert b'name="prompt"' in body
    assert b"a minimal studio product shot No text, no logos, no watermark." in body
    # width/height are rounded to the nearest multiple of 32, not passed through raw
    assert b"1088" in body
    assert b"1344" in body
    assert b"\r\n1080\r\n" not in body
    assert b"\r\n1350\r\n" not in body


async def test_cloudflare_without_credentials(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request should be sent without both credentials")

    cases = [
        ("", ""),
        ("", "token"),
        ("account", ""),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        for account_id, api_token in cases:
            provider = CloudflarePhotoProvider(
                account_id=account_id, api_token=api_token, model="m", client=client
            )
            with pytest.raises(PhotoUnavailable, match="^Cloudflare credentials are not set.$"):
                await provider.generate("a prompt", 100, 100, tmp_path / "out.png")


@pytest.mark.parametrize("status", [401, 403])
async def test_cloudflare_rejected_credentials(tmp_path: Path, status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, json=_cloudflare_envelope(None, success=False, errors=[{"message": "bad auth"}])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match="^Cloudflare rejected the credentials.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out.png")


async def test_cloudflare_allowance_used_up(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=_cloudflare_envelope(None, success=False))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match="^Cloudflare's free daily allowance is used up.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out.png")


async def test_cloudflare_error_with_message(tmp_path: Path) -> None:
    def with_message(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            json=_cloudflare_envelope(
                None, success=False, errors=[{"message": "Internal error doing the thing"}]
            ),
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(with_message)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable) as exc_info:
            await provider.generate("a prompt", 100, 100, tmp_path / "out.png")
    message = str(exc_info.value)
    assert message == "Cloudflare returned an error (500). Internal error doing the thing"

    def without_message(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json=_cloudflare_envelope(None, success=False, errors=[]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(without_message)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match=r"^Cloudflare returned an error \(502\)\.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out2.png")


async def test_cloudflare_timeout(tmp_path: Path) -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match="^Cloudflare did not answer within 180 seconds.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out.png")

    def connect_error(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(connect_error)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match="^Cloudflare could not be reached.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out2.png")


async def test_cloudflare_no_image(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_cloudflare_envelope({}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = CloudflarePhotoProvider(account_id="acct", api_token="token", model="m", client=client)
        with pytest.raises(PhotoUnavailable, match="^Cloudflare returned no image.$"):
            await provider.generate("a prompt", 100, 100, tmp_path / "out.png")


async def test_cloudflare_messages_never_hold_the_token(tmp_path: Path) -> None:
    def unauthorised(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json=_cloudflare_envelope(None, success=False, errors=[{"message": "Authentication error"}])
        )

    def rate_limited(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json=_cloudflare_envelope(None, success=False))

    def server_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500, json=_cloudflare_envelope(None, success=False, errors=[{"message": "Internal error"}])
        )

    def no_image(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_cloudflare_envelope({}))

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    for handler in (unauthorised, rate_limited, server_error, no_image, timeout):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = CloudflarePhotoProvider(
                account_id=SECRET_ACCOUNT, api_token=SECRET_TOKEN, model="m", client=client
            )
            with pytest.raises(PhotoUnavailable) as exc_info:
                await provider.generate("a prompt", 100, 100, tmp_path / "out.png")
            message = str(exc_info.value)
            assert SECRET_TOKEN not in message
            assert SECRET_ACCOUNT not in message

    provider = CloudflarePhotoProvider(account_id="", api_token=SECRET_TOKEN, model="m")
    with pytest.raises(PhotoUnavailable) as exc_info:
        await provider.generate("a prompt", 100, 100, tmp_path / "out2.png")
    assert SECRET_TOKEN not in str(exc_info.value)


# -------------------------------------------------------------------- the registry


def test_registry_follows_photo_mode(settings: Settings, store: Store) -> None:
    """The registry's adapter for the free model follows the photo mode, as v5's factory did:
    Cloudflare with its keys, the stand-in for the tests' override, and no model for "none"."""
    flux = DEFAULT_PHOTO_MODEL_ID
    cloudflare_settings = settings.model_copy(
        update={
            "photo_provider": "cloudflare",
            "cloudflare_account_id": "acct123",
            "cloudflare_api_token": "token123",
        }
    )
    provider = PhotoRegistry(load_catalogue(), cloudflare_settings, store).adapter_for(flux)
    assert isinstance(provider, CloudflarePhotoProvider)
    assert provider.name == "cloudflare"
    assert provider.account_id == "acct123"
    assert provider.api_token == "token123"
    assert provider.model == flux

    fake_settings = settings.model_copy(update={"photo_provider": "fake"})
    provider = PhotoRegistry(load_catalogue(), fake_settings, store).adapter_for(flux)
    assert isinstance(provider, FakePhotoProvider)
    assert provider.name == "fake"

    none_settings = settings.model_copy(update={"photo_provider": "none"})
    assert PhotoRegistry(load_catalogue(), none_settings, store).usable_models() == []
