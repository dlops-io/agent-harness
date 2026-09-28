"""Exercise the live shipping code path through the real SDK with local responses."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from unittest.mock import patch

from acts.act3_workflow import run_act3
from formaggio.config import load_json
from formaggio.shop.checkout import Checkout
from formaggio.shop.data_models import LineItem
from tests.support import RecordingTest
from tests.test_layers import local_api
from tests.test_workflow_layers import ProposalResponses


class ShippingResponses(ProposalResponses):
    def __init__(self, *, never_repair=False, change_intake=False):
        super().__init__(revise=False)
        self.never_repair, self.change_intake = never_repair, change_intake
        self.valid_items = self.items
        self.submitted = [LineItem.model_validate(i) for i in load_json('shipping_customer_cart.json')['items']]

    def __call__(self, request):
        self.items = self.submitted if not self.change_intake and (not self.requests or self.never_repair) else self.valid_items
        return super().__call__(request)


class ShippingLiveLessonTests(RecordingTest):
    def execute(self, scenario, backend=None):
        backend = backend or ShippingResponses()
        checkout=Checkout(); lines=[]
        async def run():
            async with local_api(backend) as api:
                result = await run_act3(self.recorder, scenario=scenario, model='fixture-model',
                                       api_client=api, checkout=checkout, progress=lines.append)
            return result
        return asyncio.run(run()), backend, checkout, '\n'.join(lines)

    def test_before_is_model_intake_then_real_rejection_without_checkout(self):
        result, backend, checkout, output = self.execute('pa-shipping-blocked')
        self.assertEqual(result['mode'],'live')
        self.assertEqual(result['model_calls'],1)
        self.assertEqual(result['outcome'].status,'blocked')
        self.assertEqual(result['outcome'].attempts,1)
        self.assertEqual(checkout.orders,())
        self.assertEqual(checkout.store.inventory,Checkout().store.inventory)
        self.assertIn('RAW MILK',output)
        self.assertIn('Customer ask:',output)
        self.assertIn('My starting cart',output)
        self.assertNotIn('Use --fixture',output)
        events=self.recorder.timeline(result['run_id'])
        self.assertTrue(any(e['event_type']=='cart.intake_checked' and e['payload']['preserved'] for e in events))
        self.assertFalse(any(e['event_type'] in {'fixture.proposal','order.placed','approval.requested'} for e in events))

    def test_after_uses_same_intake_then_model_revision_with_feedback(self):
        before, before_api, _, _=self.execute('pa-shipping-blocked')
        after, after_api, checkout, output=self.execute('pa-shipping')
        self.assertEqual(before_api.requests[0],after_api.requests[0])
        self.assertEqual(after['model_calls'],2)
        self.assertEqual(after['outcome'].status,'placed')
        self.assertEqual(len(checkout.orders),1)
        self.assertNotIn('comte',[i.product for i in checkout.orders[0].report.items])
        self.assertIn('Removed: Comté',output)
        request=json.dumps(after_api.requests[1])
        self.assertIn('Revise the complete cart',request)
        self.assertIn('Fictional shop policy blocks raw-milk shipping',request)
        self.assertIn('Authoritative classroom shop policy',request)

    def test_model_cannot_bypass_intake_or_revision_limits(self):
        backend=ShippingResponses(change_intake=True)
        with self.assertRaisesRegex(ValueError,'changed the submitted cart'):
            self.execute('pa-shipping-blocked',backend)
        result,backend,checkout,_=self.execute('pa-shipping',ShippingResponses(never_repair=True))
        self.assertEqual(result['outcome'].status,'unresolved')
        self.assertEqual(result['model_calls'],3)
        self.assertEqual(checkout.orders,())
        self.assertEqual(checkout.store.inventory,Checkout().store.inventory)

    def test_cli_defaults_to_before_and_fixture_flag_is_not_required(self):
        import cli
        async def local(recorder, **kwargs):
            async with local_api(ShippingResponses()) as api:
                return await run_act3(recorder,api_client=api,**kwargs)
        output=io.StringIO()
        with patch('sys.argv',['cli.py','--act','3','--db',str(self.root/'cli.sqlite')]), patch('acts.act3_workflow.run_act3',side_effect=local) as run, redirect_stdout(output):
            cli.main()
        self.assertEqual(run.call_args.kwargs['scenario'],'pa-shipping-blocked')
        self.assertFalse(run.call_args.kwargs['fixture'])
        self.assertIn('WORKFLOW BLOCKED',output.getvalue())
        self.assertIn('Model calls: 1',output.getvalue())
        self.assertNotIn('Receipt:',output.getvalue())
