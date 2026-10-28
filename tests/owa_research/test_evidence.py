import pytest

from owa_research.core.evidence import EvidenceStore


def test_put_get_index_roundtrip(tmp_path):
    s = EvidenceStore(tmp_path, max_bytes=1000)
    a = s.put("page", b"<html>hi</html>", url="https://a.com", content_type="text/html", ext="html")
    assert s.read_bytes(a.artifact_id) == b"<html>hi</html>"
    child = s.put_json("parsed", {"text": "hi"}, parent=a.artifact_id)
    assert s.children(a.artifact_id)[0].artifact_id == child.artifact_id
    s2 = EvidenceStore(tmp_path)
    assert s2.get(a.artifact_id).content_hash == a.content_hash and s2.read_json(child.artifact_id) == {"text": "hi"}


def test_capacity_bound(tmp_path):
    s = EvidenceStore(tmp_path, max_bytes=100)
    s.put("page", b"x" * 60, url=None)
    with pytest.raises(RuntimeError):
        s.put("page", b"y" * 60, url=None)
    with pytest.raises(KeyError):
        s.read_bytes("nope")
