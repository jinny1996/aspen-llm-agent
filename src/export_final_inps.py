"""export_final_inps.py -- write the Aspen input file (.inp) of each finished run's FINAL model, for score_structure_aspen.py.

    python export_final_inps.py --dry  # list the runs that would be exported (no Aspen)
    python export_final_inps.py        # needs the Aspen license; opens each final .bkp, exports, closes WITHOUT saving

Runs that already have an exported `*_run.inp` are skipped.  The export lands in <run>/part3/final_export_run.inp.
"""
import json
import sys
from pathlib import Path

import win32com.client

HERE = Path(__file__).resolve().parent
P13 = HERE.parent / "phase13"


def main():
    dry = "--dry" in sys.argv
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    todo = []
    for root in (P13 / "run", HERE / "run"):
        for d in sorted(root.glob("attempt_chat_*")):
            f = d / "part3" / "part3_result.json"
            if not f.exists():
                continue
            if any(list((d / x).glob("*_run.inp")) for x in ("part2", "part3")):     # list(): a bare glob() generator is always truthy
                continue
            bkp = json.loads(f.read_text(encoding="utf-8")).get("final_bkp_path")
            if bkp and Path(bkp).exists():
                todo.append((d, Path(bkp)))
    print(f"{len(todo)} runs to export")
    for d, bkp in todo:
        print("  ", d.name, "<-", bkp.name)
    if not todo or dry:
        return
    for d, bkp in todo:                       # a fresh Aspen document per run: re-using one document broke after the first export
        out = d / "part3" / "final_export_run.inp"
        doc = None
        try:
            doc = win32com.client.Dispatch("Apwn.Document")
            doc.InitFromFile2(str(bkp))
            doc.Export(4, str(out))                # 4 = INP file: exactly what export_inp() in aspen_code_v1 does
            print("OK  ", d.name, "->", out.name if out.exists() else "NOT WRITTEN")
        except Exception as e:
            print("FAIL", d.name, " ".join(str(e).split())[:160])
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
            except Exception:
                pass


if __name__ == "__main__":
    main()
