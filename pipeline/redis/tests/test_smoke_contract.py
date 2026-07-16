from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _smoke_function() -> str:
    script = (REPO_ROOT / "scripts" / "dev.sh").read_text()
    return script.split("cmd_smoke() {", 1)[1].split("\n}\n", 1)[0]


def test_smoke_sends_one_unique_event_and_queries_that_exact_marker():
    smoke = _smoke_function()

    assert 'event_name="smoke_test_$$_$(date -u +%Y%m%dT%H%M%S)"' in smoke
    assert smoke.count('http_code POST "http://localhost:8080/v1/events"') == 1
    assert '\\"event\\":\\"$event_name\\"' in smoke
    assert '\\"event_name\\":\\"$event_name\\"' in smoke
    assert '"retry":true' not in smoke
    assert "re-send" not in smoke
