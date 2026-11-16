"""Provider registry. Modules are imported here so that @register runs."""

from __future__ import annotations

from .base import Budget, BudgetExceeded, Provider, ProviderNotConfigured

REGISTRY: dict[str, type[Provider]] = {}


def register(type_name: str):
    def deco(cls: type[Provider]) -> type[Provider]:
        cls.type = type_name
        REGISTRY[type_name] = cls
        return cls

    return deco


from . import brave, serper, telegram_web, yandex  # noqa: E402,F401

__all__ = ["REGISTRY", "Budget", "BudgetExceeded", "Provider", "ProviderNotConfigured", "register"]
