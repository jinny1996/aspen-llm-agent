# Hold-out E (paper6, ammonia Case 1 C1B, SMR section) -- rules frozen at tag `holdout-E-rules-frozen`

Input: paper6.md exactly as supplied (main text + SI tables S1-S8), Fig. 5.  3 Part1 runs, EDC prompt + policy on, `--auto-ok`.
Final schema = Schema_validated.yaml when the completion step ran (run 204006), else Schema_proto.yaml.
Gold: gold_E_labels.yaml (Claude, from the paper only, written before any validator output on E was looked at).  38 ids, FIXED 31, ABSENT 7.

## Result (per run; the three runs came out identical in the counts)
| FIXED present | hit | unassessed | false alarm | ABSENT present | not cleared | FALSE CLEARANCE |
|---|---|---|---|---|---|---|
| 27 | 10 (0.37) | 16 | 1 (B7.T_out_C) | 7 | 6 | **1 (B14.T_out_C = 30)** |
Compare: dev cases A/B/C/D after the D fixes 0.74 recall, 0 false clearance; hold-out D (before the fixes) 0.44, 0 false clearance.

## The false clearance
B14.T_out_C (CWC-1 outlet, gold ABSENT: the paper never gives it) = 30 was judged DIRECT_SUPPORT from two sentences that say
"... cooled to 30 C" -- but they describe the coolers of OTHER cases (Case 3/4: the H2 / CO PSA trains behind the thermal
decomposition reactor).  paper6 describes five cases; the schema's scope excludes units of the requested case (H2PSA, ...) but not
the other cases, so the validator had no way to see that those sentences belong to another process (WRONG_CASE, reserved / not implemented).

## Other observations
* False alarm B7.T_out_C: the HPC-1 cooler outlet was read as the HTS operating temperature (gold FIXED, medium confidence);
  the text only says HTS "operates at 450 C", so "borrowed from the reactor" is a defensible reading -- partly a labelling question.
* 15 of the 16 unassessed FIXED values are 20 bar pressures that follow from "no pressure drop across heat exchangers" and
  "pressurized to 20 bar": a derivation the validator does not do.  The others: stream compositions and the 1:3 water flow.
* ABSENT items otherwise behaved: intermediate cooler outlets and the SEP-1 temperature were not cleared.

## Verdict
The safety property "no false clearances" holds on dev cases and on hold-out D but NOT on hold-out E (1 item, all runs).
Cause: no notion of which case/configuration a sentence describes.  Any fix (section / case awareness) is post-hold-out
development and turns E into a development case.
