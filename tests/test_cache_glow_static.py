import re
import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


class CacheGlowStaticTest(unittest.TestCase):
    def test_composer_contains_non_layout_glow_and_hint(self):
        composer = HTML.index('<div class="composer">')
        textarea = HTML.index('<textarea id="box"', composer)
        section = HTML[composer:textarea]
        self.assertIn('id="cacheGlow"', section)
        self.assertIn('id="cacheGlowAura"', section)
        self.assertIn('id="cacheGlowLine"', section)
        self.assertIn('id="cacheGlowHit"', section)
        self.assertIn('id="cacheGlowBlur"', section)
        self.assertIn('<feGaussianBlur stdDeviation="2.6">', section)
        self.assertIn('pathLength="100"', section)
        self.assertIn('id="cacheGlowHint"', section)
        self.assertIn('position: absolute; inset: -3px', HTML)

    def test_countdown_is_five_minutes_and_uses_linear_stroke(self):
        self.assertIn("const CACHE_GLOW_MS = 5 * 60 * 1000;", HTML)
        self.assertIn("style.transition = 'stroke-dashoffset ' + remaining + 'ms linear'", HTML)
        self.assertIn("const glowStrokes = [cacheGlowAura, cacheGlowLine];", HTML)
        self.assertIn("stroke.style.strokeDashoffset = '100';", HTML)
        self.assertIn("filter: url(#cacheGlowBlur)", HTML)
        self.assertIn("从发送按钮旁的右下角出发", HTML)
        self.assertNotIn("@keyframes cacheGlow", HTML)

    def test_successful_reply_starts_and_send_stops_countdown(self):
        self.assertRegex(
            HTML,
            re.compile(
                r"if \(d\.type === 'result'\) \{\s+"
                r"if \(!d\.is_error\) cacheGlowStart\(\);"
            ),
        )
        send = HTML[HTML.index("async function send()"):HTML.index("sendBtn.onclick = send;")]
        self.assertIn("const previousCacheEnd = cacheGlowEnd;", send)
        self.assertIn("cacheGlowStop(true);", send)
        self.assertIn("if (previousCacheEnd > Date.now())", send)
        self.assertIn("cacheGlowSave();", send)

    def test_countdown_restores_per_chat_and_after_backgrounding(self):
        self.assertIn("const CACHE_GLOW_STORAGE = 'dwellCacheGlow';", HTML)
        self.assertIn("JSON.stringify({chat_id:cacheGlowChatId, end:cacheGlowEnd})", HTML)
        self.assertIn("stored.chat_id === cacheGlowChatId", HTML)
        self.assertIn("cacheGlowSetChat(chat.id);", HTML)
        self.assertIn("cacheGlowSetChat(it.id);", HTML)
        self.assertIn("cacheGlowSetChat(cur.id);", HTML)
        self.assertIn("document.addEventListener('visibilitychange'", HTML)
        self.assertIn("cacheGlowAnimate(cacheGlowEnd)", HTML)

    def test_glow_is_tappable_accessible_and_reduced_motion_safe(self):
        self.assertIn('role="button" tabindex="-1" aria-hidden="true"', HTML)
        self.assertIn("cacheGlowHit.addEventListener('click', cacheGlowShowHint)", HTML)
        self.assertIn("'缓存还在 · '", HTML)
        self.assertIn("event.key === 'Enter' || event.key === ' '", HTML)
        self.assertIn("prefers-reduced-motion: reduce", HTML)
        self.assertIn("cacheGlowReducedMotion.matches", HTML)
        self.assertIn("new ResizeObserver", HTML)


if __name__ == "__main__":
    unittest.main()
