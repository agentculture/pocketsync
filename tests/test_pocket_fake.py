import pytest

from pocketsync.cli._errors import CliError
from pocketsync.model import Recording
from pocketsync.pocket.base import PocketSource, RateLimited
from pocketsync.pocket.fake import FakePocketSource


def _recs(n=5):
    return [
        Recording(
            id=f"r{i}",
            title=f"t{i}",
            recording_at=f"2026-01-{i + 1:02d}T10:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            summary="s",
        )
        for i in range(n)
    ]


def test_is_pocket_source_and_read_only():
    fake = FakePocketSource(_recs())
    assert isinstance(fake, PocketSource)
    names = {n for n in dir(PocketSource) if not n.startswith("_")}
    assert names == {"list_recordings", "get_recording", "get_audio_url"}


def test_pagination():
    fake = FakePocketSource(_recs(5))
    p1 = fake.list_recordings(page=1, limit=2)
    assert [r.id for r in p1.recordings] == ["r0", "r1"] and p1.has_more and p1.total == 5
    p3 = fake.list_recordings(page=3, limit=2)
    assert [r.id for r in p3.recordings] == ["r4"] and not p3.has_more


def test_date_filter_and_limit_cap():
    fake = FakePocketSource(_recs(5))
    page = fake.list_recordings(start_date="2026-01-02", end_date="2026-01-03")
    assert [r.id for r in page.recordings] == ["r1", "r2"]
    with pytest.raises(CliError):
        fake.list_recordings(limit=101)


def test_rate_limit():
    fake = FakePocketSource(_recs())
    fake.set_rate_limit(2, retry_after=7)
    for _ in range(2):
        with pytest.raises(RateLimited) as ei:
            fake.list_recordings()
        assert ei.value.retry_after == 7
    assert fake.list_recordings().recordings


def test_edit_bumps_updated_at():
    fake = FakePocketSource(_recs())
    before = fake.get_recording("r0").updated_at
    fake.edit("r0", summary="new")
    after = fake.get_recording("r0")
    assert after.summary == "new" and after.updated_at > before


def test_delete_and_merge_state():
    fake = FakePocketSource(_recs())
    fake.delete("r0")
    assert "r0" in fake.deleted
    with pytest.raises(CliError):
        fake.get_recording("r0")
    fake.merge("r1", "r2")
    assert fake.merged == {"r1": "r2"}
    assert [r.id for r in fake.list_recordings().recordings] == ["r2", "r3", "r4"]


def test_include_flags_and_audio_and_isolation():
    rec = _recs(1)[0]
    fake = FakePocketSource([rec])
    assert fake.get_recording("r0", include_summarizations=False).summary == ""
    assert "r0" in fake.get_audio_url("r0", 120)
    with pytest.raises(CliError):
        fake.get_audio_url("r0", 5)
    got = fake.get_recording("r0")
    got.title = "mutated"
    assert fake.get_recording("r0").title == "t0"
