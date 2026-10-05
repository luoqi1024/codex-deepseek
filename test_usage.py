import json
from pathlib import Path
import tempfile
import unittest
import usage_meter as meter


class UsageTests(unittest.TestCase):
    def summary(self, events, **state):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events), encoding='utf-8')
            return meter.summarize(path, {'backend':'desktop', 'provider':'opencode-go',
                'model':'deepseek-v4.1-flash', **state}, 'go')

    def event(self, seq=1, **extra):
        return {'type':'usage', 'seq':seq, 'started':'2026-10-05T04:10:00Z', 'time':'2026-10-05T04:11:00Z',
            'usage':{'inputTokens':1000, 'outputTokens':200, 'cacheReadTokens':9000, 'reasoningTokens':150}, **extra}

    def test_disjoint_cache_and_reasoning_are_not_double_counted(self):
        value = self.summary([self.event()])
        self.assertAlmostEqual(value['cost']['usd_min'], .000297)
        self.assertEqual(value['cost']['usd_max'], value['cost']['usd_min'])
        self.assertEqual(value['quota_contribution']['windows']['five_hour']['limit_usd'],12)
        self.assertFalse(value['quota_contribution']['account_remaining_known'])

    def test_peak_weekend_boundary_and_unknown_times(self):
        for start,end,expected in [
            ('2026-10-05T01:10:00Z','2026-10-05T01:11:00Z',(2,2)),
            ('2026-10-04T01:10:00Z','2026-10-04T01:11:00Z',(1,1)),
            ('2026-10-05T03:59:00Z','2026-10-05T04:01:00Z',(1,2)),
            ('2026-10-05T00:00:00Z','2026-10-05T12:00:00Z',(1,2)),
            (None,'2026-10-05T12:00:00Z',(1,2))]:
            self.assertEqual(meter.multiplier(start,end),expected)

    def test_retries_count_once_per_durable_event(self):
        first=self.event(); second=self.event(2, source='assistant/attempt')
        value=self.summary([first, first, second])
        self.assertEqual(value['tokens']['inputTokens'],2000)
        self.assertEqual(value['samples_reported'],2)
        self.assertAlmostEqual(value['cost']['usd_min'], .000594)

    def test_missing_usage_is_unknown_and_partial_never_zero(self):
        old=self.summary([{'type':'status','phase':'step_end'}])
        self.assertEqual(old['coverage'],'unavailable')
        self.assertIsNone(old['cost']['usd_min'])
        self.assertIn('未知',meter.brief(old))
        partial=self.summary([self.event(),self.event(2,usage=None)])
        self.assertEqual(partial['coverage'],'partial')
        self.assertEqual(partial['cost']['status'],'partial')
        self.assertIn('部分',meter.brief(partial))

    def test_missing_cache_foreign_model_and_invalid_counters_are_not_priced(self):
        for event in [self.event(usage={'inputTokens':10,'outputTokens':2}),
                self.event(model='another-model'),
                self.event(usage={'inputTokens':10,'outputTokens':2,'cacheReadTokens':False}),
                self.event(usage={'inputTokens':10,'outputTokens':2,'cacheReadTokens':0,'cacheWriteTokens':2})]:
            value=self.summary([event])
            self.assertEqual(value['cost']['status'],'unavailable')
            self.assertIsNone(value['cost']['usd_min'])
        for count in [True,-1,1.5,float('inf')]:
            value=self.summary([self.event(usage={'inputTokens':count,'outputTokens':2})])
            self.assertEqual(value['coverage'],'unavailable')

    def test_headless_step_deltas_and_native_no_double_count(self):
        step={'type':'status','phase':'step_end','usage':self.event()['usage']}
        headless=self.summary([step,step],backend='headless')
        self.assertEqual(headless['tokens']['inputTokens'],2000)
        native=self.summary([step,self.event()])
        self.assertEqual(native['tokens']['inputTokens'],1000)

    def test_assignment_sum_keeps_unknown_round_and_cache_fields_partial(self):
        known=self.summary([self.event()]); unknown=self.summary([])
        value=meter.combine([known,known],'go')
        self.assertEqual(value['tokens']['inputTokens'],2000)
        self.assertAlmostEqual(value['cost']['usd_min'],.000594)
        self.assertEqual(value['coverage'],'reported')
        partial=meter.combine([known,unknown],'go')
        self.assertEqual(partial['coverage'],'partial')
        self.assertEqual(partial['cost']['status'],'partial')
        self.assertIn('cacheReadTokens',partial['partial_fields'])
        self.assertIn('部分',meter.brief(partial))


if __name__ == '__main__':
    unittest.main()
