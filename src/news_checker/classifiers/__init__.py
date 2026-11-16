from __future__ import annotations

from .base import VERDICTS, Classifier, ClassifierNotConfigured, Verdict

REGISTRY: dict[str, type[Classifier]] = {}


def register(type_name: str):
    def deco(cls: type[Classifier]) -> type[Classifier]:
        cls.type = type_name
        REGISTRY[type_name] = cls
        return cls

    return deco


from . import anthropic_llm  # noqa: E402,F401

__all__ = ["REGISTRY", "VERDICTS", "Classifier", "ClassifierNotConfigured", "Verdict", "register"]
