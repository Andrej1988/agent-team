import pytest
from agent_team import git_identity

pytestmark = pytest.mark.generic


def test_identities_name_the_agent():
    c = git_identity.chat("demo-lstm")
    assert c["GIT_AUTHOR_NAME"] == c["GIT_COMMITTER_NAME"] == "demo-lstm (Chat-Agent)"
    assert c["GIT_AUTHOR_EMAIL"] == "chat.demo-lstm@agents.invalid"
    s = git_identity.stage("shop", "data-engineer")
    assert s["GIT_AUTHOR_NAME"] == "shop data-engineer (Agent)"
    assert s["GIT_COMMITTER_EMAIL"] == "shop.data-engineer@agents.invalid"
    assert git_identity.env("x", "A b/c")["GIT_AUTHOR_EMAIL"] == "a-b-c@agents.invalid"
