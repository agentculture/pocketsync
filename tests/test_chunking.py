from pocketsync.enrich.chunking import chunk_segments, estimate_tokens


def _segs(n, words=50):
    return [
        {"text": "word " * words, "start": float(i), "end": float(i) + 0.9, "speaker": "Ann"}
        for i in range(n)
    ]


def test_over_8192_tokens_yields_multiple_chunks():
    segs = _segs(400, words=60)  # ~ 400*~75 tokens > 8192
    assert sum(estimate_tokens(s["text"]) for s in segs) > 8192
    chunks = chunk_segments(segs)
    assert len(chunks) > 1
    assert all(estimate_tokens(c.text) <= 1024 for c in chunks)


def test_times_and_segment_ranges_preserved():
    chunks = chunk_segments(_segs(40), max_tokens=100)
    assert chunks[0].start == 0.0
    assert chunks[0].first_segment == 0
    assert chunks[-1].end == 39.9
    assert chunks[-1].last_segment == 39
    for a, b in zip(chunks, chunks[1:]):
        assert b.first_segment == a.last_segment + 1
        assert [c.index for c in chunks] == list(range(len(chunks)))


def test_speaker_prefix_and_small_input_single_chunk():
    chunks = chunk_segments([{"text": "hi", "start": 1, "end": 2, "speaker": "Bob"}])
    assert len(chunks) == 1
    assert chunks[0].text == "Bob: hi"
    assert chunks[0].to_dict()["segments"] == [0, 0]


def test_oversize_segment_is_split_and_empty_skipped():
    chunks = chunk_segments(
        [{"text": "x" * 1000, "start": 0, "end": 5}, {"text": "  ", "start": 5, "end": 6}],
        max_tokens=100,
    )
    assert len(chunks) > 1
    assert all(c.start == 0 and c.end == 5 for c in chunks)
