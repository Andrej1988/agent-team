"""Agents get a 409 when they file a task that restates a recent one."""

from agent_team.tasks import similar


def test_restated_titles_match_and_different_ones_do_not():
    assert similar.score("Login live mit echtem Konto prüfen (REV-2 aus #32)",
                         "Login einmal mit echtem Konto vom Handy prüfen") >= similar.THRESHOLD
    assert similar.score("Stop-Loss-Notlösung für billige Optionen",
                         "Stop-Loss-Notlösung für billige Optionen") == 1.0
    assert similar.score("Klasse „Brötchen“ mischt Stück- und Packungspreise",
                         "Eier-Klasse: „Bio Eier“ je Stück") < similar.THRESHOLD
    hits = similar.find("Login mit echtem Konto prüfen", [
        {"id": 1, "title": "Login einmal mit echtem Konto vom Handy prüfen", "status": "backlog"},
        {"id": 2, "title": "Suche findet Zitronen nicht", "status": "done"}])
    assert [h["id"] for h in hits] == [1]
