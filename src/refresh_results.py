"""refresh_results.py -- regenerate every result table from the runs on disk (read-only; no LLM, no Aspen).

    python refresh_results.py

Writes meeting/RESULTS_ALL.md (all tables in one file) and the individual files each script writes
(meeting_table.md, EVAL_SUMMARY.md, STRUCTURE_F1.md, STRUCTURE_F1_ASPEN.md).  Run it after new runs have finished.
"""
from __future__ import annotations

import contextlib
import io
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "meeting"


def capture(fn, *a, **k) -> str:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a, **k)
    return buf.getvalue()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    import collect_results
    import eval_dev
    import eval_summary
    import score_structure
    import score_stream
    import score_structure_aspen

    parts = [f"# 결과 일괄 (자동 생성 {datetime.now():%Y-%m-%d %H:%M})", "",
             "정답 라벨은 대부분 초안(C만 연구자 작성). 표본이 작아 모든 수치는 예비 결과.", ""]
    collect_results.main(["--out-prefix", str(OUT / "meeting")])
    parts += ["## 1. 케이스별 실행 요약 (논문 → Schema → Aspen)", "", (OUT / "meeting_table.md").read_text(encoding="utf-8"), ""]
    score_structure.main()
    parts += ["## 2. Structural F1 — Schema 기준 (Part 1 결과)", "", (OUT / "STRUCTURE_F1.md").read_text(encoding="utf-8"), ""]
    score_structure_aspen.main()
    parts += ["## 3. Structural F1 — 최종 Aspen 파일 기준 (Part 2/3 이후)", "",
              (OUT / "STRUCTURE_F1_ASPEN.md").read_text(encoding="utf-8"), ""]
    score_stream.main()
    parts += ["## 3b. Stream F1 (블록 간 연결 + 제품/퍼지 스트림, 중복·누락·초과 스트림 반영)", "", (OUT / "STREAM_F1.md").read_text(encoding="utf-8"), ""]
    eval_summary.main()
    parts += ["## 4. 실행 성공, 근거 검출, 질문 효율, Parameter accuracy", "", (OUT / "EVAL_SUMMARY.md").read_text(encoding="utf-8"), ""]
    parts += ["## 5. 의미 검증기 회귀표 (개발 케이스 A–E)", "", "```", capture(eval_dev.main, []), "```", ""]
    text = "\n".join(parts)
    (OUT / "RESULTS_ALL.md").write_text(text, encoding="utf-8")
    print("wrote", OUT / "RESULTS_ALL.md", f"({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
