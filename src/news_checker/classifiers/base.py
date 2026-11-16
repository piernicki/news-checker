"""Classifier interface: takes a batch of stored links and returns a verdict for each.

To add a classifier: create a module in classifiers/, subclass Classifier with @register("type") and set that
type in config.yaml (classifier.type). Verdicts: match | no_match | unclear.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from ..config import ClassifierConfig, Settings
from ..providers.base import Budget
from ..storage import StoredMention

VERDICTS = ("match", "no_match", "unclear")


class ClassifierNotConfigured(Exception):
    pass


@dataclass
class Verdict:
    verdict: str
    reason: str = ""


class Classifier(ABC):
    type: ClassVar[str]

    def __init__(self, cfg: ClassifierConfig, settings: Settings, budget: Budget):
        self.cfg, self.settings, self.budget = cfg, settings, budget
        self.setup()

    def setup(self) -> None:
        """Validate options; raise ClassifierNotConfigured if something required is missing."""

    @property
    def label(self) -> str:
        return self.type

    @abstractmethod
    def classify(self, items: list[StoredMention]) -> dict[int, Verdict]:
        """Verdicts by link id. Links without an answer are simply missing from the result."""
