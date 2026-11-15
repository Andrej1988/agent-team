import subprocess

from agent_team.worker import task_containers as tc


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def test_names_exact_task_id():
    names = ["task-12-pytest", "task-123-pytest", "task-1-x", "task-12-", "shop-app", "task-12-a.b_c"]
    assert tc.names_for(12, names) == ["task-12-a.b_c", "task-12-pytest"]


def test_remove_only_own_and_reports_refusals():
    calls = []

    def run(cmd):
        calls.append(list(cmd))
        if cmd[1] == "ps":
            return cp(out="task-7-pytest\ntask-70-other\ntask-7-db\n")
        if cmd[-1] == "task-7-db":
            return cp(1, err="denied")
        return cp()

    assert tc.remove(7, run) == ["task-7-pytest"]
    rms = [c[-1] for c in calls if c[1] == "rm"]
    assert rms == ["task-7-db", "task-7-pytest"]


def test_remove_survives_missing_docker():
    def run(cmd):
        raise FileNotFoundError("docker")

    assert tc.remove(3, run) == []
