"""Milestone 7 against the local Supabase stack: indexing, private vector search,
retrieval provenance and caching, drafting with a retrieved selection, and the
full-context fallbacks. A deterministic fake embedder stands in for the model
except in the last test, which uses the pinned model when it is installed.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import create_engine, func, select, text

from app.auth.dependencies import user_db
from app.auth.tokens import VerifiedUser
from app.config import Settings
from app.db.models import DraftVersion, EvidenceChunk, Retrieval
from app.documents.fixtures import build_fixture_master
from app.retrieval import embedder as embedder_module
from app.retrieval import service
from app.retrieval.index import FactRef, sync_chunks
from app.retrieval.search import fulltext_ranking, semantic_ranking
from app.tailoring import runs
from app.tailoring.models import Requirement
from tests.retrieval_fakes import HashEmbedder
from tests.test_isolation import _settings_or_skip, reset_rate_limits
from tests.test_tailoring_flow import create_job, make_app, prepared_account

pytestmark = pytest.mark.integration


def as_user(account) -> VerifiedUser:
    return VerifiedUser(id=account.id, email=account.email, access_token="unused")


def fact_refs(settings, user) -> list[FactRef]:
    with user_db(settings, user) as db:
        return [
            FactRef(r.id, v.version, v.text, v.section, v.entry, v.experience_type)
            for r, v in runs._verified_rows(db, user)
        ]


def sync(settings, user, embedder):
    with user_db(settings, user) as db:
        return sync_chunks(db, user.id, fact_refs(settings, user), embedder)


@pytest.fixture(scope="module")
def base_settings() -> Settings:
    settings = _settings_or_skip()
    reset_rate_limits(settings)
    return settings


@pytest.fixture(scope="module")
def docx_bytes(tmp_path_factory) -> bytes:
    return build_fixture_master(tmp_path_factory.mktemp("m7") / "master.docx").read_bytes()


@pytest.fixture(scope="module")
def demo_app(base_settings):
    return make_app(base_settings, llm_provider="demo")


def test_index_follows_fact_versions(base_settings, demo_app, docx_bytes):
    account = prepared_account(demo_app, docx_bytes)
    user = as_user(account)
    embedder = HashEmbedder()
    first = sync(base_settings, user, embedder)
    assert first.facts > 0 and first.embedded > 0 and first.deleted == 0

    again = sync(base_settings, user, embedder)
    assert (again.embedded, again.reused, again.deleted) == (0, 0, 0)  # nothing changed

    items = account.client.get("/api/v1/evidence").json()
    edited = next(i for i in items if i["verification_status"] == "verified")
    rejected = next(
        i for i in items if i["verification_status"] == "verified" and i["id"] != edited["id"]
    )
    for item, body in (
        (edited, {"text": edited["text"] + " (weekly)", "verification_status": "verified"}),
        (rejected, {"verification_status": "rejected"}),
    ):
        response = account.client.post(
            f"/api/v1/evidence/{item['id']}/versions",
            json={
                "expected_version": item["version"],
                "experience_type": item["experience_type"],
                **body,
            },
        )
        assert response.status_code == 201, response.text
    changed = sync(base_settings, user, embedder)
    assert changed.embedded == 1 and changed.deleted == 2  # new text in; edited + rejected out

    with user_db(base_settings, user) as db:
        rows = db.execute(select(EvidenceChunk.evidence_id, EvidenceChunk.owner_id)).all()
    assert {owner for _, owner in rows} == {account.id}
    assert uuid.UUID(rejected["id"]) not in {e for e, _ in rows}


def test_identical_text_reuses_the_owners_vector(base_settings, demo_app, docx_bytes):
    account = prepared_account(demo_app, docx_bytes)
    user = as_user(account)
    embedder = HashEmbedder()
    sync(base_settings, user, embedder)
    # A new version with the same text and context: no new embedding needed.
    item = next(
        i
        for i in account.client.get("/api/v1/evidence").json()
        if i["verification_status"] == "verified"
    )
    response = account.client.post(
        f"/api/v1/evidence/{item['id']}/versions",
        json={
            "expected_version": item["version"],
            "experience_type": item["experience_type"],
            "verification_status": "verified",
        },
    )
    assert response.status_code == 201, response.text
    stats = sync(base_settings, user, embedder)
    assert stats.embedded == 0 and stats.reused >= 1 and stats.deleted >= 1


def test_vector_and_fulltext_search_are_private(base_settings, demo_app, docx_bytes):
    alice = prepared_account(demo_app, docx_bytes)
    bob = prepared_account(demo_app, docx_bytes)
    embedder = HashEmbedder()
    for account in (alice, bob):
        sync(base_settings, as_user(account), embedder)
    requirement = Requirement(
        id="R1",
        category="required",
        text="SQL and Python reporting",
        terms=["SQL"],
        mandatory=False,
    )
    query = embedder.embed(["SQL and Python reporting"])[0]
    alice_ids = {r.evidence_id for r in fact_refs(base_settings, as_user(alice))}
    with user_db(base_settings, as_user(bob)) as db:
        own = semantic_ranking(db, bob.id, embedder, query)
        own_text = fulltext_ranking(db, bob.id, requirement)
        # Even asking for Alice's rows explicitly returns nothing under Bob's session.
        theirs = semantic_ranking(db, alice.id, embedder, query)
        theirs_text = fulltext_ranking(db, alice.id, requirement)
    assert own and own_text
    assert not alice_ids & {e for e, _ in own + own_text}
    assert theirs == [] and theirs_text == []

    engine = create_engine(base_settings.database_url.get_secret_value())
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(uuid.uuid4())})
        for table in ("evidence_chunks", "retrievals"):
            assert conn.execute(text(f"SELECT count(*) FROM app.{table}")).scalar_one() == 0  # noqa: S608
    for statement in (
        "UPDATE app.evidence_chunks SET body = 'x'",
        "UPDATE app.retrievals SET mode = 'full'",
        "DELETE FROM app.retrievals",
    ):
        with pytest.raises(Exception, match="permission denied"), engine.begin() as conn:
            conn.execute(text("SELECT set_config('app.user_id', :u, true)"), {"u": str(alice.id)})
            conn.execute(text(statement))
    engine.dispose()


def _draft(app, account):
    job_id = create_job(account)
    run = account.client.post("/api/v1/runs", json={"job_id": job_id}).json()
    drafted = account.client.post(f"/api/v1/runs/{run['id']}/draft")
    assert drafted.status_code in (200, 202), drafted.text
    return account.client.get(f"/api/v1/runs/{run['id']}").json()


def test_small_profiles_send_every_fact(base_settings, demo_app, docx_bytes):
    account = prepared_account(demo_app, docx_bytes)
    run = _draft(demo_app, account)
    context = run["draft"]["context"]
    assert context["mode"] == "full" and context["reason"] == "small_profile"
    assert context["facts_selected"] == context["facts_total"] > 0


def test_hybrid_draft_records_selection_and_reports_unseen_facts(
    monkeypatch,
    base_settings,
    docx_bytes,
):
    fake = HashEmbedder()
    monkeypatch.setattr(service, "load_embedder", lambda settings: fake)
    app = make_app(base_settings, llm_provider="demo", retrieval_mode="hybrid", retrieval_top_k=1)
    account = prepared_account(app, docx_bytes)
    run = _draft(app, account)
    draft = run["draft"]
    context = draft["context"]
    assert context["mode"] == "hybrid" and context["reason"] == "relevance"
    assert 0 < context["facts_selected"] < context["facts_total"]

    cited = {
        i for e in draft["content"]["entries"] for b in e["bullets"] for i in b["evidence_ids"]
    }
    cited |= {i for s in draft["content"]["skills"] for i in s["evidence_ids"]}
    omitted = {o["evidence_id"] for o in draft["content"]["omitted"]}
    all_ids = {str(r.evidence_id) for r in fact_refs(base_settings, as_user(account))}
    assert cited | omitted == all_ids  # facts the model never saw are still listed

    user = as_user(account)
    with user_db(base_settings, user) as db:
        stored = db.scalars(select(Retrieval).where(Retrieval.run_id == uuid.UUID(run["id"]))).one()
        linked = db.scalar(select(func.count()).where(DraftVersion.retrieval_id == stored.id))
        texts = [r.text for r in fact_refs(base_settings, user)]
    assert linked == 1 and stored.embedding_revision == embedder_module.MODEL_REVISION
    dumped = json.dumps(stored.result)
    assert not any(t in dumped for t in texts)  # provenance holds IDs and scores, not text
    assert set(stored.result["requirements"]) == {r["id"] for r in run["analysis"]["requirements"]}

    # Drafting again with nothing changed reuses the stored selection.
    again = account.client.post(f"/api/v1/runs/{run['id']}/draft")
    assert again.status_code in (200, 202), again.text
    with user_db(base_settings, user) as db:
        count = db.scalar(
            select(func.count()).select_from(Retrieval).where(Retrieval.owner_id == account.id)
        )
        reused = db.scalar(select(func.count()).where(DraftVersion.retrieval_id == stored.id))
    assert count == 1 and reused == 2


def test_missing_model_falls_back_to_full_context(tmp_path, base_settings, docx_bytes):
    app = make_app(
        base_settings, llm_provider="demo", retrieval_mode="hybrid", embedding_model_dir=tmp_path
    )
    account = prepared_account(app, docx_bytes)
    run = _draft(app, account)
    assert run["status"] == "awaiting_review"
    context = run["draft"]["context"]
    assert context["mode"] == "full" and context["reason"] == "embeddings_unavailable"


def test_pinned_model_ranks_by_meaning(base_settings, demo_app):
    try:
        model = embedder_module.load_embedder(base_settings)
    except embedder_module.EmbeddingUnavailableError:
        pytest.skip("embedding model not fetched (python -m scripts.fetch_embedding_model)")
    texts = [
        "Reviewed security questionnaires from outside suppliers and rated each supplier's risk.",
        "Booked 25 qualified meetings a month through cold calls.",
        "Built weekly sales reports in SQL.",
    ]
    vectors = model.embed(texts + ["Conduct vendor risk assessments"])
    query, docs = vectors[-1], vectors[:-1]
    similarities = [sum(a * b for a, b in zip(query, d, strict=True)) for d in docs]
    assert max(range(3), key=lambda i: similarities[i]) == 0
    assert all(abs(sum(x * x for x in v) - 1) < 1e-3 for v in vectors)  # normalized
    assert len(query) == embedder_module.DIMENSION
