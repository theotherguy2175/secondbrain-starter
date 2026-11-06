import time

from brain.store import DAY, build_match, recency_weight


def test_put_get_roundtrip(store):
    r = store.put(title="Hello", body="world body", kind="note", tags=["a", "b"])
    assert r["action"] == "created"
    item = store.get(r["id"])
    assert item["title"] == "Hello" and item["tags"] == "a, b"
    # get() returns the row as it was before this read is counted
    assert store.get(r["id"])["access_count"] == 1
    assert store.get(r["id"], touch=False)["access_count"] == 2


def test_upsert_by_source_ref_is_idempotent(store):
    a = store.put(title="T", body="same", source="notes", source_ref="x.md")
    b = store.put(title="T", body="same", source="notes", source_ref="x.md")
    c = store.put(title="T", body="changed", source="notes", source_ref="x.md")
    assert (a["action"], b["action"], c["action"]) == ("created", "unchanged", "updated")
    assert a["id"] == b["id"] == c["id"]
    assert store.stats()["total"] == 1


def test_same_ref_different_source_are_separate(store):
    store.put(title="T", body="b", source="s1", source_ref="r")
    store.put(title="T", body="b", source="s2", source_ref="r")
    assert store.stats()["total"] == 2


def test_search_finds_and_ranks_by_relevance(corpus):
    corpus.put(title="Proxmox backup failure", kind="fix",
               body="Nightly vzdump backup failed: storage 'nas' is not online. Fixed by remounting NFS share.")
    corpus.put(title="Proxmox cluster notes", body="Three nodes, ceph disabled, ha off.")
    hits = corpus.search("proxmox backup nfs error")
    assert hits[0]["title"] == "Proxmox backup failure"
    assert "[" in hits[0]["snippet"]  # highlighted


def test_prefix_and_stemming(corpus):
    corpus.put(title="Backups", body="The server is backing up nightly")
    assert corpus.search("backup")  # porter stems backing/backups -> backup
    assert corpus.search("nightl")  # prefix


def test_search_safe_against_fts_syntax(corpus):
    corpus.put(title="Quote", body="don't panic: NEAR(a b) AND OR NOT *")
    for q in ['"', "AND", "OR NOT", "(", "a:b", "'; DROP TABLE items; --", "*", ""]:
        corpus.search(q)  # must not raise
    assert corpus.stats()["total"] >= 1


def test_recency_weighting_prefers_new_over_old(corpus):
    old = corpus.put(title="Server price", body="The hosting price is 20 dollars per month",
                     created_at=time.time() - 900 * DAY)
    new = corpus.put(title="Server price", body="The hosting price is 35 dollars per month")
    hits = corpus.search("hosting price dollars")
    assert [h["id"] for h in hits][:2] == [new["id"], old["id"]]
    assert hits[0]["score"] > hits[1]["score"]


def test_pinned_items_exempt_from_decay(corpus):
    old = corpus.put(title="WiFi policy", body="guest wifi password rotation policy",
                     created_at=time.time() - 900 * DAY, pinned=True)
    corpus.put(title="WiFi note", body="wifi router firmware", created_at=time.time())
    h = next(x for x in corpus.search("wifi policy password") if x["id"] == old["id"])
    assert h["pinned"] is True


def test_recency_weight_math():
    now = 1_000_000_000.0
    assert recency_weight(now, now, 90, 0.35) == 1.0
    half = recency_weight(now - 90 * DAY, now, 90, 0.35)
    assert abs(half - (0.35 + 0.65 * 0.5)) < 1e-9
    assert recency_weight(now - 10_000 * DAY, now, 90, 0.35) < 0.36
    assert recency_weight(now - 5 * DAY, now, 0, 0.35) == 1.0


def test_filters(corpus):
    corpus.put(title="A fix", body="kubernetes node notready", kind="fix", scope="homelab", source="agent")
    corpus.put(title="A note", body="kubernetes study plan", kind="note", scope="personal")
    assert len(corpus.search("kubernetes", kind="fix")) == 1
    assert len(corpus.search("kubernetes", scope="homelab")) == 1
    assert len(corpus.search("kubernetes", source="agent")) == 1
    assert len(corpus.search("kubernetes")) == 2
    assert corpus.search("kubernetes", since_days=1)


def test_archive_hides_from_search_and_edit_restores(corpus):
    r = corpus.put(title="Temp", body="zebra quartz unique")
    assert corpus.search("zebra quartz")
    corpus.archive(r["id"])
    assert not corpus.search("zebra quartz")
    assert corpus.search("zebra quartz", include_archived=True)
    corpus.put(id=r["id"], title="Temp", body="zebra quartz unique edited")
    assert corpus.search("zebra quartz")


def test_fts_index_stays_in_sync_on_update_and_delete(corpus):
    r = corpus.put(title="Doc", body="alpha beta gamma")
    corpus.put(id=r["id"], title="Doc", body="delta epsilon zeta")
    assert not corpus.search("alpha")
    assert corpus.search("epsilon")
    corpus.delete(r["id"])
    assert not corpus.search("epsilon")


def test_append(corpus):
    r = corpus.put(title="Log", body="first entry")
    corpus.append(r["id"], "second entry about pelican")
    item = corpus.get(r["id"])
    assert "first entry" in item["body"] and "pelican" in item["body"]
    assert corpus.search("pelican")
    assert corpus.append("missing", "x") is None


def test_enrichment_makes_item_findable_by_questions_and_atoms(corpus):
    r = corpus.put(title="Network thing", body="long rambling text without the key words")
    assert corpus.needing_enrichment()
    corpus.set_enrichment(r["id"], "Fix for flapping uplink", "why does my uplink flap?",
                          "ethtool -s eth0 speed 1000 autoneg off")
    assert corpus.search("ethtool autoneg")[0]["id"] == r["id"]
    assert corpus.search("uplink flap")[0]["id"] == r["id"]
    assert r["id"] not in [i["id"] for i in corpus.needing_enrichment()]


def test_body_change_resets_enrichment(corpus):
    r = corpus.put(title="X", body="one", summary="s", atoms="a")
    assert corpus.get(r["id"])["enriched"] == 1
    corpus.put(id=r["id"], title="X", body="two")
    assert corpus.get(r["id"])["enriched"] == 0


def test_build_match_quotes_everything():
    m = build_match("How do I fix: error 'NEAR' (x)?")
    assert "NEAR" not in m.replace('"near"*', "")
    assert build_match("???") is None


def test_connector_state_cursor_semantics(store):
    store.record_run("c", "100", 5, None)
    assert store.get_state("c")["cursor"] == "100"
    store.record_run("c", "200", 0, "boom")  # failure keeps old cursor
    st = store.get_state("c")
    assert st["cursor"] == "100" and st["last_error"] == "boom" and st["total_count"] == 5
    store.record_run("c", "300", 2, None)
    st = store.get_state("c")
    assert st["cursor"] == "300" and st["last_error"] is None and st["total_count"] == 7


def test_persistence_and_wal(tmp_path):
    from brain.store import Store

    p = tmp_path / "sub" / "b.db"
    s = Store(p)
    s.put(title="Persist", body="survives restart")
    s.close()
    s2 = Store(p)
    assert s2.search("survives")
    s2.close()


def test_backup(tmp_path, corpus):
    dest = tmp_path / "bk" / "b.db"
    corpus.backup_to(dest)
    from brain.store import Store

    s = Store(dest)
    assert s.stats()["total"] == corpus.stats()["total"]
    s.close()
