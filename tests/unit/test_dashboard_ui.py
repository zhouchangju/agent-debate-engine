from agent_debate.dashboard_ui import DASHBOARD_HTML


def test_dashboard_uses_structured_synthesis_renderer() -> None:
    assert "const synthesis = (value) =>" in DASHBOARD_HTML
    assert "${synthesis(decision.synthesis || doc.summary?.final_markdown)}" in DASHBOARD_HTML
    assert 'class="synthesis-ranking"' in DASHBOARD_HTML
    assert 'class="synthesis-boundaries"' in DASHBOARD_HTML


def test_structured_synthesis_preserves_markdown_fallback_and_escaping() -> None:
    assert "return markdown(text)" in DASHBOARD_HTML
    assert "const inline = (value) => esc(value)" in DASHBOARD_HTML
    assert "暂无最终结论" in DASHBOARD_HTML
