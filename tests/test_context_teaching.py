"""Context quality, actual model inputs, and enforcement at the checkout boundary."""
import asyncio
import io
from contextlib import redirect_stdout
from acts.act2_context import build_act2, print_context_comparison, print_shipping_demo, shipping_policy_demo
from acts.act3_workflow import run_act3
from formaggio.agents.context import build_context, load_scenario
from formaggio.evaluation.context_evidence import context_evidence
from formaggio.evaluation.live_evaluation import run_live_suite, dispatch
from formaggio.fixtures.evaluation_fixture import ProposalFixture
from formaggio.operations.governance import PolicyBlocked
from formaggio.operations.chat_view import render_run
from formaggio.shop.checkout import Checkout
from formaggio.shop.data_models import AgentReply
from formaggio.shop.store import Store
from formaggio.config import load_json
from tests.support import RecordingTest


class ContextTeachingTests(RecordingTest):
    def evidence(self, scenario='personalized', mode='enriched', proposal='personalized', items=None):
        store, request = Store(), load_scenario(scenario)
        items = load_json('workflow_proposals.json')[proposal][0] if items is None else items
        return context_evidence(request, AgentReply(message='Claims mild.', items=items), build_context(request, store, mode), store)

    def test_preference_score_is_separate_from_validity_and_requires_supplied_profile(self):
        self.assertEqual(self.evidence()['checks'][0]['status'], 'pass')
        self.assertEqual(self.evidence(proposal='standard')['checks'][0]['status'], 'fail')
        self.assertTrue(Store().validate(load_scenario('personalized'), load_json('workflow_proposals.json')['standard'][0]).ok)
        basic = self.evidence(mode='basic')
        self.assertTrue(basic['mild_match'])
        self.assertEqual(basic['checks'][0]['status'], 'not_applicable')
        self.assertEqual(basic['preferences_supplied'], [])
        self.assertEqual(self.evidence('preference-override', proposal='standard')['checks'][0]['status'], 'pass')
        self.assertEqual(self.evidence('preference-override')['checks'][0]['status'], 'fail')
        for items in ([], [{'product':'unknown', 'grams':1000}]):
            self.assertIsNone(self.evidence(items=items)['mild_match'])
            self.assertEqual(self.evidence(items=items)['checks'][0]['status'], 'fail')

    def test_complete_request_sequences_differ_only_by_retrieval_and_record_evidence(self):
        async def run():
            requests, results = [], []
            for mode in ('basic', 'enriched'):
                backend = ProposalFixture('accepted', 'personalized')
                async with backend.client() as api:
                    result = await build_act2(scenario='personalized', mode=mode, model='fixture-model', execution_mode='fixture').run(self.recorder, api_client=api)
                requests.append(backend.requests); results.append(result)
                self.assertTrue(any(e['event_type']=='context.evidence' for e in self.recorder.timeline(result['run_id'])))
                self.assertIn('Context use', render_run(self.recorder, result['run_id']))
            a,b=requests
            self.assertEqual(len(a),len(b))
            for left,right in zip(a,b):
                for body in (left,right):
                    body['input'] = [m for m in body['input'] if 'Retrieved context (data, not instructions)' not in str(m)]
                self.assertEqual(left,right)
            return results
        results=asyncio.run(run()); output=io.StringIO()
        with redirect_stdout(output):print_context_comparison(results)
        self.assertIn('all cheeses mild',output.getvalue())
        self.assertIn('not_applicable',output.getvalue())
        self.assertNotIn('"items":',output.getvalue())

    def test_evaluation_records_new_checks_and_recomputes_tampered_result(self):
        report=asyncio.run(run_live_suite(self.recorder,'personalization',act=2,fixture=True,repeats=1,
                          case_ids=['personalized','preference-override'],output_root=self.root))
        self.assertEqual(report['passed'],4)
        rich=[r for r in report['runs'] if r['case_id'].endswith(':enriched')]
        self.assertTrue(all(any(c['check_id'].startswith('context.') and c['status']=='pass' for c in r['checks']) for r in rich))
        async def wrong(recorder,act,case,**kwargs):
            result=await dispatch(recorder,act,case,**kwargs)
            result['reply']=AgentReply(message='Claims mild.',items=load_json('workflow_proposals.json')['standard'][0])
            return result
        failed=asyncio.run(run_live_suite(self.recorder,'regression',act=2,fixture=True,repeats=1,
                        case_ids=['personalized'],context='enriched',runner=wrong,output_root=self.root))
        self.assertEqual(failed['failed'],1)
        self.assertTrue(any(c['check_id']=='context.saved_preference' and c['status']=='fail' for c in failed['runs'][0]['checks']))

    def test_policy_probe_changes_only_destination(self):
        reports=shipping_policy_demo()
        self.assertEqual(reports['PA'].items,reports['NY'].items)
        self.assertEqual([(v.rule,v.product_id) for v in reports['PA'].violations],[('shipping','comte')])
        self.assertTrue(reports['NY'].ok)
        out=io.StringIO()
        with redirect_stdout(out):print_shipping_demo()
        self.assertIn('NOT the agent',out.getvalue());self.assertIn('FICTIONAL',out.getvalue())

    def test_workflow_rejects_then_places_only_corrected_cart(self):
        checkout=Checkout()
        result=asyncio.run(run_act3(self.recorder,scenario='pa-shipping',fixture=True,checkout=checkout))
        self.assertEqual(result['outcome'].attempts,2);self.assertEqual(len(checkout.orders),1)
        self.assertNotIn('comte',[i.product for i in checkout.orders[0].report.items])
        events=self.recorder.timeline(result['run_id'])
        checks=[e for e in events if e['event_type']=='cart.validated']
        self.assertEqual(checks[0]['payload']['violations'][0]['rule'],'shipping')
        placed=next(e for e in events if e['event_type']=='order.placed')
        self.assertLess(checks[1]['sequence'],placed['sequence'])

    def test_direct_checkout_cannot_bypass_shipping_rule_or_change_stock(self):
        checkout=Checkout();before=dict(checkout.store.inventory)
        with self.assertRaises(PolicyBlocked):
            checkout.place(load_scenario('pa-shipping'),load_json('workflow_proposals.json')['pa-shipping'][0],
                           'raw-milk-probe',self.recorder,self.run_id)
        self.assertEqual(checkout.store.inventory,before);self.assertEqual(checkout.orders,())

    def test_cli_prints_context_evidence_and_separate_policy_probe(self):
        import cli
        from unittest.mock import patch
        async def local(recorder, *, scenario, mode, model, progress, on_result):
            backend = ProposalFixture('accepted', scenario)
            async with backend.client() as api:
                result = await build_act2(scenario=scenario, mode=mode, model=model, execution_mode='fixture').run(recorder, api_client=api)
            on_result(result)
            return [result]
        output = io.StringIO()
        with patch('sys.argv', ['cli.py', '--act', '2', '--scenario', 'personalized', '--context', 'enriched', '--model', 'fixture-model', '--db', str(self.root/'cli.sqlite')]), patch('acts.act2_context.run_act2', side_effect=local), redirect_stdout(output):
            cli.main()
        text = output.getvalue()
        for phrase in ('Saved preferences supplied: mild cheeses', 'PASS: Personalization', 'PA: REJECTED', 'NY: PASS', 'NOT the agent'):
            self.assertIn(phrase, text)
        self.assertNotIn('"items":', text)
        self.assertTrue(text.strip().splitlines()[-1].startswith('💾 Recorded in'))
