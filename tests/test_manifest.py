"""pack_manifest(): client-facing metadata (name/description/sample_questions), with
sample_questions defaulting to the golden-set questions when a pack doesn't set them."""
import pytest

import engine.graph as G
from engine.graph import pack_manifest

# Packs that ship a golden set -> sample_questions default to those questions.
PACKS = ["dq_qals", "kpi_analytics", "anomaly_rca", "parity_hana_snowflake", "inventory_balance"]


@pytest.mark.parametrize("uc", PACKS)
def test_manifest_defaults_samples_from_golden_set(uc):
    m = pack_manifest(uc)
    assert m["use_case"] == uc
    assert m["name"]
    assert isinstance(m["sample_questions"], list) and m["sample_questions"], \
        f"{uc}: expected non-empty sample_questions (golden-set default)"
    assert all(isinstance(q, str) and q.strip() for q in m["sample_questions"])
    assert len(m["sample_questions"]) <= 20


def test_explicit_sample_questions_override_and_are_cleaned(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {
        "name": "X", "description": "d", "sample_questions": ["q1", "  ", "q2"]})
    m = pack_manifest("whatever")
    assert m["sample_questions"] == ["q1", "q2"]      # blanks dropped, order + explicit set kept
    assert m["description"] == "d"


def test_manifest_no_config_samples_no_golden_is_empty(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {"name": "X"})
    monkeypatch.setattr(G, "_golden_questions", lambda uc: [])
    m = pack_manifest("whatever")
    assert m["sample_questions"] == [] and m["name"] == "X"
