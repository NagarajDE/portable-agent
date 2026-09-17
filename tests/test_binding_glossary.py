"""Term->value binding (auto recovery from the failed predicate + low-cardinality catalog), the business
glossary (shared + pack, pack wins), per-pack models, and the explicit-model Databricks client.
All offline: the Cortex capabilities (last_sql / dimension_paths / distinct_values) are duck-typed doubles."""
import pytest

import engine.graph as G
from engine.binding import filter_columns, resolve_dims, render_values_block, RECOVERY_HEADER
from engine.config import PackConfig
from engine.graph import build_graph, initial_state
from engine.llm_client import DatabricksClient, _databricks_base_url, _resolve_models
from engine.packs import render_glossary
from engine.retrieval import SqlRetriever
from engine.sql_tool import no_data
from tests.doubles import Cap, Judge, Sql

_CFG = {"max_score": 18, "pass_score": 18, "max_iters": 0, "eval_retries": 0, "max_stall": 0,
        "default_sql_tool": "mock", "loop": True}


# --- binding: pure functions --------------------------------------------------------------------------
def test_filter_columns_finds_string_predicates_only():
    sql = ("SELECT * FROM T WHERE BUSINESS_STATUS = 'Active' AND t.\"Geo Region\" IN ('EMEA', 'APAC') "
           "AND AMOUNT = 5 AND NAME LIKE 'x%' AND STATUS <> 'It''s'")
    assert filter_columns(sql) == ["BUSINESS_STATUS", "STATUS", "GEO REGION"]   # numeric / LIKE ignored


def test_resolve_dims_maps_bare_columns_to_view_paths():
    paths = ["BLV_ICERTIS_CONTRACT.BUSINESS_STATUS", "X.OTHER", "Y.BUSINESS_STATUS"]
    assert resolve_dims(["business_status", "NOPE"], paths) == \
        ["BLV_ICERTIS_CONTRACT.BUSINESS_STATUS", "Y.BUSINESS_STATUS"]
    assert render_values_block({}, header="H") == ""


# --- the auto-recovery path: no hand-declared dims, values come from the failed predicate -------------
class _CortexLike(Sql):
    """A SQL double with the optional Cortex capabilities the binding mechanism duck-types."""
    def __init__(self, *results, sql="", dims=(), values=None):
        super().__init__(*results)
        self.last_sql, self._dims, self._values, self.lookups = sql, list(dims), values or {}, []

    def dimension_paths(self):
        return self._dims

    def distinct_values(self, paths, limit=50):
        self.lookups.append((list(paths), limit))
        return {p: self._values[p] for p in paths if p in self._values}


def _tool():
    return _CortexLike(no_data("no rows"), "SUPPLIER | TOTAL\nIBM | 9",
                       sql="SELECT ... WHERE BUSINESS_STATUS = 'Active'",
                       dims=["C.BUSINESS_STATUS", "C.SUPPLIER_NAME"],
                       values={"C.BUSINESS_STATUS": ["Executed", "Approved"],
                               "C.SUPPLIER_NAME": [f"S{i}" for i in range(40)]})


def test_recovery_block_auto_derives_the_failed_column_without_a_declared_list():
    r = SqlRetriever(_tool())                       # NO recover_dims declared
    r.retrieve("q")                                 # blank first try -> last_sql has the predicate
    block = r.recovery_block()
    assert RECOVERY_HEADER in block and "BUSINESS_STATUS: Executed, Approved" in block
    assert "SUPPLIER_NAME" not in block             # only the column that was actually filtered


def test_declared_dims_come_first_and_lookups_are_cached():
    t = _tool()
    r = SqlRetriever(t, recover_dims=["C.SUPPLIER_NAME"])
    r.retrieve("q")
    b1, b2 = r.recovery_block(), r.recovery_block()
    assert b1 == b2 and b1.index("SUPPLIER_NAME") < b1.index("BUSINESS_STATUS")   # declared first
    assert len(t.lookups) == 1                       # second call served from the per-dim cache


def test_blank_then_recovers_via_auto_binding_end_to_end(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "max_data_retries": 1})
    worker = Cap("supplier totals where BUSINESS_STATUS IN ('Executed','Approved')", "IBM leads at 9.")
    judge = Judge("SCORE: 18/18 - grounded")
    final = build_graph("dq_qals", llm=worker, eval_llm=judge, sql=_tool(), verbose=False) \
        .invoke(initial_state("supplier totals"))
    assert final["grounded"] is True and final["best_score"] == 18
    assert any("Executed, Approved" in p for p in worker.prompts)   # real values reached the reword


# --- the proactive path: a low-cardinality catalog feeds FRAMING -------------------------------------------
def test_catalog_block_profiles_only_low_cardinality_dims_and_caches():
    t = _tool()
    r = SqlRetriever(t, value_catalog={"max_cardinality": 5})
    b1, b2 = r.catalog_block(), r.catalog_block()
    assert "BUSINESS_STATUS: Executed, Approved" in b1 and "SUPPLIER_NAME" not in b1   # 40 values skipped
    assert b1 == b2 and len(t.lookups) == 1
    assert SqlRetriever(t).catalog_block() == ""     # off unless configured


