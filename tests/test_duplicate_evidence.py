import copy
import datetime as dt
import tempfile
import unittest
from pathlib import Path

from talent_credentials.credential_review.evidence_registry import EvidenceRegistry
from talent_credentials.recognition.engine import RecognitionEngine
from talent_credentials.service import ApplicationService
from talent_credentials.storage import Database

TODAY = dt.date(2026, 11, 16)


def experience(serial, **extra):
    row = {'type': 'experience', 'issuer': 'employer', 'serial': serial}
    row.update(extra)
    return row


class DuplicateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.registry = EvidenceRegistry()
        self.engine = RecognitionEngine()
        self.diploma = {'type': 'diploma', 'issuer': 'university', 'serial': 'D-1'}
        self.rulebook = {'version': 'v2', 'rules': [{'type': 'diploma', 'minimum': 1}, {'type': 'experience', 'minimum': 2}]}
        self.rules = self.rulebook['rules']

    def duplicated_uploads(self, serial='E-1'):
        return [
            experience(serial, received_at='2026-10-01', batch='b1'),
            experience(serial, received_at='2026-11-01', batch='b2'),
        ]

    def test_same_material_twice_does_not_meet_threshold(self):
        evidence = [self.diploma] + self.duplicated_uploads()
        result = self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reasons'], ['rule_unsatisfied:experience'])
        self.assertEqual(result['active_counts'], {'diploma': 1, 'experience': 1})
        self.assertEqual(result['submissions'], 3)
        self.assertEqual(len(result['duplicates']), 1)
        self.assertEqual(result['duplicates'][0]['type'], 'experience')
        self.assertEqual(result['duplicates'][0]['copies'], 1)
        self.assertEqual(result['duplicates'][0]['digest'], self.registry.digest(experience('E-1')))

    def test_distinct_materials_meet_threshold(self):
        evidence = [self.diploma, experience('E-1'), experience('E-2', issuer='other-employer')]
        result = self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['active_counts']['experience'], 2)
        self.assertEqual(result['duplicates'], [])
        self.assertEqual(len(result['evidence_digests']), 3)

    def test_revoked_copy_excluded_active_copy_counts_once(self):
        evidence = [self.diploma, experience('E-1'), experience('E-1', revoked=True), experience('E-2')]
        result = self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.assertTrue(result['accepted'])
        self.assertEqual(result['active_counts']['experience'], 2)
        self.assertEqual(result['submissions'], 3)

    def test_all_copies_revoked_means_material_unavailable(self):
        evidence = [self.diploma, experience('E-1', revoked=True), experience('E-1', revoked=True, batch='b2')]
        result = self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['active_counts'], {'diploma': 1})
        self.assertEqual(result['submissions'], 1)

    def test_expired_upload_excluded_and_late_resubmission_counts(self):
        expired = experience('E-1', expires='2026-01-01')
        fresh = experience('E-2')
        result = self.registry.evaluate({'evidence': [self.diploma, expired, expired.copy(), fresh]}, self.rulebook, TODAY)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['active_counts'], {'diploma': 1, 'experience': 1})
        supplemented = self.registry.evaluate({'evidence': [self.diploma, expired, fresh, experience('E-3')]}, self.rulebook, TODAY)
        self.assertTrue(supplemented['accepted'])

    def test_replay_treats_duplicate_as_one_in_every_version(self):
        evidence = [self.diploma] + self.duplicated_uploads()
        books = [
            {'version': 'v1', 'rules': [{'type': 'diploma', 'minimum': 1}, {'type': 'experience', 'minimum': 1}]},
            {'version': 'v2', 'rules': [{'type': 'diploma', 'minimum': 1}, {'type': 'experience', 'minimum': 2}]},
        ]
        replay = self.registry.replay({'evidence': evidence}, books, TODAY)
        self.assertTrue(replay['v1']['accepted'])
        self.assertFalse(replay['v2']['accepted'])
        self.assertEqual(replay['v2']['active_counts']['experience'], 1)
        changes = self.registry.changed_decisions(replay)
        self.assertIn({'from': 'v1', 'to': 'v2', 'accepted_from': True, 'accepted_to': False}, changes)

    def test_replay_with_genuinely_distinct_materials(self):
        evidence = [self.diploma, experience('E-1'), experience('E-2')]
        books = [
            {'version': 'v1', 'rules': [{'type': 'experience', 'minimum': 1}]},
            {'version': 'v2', 'rules': [{'type': 'experience', 'minimum': 2}]},
        ]
        replay = self.registry.replay({'evidence': evidence}, books, TODAY)
        self.assertTrue(replay['v1']['accepted'])
        self.assertTrue(replay['v2']['accepted'])
        self.assertEqual(self.registry.changed_decisions(replay), [])

    def test_appeal_scope_ignores_submission_metadata(self):
        old = [experience('E-1', received_at='2026-10-01', batch='b1')]
        new = [experience('E-1', received_at='2026-11-01', batch='b2', review_note='rechecked')]
        scope = self.registry.appeal_scope(old, new)
        self.assertEqual(scope['added'], [])
        self.assertEqual(scope['removed'], [])
        self.assertFalse(scope['requires_full_review'])

    def test_appeal_scope_dropping_one_duplicate_copy_changes_nothing(self):
        uploads = self.duplicated_uploads()
        scope = self.registry.appeal_scope(uploads, uploads[:1])
        self.assertEqual(scope['added'], [])
        self.assertEqual(scope['removed'], [])
        self.assertFalse(scope['requires_full_review'])

    def test_appeal_scope_flags_genuine_removal_and_addition(self):
        old = [experience('E-1'), experience('E-2')]
        new = [experience('E-1'), experience('E-3')]
        scope = self.registry.appeal_scope(old, new)
        self.assertEqual(scope['added'], [self.registry.digest(experience('E-3'))])
        self.assertEqual(scope['removed'], [self.registry.digest(experience('E-2'))])
        self.assertTrue(scope['requires_full_review'])

    def test_engine_judgment_matches_registry_basis(self):
        evidence = [self.diploma] + self.duplicated_uploads()
        context = {'today': '2026-11-16', 'rules': self.rules}
        result = self.engine.evaluate({'id': 'app-dup', 'evidence': evidence}, context)
        registry_result = self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.assertFalse(result.accepted)
        self.assertEqual(result.accepted, registry_result['accepted'])
        self.assertEqual(list(result.reasons), registry_result['reasons'])
        self.assertEqual(result.facts['counts'], registry_result['active_counts'])
        self.assertEqual(result.facts['duplicates'], registry_result['duplicates'])
        self.assertEqual(result.facts['submissions'], registry_result['submissions'])

    def test_engine_rulebook_path_matches_rules_path(self):
        evidence = [self.diploma] + self.duplicated_uploads()
        via_rules = self.engine.evaluate({'id': 'app-dup', 'evidence': evidence}, {'today': '2026-11-16', 'rules': self.rules})
        via_rulebook = self.engine.evaluate({'id': 'app-dup', 'evidence': evidence}, {'today': '2026-11-16', 'rulebook': self.rulebook})
        self.assertEqual(via_rules.accepted, via_rulebook.accepted)
        self.assertEqual(via_rules.reasons, via_rulebook.reasons)

    def test_allocation_grouping_excludes_duplicate_only_applicant(self):
        solo = {'id': 'solo', 'evidence': self.duplicated_uploads(), 'demand': 1}
        pair = {'id': 'pair', 'evidence': [experience('E-1'), experience('E-2')], 'demand': 1}
        context = {'today': '2026-11-16', 'capacity': 1, 'rules': [{'type': 'experience', 'minimum': 2}]}
        accepted, rejected = self.engine.allocate([solo, pair], context)
        self.assertEqual([item.key for item in accepted], ['pair'])
        self.assertEqual([item.key for item in rejected], ['solo'])
        self.assertIn('rule_unsatisfied:experience', rejected[0].reasons)

    def test_evaluation_preserves_upload_records(self):
        evidence = [self.diploma] + self.duplicated_uploads()
        snapshot = copy.deepcopy(evidence)
        self.registry.evaluate({'evidence': evidence}, self.rulebook, TODAY)
        self.engine.evaluate({'id': 'app-dup', 'evidence': evidence}, {'today': '2026-11-16', 'rules': self.rules})
        self.registry.replay({'evidence': evidence}, [self.rulebook], TODAY)
        self.assertEqual(evidence, snapshot)

    def test_service_decision_and_audit_trail(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        database=Database(Path(temp.name)/'db.sqlite3')
        service=ApplicationService(database)
        payload = {
            'id': 'app-dup',
            'owner_id': 'person-9',
            'evidence': [self.diploma] + self.duplicated_uploads(),
            'effective_from': '2026-11-16',
        }
        context = {'today': '2026-11-16', 'rules': self.rules}
        first = service.process(payload, context, 'req-dup-1')
        self.assertFalse(first['accepted'])
        self.assertIn('rule_unsatisfied:experience', first['reasons'])
        self.assertEqual(first['facts']['submissions'], 3)
        self.assertEqual(first['facts']['duplicates'][0]['copies'], 1)
        replayed = service.process(payload, context, 'req-dup-1')
        self.assertEqual(replayed, first)
        record = database.get_record('applicant', 'app-dup')
        self.assertEqual(record['status'], 'cancelled')
        self.assertEqual(record['attributes']['facts']['submissions'], 3)
        events = database.events_for('applicant', 'app-dup')
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['payload']['facts']['duplicates'][0]['copies'], 1)


if __name__ == '__main__':
    unittest.main()
