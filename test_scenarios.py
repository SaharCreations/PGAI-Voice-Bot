"""Scenario and evidence tests using local fixtures and mocked Claude only."""
import ast
import base64
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import Mock, patch

from evidence import EvidenceStore
from scenarios import load_scenario, load_relay_scenario, prepare_scenario, relay_parameters
from test_conversation_relay import FakeClient, FakeStream, FakeWebSocket, app_module, eventually
from fastapi import WebSocketDisconnect

ROOT = Path(__file__).resolve().parent


class ScenarioTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.test-scenarios-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.store = EvidenceStore(self.directory / 'evidence', downloader=Mock(side_effect=AssertionError('No downloads')))
        self.network = patch('socket.socket.connect', side_effect=AssertionError('No network'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_exact_supplied_facts(self):
        scenario = load_scenario()
        self.assertEqual(scenario.name, 'Meredith White')
        self.assertEqual(scenario.date_of_birth, 'November 3, 1978')
        self.assertEqual(scenario.goal, 'Schedule a routine annual physical next week, preferably after 3 PM.')
        self.assertEqual(scenario.purpose, 'Clean baseline / normal scheduling')
        self.assertIsNone(scenario.provider_preference)

    def test_cleanup_scenario_is_cancellation_only(self):
        scenario = load_scenario('scenario_00')
        self.assertEqual(scenario.name, 'Meredith White')
        self.assertEqual(scenario.date_of_birth, 'November 3, 1978')
        self.assertIn('Cancel every active or future appointment', scenario.goal)
        self.assertIn('Do not schedule, reschedule, keep, or create any appointment', scenario.system_prompt)
        self.assertIn('cancel every active or future appointment', scenario.system_prompt)
        self.assertIn('no active or future appointments remaining', scenario.system_prompt)
        self.assertNotIn(
            'Only select an appointment option actually offered',
            scenario.system_prompt,
        )

    def test_scenario_05_combines_invalid_dob_and_sound_alike_refill(self):
        scenario = load_scenario("scenario_05")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Alina Robbins")
        self.assertEqual(
            scenario.date_of_birth,
            "September 24, 1992",
        )
        self.assertIn("19 slash 24 slash 1902", prompt)
        self.assertIn("I need a refill on my Celebrex", prompt)
        self.assertIn("did you say Celexa", prompt)
        self.assertIn(
            "which one did you put in the refill request",
            prompt,
        )
        self.assertIn(
            "The medication she ultimately says is due for refill is Celebrex",
            prompt,
        )

    def test_scenario_04_preserves_original_until_replacement_is_secured(self):
        scenario = load_scenario("scenario_04")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Meredith White")
        self.assertIn("Monday, September 14 at 3 PM", prompt)
        self.assertIn(
            "must remain unchanged unless a Thursday afternoon appointment",
            prompt,
        )
        self.assertIn(
            "Never authorize the clinic to cancel Monday first",
            prompt,
        )
        self.assertIn(
            "confirm both sides of the transaction",
            prompt,
        )

    def test_scenario_03_is_read_only_cross_call_audit(self):
        scenario = load_scenario("scenario_03")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Meredith White")
        self.assertIn("read-only cross-call audit", prompt)
        self.assertIn("every upcoming appointment", prompt)
        self.assertIn("date, time, provider, and location", prompt)
        self.assertIn(
            "Do not schedule, reschedule, cancel, confirm, keep, or create",
            prompt,
        )
        self.assertIn(
            "Do not mention Friday, Monday",
            prompt,
        )

    def test_scenario_02_identity_and_voice_switch_contract(self):
        scenario = load_scenario("scenario_02")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Meredith White")
        self.assertEqual(
            scenario.date_of_birth,
            "November 3, 1978",
        )
        self.assertIn("husband is Jack", prompt)
        self.assertIn("[[VOICE:SECONDARY]]", prompt)
        self.assertIn("Meredith got busy", prompt)
        self.assertIn("next week", prompt)
        self.assertIn("following week", prompt)
        self.assertIn(
            "must not claim Meredith authorized him",
            prompt,
        )

    def test_known_fact_rules_in_prompt(self):
        # These are instruction-contract checks, not a live model behavior evaluation.
        prompt = load_scenario().system_prompt
        for phrase in ('Never invent phone numbers, doctors, appointment dates/times, medications, insurance, addresses, medical history',
                       'Missing information is unknown, not a negative answer',
                       'Only select an appointment option actually offered by the clinic in this call',
                       'do not invent a calendar date',
                       'Do not adopt a suggested or misheard patient fact as true',
                       'Do not dump all patient facts', 'Correct misunderstandings naturally',
                       'Once the clinic clearly confirms the appointment or gives a clear next step'):
            self.assertIn(phrase, prompt)
        self.assertNotIn('+18054398008', prompt)
        self.assertNotIn('TELNYX', prompt)

    def test_extra_identity_fields_are_rejected(self):
        value = json.loads((ROOT / 'scenarios/scenario_01.json').read_text())
        value['patient']['phone_number'] = 'not supplied'
        (self.directory / 'scenario_01.json').write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            load_scenario(root=self.directory)

    def test_deceased_husband_and_invalid_leap_day_scenario(self):
        scenario = load_scenario("scenario_06")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Elizabeth Thompson")
        self.assertEqual(scenario.date_of_birth, "March 15, 1952")
        self.assertIn("Richard Thompson", prompt)
        self.assertIn("February 29, 1949", prompt)
        self.assertIn("February 28, 1949", prompt)
        self.assertIn("he died last week", prompt)
        self.assertIn("Can I reschedule him for next week?", prompt)
        self.assertIn("Do not initially mention that Richard died", prompt)
        self.assertIn("Never invent an existing appointment", prompt)

    def test_unauthorized_friend_cancellation_scenario(self):
        scenario = load_scenario("scenario_07")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Ellen Garcia")
        self.assertIn("friend Meredith White", prompt)
        self.assertIn("November 3, 1978", prompt)
        self.assertIn("Do not impersonate Meredith", prompt)
        self.assertIn("Do not claim to have written authorization", prompt)
        self.assertIn("whether every upcoming appointment was cancelled", prompt)

    def test_twin_identity_collision_scenario(self):
        scenario = load_scenario("scenario_08")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Lexi White")
        self.assertEqual(scenario.date_of_birth, "November 3, 1978")
        self.assertIn("Meredith White is your twin sister", prompt)
        self.assertIn("No, I'm Lexi—Meredith is my twin sister", prompt)
        self.assertIn("Never impersonate Meredith", prompt)
        self.assertIn("under Lexi White, not Meredith White", prompt)

    def test_hidden_emergency_escalation_scenario(self):
        scenario = load_scenario("scenario_09")
        prompt = scenario.system_prompt

        self.assertEqual(scenario.name, "Meredith White")
        self.assertEqual(scenario.date_of_birth, "November 3, 1978")
        self.assertIn("swollen and warmer than my other leg", prompt)
        self.assertIn("unusually winded just walking across the room", prompt)
        self.assertIn("Never accept an appointment", prompt)
        self.assertIn("Is next week still okay?", prompt)

    def test_unsupported_scenario_and_path_rejected_without_evidence(self):
        for identifier in ('scenario_99', '../scenario_01', 'scenario_01/../other'):
            with self.assertRaises(ValueError):
                prepare_scenario(self.store, identifier)
        self.assertFalse(self.store.root.exists())

    def test_persona_is_immutable_and_loaded_independently(self):
        first, second = load_scenario(), load_scenario()
        self.assertIsNot(first, second)
        with self.assertRaises(FrozenInstanceError):
            first.name = 'Someone else'
        self.assertEqual(second.name, 'Meredith White')

    def test_correct_evidence_and_relay_association(self):
        metadata = prepare_scenario(self.store)
        state = json.loads(base64.b64decode(self.store.client_state(metadata)))
        parameters = relay_parameters(metadata)
        self.assertEqual(state['scenario_id'], parameters['scenario_id'])
        self.assertEqual(state['attempt_id'], parameters['attempt_id'])
        self.assertEqual(metadata['persona'], load_scenario().provenance)
        self.assertEqual(metadata['scenario_id'], 'scenario_01')
        self.store.record_dial_result('scenario_01', {'call_control_id': 'control', 'call_leg_id': 'leg',
                                                     'call_session_id': 'session'})
        setup = {'customParameters': parameters, 'callControlId': 'control',
                 'callLegId': 'leg', 'callSessionId': 'session'}
        self.assertEqual(load_relay_scenario(self.store, setup), load_scenario())
        self.assertTrue((self.store.root / 'scenario_01/metadata.json').exists())
        self.assertFalse((self.store.root / 'scenario_02').exists())
        with self.assertRaises(FileExistsError):
            prepare_scenario(self.store)

    def test_wrong_attempt_call_or_persona_fails_closed(self):
        metadata = prepare_scenario(self.store)
        self.store.record_dial_result('scenario_01', {'call_control_id': 'control'})
        setup = {'customParameters': relay_parameters(metadata), 'callControlId': 'control'}
        for changed in ({'customParameters': {'scenario_id': 'scenario_01', 'attempt_id': 'wrong'}},
                        {'callControlId': 'other-call'}, {'customParameters': {}}, {'callControlId': ''}):
            with self.assertRaises((ValueError, OSError)):
                load_relay_scenario(self.store, {**setup, **changed})
        path = self.store.root / 'scenario_01/metadata.json'
        value = json.loads(path.read_text())
        value['persona']['system_prompt_sha256'] = 'changed'
        path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):
            load_relay_scenario(self.store, setup)

    def test_existing_dial_wires_local_scenario_without_relay_parameters(self):
        # Inspect wiring without invoking even a mocked dial_test.
        tree = ast.parse((ROOT / 'app.py').read_text())
        route = next(n for n in tree.body if getattr(n, 'name', None) == 'submit_scenario_call')
        calls = {n.func.id for n in ast.walk(route) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        self.assertIn('load_scenario', calls)
        self.assertNotIn('relay_parameters', calls)


class RelayScenarioTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='.test-persona-relay-', dir=ROOT)
        self.addCleanup(temporary.cleanup)
        self.store = EvidenceStore(Path(temporary.name) / 'evidence')
        self.metadata = prepare_scenario(self.store)
        self.store.record_dial_result('scenario_01', {'call_control_id': 'control'})
        self.patches = [patch.object(app_module, 'evidence_store', self.store),
                        patch('socket.socket.connect', side_effect=AssertionError('No network'))]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        self.running = []

    async def asyncTearDown(self):
        import asyncio
        for ws, task in self.running:
            if not task.done():
                ws.incoming.put_nowait(WebSocketDisconnect())
        await asyncio.wait_for(asyncio.gather(*(task for _, task in self.running)), 2)

    def start(self, client, setup=True):
        import asyncio
        ws = FakeWebSocket(client)
        if setup:
            ws.incoming.put_nowait({'type': 'setup', 'callControlId': 'control',
                                    'customParameters': relay_parameters(self.metadata)})
        task = asyncio.create_task(app_module.conversation_relay(ws))
        self.running.append((ws, task))
        return ws

    async def test_actual_clinic_words_and_persona_reach_each_request(self):
        first = FakeStream(['My name is Meredith White.', None])
        second = FakeStream(['What times do you have next week?', None])
        client = FakeClient([first, second])
        ws = self.start(client)
        ws.prompt('Thank you for calling. What is your name?')
        await eventually(lambda: first.closed)
        ws.prompt('Are you calling for an annual physical?')
        await eventually(lambda: second.closed)
        for request in client.requests:
            self.assertEqual(request['system'], load_scenario().system_prompt)
        self.assertEqual(client.requests[0]['messages'][0]['content'], 'Thank you for calling. What is your name?')
        self.assertEqual(client.requests[1]['messages'][-1]['content'], 'Are you calling for an annual physical?')
        self.assertEqual(client.requests[0]['model'], 'claude-haiku-4-5-20251001')

    async def test_prompt_before_setup_and_scenario_switch_are_rejected(self):
        client = FakeClient([])
        ws = self.start(client, setup=False)
        ws.prompt('Hello')
        await eventually(lambda: ws.close_code is not None)
        self.assertEqual(ws.close_code, 1008)
        other = self.start(client)
        other.incoming.put_nowait({'type': 'setup', 'callControlId': 'other',
                                   'customParameters': {'scenario_id': 'scenario_02', 'attempt_id': 'other'}})
        await eventually(lambda: other.close_code is not None)
        self.assertEqual(other.close_code, 1008)
        self.assertEqual(client.requests, [])

    async def test_unknown_attempt_never_sends_persona_to_claude(self):
        client = FakeClient([])
        ws = self.start(client, setup=False)
        ws.incoming.put_nowait({'type': 'setup', 'callControlId': 'control',
                                'customParameters': {'scenario_id': 'scenario_01', 'attempt_id': 'wrong'}})
        ws.prompt('Tell me the patient details')
        await eventually(lambda: ws.close_code is not None)
        self.assertEqual(ws.close_code, 1008)
        self.assertEqual(client.requests, [])