def test_catalog_reaches_the_framing_prompt(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "frame_query": True, "max_data_retries": 0,
                                                       "value_catalog": {"max_cardinality": 5}})
    worker = Cap("supplier totals", "answer")        # first call = framing, second = generate
    build_graph("dq_qals", llm=worker, eval_llm=Judge(), sql=_tool(), verbose=False) \
        .invoke(initial_state("supplier totals"))
    assert "Executed, Approved" in worker.prompts[0]   # the real values reached the FRAMING prompt
    assert "SUPPLIER_NAME" not in worker.prompts[0]    # the high-cardinality dim was left out


def test_value_catalog_config_is_validated():
    assert PackConfig.from_dict({"value_catalog": {"max_cardinality": 10}}).value_catalog == {"max_cardinality": 10}
    with pytest.raises(ValueError):
        PackConfig.from_dict({"value_catalog": {"max_cardinality": 0}})
    with pytest.raises(ValueError):
        PackConfig.from_dict({"value_catalog": "yes"})


# --- business glossary: shared + pack, merged by term, pack wins --------------------------------------
def test_glossary_merges_by_term_with_pack_override(tmp_path, monkeypatch):
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "glossary.yaml").write_text(
        "- {term: active contract, meaning: SHARED, maps_to: X}\n- {term: fiscal year, meaning: FY}\n", encoding="utf-8")
    pack = tmp_path / "uc" / "p"; pack.mkdir(parents=True)
    (pack / "glossary.yaml").write_text(
        "- {term: Active Contract, aliases: [live], meaning: PACK, maps_to: \"BUSINESS_STATUS IN ('Executed')\"}\n",
        encoding="utf-8")
    monkeypatch.setattr(G, "SHARED", tmp_path / "shared")
    monkeypatch.setattr(G, "USECASES", tmp_path / "uc")
    entries = G.load_glossary("p")
    by_term = {e["term"].lower(): e for e in entries}
    assert by_term["active contract"]["meaning"] == "PACK"       # pack overrides shared (case-insensitive)
    assert "fiscal year" in by_term                              # shared-only term inherited
    text = render_glossary(entries)
    assert "BUSINESS GLOSSARY" in text and "(aka live)" in text and "Executed" in text


def test_glossary_reaches_framing_and_generate(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: {**_CFG, "frame_query": True})
    monkeypatch.setattr(G, "load_glossary", lambda uc: [
        {"term": "active", "meaning": "in force", "maps_to": "BUSINESS_STATUS IN ('Executed','Approved')"}])
    worker = Cap("q", "answer")
    build_graph("dq_qals", llm=worker, eval_llm=Judge(), sql=Sql("a | 1"), verbose=False) \
        .invoke(initial_state("q"))
    assert all("BUSINESS GLOSSARY" in p for p in worker.prompts[:2])   # framing AND generate saw it


def test_no_glossary_means_no_block(monkeypatch):
    monkeypatch.setattr(G, "load_config", lambda uc: dict(_CFG))
    monkeypatch.setattr(G, "load_glossary", lambda uc: [])
    worker = Cap("answer")
    build_graph("dq_qals", llm=worker, eval_llm=Judge(), sql=Sql("a | 1"), verbose=False).invoke(initial_state("q"))
    assert "BUSINESS GLOSSARY" not in worker.prompts[0]


def test_shipped_glossaries_parse_and_procurement_overrides_shared():
    entries = G.load_glossary("procurement_contracts")
    terms = {e["term"].lower() for e in entries}
    assert {"active contract", "contract value", "supplier", "fiscal year"} <= terms
    assert G.load_glossary("dq_qals") and G.load_glossary("dq_qals")[0]["term"] == "fiscal year"   # shared only


# --- per-pack models: the AI+BI packs declare Cortex defaults; env still wins --------------------------
@pytest.mark.parametrize("uc", ["goa_spend", "icertis_procurement", "inventory_balance",
                                "inventory_excess_disposition", "procurement_contracts"])
def test_ai_bi_packs_declare_cortex_models_and_env_wins(uc, monkeypatch):
    cfg = PackConfig.from_dict(G.load_config(uc))
    assert _resolve_models(cfg.models)[:2] == ("cortex", "claude-opus-5")
    assert _resolve_models(cfg.models)[2:] == ("cortex", "claude-opus-4-8")
    monkeypatch.setenv("WORKER_PROVIDER", "mock")
    assert _resolve_models(cfg.models)[0] == "mock"              # one env var flips the pack


# --- Databricks: explicit model required; host normalized, never rejected for a path ---------------------
def test_databricks_requires_an_explicit_model():
    with pytest.raises(ValueError) as e:
        DatabricksClient()                                       # no model, no DATABRICKS_MODEL
    assert "explicit model" in str(e.value)


@pytest.mark.parametrize("host", ["https://w.cloud.databricks.com", "https://w.cloud.databricks.com/",
                                  "https://w.cloud.databricks.com/serving-endpoints"])
def test_databricks_host_path_is_stripped(host):
    assert _databricks_base_url(host) == "https://w.cloud.databricks.com/serving-endpoints"


@pytest.mark.parametrize("bad", ["http://w.cloud.databricks.com", "https://u:p@w.cloud.databricks.com",
                                 "https://w.cloud.databricks.com/?x=1", "w.cloud.databricks.com"])
def test_databricks_host_rejects_unsafe_forms(bad):
    with pytest.raises(ValueError):
        _databricks_base_url(bad)
