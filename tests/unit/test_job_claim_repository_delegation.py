from __future__ import annotations

from hunter.storage.job_claims import ClaimedJob, JobClaimRepository


async def test_claim_repository_delegates_all_job_transitions_to_database_facade() -> None:
    class Database:
        def __init__(self) -> None:
            self.calls: list[tuple] = []

        async def lease_job(self, **kwargs):
            self.calls.append(("lease", kwargs))
            return {
                "id": 9,
                "kind": "round2",
                "product_id": "p",
                "dedupe_key": "d",
                "payload": {"x": 1},
                "priority": 4,
                "attempts": 2,
                "max_attempts": 5,
                "lease_token": "fence",
                "lease_until": "2099-01-01T00:00:00+00:00",
            }

        async def complete_job(self, *args, **kwargs):
            self.calls.append(("complete", args, kwargs))
            return True

        async def fail_job(self, *args, **kwargs):
            self.calls.append(("fail", args, kwargs))
            return "queued"

        async def renew_job_lease(self, *args, **kwargs):
            self.calls.append(("renew", args, kwargs))
            return True

    db = Database()
    repo = JobClaimRepository(db)

    job = await repo.claim_job("round2", lease_duration_sec=17, kinds=["round2", "photo_check"])

    assert job == ClaimedJob(
        9, "round2", "p", "d", {"x": 1}, 4, 2, 5, "fence", "2099-01-01T00:00:00+00:00"
    )
    assert await repo.complete_job(9, "fence", {"done": True}) is True
    assert await repo.fail_job(9, "fence", "retry") == "queued"
    assert await repo.renew_lease(9, "fence", extend_sec=11) is True
    assert db.calls == [
        ("lease", {"kind": "round2", "lease_duration_sec": 17, "kinds": ["round2", "photo_check"]}),
        ("complete", (9,), {"payload": {"done": True}, "lease_token": "fence"}),
        ("fail", (9, "retry"), {"lease_token": "fence"}),
        ("renew", (9, "fence"), {"extend_sec": 11}),
    ]
