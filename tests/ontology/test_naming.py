"""Unit tests for ontology output-file naming."""
from datetime import datetime

from semantic_mapper.ontology.naming import model_short_name, ontology_filename


class TestModelShortName:
    def test_strips_instruct_and_quant(self):
        assert model_short_name("qwen2.5:72b-instruct-q4_K_M") == "qwen2.5-72b"

    def test_plain_gpt(self):
        assert model_short_name("gpt-4o") == "gpt-4o"

    def test_strips_fp_suffix(self):
        assert model_short_name("llama3.1:70b-instruct-fp16") == "llama3.1-70b"

    def test_empty_falls_back(self):
        assert model_short_name("") == "model"


class TestOntologyFilename:
    WHEN = datetime(2026, 6, 6)

    def test_format(self, tmp_path):
        p = ontology_filename(tmp_path, "mimic", "rigor", "gpt-4o", self.WHEN)
        assert p.name == "mimic_rigor_gpt-4o_06JUN26.owl"

    def test_full_strategy_and_local_model(self, tmp_path):
        p = ontology_filename(tmp_path, "mimic", "full", "qwen2.5:72b-instruct-q4_K_M", self.WHEN)
        assert p.name == "mimic_full_qwen2.5-72b_06JUN26.owl"

    def test_project_underscores_become_dashes(self, tmp_path):
        # keeps the single-underscore component separators unambiguous
        p = ontology_filename(tmp_path, "mimic_bronze", "rigor", "gpt-4o", self.WHEN)
        assert p.name == "mimic-bronze_rigor_gpt-4o_06JUN26.owl"

    def test_avoids_clobbering_same_day_run(self, tmp_path):
        p1 = ontology_filename(tmp_path, "mimic", "rigor", "gpt-4o", self.WHEN)
        p1.write_text("x")
        p2 = ontology_filename(tmp_path, "mimic", "rigor", "gpt-4o", self.WHEN)
        assert p2.name == "mimic_rigor_gpt-4o_06JUN26_2.owl"
