"""Classroom comparison uses measured outcomes and preserves saved-label CLI behavior."""
import asyncio
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from unittest.mock import patch

import cli
from acts.act2_context import build_act2, context_comparison, print_context_comparison
from formaggio.agents.harness import Harness
from formaggio.fixtures.evaluation_fixture import ProposalFixture
from tests.support import RecordingTest


class ContextCompareTests(RecordingTest):
    def results(self, basic='standard', enriched='personalized', scenario='personalized'):
        async def execute():
            results=[]
            for mode, proposal in [('basic',basic),('enriched',enriched)]:
                async with ProposalFixture('accepted', proposal).client() as api:
                    results.append(await build_act2(mode=mode,scenario=scenario,model='fixture-model',execution_mode='fixture').run(self.recorder,api_client=api))
            return results
        return asyncio.run(execute())

    def test_improvement_ties_regressions_and_unknown_usage(self):
        for basic,enriched,expected in [('standard','personalized','Better personalization'),
                                        ('personalized','personalized','No personalization improvement'),
                                        ('personalized','standard','Worse personalization')]:
            with self.subTest(expected=expected):
                results=self.results(basic,enriched)
                events={r['run_id']:self.recorder.timeline(r['run_id']) for r in results}
                report=context_comparison(results,events)
                self.assertIn(expected,report['verdict'])
                self.assertEqual(report['rows'][0]['input_tokens'],200)
                self.assertIsNone(context_comparison(results)['rows'][0]['input_tokens'])
        results=self.results()
        results[1]['cart_check_status']='failed'
        self.assertIn('No overall improvement',context_comparison(results)['verdict'])
        results[1]['context_evidence']['mild_match']=None
        self.assertIn('unavailable',context_comparison(results)['verdict'])

    def test_override_is_not_graded_against_all_mild_and_duplicate_varieties_count_once(self):
        results=self.results(scenario='preference-override',enriched='standard')
        self.assertIn('No mild-preference comparison',context_comparison(results)['verdict'])
        results=self.results()
        evidence=results[0]['context_evidence']
        evidence['products'].append(evidence['products'][0])
        self.assertEqual(context_comparison(results)['rows'][0]['variety_count'],3)
        output=io.StringIO()
        with redirect_stdout(output):print_context_comparison(results)
        self.assertIn('unavailable',output.getvalue())
        self.assertNotIn('Input-token cost:',output.getvalue())

    def test_exact_cli_command_runs_both_modes_exports_metrics_and_keeps_full_trace(self):
        original=Harness.run;calls=[]
        async def local_run(harness, recorder, **kwargs):
            calls.append((harness.scenario,harness.model,harness.layers))
            mode=next(layer.mode for layer in harness.layers if layer.name=='context')
            async with ProposalFixture('accepted','standard' if mode=='basic' else 'personalized').client() as api:
                return await original(replace(harness,execution_mode='fixture'),recorder,api_client=api,**kwargs)
        output=io.StringIO();db=self.root/'comparison.sqlite';export=self.root/'comparison.json'
        with patch('sys.argv',['cli.py','--act','2','--compare','--model','fixture-model','--db',str(db),'--json-output',str(export)]), patch.object(Harness,'run',local_run), redirect_stdout(output):
            cli.main()
        self.assertEqual([c[:2] for c in calls],[('personalized','fixture-model')]*2)
        text=output.getvalue()
        for phrase in ('BASIC','ENRICHED','Better personalization','1/3 (33.3333%)','3/3 (100%)','Input tokens','Starting basic','Starting enriched'):
            self.assertIn(phrase,text)
        self.assertNotIn('🤖 Model call',text)
        self.assertNotIn('Harness policy check',text)
        saved=json.loads(export.read_text())
        self.assertEqual(saved['scenario'],'personalized')
        self.assertEqual(saved['rows'][1]['input_tokens'],200)
        with type(self.recorder)(db) as recorder:
            for row in saved['rows']:
                self.assertTrue(any(e['event_type']=='model.request' for e in recorder.timeline(row['run_id'])))

    def test_saved_comparison_still_uses_labels_and_never_calls_agents(self):
        output=io.StringIO()
        with patch('sys.argv',['cli.py','--compare','before','after','--db',str(self.root/'saved.sqlite')]), patch('cli.compare',return_value={'result':'saved'}) as compare, patch.object(Harness,'run',side_effect=AssertionError('No agent call allowed')), redirect_stdout(output):
            cli.main()
        self.assertEqual(compare.call_args.args[1:],('before','after'))
        self.assertIn('saved',output.getvalue())

    def test_invalid_combinations_rejected_before_database_or_live_calls(self):
        arguments=[[],['--compare'],['--compare','one'],['--compare','one','two','three'],
                   ['--act','1','--compare'],['--act','2','--compare','one','two'],
                   ['--act','2','--compare','--context','basic'],
                   ['--act','2','--compare','--evaluate','core','--label','x'],
                   ['--report','x','--compare','a','b']]
        for args in arguments:
            with self.subTest(args=args), patch('sys.argv',['cli.py',*args]), patch('cli.Recorder',side_effect=AssertionError('No database')), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                cli.main()
            self.assertEqual(error.exception.code,2)
