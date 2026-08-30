from pathlib import Path

from project_info import (
    PROJECT_LICENSE_NAME,
    PROJECT_LICENSE_URL,
    PROJECT_REPOSITORY_URL,
    PROJECT_SOURCE_NOTICE,
    REQUIRED_NOTICE,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_required_notice_preserves_project_identity_and_noncommercial_terms():
    assert REQUIRED_NOTICE.startswith("Required Notice:")
    assert PROJECT_REPOSITORY_URL in REQUIRED_NOTICE
    assert PROJECT_LICENSE_NAME in REQUIRED_NOTICE
    assert "开源项目" in PROJECT_SOURCE_NOTICE
    assert "非商业" in PROJECT_SOURCE_NOTICE
    assert PROJECT_LICENSE_URL.endswith("/noncommercial/1.0.0")


def test_required_notice_and_license_are_kept_in_distribution_surfaces():
    notice = (PROJECT_ROOT / "NOTICE").read_text(encoding="utf-8").strip()
    spec = (PROJECT_ROOT / "douyin-monitor.spec").read_text(encoding="utf-8")
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    assert notice == REQUIRED_NOTICE
    assert "('LICENSE', '.')" in spec
    assert "('NOTICE', '.')" in spec
    assert 'src="assets/app_icon.png"' in readme
    assert PROJECT_REPOSITORY_URL in readme
