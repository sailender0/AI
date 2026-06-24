"""
Verify SQLAlchemy single-table inheritance configuration on the Integration model.
No DB connection required — these inspect mapper metadata only.
"""
from app.storage.models import (
    GitHubIntegration,
    GitLabIntegration,
    Integration,
    JiraIntegration,
    TeamsIntegration,
)


def test_integration_polymorphic_on_is_set():
    assert "polymorphic_on" in Integration.__mapper_args__


def test_teams_integration_identity():
    assert TeamsIntegration.__mapper_args__["polymorphic_identity"] == "teams_subscription"


def test_github_integration_identity():
    assert GitHubIntegration.__mapper_args__["polymorphic_identity"] == "github"


def test_gitlab_integration_identity():
    assert GitLabIntegration.__mapper_args__["polymorphic_identity"] == "gitlab"


def test_jira_integration_identity():
    assert JiraIntegration.__mapper_args__["polymorphic_identity"] == "jira"


def test_all_subclasses_inherit_from_integration():
    assert issubclass(TeamsIntegration, Integration)
    assert issubclass(GitHubIntegration, Integration)
    assert issubclass(GitLabIntegration, Integration)
    assert issubclass(JiraIntegration, Integration)


def test_all_subclasses_use_same_table():
    """Single-table inheritance: no subclass defines its own __tablename__."""
    for cls in (TeamsIntegration, GitHubIntegration, GitLabIntegration, JiraIntegration):
        assert cls.__tablename__ == "integrations", f"{cls.__name__} has wrong tablename"


def test_identities_are_unique():
    identities = [
        TeamsIntegration.__mapper_args__["polymorphic_identity"],
        GitHubIntegration.__mapper_args__["polymorphic_identity"],
        GitLabIntegration.__mapper_args__["polymorphic_identity"],
        JiraIntegration.__mapper_args__["polymorphic_identity"],
    ]
    assert len(identities) == len(set(identities)), "Duplicate polymorphic_identity values"
