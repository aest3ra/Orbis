import json

import pytest

from orbis.config import ScanConfig
from orbis.telemetry import ScanLog, clean


SCHEMES = ("http", "https", "HTTP", "HTTPS", "HtTp", "hTtPs")
PRIVATE_MARKERS = ("synthetic-password", "synthetic-token", "synthetic-fragment")


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("position", ("target", "nested", "embedded"))
def test_clean_redacts_url_secrets_regardless_of_scheme_case(scheme, position):
    url = f"{scheme}://user:synthetic-password@example.test/page?token=synthetic-token#synthetic-fragment"
    safe = f"{scheme.lower()}://example.test/page?token=%5Bredacted%5D"
    if position == "target":
        value, expected = {"target": url}, {"target": safe}
    elif position == "nested":
        value = {"details": [{"urls": (url,)}]}
        expected = {"details": [{"urls": [safe]}]}
    else:
        value, expected = f"before {url} after", f"before {safe} after"

    actual = clean(value)

    assert all(marker not in json.dumps(actual) for marker in PRIVATE_MARKERS)
    assert actual == expected


def test_scan_log_redacts_original_target_with_mixed_case_scheme(tmp_path):
    config = ScanConfig(
        target="hTtPs://user:synthetic-password@example.test/page?token=synthetic-token#synthetic-fragment"
    )
    trace = ScanLog(str(tmp_path / "scan.db"), 1)
    try:
        record = trace.emit("scan_start", target=config.target)
    finally:
        trace.close()

    text = trace.path.read_text(encoding="utf-8")
    assert all(marker not in text for marker in PRIVATE_MARKERS)
    assert json.loads(text) == record
    assert record["target"] == "https://example.test/page?token=%5Bredacted%5D"
