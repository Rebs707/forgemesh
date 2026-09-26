# Copyright (c) 2026 Ben Tagger
# SPDX-License-Identifier: Apache-2.0
from enum import Enum

class EntityType(Enum):
    CLASS = "owl:Class"
    DATATYPE_PROPERTY = "owl:DatatypeProperty"
    OBJECT_PROPERTY = "owl:ObjectProperty"

class Severity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"

DATE_FORMAT_XSD = "http://www.w3.org/2001/XMLSchema#date"
STRING_FORMAT_XSD = "http://www.w3.org/2001/XMLSchema#string"
