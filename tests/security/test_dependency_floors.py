"""SC-14: dependency floors that exclude known-vulnerable releases stay in place."""
import pathlib
import re

REQUIREMENTS = pathlib.Path(__file__).resolve().parents[2] / "app" / "requirements.txt"


def _floor(package: str) -> tuple[int, ...]:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(package)}\s*(?:>=|==)\s*([0-9.]+)", line.strip(), re.IGNORECASE)
        if match:
            return tuple(int(part) for part in match.group(1).split("."))
    raise AssertionError(f"{package} is not pinned in requirements.txt")


def test_requests_floor_excludes_the_netrc_and_verify_cves():
    assert _floor("requests") >= (2, 32, 4)


def test_other_floors_from_the_audit_are_not_lowered():
    assert _floor("pymysql") >= (1, 1, 1)      # CVE-2024-36039
    assert _floor("GitPython") >= (3, 1, 41)   # CVE-2024-22190
