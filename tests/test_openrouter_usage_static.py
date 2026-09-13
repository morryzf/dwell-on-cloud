import re
import unittest
from pathlib import Path


HTML = (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")


class OpenRouterUsageStaticTest(unittest.TestCase):
    def test_usage_lives_in_sidebar_after_heartbeat(self):
        heartbeat = HTML.index('id="navHeartbeat"')
        usage = HTML.index('id="navUsage"')
        recents = HTML.index('class="drawer-section-head"', usage)
        self.assertLess(heartbeat, usage)
        self.assertLess(usage, recents)
        self.assertIn('id="navUsage"><span class="ic" data-i="trend"></span>用量', HTML)
        self.assertNotIn('id="usageRow"', HTML)

    def test_usage_sheet_has_summary_chart_and_controls(self):
        self.assertIn('<div class="sheetWrap page" id="usageSheet">', HTML)
        for element_id in (
            "usageBalanceValue", "usageToday", "usageWeek", "usageMonth",
            "usageCacheRate", "usageCacheMeta", "usageChartDetail",
            "usageChart", "usageRefresh", "usageNote",
        ):
            self.assertIn(f'id="{element_id}"', HTML)
        for range_name in ("daily", "weekly", "monthly"):
            self.assertIn(f'data-usage-range="{range_name}"', HTML)
        for currency in ("USD", "CNY"):
            self.assertIn(f'data-usage-currency="{currency}"', HTML)

    def test_usage_fetches_openrouter_endpoint_and_converts_currency(self):
        self.assertIn("fetch('/api/openrouter/usage'", HTML)
        self.assertIn("openRouterUsageCurrency === 'CNY'", HTML)
        self.assertIn("openRouterUsageData?.exchange_rate?.rate", HTML)
        self.assertIn("localStorage.setItem('dwellUsageCurrency'", HTML)
        self.assertIn("usage_daily", HTML)
        self.assertIn("usage_weekly", HTML)
        self.assertIn("usage_monthly", HTML)

    def test_chart_and_period_context_are_accessible(self):
        self.assertIn('role="group" aria-label="显示货币"', HTML)
        self.assertIn('role="group" aria-label="统计周期"', HTML)
        self.assertIn("bar.setAttribute('aria-label'", HTML)
        self.assertIn("bar.setAttribute('aria-pressed'", HTML)
        self.assertIn("bar.onclick = () => selectPoint(point)", HTML)
        self.assertIn("openRouterUsageExactAmount", HTML)
        self.assertIn("openRouterUsageHitText", HTML)
        self.assertIn("chart.setAttribute('aria-label'", HTML)
        self.assertIn("按 UTC 统计", HTML)

    def test_usage_navigation_opens_page_and_reloads_data(self):
        pattern = re.compile(
            r"document\.getElementById\('navUsage'\)\.onclick = \(\) => \{"
            r" closeDrawer\(\); sheets\.usage\.classList\.add\('open'\);"
            r" loadOpenRouterUsage\(\); pushState\(\); \};"
        )
        self.assertRegex(HTML, pattern)
        self.assertIn("usage: 'navUsage'", HTML)


if __name__ == "__main__":
    unittest.main()
