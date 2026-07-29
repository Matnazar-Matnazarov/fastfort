"""Tests for the admin registry, key derivation and the hook dispatcher."""

from __future__ import annotations

import pytest

from fastfort.core.exceptions import ConfigurationError, RegistrationError
from fastfort.core.hooks import Hook, HookRegistry
from fastfort.core.registry import AdminRegistry, default_model_key


class Product:
    pass


class ProductVariant:
    pass


class Category:
    pass


@pytest.fixture
def registry() -> AdminRegistry[str]:
    return AdminRegistry()


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("module", "class_name", "expected"),
    [
        ("app.products.models", "Product", "products.product"),
        ("app.products.models", "ProductVariant", "products.product_variant"),
        ("shop.models", "Order", "shop.order"),
        ("app.models.entities", "Tag", "app.tag"),
        ("standalone", "Thing", "standalone.thing"),
        ("app.shop.models", "HTTPLog", "shop.http_log"),
    ],
)
def test_key_is_derived_from_module_and_class(module: str, class_name: str, expected: str) -> None:
    """Modules named `models` describe location, not ownership, so they are skipped."""
    model = type(class_name, (), {"__module__": module})
    assert default_model_key(model) == expected


def test_derived_keys_are_url_safe() -> None:
    model = type("My Weird Name", (), {"__module__": "a.b"})
    key = default_model_key(model)
    assert key == "b.my_weird_name"


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_register_and_look_up(registry: AdminRegistry[str]) -> None:
    entry = registry.register(Product, "ProductAdmin")

    assert entry.key.endswith(".product")
    assert registry.get(Product) is entry
    assert registry.get_by_key(entry.key) is entry
    assert Product in registry
    assert entry.key in registry
    assert len(registry) == 1


def test_registering_the_same_model_twice_is_an_error(registry: AdminRegistry[str]) -> None:
    """The second registration would shadow the first and never take effect."""
    registry.register(Product, "First")
    with pytest.raises(RegistrationError, match="already registered"):
        registry.register(Product, "Second")


def test_key_collision_names_both_models(registry: AdminRegistry[str]) -> None:
    other = type("Product", (), {"__module__": Product.__module__})
    registry.register(Product, "A")

    with pytest.raises(RegistrationError) as caught:
        registry.register(other, "B")

    message = str(caught.value)
    assert "collides" in message
    assert "explicit key" in message


def test_an_explicit_key_resolves_a_collision(registry: AdminRegistry[str]) -> None:
    other = type("Product", (), {"__module__": Product.__module__})
    registry.register(Product, "A")
    entry = registry.register(other, "B", key="legacy.product")
    assert entry.key == "legacy.product"


@pytest.mark.parametrize("bad", ["Product", "shop.Product", "shop product", "", "a.b.c"])
def test_malformed_keys_are_rejected(registry: AdminRegistry[str], bad: str) -> None:
    with pytest.raises(RegistrationError, match="not a valid registry key"):
        registry.register(Product, "A", key=bad)


def test_registering_an_instance_is_an_error(registry: AdminRegistry[str]) -> None:
    with pytest.raises(RegistrationError, match="model class"):
        registry.register(Product(), "A")  # type: ignore[arg-type]


def test_unregister_frees_both_indexes(registry: AdminRegistry[str]) -> None:
    entry = registry.register(Product, "A")
    registry.unregister(Product)

    assert registry.get(Product) is None
    assert registry.get_by_key(entry.key) is None
    registry.register(Product, "B")  # the key is available again


def test_unregistering_an_unknown_model_is_an_error(registry: AdminRegistry[str]) -> None:
    with pytest.raises(RegistrationError, match="not registered"):
        registry.unregister(Product)


# ---------------------------------------------------------------------------
# Lookup errors
# ---------------------------------------------------------------------------


def test_entry_for_explains_how_to_register(registry: AdminRegistry[str]) -> None:
    with pytest.raises(RegistrationError) as caught:
        registry.entry_for(Product)
    assert "@admin.register" in str(caught.value)


def test_entry_for_key_lists_what_is_registered(registry: AdminRegistry[str]) -> None:
    registry.register(Product, "A", key="shop.product")
    with pytest.raises(RegistrationError, match=r"shop\.product"):
        registry.entry_for_key("shop.missing")


# ---------------------------------------------------------------------------
# Ordering and grouping
# ---------------------------------------------------------------------------


def test_registration_order_is_preserved(registry: AdminRegistry[str]) -> None:
    """The sidebar shows models in the order they were declared."""
    registry.register(Category, "C", key="shop.category")
    registry.register(Product, "P", key="shop.product")
    assert registry.keys == ("shop.category", "shop.product")


def test_entries_are_grouped_by_namespace(registry: AdminRegistry[str]) -> None:
    registry.register(Product, "P", key="shop.product")
    registry.register(Category, "C", key="shop.category")
    registry.register(ProductVariant, "U", key="accounts.user")

    groups = registry.grouped()
    assert list(groups) == ["shop", "accounts"]
    assert [e.key for e in groups["shop"]] == ["shop.product", "shop.category"]


# ---------------------------------------------------------------------------
# Hooks
# ---------------------------------------------------------------------------


async def test_sync_and_async_listeners_both_run() -> None:
    hooks = HookRegistry()
    seen: list[str] = []

    @hooks.on(Hook.AFTER_CREATE)
    def sync_listener(**kwargs: object) -> None:
        seen.append(f"sync:{kwargs['obj']}")

    @hooks.on(Hook.AFTER_CREATE)
    async def async_listener(**kwargs: object) -> None:
        seen.append(f"async:{kwargs['obj']}")

    await hooks.emit(Hook.AFTER_CREATE, obj="x")
    assert seen == ["sync:x", "async:x"]


async def test_listeners_run_in_registration_order() -> None:
    hooks = HookRegistry()
    order: list[int] = []
    for index in range(3):
        hooks.add(Hook.AFTER_UPDATE, lambda index=index, **_: order.append(index))  # type: ignore[misc]

    await hooks.emit(Hook.AFTER_UPDATE)
    assert order == [0, 1, 2]


async def test_a_failing_listener_is_not_swallowed() -> None:
    """A hook that fails silently is worse than no hook at all."""
    hooks = HookRegistry()

    @hooks.on(Hook.AFTER_DELETE)
    def boom(**_: object) -> None:
        raise RuntimeError("listener failed")

    with pytest.raises(RuntimeError, match="listener failed"):
        await hooks.emit(Hook.AFTER_DELETE)


async def test_emitting_an_event_with_no_listeners_is_a_no_op() -> None:
    await HookRegistry().emit(Hook.AFTER_CREATE, obj=None)


async def test_listeners_can_be_removed() -> None:
    hooks = HookRegistry()
    calls: list[int] = []

    def listener(**_: object) -> None:
        calls.append(1)

    hooks.add("custom", listener)
    hooks.remove("custom", listener)
    await hooks.emit("custom")

    assert calls == []
    assert len(hooks) == 0


def test_non_callable_listeners_are_rejected() -> None:
    with pytest.raises(ConfigurationError, match="callable"):
        HookRegistry().add(Hook.AFTER_CREATE, "not a function")  # type: ignore[arg-type]


async def test_a_listener_added_during_emit_does_not_run_in_that_emit() -> None:
    """Iterating a snapshot keeps a self-registering listener from looping forever."""
    hooks = HookRegistry()
    calls: list[str] = []

    def outer(**_: object) -> None:
        calls.append("outer")
        hooks.add("evt", lambda **_: calls.append("inner"))

    hooks.add("evt", outer)
    await hooks.emit("evt")

    assert calls == ["outer"]
