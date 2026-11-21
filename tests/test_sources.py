import csv
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1] / "sources"
HEADERS = {'meter_readings': ['station', 'meter', 'timestamp_minute', 'kw', 'observed_day', 'supersedes', 'reading'], 'meter_roles': ['station', 'meter', 'parent', 'role', 'capacity_kw'], 'tariffs': ['tariff', 'energy_cent_per_kwh', 'demand_cent_per_kw', 'reserve_cent_per_kw', 'cap_kw', 'meter_interval_min'], 'guarantees': ['right', 'client', 'home', 'reserve_site', 'start_minute', 'end_minute', 'firm_kw', 'reserve_kw', 'contract'], 'reserve_inventory': ['site', 'from_min', 'to_min', 'free_kw', 'known_day', 'state'], 'transition_events': ['event', 'right', 'day', 'state', 'effective_minute', 'temporary_site']}
REFERENCES = [['meter_readings', 'supersedes', 'meter_readings', 'reading'], ['transition_events', 'right', 'guarantees', 'right'], ['guarantees', 'contract', 'tariffs', 'tariff']]
COUNTS = [['meter_readings', 'kw', '', 2], ['reserve_inventory', 'state', 'reduced', 1], ['transition_events', 'state', 'partial_signed', 1]]


class SourceContractTests(unittest.TestCase):
    def setUp(self):
        self.records = {}
        for name, header in HEADERS.items():
            with (ROOT / f"{name}.csv").open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, header)
                self.records[name] = list(reader)

    def test_record_shape(self):
        for name, records in self.records.items():
            with self.subTest(source=name):
                self.assertTrue(records)
                for row in records:
                    self.assertEqual(set(row), set(HEADERS[name]))
                    self.assertNotIn(None, row.values())
                    self.assertTrue(row[HEADERS[name][0]])
                self.assertEqual(len(records), len({tuple(row.values()) for row in records}))

    def test_cross_source_references(self):
        for child, fk, parent, pk in REFERENCES:
            with self.subTest(source=child, field=fk):
                keys = {row[pk] for row in self.records[parent]}
                values = {row[fk] for row in self.records[child] if row[fk]}
                self.assertTrue(values)
                self.assertFalse(values - keys)

    def test_source_has_relevant_observations(self):
        for source, field, value, minimum in COUNTS:
            with self.subTest(source=source, field=field, value=value):
                self.assertGreaterEqual(sum(row[field] == value for row in self.records[source]), minimum)
