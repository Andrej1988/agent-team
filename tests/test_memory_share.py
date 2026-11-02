from agent_team import memory_share


def test_sub_repo_memory_moves_into_the_project_and_links(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_share, "PROJECTS", tmp_path)
    root, sub = memory_share.memory_dir("/w/demo"), memory_share.memory_dir("/w/demo/demo-lstm")
    root.mkdir(parents=True)
    (root / "MEMORY.md").write_text("- [A](a.md) — a\n")
    (root / "a.md").write_text("A")
    sub.mkdir(parents=True)
    (sub / "MEMORY.md").write_text("- [B](b.md) — b\n- [A](a.md) — a\n")
    (sub / "b.md").write_text("B")
    (sub / "a.md").write_text("other A")
    assert memory_share.share("/w/demo/demo-lstm", "/w/demo")
    assert sub.is_symlink() and sub.resolve() == root.resolve()
    assert (root / "b.md").read_text() == "B" and (root / "lstm_a.md").read_text() == "other A"
    assert (root / "MEMORY.md").read_text().splitlines() == ["- [A](a.md) — a", "- [B](b.md) — b"]
    assert memory_share.share("/w/demo/demo-lstm", "/w/demo")          # idempotent
    assert not memory_share.share("/w/demo", "/w/demo")               # the root itself
    assert not memory_share.share("/w/other", "/w/demo")            # not below the root
