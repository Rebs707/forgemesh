# Offline terminology code lists

Small code lists used by terminology binding (`terminology_sources` in
`projects/synthea.yaml`) as an offline stand-in for a live terminology service. The
format is `code<TAB>label`, with `#` comment lines.

| File | Code system | Where it comes from |
|---|---|---|
| `snomedct.tsv` | SNOMED CT | The 659 codes and descriptions that appear in the bundled Synthea sample (`datasets/synthea/sample/`), exactly as Synthea emits them |
| `rxnorm.tsv` | RxNorm | The 226 medication codes and names that appear in the bundled Synthea sample |
| `hl7_administrative_gender.tsv` | HL7 v2 table 0001 | [terminology.hl7.org](https://terminology.hl7.org/CodeSystem/v2-0001) |
| `hl7_marital_status.tsv` | HL7 v3 MaritalStatus | [terminology.hl7.org](https://terminology.hl7.org/CodeSystem/v3-MaritalStatus) |

These lists are not complete code systems and aren't meant for clinical use.

**SNOMED CT** is owned by SNOMED International. Using SNOMED CT beyond these sample
codes, for example binding your own data against the full release, generally needs a
licence: through your country's National Release Centre in member countries, or an
Affiliate Licence elsewhere. See [snomed.org](https://www.snomed.org/get-snomed).

**RxNorm** is published by the US National Library of Medicine
([nlm.nih.gov/research/umls/rxnorm](https://www.nlm.nih.gov/research/umls/rxnorm/)).

To bind your own data against a fuller list, point a project's `terminology_sources`
entry at your own file in the same format.
