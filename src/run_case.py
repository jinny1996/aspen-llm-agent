"""Run one EDC test case through the phase13 chat pipeline in a terminal REPL.

    python run_case.py A        # ATR paper (paper2)
    python run_case.py B        # methanol aromatization, Fig. 2 unit only
    python run_case.py C        # paper3 methanation, Fig. 2 (MIXER -> RGIBBS) only
    python run_case.py D        # paper5 LOHC dehydrogenation (hold-out)
    python run_case.py C --dry  # build the session + print the inputs, NO LLM/Aspen calls
    python run_case.py C --part1-only --auto-ok   # Part1 only, unattended (EDC-2e repeats)
    python run_case.py B --reply "B3.T_C=425; B3.P_bar=2.95"   # first question answered with this text

Flags (environment): PART1_EDC_POLICY=1 (EDC question policy), PART1_EDC_PROMPT=1 (EDC-2e prompt addendum).

WARNING: without --dry this makes real OpenAI API calls and uses the Aspen COM
license (same as `python chat_orchestrator.py`).  Run it yourself.

The first request is a fixed, NEUTRAL prompt per case: it restricts the structure
(which figure/flowsheet) but does not tell the system to ask questions or how to fill
gaps, so the pipeline's own behaviour is what gets measured.
"""
import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent      # ...\3. LLM (2601)
PARSED = ROOT / "jinny_DB" / "parsed"

