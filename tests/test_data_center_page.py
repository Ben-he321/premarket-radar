"""Offline Streamlit rendering: no secrets and no external data calls."""
from pathlib import Path
from streamlit.testing.v1 import AppTest


def test_page_without_credentials():
    page = Path(__file__).resolve().parents[1] / "pages" / "11_数据中心.py"
    app = AppTest.from_file(str(page), default_timeout=30).run()
    assert not app.exception
    assert not app.error
    assert any("缺少凭证" in item.value for item in app.warning)
    assert len(app.dataframe) == 1
    assert len(app.dataframe[0].value) == 36
    assert all(b.disabled for b in app.button if b.label in ("首次下载 / 断点续传", "增量更新"))
    app.button(key="historical").click().run()
    assert not app.exception
    assert any("缺少凭证" in item.value for item in app.error)
