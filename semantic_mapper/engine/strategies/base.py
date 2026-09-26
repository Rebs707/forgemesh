# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from abc import ABC, abstractmethod
from typing import List, Union
import rdflib
from semantic_mapper.config.settings import Settings
from semantic_mapper.db.models import TableProfile, ColumnProfile
from semantic_mapper.schema.mapping_output import MappingEvidence

class BaseMappingStrategy(ABC):
    def __init__(self, graph: rdflib.Graph, settings: Settings):
        self.graph = graph
        self.settings = settings

    @abstractmethod
    def evaluate_column(self, profile: ColumnProfile, table_profile: TableProfile) -> List[MappingEvidence]:
        pass

    @abstractmethod
    def evaluate_table(self, profile: TableProfile) -> List[MappingEvidence]:
        pass
