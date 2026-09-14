from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_chat_mcp_options_use_dwell_selection_style():
    assert '#chatMcpBody .chat-mcp-input {' in INDEX
    assert 'inline-size: 1px; block-size: 1px; opacity: 0;' in INDEX
    assert '#chatMcpBody .chat-mcp-input:checked + .chat-mcp-name { color: var(--accent); }' in INDEX
    assert '#chatMcpBody .chat-mcp-input:checked ~ .chat-mcp-check { opacity: 1; }' in INDEX
    assert 'class="chat-mcp-check" aria-hidden="true">✓</span>' in INDEX
    assert '<input type="checkbox" id="chatHomeTodos"' not in INDEX


def test_chat_mcp_copy_uses_compact_type_hierarchy():
    name_rule = INDEX.index('#chatMcpBody .chat-mcp-name {')
    content_rule = INDEX.index('#chatMcpBody .chat-mcp-content {')
    assert 'font-size: 15px' in INDEX[name_rule:name_rule + 180]
    assert 'font-size: 13px' in INDEX[content_rule:content_rule + 240]
    assert 'class="chat-mcp-name">允许 Claude 使用待办、日记和日历</span>' in INDEX
    assert 'class="chat-mcp-content">' in INDEX


def test_chat_mcp_sections_are_quiet_labels_outside_cards():
    for label in ('内置网页工具', '家里的功能', 'MCP 服务器'):
        assert f'class="group-label chat-mcp-section-label">{label}</div>' in INDEX

    label_rule = INDEX.index('#chatMcpBody .chat-mcp-section-label {')
    assert 'color: var(--dim)' in INDEX[label_rule:label_rule + 180]
    assert 'font-size: 13px' in INDEX[label_rule:label_rule + 180]
    assert 'font-weight: 400' in INDEX[label_rule:label_rule + 180]
    assert '#chatMcpBody .chat-mcp-option:has(.chat-mcp-input:focus-visible)' in INDEX
