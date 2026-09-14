from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_chat_instruction_options_use_dwell_selection_style():
    assert '#chatInstructionsBody .chat-instruction-input {' in INDEX
    assert 'inline-size: 1px; block-size: 1px; opacity: 0;' in INDEX
    assert '#chatInstructionsBody .chat-instruction-input:checked + .chat-instruction-name { color: var(--accent); }' in INDEX
    assert '#chatInstructionsBody .chat-instruction-input:checked ~ .chat-instruction-check { opacity: 1; }' in INDEX
    assert 'class="chat-instruction-check" aria-hidden="true">✓</span>' in INDEX


def test_chat_instruction_copy_uses_compact_type_hierarchy():
    name_rule = INDEX.index('#chatInstructionsBody .chat-instruction-name {')
    content_rule = INDEX.index('#chatInstructionsBody .chat-instruction-content {')
    assert 'font-size: 15px' in INDEX[name_rule:name_rule + 180]
    assert 'font-size: 13px' in INDEX[content_rule:content_rule + 240]
    assert 'class="chat-instruction-name">分条回复</span>' in INDEX
    assert 'class="chat-instruction-content">按自然空行自动拆开</span>' in INDEX


def test_chat_instruction_checkbox_keeps_keyboard_focus_feedback():
    assert '#chatInstructionsBody .chat-instruction-option:has(.chat-instruction-input:focus-visible)' in INDEX
    assert '<label class="grow"><input type="checkbox" id="splitReplies"' not in INDEX


def test_reply_style_heading_is_a_quiet_label_outside_the_card():
    assert 'class="group-label chat-instruction-section-label">回复方式</div>' in INDEX
    assert 'class="group chat-instruction-reply-group"' in INDEX
    assert '<div class="ap-section"><div class="ap-section-title">回复方式</div>' not in INDEX

    label_rule = INDEX.index('#chatInstructionsBody .chat-instruction-section-label {')
    assert 'color: var(--dim)' in INDEX[label_rule:label_rule + 180]
    assert 'font-size: 13px' in INDEX[label_rule:label_rule + 180]
    assert 'font-weight: 400' in INDEX[label_rule:label_rule + 180]
    assert '#chatInstructionsBody .chat-instruction-reply-group { margin-bottom: 20px; }' in INDEX
