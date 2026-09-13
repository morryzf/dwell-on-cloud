from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def test_memory_overview_describes_conversation_continuity():
    assert "总览维持对话的连续性" in INDEX
    assert "总览维持这段关系的连续性" not in INDEX


def test_primary_settings_actions_share_a_clear_visual_style():
    primary_buttons = (
        'id="heartbeatSave">保存节奏',
        'id="backgroundChoose">从设备选择图片',
        'id="ttsSave">保存语音设置',
        'id="apSave">保存供应商',
        'id="mcpSave">保存服务器',
        'id="instructionSave">${current ? \'保存修改\' : \'添加指令\'}',
    )
    for button in primary_buttons:
        position = INDEX.index(button)
        assert 'class="ap-b go"' in INDEX[position - 80:position]

    primary_rule = INDEX.index(".sheet .ap-b.go {")
    glass_override = INDEX.index("/* Quiet glass edges:")
    assert primary_rule > glass_override
    assert "background: linear-gradient(145deg, #efc4d2, #d8a0b4) !important;" in INDEX
    dark_rule = INDEX.index('html[data-theme="dark"] .sheet .ap-b.go {')
    assert "background: linear-gradient(145deg, #a6657d, #875064) !important;" in INDEX[dark_rule:dark_rule + 420]
    assert ".sheet .ap-b.go:active { transform: scale(.97); }" in INDEX
    assert "@media (hover: hover) and (pointer: fine)" in INDEX
