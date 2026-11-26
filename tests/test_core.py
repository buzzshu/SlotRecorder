"""Unit tests for the parts that decide the numbers and the file layout. No emulator, no video, a few seconds:

    python -m unittest discover -s tests -v          (or:  run_tests.bat)

For "did my change move the statistics of real recordings" use  python -m core.regress  (see core/regress.py).
"""
import csv, glob, json, os, random, re, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import numpy as np
from core import analyze as A
from core import config as C
from core.ocr import parse_number, _pick


def series_from(steps, fps=4, hold=1.5):
    """balance values held `hold` seconds each -> [(t, value)] sampled at fps (what read_series produces)"""
    out, t = [], 0.0
    for v in steps:
        for _ in range(int(hold * fps)): out.append((round(t, 2), v)); t += 1 / fps
    return out


class ParseNumber(unittest.TestCase):
    def test_plain_and_separators(self):
        self.assertEqual(parse_number('90,414,000,000'), 90414000000)
        self.assertEqual(parse_number('8000'), 8000)
        self.assertEqual(parse_number('11.888.400'), 11888400)        # OCR turns ',' into '.'
        self.assertEqual(parse_number('$ 7,537,000'), 7537000)

    def test_suffix(self):
        self.assertEqual(parse_number('1.25M'), 1250000)
        self.assertEqual(parse_number('24K'), 24000)

    def test_letter_lookalikes(self):
        self.assertEqual(parse_number('9O,414,000'), 90414000)
        self.assertEqual(parse_number('l2,345'), 12345)
        self.assertIsNone(parse_number('1O,5OO'))                     # mostly letters -> refused rather than guessed

    def test_rejects_broken_groups_and_words(self):
        for bad in ('12,34', '1,2345', 'Coarwa', '', 'SPIN', '1234,567'):
            self.assertIsNone(parse_number(bad), bad)


class SafeButton(unittest.TestCase):
    """OCR button choice: never a purchase, and on a page that shows a price only an explicit refusal"""
    @staticmethod
    def box(x, y): return [[x - 40, y - 12], [x + 40, y - 12], [x + 40, y + 12], [x - 40, y + 12]]

    def test_plain_dismiss(self):
        c, label, danger = _pick([(self.box(360, 900), 'COLLECT', 0.9)])
        self.assertEqual((c, label, danger), ((360, 900), 'COLLECT', False))

    def test_price_page_only_refusal(self):
        res = [(self.box(360, 700), '$4.99', 0.9), (self.box(360, 900), 'COLLECT', 0.9)]
        self.assertEqual(_pick(res)[:2], (None, None))
        res.append((self.box(360, 1100), 'NO THANKS', 0.9))
        self.assertEqual(_pick(res)[1], 'NO THANKS')

    def test_decline_beats_confirm(self):
        res = [(self.box(200, 900), '確定', 0.9), (self.box(500, 900), '取消', 0.9)]
        self.assertEqual(_pick(res)[1], '取消')

    def test_jackpot_meter_is_not_a_price(self):
        c, label, danger = _pick([(self.box(360, 300), '$500,000M', 0.9), (self.box(360, 900), 'CONTINUE', 0.9)])
        self.assertEqual((label, danger), ('CONTINUE', False))


class _DefaultKnobs(unittest.TestCase):
    """run with the ADVANCED defaults, whatever this PC's config.json overrides"""
    def setUp(self):
        self._cfg = dict(C.CFG)
        for k in C.ADVANCED: C.CFG.pop(k, None)
    def tearDown(self):
        C.CFG.clear(); C.CFG.update(self._cfg)


