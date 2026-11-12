from __future__ import annotations

import threading

from typing import TYPE_CHECKING

import requests

from cachecontrol import CacheControlAdapter
from cachecontrol.caches import SeparateBodyFileCache
from cachecontrol.controller import CacheController
from cachecontrol.serialize import Serializer
from urllib3 import HTTPResponse

from poetry.utils.authenticator import AtomicSeparateBodyFileCache
from poetry.utils.authenticator import Authenticator


if TYPE_CHECKING:
    from pathlib import Path

    from tests.conftest import Config


KEY = "https://foo.bar/simple/setuptools/"
METADATA = b"cc=4,metadata"
BODY = b"<html><a href='setuptools-84.0.0-py3-none-any.whl'>setuptools</a></html>"


def test_entry_is_not_visible_before_the_body_is_written(tmp_path: Path) -> None:
    cache = AtomicSeparateBodyFileCache(tmp_path)

    cache.set(KEY, METADATA)

    assert cache.get(KEY) is None

    cache.set_body(KEY, BODY)

    assert cache.get(KEY) == METADATA
    body_file = cache.get_body(KEY)
    assert body_file is not None
    assert body_file.read() == BODY


def test_concurrent_lookup_never_sees_an_incomplete_entry(tmp_path: Path) -> None:
    """
    A lookup running while an entry is being published must either miss or see
    the complete entry, never metadata paired with a missing body.
    """
    cache = AtomicSeparateBodyFileCache(tmp_path)
    reader = AtomicSeparateBodyFileCache(tmp_path)

    metadata_published = threading.Event()
    publish_body = threading.Event()

    def writer() -> None:
        cache.set(KEY, METADATA)
        metadata_published.set()
        publish_body.wait(timeout=10)
        cache.set_body(KEY, BODY)

    thread = threading.Thread(target=writer)
    thread.start()
    assert metadata_published.wait(timeout=10)

    assert reader.get(KEY) is None
    assert reader.get_body(KEY) is None

    publish_body.set()
    thread.join(timeout=10)
    assert not thread.is_alive()

    assert reader.get(KEY) == METADATA
    body_file = reader.get_body(KEY)
    assert body_file is not None
    assert body_file.read() == BODY


def test_concurrent_lookup_between_body_and_metadata_is_a_miss(
    tmp_path: Path,
) -> None:
    """
    The publication window has two halves. The body is written first, so a
    lookup may also land after the body write but before the metadata write.
    """
    cache = AtomicSeparateBodyFileCache(tmp_path)
    reader = AtomicSeparateBodyFileCache(tmp_path)

    cache.set(KEY, METADATA)
    # Bypass the atomic cache to emulate the intermediate state inside
    # ``set_body()``: the body is on disk, the metadata is not published yet.
    SeparateBodyFileCache.set_body(cache, KEY, BODY)

    assert reader.get_body(KEY) is not None
    assert reader.get(KEY) is None

    cache.set_body(KEY, BODY)

    assert reader.get(KEY) == METADATA


def test_upstream_cache_exposes_the_same_window(tmp_path: Path) -> None:
    """
    Guards the premise of this fix: the unmodified cache publishes metadata
    before the body, so a lookup in that window sees an entry with no body.
    """
    cache = SeparateBodyFileCache(tmp_path)
    reader = SeparateBodyFileCache(tmp_path)

    cache.set(KEY, METADATA)

    assert reader.get(KEY) == METADATA
    assert reader.get_body(KEY) is None

    cache.set_body(KEY, BODY)


def test_entry_without_body_is_treated_as_a_miss(tmp_path: Path) -> None:
    """
    Metadata left behind without its body, as happens when a write is
    interrupted, must not be served.
    """
    cache = AtomicSeparateBodyFileCache(tmp_path)
    SeparateBodyFileCache.set(cache, KEY, METADATA)

    assert cache.get(KEY) is None


def test_revalidation_without_a_body_keeps_the_existing_entry(
    tmp_path: Path,
) -> None:
    cache = AtomicSeparateBodyFileCache(tmp_path)

    cache.set(KEY, METADATA)
    cache.set_body(KEY, BODY)
    cache.set(KEY, b"cc=4,revalidated")

    assert cache.get(KEY) == b"cc=4,revalidated"
    body_file = cache.get_body(KEY)
    assert body_file is not None
    assert body_file.read() == BODY


def test_delete_removes_a_pending_entry(tmp_path: Path) -> None:
    cache = AtomicSeparateBodyFileCache(tmp_path)

    cache.set(KEY, METADATA)
    cache.delete(KEY)
    cache.set_body(KEY, BODY)

    assert cache.get(KEY) is None


def test_controller_lookup_during_publication_is_a_miss(tmp_path: Path) -> None:
    """
    The regression from #11048: a lookup between the two writes used to
    deserialize the metadata into a response with an empty body, which a package
    index reads as "no releases at all".
    """
    cache = AtomicSeparateBodyFileCache(tmp_path)
    controller = CacheController(cache, serializer=Serializer())
    request = requests.Request("GET", KEY).prepare()
    response = HTTPResponse(
        body=BODY,
        headers={"Date": "Wed, 01 Jan 2025 00:00:00 GMT", "ETag": '"abc"'},
        status=200,
        preload_content=False,
    )

    # What CacheController._cache_set() does, paused after the metadata call.
    cache.set(KEY, controller.serializer.dumps(request, response, b""), expires=60)

    assert controller._load_from_cache(request) is None

    cache.set_body(KEY, BODY)

    cached_response = controller._load_from_cache(request)
    assert cached_response is not None
    assert cached_response.read() == BODY


def test_authenticator_uses_atomic_cache(config: Config) -> None:
    assert isinstance(
        Authenticator(config, disable_cache=False)._cache_control,
        AtomicSeparateBodyFileCache,
    )


def test_authenticator_lookup_during_publication_is_a_cache_miss(
    config: Config,
) -> None:
    """
    The reported symptom, exercised through the cache the authenticator builds:
    a lookup landing between the metadata and body writes must not return an
    empty package index.
    """
    url = "https://foo.bar/simple/setuptools/"

    authenticator = Authenticator(config)
    cache = authenticator._cache_control
    assert isinstance(cache, AtomicSeparateBodyFileCache)
    adapter = authenticator.get_session(url).get_adapter(url)
    assert isinstance(adapter, CacheControlAdapter)
    controller = adapter.controller

    request = requests.Request("GET", url).prepare()
    response = HTTPResponse(
        body=BODY,
        headers={
            "Date": "Wed, 01 Jan 2025 00:00:00 GMT",
            "ETag": '"abc"',
            "Cache-Control": "max-age=60",
        },
        status=200,
        preload_content=False,
    )
    cache_url = controller.cache_url(url)
    cache.set(
        cache_url,
        controller.serializer.dumps(request, response, b""),
        expires=60,
    )

    assert controller._load_from_cache(request) is None

    cache.set_body(cache_url, BODY)

    cached_response = controller._load_from_cache(request)
    assert cached_response is not None
    assert cached_response.read() == BODY
