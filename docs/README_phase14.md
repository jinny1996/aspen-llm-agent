# phase14 — 인수인계 (2026-10-08 기준)

phase13의 복사본(자체 git 저장소). phase13 원본은 `PHASE13_ORIGINAL_SHA256.txt`로 불변을 확인할 수 있음. 공유 파일(`2608 수정/`, 루트)은 수정하지 않음.

## 1. 무엇이 추가됐나
| 구분 | 파일 | 내용 |
|---|---|---|
| 의미 검증기 (P0-1, report-only) | `edc_claims.py`, `edc_semantic_validator.py`, `run_semantic_report.py` | 논문 근거가 해당 설비·변수·케이스에 맞는지 결정적 규칙으로 판정 (LLM 호출 없음) |
| 질문 정책 v2 (기본 off) | `edc_policy_v2.py` | 환경변수 `PART1_EDC_POLICY_V2=1`일 때만 작동. 기존 `edc_policy.py`는 호출 지점 2곳만 수정 |
| Part 2 호환 패치 (phase14 한정) | `part2_stage_functions.py` | 압축기 블록 종류 `Compr`, 성분 ID=데이터뱅크 이름, 압축기 기본 설정 보장(`merge_compressor_defaults_code`) |
| 평가 도구 | `score_semantic.py`, `score_structure.py`, `score_structure_aspen.py`, `eval_dev.py`, `eval_summary.py`, `simulate_policy.py`, `collect_results.py`, `refresh_results.py` | 아래 3절 |
| 정답 라벨 | `gold_A_draft.yaml`, `gold_B_draft.yaml`, `gold_D_labels.yaml`, `gold_E_labels.yaml`, `gold_structure.yaml` | **대부분 Claude 초안**(C만 연구자 작성, `../phase13/2e_gold_labels.yaml`) |
| 실행 케이스 | `run_case.py` | A–C(기존), D/D2/E/EM/F, 구조 시험 변형 Bn Cn Dn En Fn |

## 2. 실행 (모두 phase14 폴더, 환경변수 EDC 켬)
```powershell
$env:PART1_EDC_POLICY="1"; $env:PART1_EDC_PROMPT="1"
python run_case.py <CASE> --auto-ok                 # Part 1~3 (OpenAI API + Aspen 라이선스)
python run_case.py <CASE> --part1-only --auto-ok    # Part 1만 (OpenAI API만)
python run_case.py <CASE> --dry                     # 입력만 확인, 비용 없음
python export_final_inps.py                         # 최종 .bkp -> .inp (Aspen, 구조 F1용). 다른 Aspen 실행과 동시에 돌리지 말 것
python refresh_results.py                           # 모든 결과 표 갱신 (읽기 전용) -> meeting/RESULTS_ALL.md
```
- Aspen COM은 한 번에 하나만 쓰기. Part 1만 하는 실행은 Aspen을 쓰지 않으므로 Aspen 실행과 동시 가능.
- 케이스: A=ATR(paper2), B=메탄올 방향족화(paper/auto), C=메탄화(paper3), D=LOHC(paper5), E=암모니아 SMR 구간(paper6), F=암모니아 Haber-Bosch 구간(paper6). `n` 접미사 = 유닛 순서를 첫 요청에서 뺀 변형.

## 3. 지표와 정의
| p.7 지표 | 도구 | 비고 |
|---|---|---|
| Structural F1 | `score_structure.py`(Schema), `score_structure_aspen.py`(최종 Aspen 파일) | 정답 구조는 `gold_structure.yaml`(그림에서 읽은 초안) |
| Simulation success | `eval_summary.py` 1절 | 개발 중 실패는 결함 수정 전 실행이라 "최종 신뢰도"가 아님 |
| Source fidelity | `eval_summary.py` 4절 | 논문 명시값 정확도(FIXED 숫자값). 미명시 값은 B만 SI로 확인 가능 |
| Evidence 검출 / 근거 확인 | `score_semantic.py`, `eval_dev.py` | **틀린 안심(false clearance)** 이 핵심 안전 지표 |
| HITL 효율 | `eval_summary.py` 3절, `simulate_policy.py` | 질문 상한 5개 |
| Error recovery, Ablation B0–B2 | 미구현 | 오류 사례 없음 / 단계 스위치 필요 |

## 4. 평가 절차의 원칙 (바꾸면 결과 해석이 달라짐)
- A·B·C는 개발용(규칙을 이 케이스들을 보고 만듦). D·E는 규칙을 고정(`git tag holdout-D-rules-frozen`, `holdout-E-rules-frozen`)한 뒤 처음 평가한 값이 `HOLDOUT_D_RESULT.md`, `HOLDOUT_E_RESULT.md`. E에서 틀린 안심 1건이 나와 케이스/절 인식을 추가했고, 그 이후 D·E는 개발 케이스. **현재 깨끗한 독립 평가 케이스는 없음**(F는 E와 같은 논문이라 준독립).
- 라벨은 시스템 결과를 보기 전에 붙일 것. 정답 라벨을 시스템 개발자(Claude)가 썼다는 점이 한계.
- 정답 라벨은 `FIXED`(논문이 정함) / `OPEN`(범위·후보 여럿) / `ABSENT`(논문에 없음).

## 5. 알려진 한계
- 근거 확인 비율이 낮음(개발 케이스 합계 약 0.5): 압력처럼 가정에서 도출되는 값, 조성, 비율 기반 값은 판정 못 함.
- 순서 없는 요청의 큰 흐름도(E, F)에서 작은 열교환기가 Schema에서 빠짐(Block F1 0.70~0.75, 케이스당 1회).
- Part 2에서 새 논문마다 결함이 발견됨(압축기 이름, 성분 ID, 압축기 TYPE/효율 누락) — 모두 phase14에서 수정, 공유 파일 미수정.
- 스키마의 `eff`(압축기 효율)가 이전에는 Aspen에 반영되지 않았음(수정 전 D, E 결과는 그 상태).
- P0-2(원문→Schema→Aspen 입력값 일치 감사)는 구조 수준만 구현(`score_structure_aspen.py`), 값 수준은 미구현.

## 6. 문서
`HOLDOUT_D_RESULT.md`, `HOLDOUT_E_RESULT.md`, `meeting/RESULTS_ALL.md`(자동 생성), 연구 방향 문서(`3. LLM (보고자료 및 참고자료)/2610_논문 방향 수립/`).
