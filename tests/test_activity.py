"""Tests for team activity scoring and classification."""

from utils import activity


class TestActivityScore:
    def test_zero_activity_scores_zero(self):
        assert activity.compute_activity_score() == 0

    def test_max_activity_scores_100(self):
        score = activity.compute_activity_score(50, 20, 20, 20)
        assert score == 100

    def test_commits_are_capped(self):
        one = activity.compute_activity_score(commits=30)
        many = activity.compute_activity_score(commits=500)
        assert one == many

    def test_weights_influence_score(self):
        heavy_commits = activity.compute_activity_score(commits=30, prs=0, reviews=0, issues=0)
        heavy_reviews = activity.compute_activity_score(commits=0, prs=0, reviews=10, issues=0)
        assert heavy_commits > heavy_reviews

    def test_custom_weights(self):
        weights = {"commits": 50, "prs": 50, "reviews": 0, "issues": 0}
        score = activity.compute_activity_score(commits=30, prs=10, reviews=10, issues=10, weights=weights)
        assert score == 100


class TestActivityStatus:
    def test_none_is_inactive(self):
        assert activity.activity_status(None) == "INACTIVE"

    def test_recent_is_active(self):
        assert activity.activity_status(0) == "ACTIVE"
        assert activity.activity_status(activity.settings.RECENTLY_ACTIVE_DAYS) == "ACTIVE"

    def test_mid_band_is_recently_active(self):
        assert activity.activity_status(activity.settings.RECENTLY_ACTIVE_DAYS + 1) == "RECENTLY ACTIVE"

    def test_old_is_inactive(self):
        assert activity.activity_status(activity.settings.INACTIVE_DAYS + 1) == "INACTIVE"


class TestActivityLabel:
    def test_high(self):
        assert activity.activity_label(90) == "Highly Active"

    def test_mid(self):
        assert activity.activity_label(70) == "Active"

    def test_low_band(self):
        assert activity.activity_label(45) == "Low Activity"

    def test_inactive(self):
        assert activity.activity_label(10) == "Inactive"


class TestScoreReason:
    def test_mentions_metrics(self):
        reason = activity.score_reason("alice", 10, 2, 1, 3)
        assert "10 commits" in reason
        assert "2 PRs" in reason
        assert "1 review" in reason
        assert "3 issues" in reason

    def test_no_activity_message(self):
        assert "no recorded activity" in activity.score_reason("bob", 0, 0, 0, 0)

    def test_singular_plural(self):
        assert "1 commit" in activity.score_reason("alice", 1, 0, 0, 0)
        assert "1 review" in activity.score_reason("alice", 0, 0, 1, 0)


class TestLastActiveText:
    def test_no_activity(self):
        assert activity.last_active_text(None) == "No activity"

    def test_today(self):
        assert activity.last_active_text(0) == "Today"

    def test_yesterday(self):
        assert activity.last_active_text(1) == "Yesterday"

    def test_days_ago(self):
        assert activity.last_active_text(12) == "12 days ago"


class TestEnrichMember:
    def test_enriches_with_score_and_status(self):
        member = activity.enrich_member(
            {
                "username": "alice",
                "commits": 10,
                "prs_created": 2,
                "prs_reviewed": 4,
                "issues_created": 2,
                "last_active_days": 2,
            }
        )
        assert "activity_score" in member
        assert "activity_label" in member
        assert member["activity_status"] == "ACTIVE"
        assert member["is_active"] is True
        assert member["last_active_text"] == "2 days ago"
        assert member["score_reason"]

    def test_no_activity_is_not_active(self):
        member = activity.enrich_member(
            {
                "username": "bob",
                "commits": 0,
                "prs_created": 0,
                "prs_reviewed": 0,
                "issues_created": 0,
                "last_active_days": None,
            }
        )
        assert member["is_active"] is False
        assert member["last_active_text"] == "No activity"
