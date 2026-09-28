"""The lesson shows actual proposals and revisions, without changing the workflow."""
import asyncio
from contextlib import redirect_stdout
import io
from unittest.mock import patch

import cli
from acts.act3_workflow import run_act3
from formaggio.agents.context import customer_ask, load_scenario
from formaggio.operations.workflow_view import proposal_details, proposal_lines
from formaggio.shop.data_models import LineItem
from formaggio.shop.store import Store
from tests.support import RecordingTest
from tests.test_layers import local_api
from tests.test_workflow_layers import ProposalResponses


class WorkflowOutputTests(RecordingTest):
    def test_cli_shows_ask_raw_milk_and_real_removal_then_receipt(self):
        output = io.StringIO()
        with patch('sys.argv', ['cli.py','--act','3','--scenario','pa-shipping','--fixture','--db',str(self.root/'cli.sqlite')]), redirect_stdout(output):
            cli.main()
        text = output.getvalue()
        for phrase in ('Classroom simulation', '👤 Customer ask:', customer_ask(load_scenario('pa-shipping')),
                       'Comté 18 mois (comte) — 350 g · RAW MILK', 'shipping: comte',
                       'Removed: Comté 18 mois (comte) — 350 g', 'Added: Taleggio (taleggio) — 350 g',
                       'Shipping policy for PA: passed', 'Order placed.', 'Receipt:'):
            self.assertIn(phrase, text)
        self.assertNotIn('Mock', text)
        self.assertLess(text.index('RAW MILK'), text.index('Cart attempt 1: shipping'))
        self.assertLess(text.index('Cart attempt 1: shipping'), text.index('Removed:'))
        self.assertLess(text.index('Cart attempt 2: valid'), text.index('Receipt:'))

    def test_repeated_invalid_cart_says_unchanged_and_never_prints_receipt(self):
        lines = []
        result = asyncio.run(run_act3(self.recorder,scenario='pa-shipping-blocked',fixture=True,progress=lines.append))
        output = io.StringIO()
        with redirect_stdout(output):cli.print_workflow_result(result)
        self.assertEqual(sum('same cart was proposed again' in line for line in lines), 2)
        self.assertTrue(any('Revision limit reached; no order' in line for line in lines))
        self.assertNotIn('Receipt:', output.getvalue())
        proposals=[e['payload'] for e in self.recorder.timeline(result['run_id']) if e['event_type']=='cart.proposed']
        self.assertEqual(len(proposals),3)
        self.assertEqual(proposals[1]['removed'],[])
        self.assertEqual(proposals[1]['added'],[])

    def test_differences_aggregate_aliases_and_preserve_unknown_products(self):
        store=Store()
        before=[LineItem(product='Comte',grams=150),LineItem(product='comte',grams=200)]
        after=[LineItem(product='comte',grams=300),LineItem(product='unknown',grams=100)]
        details={'attempt':2,**proposal_details(store,after,before)}
        self.assertEqual(details['removed'],[])
        self.assertEqual(details['quantity_changes'],[{'product':'comte','before_grams':350,'after_grams':300}])
        self.assertIsNone(details['added'][0]['raw_milk'])
        text='\n'.join(proposal_lines(details))
        self.assertIn('unknown product',text)
        self.assertIn('350 g → 300 g',text)
