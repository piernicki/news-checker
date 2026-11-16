"""Running the classifier over links without a verdict (in batches), with logging and budget accounting."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .classifiers import REGISTRY, Classifier, ClassifierNotConfigured
from .config import Settings
from .providers.base import Budget, BudgetExceeded
from .storage import Store, StoredMention

log = logging.getLogger(__name__)


@dataclass
class ClassifyResult:
    status: str = "ok"
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


def make_classifier(settings: Settings, store: Store) -> Classifier:
    cfg = settings.classifier
    if not cfg.enabled:
        raise ClassifierNotConfigured("classifier is disabled (classifier.enabled: false)")
    cls = REGISTRY.get(cfg.type)
    if cls is None:
        raise ClassifierNotConfigured(f"unknown classifier.type '{cfg.type}'")
    return cls(cfg, settings, Budget(store, f"classifier:{cfg.type}", None, cfg.monthly_limit))


def classify_mentions(settings: Settings, store: Store, mentions: list[StoredMention],
                      classifier: Classifier | None = None) -> ClassifyResult:
    result = ClassifyResult()
    if not mentions:
        result.status = "nothing to classify"
        return result
    try:
        classifier = classifier or make_classifier(settings, store)
    except ClassifierNotConfigured as exc:
        result.status = f"skipped: {exc}"
        log.info("CLASSIFY skip reason=%r", str(exc))
        return result

    size = max(1, settings.classifier.batch_size)
    log.info("CLASSIFY start classifier=%s links=%d batches=%d", classifier.label, len(mentions),
             -(-len(mentions) // size))
    by_id = {m.id: m for m in mentions}
    for start in range(0, len(mentions), size):
        batch = mentions[start:start + size]
        try:
            verdicts = classifier.classify(batch)
        except BudgetExceeded as exc:
            result.status = str(exc)
            result.errors.append(str(exc))
            log.warning("BUDGET %s", exc)
            break
        except Exception as exc:  # noqa: BLE001 — unclassified links simply keep no verdict
            result.errors.append(str(exc))
            log.warning("CLASSIFY error batch=%d-%d error=%r", start, start + len(batch), str(exc))
            continue
        for mid, v in verdicts.items():
            store.set_verdict(mid, verdict=v.verdict, reason=v.reason, classifier=classifier.label)
            result.counts[v.verdict] = result.counts.get(v.verdict, 0) + 1
            m = by_id[mid]
            log.info("CLASSIFY id=%d verdict=%s domain=%s url=%s reason=%r", mid, v.verdict, m.domain,
                     m.original_url, v.reason)
    result.cost_usd = getattr(classifier, "cost_usd", 0.0)
    log.info("CLASSIFY end %s errors=%d tokens_in=%s tokens_out=%s cost_usd=%.4f",
             " ".join(f"{k}={v}" for k, v in sorted(result.counts.items())) or "-", len(result.errors),
             getattr(classifier, "tokens_in", "-"), getattr(classifier, "tokens_out", "-"), result.cost_usd)
    return result