CASES = {
    "A": {
        "label": "ATR (paper2, full-spec control)",
        "paper_text_path": PARSED / "paper2" / "paper_h2.md",
        "paper_image_path": PARSED / "paper2" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper2" / "*.jpg"),
        "paper_image_top_k": 4,
        "prompt": "ATR 공정을 설계해줘",
    },
    "B": {
        "label": "Methanol aromatization, paper Figure 2 unit only (gap case)",
        # main text only (paper.md = main text + SI appended by the user).  The SI is the hidden gold:
        # Part1 must not see it, otherwise there is no gap left to test.
        "paper_text_path": PARSED / "paper" / "auto" / "paper_main.md",
        "gold_text_path": PARSED / "paper" / "auto" / "paper.md",
        "paper_image_path": PARSED / "paper" / "auto" / "Fig. 1(paper1).jpg",
        "paper_image_glob": str(PARSED / "paper" / "auto" / "Fig. 1(paper1).jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Methanol aromatization 공정을 설계해줘. 논문의 Methanol aromatization unit "
                   "(P0101 -> E0101 -> R0101 -> E0102)만 사용하고, reactor는 RGibbs로 해줘."),
    },
    "C": {
        "label": "paper3 methanation, Fig. 2 equilibrium flowsheet only (gap case)",
        "paper_text_path": PARSED / "paper3" / "paper3.md",
        "paper_image_path": PARSED / "paper3" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper3" / "Fig. 2.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Methanation 공정을 설계해줘. 논문의 Fig. 2 flowsheet "
                   "(SYNGAS, EXTRA-H2 -> MIXER -> RGIBBS -> PRODUCT)만 사용하고, reactor는 RGibbs로 해줘."),
    },
    # HOLD-OUT case (paper5, added after the validator / policy-v2 rules were frozen: tag validator-freeze-pre-holdout
    # + the policy-v2 commit).  No SI; gold labels are made by the user BEFORE looking at the validator output.
    "D": {
        "label": "paper5 LOHC (MCH) dehydrogenation, Fig. 2 computational scheme (hold-out, no SI)",
        "paper_text_path": PARSED / "paper5" / "paper5.md",
        "paper_image_path": PARSED / "paper5" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper5" / "*.jpg"),      # Fig. 2 (structure) + Fig. 7 (stream T / flows)
        "paper_image_top_k": 2,
        "prompt": ("LOHC(MCH) dehydrogenation 공정을 설계해줘. 논문의 Fig. 2 computational scheme "
                   "(Cold MCH -> H/ex -> Evaporator -> Reformer/reactor -> H/ex -> Condens1 -> Separator1 "
                   "-> Compressor -> Cond2 -> Separator2)만 사용하고, reformer는 RGibbs로 해줘."),
    },
    # Same paper and prompt as D, but the PFD (Fig. 1: tank, pump, purification) is given as well.  Fig. 1 was added to the
    # folder after the first three D runs, so D = Fig. 2 + Fig. 7 only and D2 = Fig. 1 + Fig. 2 + Fig. 7.
    "D2": {
        "label": "paper5 LOHC (MCH) dehydrogenation, hold-out, with the PFD Fig. 1",
        "paper_text_path": PARSED / "paper5" / "paper5.md",
        "paper_image_path": PARSED / "paper5" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper5" / "*.jpg"),
        "paper_image_top_k": 3,
        "prompt": ("LOHC(MCH) dehydrogenation 공정을 설계해줘. 논문의 Fig. 2 computational scheme "
                   "(Cold MCH -> H/ex -> Evaporator -> Reformer/reactor -> H/ex -> Condens1 -> Separator1 "
                   "-> Compressor -> Cond2 -> Separator2)만 사용하고, reformer는 RGibbs로 해줘."),
    },
    # HOLD-OUT case E (paper6, added AFTER the first hold-out D; evaluated with the rules frozen at the tag made after the
    # D-diagnosis fixes).  Input = paper6.md exactly as the user supplied it (main text + the SI tables S1-S8 appended).
    "E": {
        "label": "paper6 ammonia Case 1 (C1B, Fig. 5a), steam-methane-reforming section only (hold-out, paper as supplied)",
        "paper_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 steam methane reforming 구간만 설계해줘: "
                   "NG -> CP-1, H2O -> P-1 -> MIX-1 -> LPH-1 -> FHH-1 -> REFORM -> HPC-1 -> HTS -> HPC-2 -> MPC-1 -> LTS "
                   "-> MPC-2 -> LPC-1 -> CWC-1 -> SEP-1 (SEP-1 이후 H2PSA와 다른 구간은 제외). "
                   "REFORM, HTS, LTS는 RGibbs로 해줘."),
    },
    # Optional variant of E: the SI tables removed from the input (paper6_main.md), SI = hidden truth.  Not run.
    "EM": {
        "label": "paper6 ammonia C1B SMR section, main text only (SI tables removed)",
        "paper_text_path": PARSED / "paper6" / "paper6_main.md",
        "gold_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 steam methane reforming 구간만 설계해줘: "
                   "NG -> CP-1, H2O -> P-1 -> MIX-1 -> LPH-1 -> FHH-1 -> REFORM -> HPC-1 -> HTS -> HPC-2 -> MPC-1 -> LTS "
                   "-> MPC-2 -> LPC-1 -> CWC-1 -> SEP-1 (SEP-1 이후 H2PSA와 다른 구간은 제외). "
                   "REFORM, HTS, LTS는 RGibbs로 해줘."),
    },
    # SEMI-INDEPENDENT check (case F): same paper as E (paper6) but a different unit -- the Haber-Bosch loop of Case 1.  Its claims and
    # labels are new, but the document conventions (case sections, SI tables) were seen while fixing E, so F is NOT a clean
    # hold-out: it only shows whether the fixes survive another unit of the same paper.  The other cases (2-4) contain similar HB
    # loops, so sentences about them are exactly what the case-awareness rule has to keep out.
    "F": {
        "label": "paper6 ammonia Case 1 (C1B, Fig. 5a), Haber-Bosch loop only (semi-independent: same paper as E)",
        "paper_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 Haber-Bosch 구간만 설계해줘: "
                   "H2와 N2 공급 -> MIX-3 -> CP-4 -> MIX-4 -> HPH-1 -> FHH-2 -> NH3SYN -> HPC-4 -> MPC-4 -> LPC-3 -> CWC-2 "
                   "-> RFC-1 -> SEP-2, SEP-2 기체는 SPL-1에서 purge와 MIX-4로 가는 recycle로 나누고, SEP-2 액체는 "
                   "V-1 -> SEP-3 -> NH3. (SMR, ASU, H2PSA 구간은 제외하고 H2와 N2는 MIX-3로 들어오는 공급 스트림으로 처리). "
                   "NH3SYN은 RGibbs로 해줘."),
    },
    # Structure-only variants of D and E: the prompt names the figure / boundary but NOT the unit sequence, so the topology has
    # to be read from the figure (the D / E prompts list every unit in order).  Part1 only is enough for Structural F1.
    "Dn": {
        "label": "paper5 LOHC, prompt without the unit sequence (structure test)",
        "paper_text_path": PARSED / "paper5" / "paper5.md",
        "paper_image_path": PARSED / "paper5" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper5" / "*.jpg"),
        "paper_image_top_k": 2,
        "prompt": "LOHC(MCH) dehydrogenation 공정을 설계해줘. 논문의 Fig. 2 computational scheme을 사용하고, reformer는 RGibbs로 해줘.",
    },
    "Bn": {
        "label": "paper1 methanol aromatization, prompt without the unit sequence (structure test)",
        "paper_text_path": PARSED / "paper" / "auto" / "paper_main.md",
        "paper_image_path": PARSED / "paper" / "auto" / "Fig. 1(paper1).jpg",
        "paper_image_glob": str(PARSED / "paper" / "auto" / "Fig. 1(paper1).jpg"),
        "paper_image_top_k": 1,
        "prompt": "Methanol aromatization 공정을 설계해줘. reactor는 RGibbs로 해줘.",
    },
    "Cn": {
        "label": "paper3 methanation, prompt without the unit sequence (structure test)",
        "paper_text_path": PARSED / "paper3" / "paper3.md",
        "paper_image_path": PARSED / "paper3" / "Fig. 2.jpg",
        "paper_image_glob": str(PARSED / "paper3" / "Fig. 2.jpg"),
        "paper_image_top_k": 1,
        "prompt": "Methanation 공정을 설계해줘. reactor는 RGibbs로 해줘.",
    },
    # F as the first request gave it (every F run stopped in Part1: SPL-1 split_fractions never filled, the paper gives no purge ratio),
    # plus the missing number supplied by the user, like the feed amounts the first requests used to carry.
    "Fs": {
        "label": "paper6 ammonia C1B Haber-Bosch loop, user supplies the purge ratio",
        "paper_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 Haber-Bosch 구간만 설계해줘: "
                   "H2와 N2 공급 -> MIX-3 -> CP-4 -> MIX-4 -> HPH-1 -> FHH-2 -> NH3SYN -> HPC-4 -> MPC-4 -> LPC-3 -> CWC-2 "
                   "-> RFC-1 -> SEP-2, SEP-2 기체는 SPL-1에서 purge와 MIX-4로 가는 recycle로 나누고, SEP-2 액체는 "
                   "V-1 -> SEP-3 -> NH3. (SMR, ASU, H2PSA 구간은 제외하고 H2와 N2는 MIX-3로 들어오는 공급 스트림으로 처리). "
                   "NH3SYN은 RGibbs로 해줘. SPL-1의 purge 비율은 1%로 가정해줘."),
    },
    "Fn": {
        "label": "paper6 ammonia C1B Haber-Bosch loop, boundary only (structure test)",
        "paper_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 Haber-Bosch 구간만 설계해줘 "
                   "(SMR, ASU, H2PSA 구간은 제외하고 H2와 N2는 공급 스트림으로 처리). NH3SYN은 RGibbs로 해줘."),
    },
    "En": {
        "label": "paper6 ammonia C1B SMR section, prompt without the unit sequence (structure test)",
        "paper_text_path": PARSED / "paper6" / "paper6.md",
        "paper_image_path": PARSED / "paper6" / "Fig. 5.jpg",
        "paper_image_glob": str(PARSED / "paper6" / "Fig. 5.jpg"),
        "paper_image_top_k": 1,
        "prompt": ("Ammonia 공정 Case 1의 base configuration C1B(Fig. 5a) 중 steam methane reforming 구간만 SEP-1까지 설계해줘 "
                   "(H2PSA와 그 이후 구간은 제외). REFORM, HTS, LTS는 RGibbs로 해줘."),
    },
}