class Accounting(_DefaultKnobs):
    def test_stable_values_needs_two_samples(self):
        s = [(0, 100), (1, 100), (2, 90), (3, 80), (4, 80), (5, None), (6, 80)]
        self.assertEqual([(v, n) for _, v, n, _, _ in A.stable_values(s)], [(100, 2), (80, 2)])

    def test_split_step_conserves_coins(self):
        """whatever it decides, a step A -> B must satisfy  B - A == win - n * bet"""
        rnd = random.Random(7)
        for _ in range(3000):
            bet = rnd.choice([50, 8000, 24000]); A0 = rnd.randrange(10 ** 6, 10 ** 9)
            n_true = rnd.randrange(0, 4); win_true = rnd.choice([0, 0, bet // 2, bet * 3, bet * 40])
            B0 = A0 - n_true * bet + win_true
            between = [A0 + rnd.randrange(-2 * bet, win_true + 1) for _ in range(rnd.randrange(0, 5))]
            n, win, before = A.split_step(A0, B0, between, bet)
            self.assertEqual(B0 - A0, win - n * bet)
            self.assertGreaterEqual(n, 0); self.assertGreaterEqual(win, 0)

    def test_build_spins_recovers_a_simulated_session(self):
        rnd = random.Random(3); bet = 8000; bal = 10 ** 8; steps = [bal]; truth = []
        for _ in range(400):
            w = rnd.choice([0, 0, 0, bet // 2, bet * 2, bet * 12])
            bal -= bet; steps.append(bal)
            if w: bal += w; steps.append(bal)
            truth.append(w)
        series = series_from(steps); vals = A.stable_values(series)
        self.assertEqual(A.infer_bet(vals)[0], bet)
        spins, gifts, jumps, odd = A.build_spins(series, vals, bet, [], [])
        self.assertEqual(len(spins), len(truth))
        self.assertEqual([s['win'] for s in spins], truth)
        self.assertEqual((gifts, jumps), ([], []))
        self.assertEqual(steps[-1] - steps[0], sum(s['win'] for s in spins) - bet * len(spins))

    def test_small_bump_at_a_popup_is_a_gift_but_a_big_one_is_a_win(self):
        bet = 100; base = [10000, 9900]
        for bump, is_gift in ((5 * bet, True), (99 * bet, False)):          # gift_max_x default 15
            steps = base + [9900 + bump, 9800 + bump]; series = series_from(steps); vals = A.stable_values(series)
            t_bump = vals[2][0]
            spins, gifts, _, _ = A.build_spins(series, vals, bet, [(t_bump - 2, t_bump + 12)], [])
            self.assertEqual(bool(gifts), is_gift)
            self.assertEqual(sum(s['win'] for s in spins), 0 if is_gift else bump)

    def test_reconstruct_prefers_recorder_and_meter_bet_over_a_noisy_guess(self):
        steps = [10 ** 6, 10 ** 6 - 300, 10 ** 6 - 600, 10 ** 6 - 900]       # looks like bet 300 ...
        series = series_from(steps)
        R = A._reconstruct(series, [(0, 100), (10, 100)], {'bet': 100}, 4, say=lambda *_: None)
        self.assertEqual(R['bet'], 100)                                        # ... but recorder + bet meter agree on 100

    def test_spin_stats_and_wilson(self):
        core, mult = A.spin_stats([0, 0, 200, 0], [100] * 4)
        self.assertAlmostEqual(core['rtp'], 0.5); self.assertEqual(core['hit_every_n'], 4.0)
        self.assertEqual(sum(d['count'] for d in core['distribution']), 4)
        lo, hi = A.wilson(0, 4)
        self.assertEqual(lo, 0.0); self.assertTrue(0.4 < hi < 0.6)

    def test_trust_gates(self):
        ok = {'meter_read_rate': 0.9, 'balance_check': 0, 'bet': 100}
        self.assertTrue(A.trust_overall(ok) and A.trust_feature(ok))
        mid = dict(ok, meter_read_rate=0.55)
        self.assertTrue(A.trust_overall(mid)); self.assertFalse(A.trust_feature(mid))
        self.assertFalse(A.trust_overall(dict(ok, balance_check=500)))


class OffGameTime(_DefaultKnobs):
    """a recorder span that runs past the end of the video must not turn play time into zero (v1.25.0)"""
    def summarize(self, timeline, video_s=120):
        bet = 8000; steps = [10 ** 7 - k * bet for k in range(6)]
        series = series_from(steps); stats = {'bet': bet, 'video_s': video_s, 'timeline': timeline, 'name': 'T'}
        R = A._reconstruct(series, [], stats, 4, say=lambda *_: None)
        s, _ = A._summarize('x', {'name': 'App'}, stats, series, R, None, 'app', 0, say=lambda *_: None)
        return s

    def test_span_longer_than_the_video_is_clipped(self):
        s = self.summarize([[11.3, 414.7, '{"esc": true}']])
        self.assertEqual(s['off_game'], '0:01:48')
        self.assertGreater(s['play_hours'], 0)
        self.assertLess(s['spins_per_hour'], 5000)                    # was 4,000,000 before the clip
        self.assertEqual(s['balance_check'], 0)

    def test_normal_spans_are_unchanged(self):
        self.assertEqual(self.summarize([[10, 30, '{"x": 1}'], [50, 60, '{"x": 1}']])['off_game'], '0:00:30')


class Layout(unittest.TestCase):
    def test_prod_and_base_of_are_inverse_for_every_product(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, 'app', 'slot', '20260101_000000_Game')
            for suf, bucket in C.PROD_GROUP.items():
                p = C.prod(base, suf, make=False)
                self.assertEqual(os.path.basename(os.path.dirname(p)), bucket)
                self.assertEqual(C.base_of(p), base)
            self.assertEqual(C.base_of(base + '_summary.json'), base)             # legacy flat layout

    def test_find_prefers_new_then_legacy_and_outp_never_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            base = os.path.join(d, 'Game')
            self.assertEqual(C.find(base, '_stats.json'), C.prod(base, '_stats.json', make=False))
            open(base + '_stats.json', 'w').close()
            self.assertEqual(C.find(base, '_stats.json'), base + '_stats.json')
            self.assertEqual(C.outp(base, '_stats.json'), base + '_stats.json')   # overwrite in place, no second copy


class Knobs(unittest.TestCase):
    def test_every_adv_key_used_in_the_code_is_declared(self):
        used = set()
        for f in glob.glob(os.path.join(ROOT, 'core', '*.py')) + [os.path.join(ROOT, 'gui.py')]:
            with open(f, encoding='utf-8') as fh: used |= set(re.findall(r"\badv(?:_nz)?\('([a-z_]+)'\)", fh.read()))
        self.assertTrue(used, 'no adv() call found - regex broken?')
        self.assertEqual(used - set(C.ADVANCED), set())

    def test_adv_falls_back_to_the_default(self):
        old = dict(C.CFG)
        try:
            C.CFG.pop('feat_skip_s', None); self.assertEqual(C.adv('feat_skip_s'), C.ADVANCED['feat_skip_s'][0])
            C.CFG['feat_skip_s'] = 9; self.assertEqual(C.adv('feat_skip_s'), 9)
            C.CFG['gift_max_x'] = 0; self.assertEqual(C.adv_nz('gift_max_x'), C.ADVANCED['gift_max_x'][0])
        finally:
            C.CFG.clear(); C.CFG.update(old)


class Profiles(unittest.TestCase):
    """every app shipped in apps/: the profile parses, and the templates a recording needs are really there"""
    def apps(self):
        from core.app import App, list_apps
        return [App(i) for i, _ in list_apps()]

    def test_profiles_reference_existing_templates(self):
        for app in self.apps():
            p = app.p
            names = [p['ingame_marker'], p['lobby_marker'], p['spin_btn']] + list(p.get('x_templates') or []) \
                + [n for n, _ in p.get('dismiss_templates') or []] + ([p['low_coins']] if p.get('low_coins') else []) \
                + [s['template'] for steps in (p.get('enter_via') or {}).values() for s in steps if s.get('template')]
            missing = [n for n in names if not app.t.has(n)]
            self.assertEqual(missing, [], f'{app.id}: profile names templates that do not exist')

    def test_listed_slots_have_what_a_recording_needs(self):
        for app in self.apps():
            rows = app.slots(); ids = [r['id'] for r in rows]
            self.assertEqual(len(ids), len(set(ids)), f'{app.id}: duplicate slot id')
            for r in rows:
                missing = [n for n in app.required_templates(r['id']) if not app.t.has(n)]
                self.assertEqual(missing, [], f"{app.id}/{r['id']}")

    def test_enter_via_slot_needs_no_thumbnail(self):
        from core.app import App
        app = App('cash_rally')
        self.assertNotIn('wolf_run_eclipse', app.required_templates('wolf_run_eclipse'))
        self.assertIn('stinkin_rich', app.required_templates('stinkin_rich'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
