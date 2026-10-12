# Hold-out D (paper5, LOHC/MCH dehydrogenation) -- result with rules frozen at tag `holdout-D-rules-frozen`

Input: Fig. 2 + Fig. 7 (Fig. 1 was added later; see case D2).  3 Part1 runs (EDC prompt + policy on, `--auto-ok`),
final schema = Schema_validated.yaml.  Gold: gold_D_labels.yaml (written by Claude from the paper only, before any validator
output on D was looked at; two items decided by the user).  Not independent of the system builder.

## Semantic validator (score_semantic.py)
| run | FIXED hit | FIXED unassessed | false alarm | false clearance (FIXED value wrong / ABSENT cleared) |
|---|---|---|---|---|
| 182632 | 8 / 18 | 9 | 1 (B4.P_bar) | 0 |
| 182917 | 8 / 18 | 8 (2 gold ids absent from the schema) | 0 | 0 |
| 183140 | 8 / 18 | 9 | 1 (B4.P_bar) | 0 |
Dev cases for comparison (rules were tuned on them): A 19/24 (0.79), C 5/6 (0.83), B 3/4.
ABSENT items (B1.T_out_C[S2], B2.T_out_C when present) were never cleared.

## Why recall dropped (diagnosis only; rules NOT changed after seeing D)
1. `value_too_common_in_paper`: the value 2 (bar) occurs > 8 times with a unit, so the small-integer cap dropped all text evidence
   for every 2-bar pressure claim (B2.P, B3.P, S1.P, B5.P, ...).
2. Short abbreviations as names ("Cold MCH", 3 letters) are not used as subject cues -> S1.T_C, S1.flow stay undecided.
3. Subject named AFTER the number ("2.497 kW ... supplied ... in an evaporator", label "dH H/ex") -> B2.duty undecided.
4. Not supported by design: ratios (B6.ratio) and compositions (S1.z).
5. B4.P_bar false alarm: the claim's own rationale ties 2 bar to the reactor ("rationale_only" borrowing rule); the gold says the
   loop is at the system pressure (label FIXED, medium) -- partly a labelling question.

## Question policy (edc_policy, V2 off vs on)
Needed questions (gold ABSENT/OPEN present in the schema) were all asked in all runs, with and without V2.
Over-asked FIXED values per run: 3 (B4.P_bar, B5.P_out_bar, S1.z or B2.P_bar) -- identical with V2: the pressure items are
inferred without quote, S1.z is not NAME_IMPLIED because the component ids (METHY-01, M-CYC-01) are not aliases of "MCH".
V2 gave no gain on D.

## Verdict
Safe (0 false clearances in 60 claim judgements) but low recall on a new paper (0.44 vs 0.79-0.83 on the tuning cases).
Any rule change from here on is post-hold-out development: D then becomes a development case and a new paper is needed.