def build_session(case_key: str, part1_only: bool = False):
    from chat_orchestrator import PipelineSession   # heavy import (LLM stack); keep lazy
    c = CASES[case_key]
    return PipelineSession(
        paper_text_path=c["paper_text_path"], paper_image_path=c["paper_image_path"],
        paper_image_glob=c["paper_image_glob"], paper_image_top_k=c["paper_image_top_k"],
        part1_only=part1_only,
    ), c


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("case", choices=sorted(CASES))
    ap.add_argument("--dry", action="store_true", help="no LLM/Aspen: only build session and print inputs")
    ap.add_argument("--part1-only", action="store_true", help="stop after Part1 (no Part2/3, no Aspen license)")
    ap.add_argument("--auto-ok", action="store_true",
                    help="answer every human question with `ok` (unattended repeats; the question is printed)")
    ap.add_argument("--reply", default=None,
                    help="send this text as the answer to the FIRST human question instead of typing it, e.g. "
                         "--reply \"B3.T_C=425; B3.P_bar=2.95\"; later questions are answered `ok`")
    args = ap.parse_args()

    session, c = build_session(args.case, part1_only=args.part1_only)
    import os
    print("flags: PART1_EDC_POLICY=%s PART1_EDC_PROMPT=%s part1_only=%s auto_ok=%s" % (
        os.environ.get("PART1_EDC_POLICY", "0"), os.environ.get("PART1_EDC_PROMPT", "0"),
        args.part1_only, args.auto_ok))
    print(f"=== case {args.case}: {c['label']} ===")
    print("first request:", c["prompt"])
    p1 = session.part1_input
    print(json.dumps({k: p1[k] for k in ("paper_text_path", "paper_image_path",
                                         "paper_image_glob", "paper_image_top_k")},
                     ensure_ascii=False, indent=2))
    for k in ("paper_text_path", "paper_image_path"):
        if not Path(p1[k]).exists():
            raise SystemExit(f"MISSING input file: {p1[k]}")
    if args.dry:
        print("[dry] inputs OK, run dir:", session.runner_dir)
        return

    result = session.handle_user_message(c["prompt"])
    reply_sent = False
    while True:
        print(f"\n[{result.get('status')}] {result.get('message')}\n")
        if result.get("status") in {"done", "error"}:
            break
        if args.reply is not None and not reply_sent:
            reply_sent = True
            print(f"You: {args.reply}  (--reply)")
            result = session.handle_user_message(args.reply)
        elif args.reply is not None or args.auto_ok:
            print("You: ok  (automatic)")
            result = session.handle_user_message("ok")
        else:
            result = session.handle_user_message(input("You: ").strip())


if __name__ == "__main__":
    main()
