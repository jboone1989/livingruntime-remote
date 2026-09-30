from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import cognition


class CortexResponseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'LIVINGRUNTIME_COGNITION_ROOT': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def request(self, rid='llmreq_contract', purpose='content_cognition', tools=None):
        return cognition.submit(agent_id='ferro', purpose=purpose,
            messages=[{'role':'user','content':'Inspect the supplied file.'}],
            tools=tools, metadata={'routing_task_class':purpose}, request_id=rid)

    def claim(self, request):
        return cognition.claim_request(request_id=request['request_id'],
            watcher_id='chatgpt-work-ferro-cortex', claim_seconds=300)

    def test_string_envelope_preserves_real_tool_selection(self):
        request = self.request(tools=[{'name':'read','parameters':{}}])
        claim = self.claim(request)
        answer = {'schema':'livingruntime.cognition-response.v1', 'text':'',
            'tool_calls':[{'id':'call_read','name':'read','arguments':{'path':'README.md'}}]}
        done = cognition.complete(request_id=request['request_id'],
            claim_token=claim['claim']['token'], response_text=json.dumps(answer))
        self.assertEqual(done['response']['text'], '')
        self.assertEqual(done['response']['tool_calls'], answer['tool_calls'])

    def test_unmarked_json_answer_is_preserved(self):
        request = self.request()
        claim = self.claim(request)
        answer = '{"text":"answer","tool_calls":[]}'
        done = cognition.complete(request_id=request['request_id'],
            claim_token=claim['claim']['token'], response_text=answer)
        self.assertEqual(done['response']['text'], answer)

    def test_unknown_tool_fails_without_completing(self):
        request = self.request(tools=[{'name':'read','parameters':{}}])
        claim = self.claim(request)
        answer = {'schema':'livingruntime.cognition-response.v1', 'text':'',
            'tool_calls':[{'id':'call_bad','name':'delete','arguments':{}}]}
        with self.assertRaisesRegex(ValueError, 'not supplied'):
            cognition.complete(request_id=request['request_id'], claim_token=claim['claim']['token'],
                response_text=json.dumps(answer))
        self.assertEqual(cognition.get(request['request_id'])['status'], 'DISPATCHED')

    def test_claim_next_stays_in_lane_despite_higher_priority_public_request(self):
        self.request('llmreq_content_next')
        self.request('llmreq_public_next', 'simple_public_text')
        claimed = cognition.claim_next(agent_id='ferro', watcher_id='chatgpt-work-ferro-cortex', lane='content')
        self.assertEqual(claimed['request_id'], 'llmreq_content_next')
        self.assertEqual(cognition.get('llmreq_public_next')['status'], 'PENDING')


class CortexBridgeTests(unittest.TestCase):
    setUp = CortexResponseTests.setUp
    tearDown = CortexResponseTests.tearDown
    request = CortexResponseTests.request
    claim = CortexResponseTests.claim
    def test_watch_does_not_claim_or_wait(self):
        import bridge
        request = self.request()
        with patch.object(bridge, '_audit'), patch.object(bridge, 'wait_pending_cognition_request', side_effect=AssertionError('poll')):
            receipt = bridge.watch_agent_cognition('ferro')
        self.assertEqual(receipt['watcherId'], 'chatgpt-work-ferro-cortex')
        self.assertEqual(receipt['watcherState'], 'SUBSCRIPTION_REQUIRED')
        self.assertFalse(receipt['autoClaimed'])
        self.assertEqual(cognition.get(request['request_id'])['status'], 'PENDING')

    def test_completion_returns_immediate_same_lane_without_wait(self):
        import bridge
        request = self.request('llmreq_current')
        claim = self.claim(request)
        self.request('llmreq_next')
        self.request('llmreq_other', 'simple_public_text')
        with patch.object(bridge, '_audit'), patch.object(bridge, 'wait_pending_cognition_request', side_effect=AssertionError('poll')):
            receipt = bridge.complete_llm_request(request_id=request['request_id'],
                claim_token=claim['claim']['token'], response_text='Actual model answer')
        self.assertTrue(receipt['nextRequestAutoClaimed'])
        self.assertEqual(receipt['request_id'], 'llmreq_next')
        self.assertEqual(receipt['claim']['watcher_id'], 'chatgpt-work-ferro-cortex')
        self.assertEqual(cognition.get('llmreq_other')['status'], 'PENDING')


if __name__ == '__main__':
    unittest.main()
