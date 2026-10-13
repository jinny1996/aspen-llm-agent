# 코드 구조 개요

논문(텍스트 + 공정도)을 입력하면 Aspen Plus 모델 파일을 만들어 주는 Gradio 챗봇 시스템의 코드 구조 설명이다. 파이프라인은 세 파트(Part1~3)로 나뉘고, 각 파트 안에서는 LLM이 도구를 골라 호출한다. 그 위에, 논문에 근거가 없는 값을 찾아 기록하고 필요하면 사람에게 묻는 EDC(Evidence-grounded DoF Closure) 기능이 플래그로 얹혀 있다.

## 1. 전체 구조

```
[Gradio app.py] ──(워커 스레드 1개, COM 스레드 고정)──▶ [PipelineSession.handle_user_message(text)]
                                                              │  (chat_orchestrator.py)
        ┌─────────────────────────────────────────────────────┼──────────────────────────────┐
        ▼ stage="part1"                                       ▼ stage="part2"                ▼ stage="part3"
  tool-calling 에이전트 루프                            tool-calling 에이전트 루프        tool-calling 에이전트 루프
  (LLM이 도구를 골라 호출)                              (LLM이 도구를 골라 호출)          (LLM이 도구를 골라 호출)
        │                                                     │                              │
  part1_stage_functions.py                              part2_stage_functions.py        part3_stage_functions.py
  (논문→스키마 YAML)                                     (스키마→Aspen COM 모델)          (Aspen 실행 로그→진단·패치)
        │                                                     │                              │
        └──── Schema_validated.yaml ──────────────────────────┘                              │
                                      └──── .bkp (Aspen 모델) ───────────────────────────────┘
                                                                                             ▼
                                                                                  최종 .bkp + [가정 요약]
```

설계 원칙
1. **한 개의 살아 있는 프로세스**가 턴마다 호출된다. 사람이 필요한 지점은 "함수가 리턴하고 다음 사용자 메시지를 기다리는 것"으로 처리한다.
2. **LLM은 도구를 고르고 순서를 정할 뿐**, 실제 작업은 일반 파이썬 함수(stage functions)가 한다. 검증·안전검사·반복 횟수 상한은 프롬프트가 아니라 코드로 강제한다.
3. **세션 상태는 파트별 `session` dict에 모이고**, 중간 산출물은 파일(YAML/JSON/.bkp)로 남는다.

## 2. 진입점과 실행 방식 (`src/`)

| 파일 | 역할 |
|---|---|
| `app.py` | Gradio UI. 전역 `PipelineSession` 1개. `_PipelineWorker`가 전용 스레드 1개를 만들어 모든 턴을 그 스레드에서 실행한다(Aspen COM은 `doc`을 만든 스레드에서만 호출 가능, 스레드 시작 시 `CoInitialize`). 진행 상황은 큐로 실시간 스트리밍 |
| `chat_orchestrator.py` | `PipelineSession`: 세 파트를 한 프로세스 안에서 차례로 실행. 도구 호출을 한국어 진행 문구로 바꾸는 내레이션 계층. CLI(`python chat_orchestrator.py`)로도 실행 가능 |
| `agent_loop_utils.py` | 직접 만든 tool-calling 루프 `run_tool_calling_agent` / `resume_tool_calling_agent`, 일시정지 예외 `HumanInputRequired` |
| `mcp_server.py`, `mcp_call_tool.py` | MCP 서버(stdio)와 클라이언트. 서버는 컨텍스트·검증·평가 도구 10개를 노출한다. `MCP_ADAPTER_MODE`로 로컬 어댑터와 전환 |
| `paths.py` | 공용 데이터 위치 해석(`ASPEN_AGENT_DATA_DIR`, `ASPEN_AGENT_PAPERS_DIR`) |
| `experiment_logging.py` | 이벤트·토큰 사용량 로그 |

### 에이전트 루프와 사람 개입
- `run_tool_calling_agent`: `llm.bind_tools(tools)` → 모델이 도구 호출 → **같은 스레드에서** `tool.invoke` → 결과를 `ToolMessage`로 되돌림 → 도구 호출이 없으면 종료. 최대 스텝 제한(Part1 25, Part2·3 40). LangGraph의 ToolNode는 도구를 다른 스레드에서 실행하는데 Aspen COM 객체가 스레드 종속이라 쓰지 않았다.
- 도구가 `HumanInputRequired(payload)`를 던지면 루프가 `status="paused"`로 리턴하고, `PipelineSession`이 상태를 저장한 뒤 질문을 채팅에 표시한다.
- 다음 사용자 메시지가 오면 `resume_tool_calling_agent`가 **같은 도구를 `human_answer`와 함께 다시 호출**해 부기(상태 기록)를 정상 수행하고 루프를 이어간다. 사람 개입이 가능한 도구는 `human_answer=None` 인자를 받는 규약이다.
- 각 파트의 `PART{1,2,3}_HITL_MODE=chat`이면 이 경로, `bypass`면 자동 승인이다.
- `on_tool_call(name, args)` 콜백이 도구 호출 직전에 불려 UI에 진행 상황을 실시간으로 전달한다.

### 도구 구성
에이전트가 직접 호출하는 도구는 LangChain `@tool` 27개(Part1 7, Part2 8, Part3 12)다. 이 중 일부가 MCP 도구(컨텍스트·검증·평가 10개)를 어댑터로 호출하며, `MCP_ADAPTER_MODE=mcp_client`이면 stdio MCP 서버를 거치고 기본값 `local_adapter`이면 같은 함수를 프로세스 안에서 호출한다.

### 파트 전환
루프가 `done`이면 `_finish_partN`이 결과 파일을 확인하고 곧바로 다음 파트를 시작한다. 실패하면 오류 메시지와 함께 멈춘다.

## 3. Part1 — 논문 → 공정 스키마 (`part1_stage_functions.py`)

```
fetch_paper_context → assess_input_readiness ─(부족)→ request_human_clarification
        → draft_schema → assess_schema_quality
              ├─ enough ──────────────────────▶ 종료 (Schema_proto.yaml)
              ├─ needs_human_review ──▶ review_schema_with_human ──▶ 종료
              └─ needs_completion ───▶ complete_and_validate ────▶ 종료 (Schema_validated.yaml)
```

| 단계 | 내용 |
|---|---|
| `fetch_paper_context` | 논문 텍스트, 그림 힌트, 스키마 규칙(`Schema_v5.yaml`)을 MCP 도구로 수집 |
| `assess_input_readiness` | 사용자 요청이 충분히 구체적인지 판단. 부족하면 되묻는다 |
| `draft_schema` | VLM으로 그림에서 정보를 추출하고 LLM으로 스키마 초안 YAML 생성. 값마다 논문 인용 근거를 붙인다. 블록 ID 4글자 초과 시 `B<n>`으로 정규화(`block_ids.py`) |
| `assess_schema_quality` | 사전 검사. `enough` / `needs_completion` / `needs_human_review` 판정 |
| `complete_and_validate` | 최대 3회 반복: 검증 → 누락 항목 → 논문에서 근거 창 검색 → LLM이 JSON 패치 생성 → 적용 |
| `review_schema_with_human` | 검토 항목을 사용자에게 질문 |

산출물: `Schema_proto.yaml` 또는 `Schema_validated.yaml`, `part1_status.json`.

## 4. EDC (플래그, 기본 off)

LLM의 자기보고 신뢰도에 의존하지 않고, **결정적 규칙(LLM 호출 없음)** 으로 값의 근거를 분류하고 질문을 고른다.

| 기능 | 환경변수 | 파일 |
|---|---|---|
| 자유도 장부 | (항상) | `dof_ledger.py` — 블록별 필요한 값이 다 있는지, 인용문이 논문 원문에 있는지 검사 |
| 근거 분류 + 질문 선택 | `PART1_EDC_POLICY=1` | `edc_policy.py` — STATED(명시) / SWEPT(범위) / ALTERNATIVE(후보 중 선택) / BORROWED_SUSPECT(타 유닛 값 차용 의심) / ASSUMED(가정) 등으로 분류, 영향도 기준 질문 상한 5개 |
| 답변 반영 | 〃 | `ID=값` 답변을 스키마에 반영하고 원본은 `*.pre_review.yaml`로 백업, 인식 실패 시 1회 재질문 |
| 프롬프트 보강 | `PART1_EDC_PROMPT=1` | `edc_prompt.py` — 인용 원문, 후보 값 목록, 인용 범위, 계산식, 조성 근거, 물성 방법 대체 기록 요구 |
| 의미 검증기 / 정책 v2 | `PART1_EDC_POLICY_V2=1` | `edc_claims.py`, `edc_semantic_validator.py`, `edc_policy_v2.py` — 근거가 해당 설비·변수에 맞는지 판정 |
| 가정 요약 | `PART1_EDC_POLICY=1` | 파이프라인 마지막에 "직접 바꾼 값 / 질문에서 확인한 값 / 묻지 않고 가정한 값" 출력 |

## 5. Part2 — 스키마 → Aspen COM 모델 (`part2_stage_functions.py`)

### 5.1 `build_session` (에이전트 루프 시작 전 순차 실행)
```
_generate_inp_skeleton()      LLM + inp_v1.yaml(INP 문법 DB)로 초기 INP 골격 생성
_convert_inp_to_bkp()         aspsetup.bat으로 INP → BKP 변환
_build_com_action_plan()      LLM + aspen_code_v3.yaml(COM 함수 카탈로그)로 COM 작업 계획(JSON)
_generate_com_skeleton_code() 계획 → COM 파이썬 코드 생성
_execute_com_skeleton()       코드를 실행해 Aspen 문서 `doc`을 만듦
```

### 5.2 에이전트 도구 순서
```
prepare_part2 → fetch_part2_context
→ run_repair_phase("property_method") → ("connectivity") → ("feeds") → ("block_specs")
→ final_validate → assess_final ─(검토 필요)→ review_with_human → [repair_with_human_feedback → …]
→ write_result
```

### 5.3 `repair_phase(phase)` — 단계별 수리 루프
1. **스키마 기반 결정적 수리**(`shared/validators/valid_*_schema_repair.py`): 물성 방법, 연결, 피드, 블록 사양을 스키마 값으로 직접 적용.
2. **검증기 실행**(`shared/validators/valid_*.py`).
3. 통과 못 하면 최대 3회: LLM이 패치 계획 생성 → **안전 검사**(`safety_check_patchplan`, 단계별 허용 함수 목록) → COM 보정 코드 생성·실행 → 재검증.

### 5.4 최종 단계
전체 검증(`final_all`) 후 Aspen을 실행해 `.his` 로그를 만들고(Part3 입력), 검토가 필요하면 사람에게 approve / revise / stop을 묻는다. 결과는 `part2_result_iter1.json`.

Aspen 입력 단위 점검(`check_feed_flow_units`): Aspen의 `TOTFLOW.UnitString`을 읽어 스키마의 유량 기준(몰/질량)과 대조하고, 어긋나면 한 번 정정한 뒤 다시 확인한다.

## 6. Part3 — 실행 로그 진단·패치 (`part3_stage_functions.py`)

```
run_aspen_check → parse_his ─(severe·error 없음)→ early_exit → assess_result → write_result
      │
      └(있음)→ [ begin_repair_attempt(최대 3회, 코드로 강제)
                 → search_error_docs(RAG) → diagnose_issue → plan_patch
                 → generate_patch_code → apply_patch → rerun_and_check ] × N
              → assess_result ─(검토 필요)→ review_with_human → write_result
```

- `parse_his`: `SEVERE ERROR / ERROR / WARNING` 블록을 추출·집계한다. Part2에서 이미 오류 0이면 파싱을 생략하고 조기 종료한다.
- `begin_repair_attempt`가 수리 횟수를 세어 상한을 넘으면 `allowed=false`를 돌려주므로, 프롬프트가 아니라 **코드 수준에서** 무한 루프를 막는다.
- 패치는 적용 전에 `part3_patch_safety.py`의 안전 검사(대상 존재, 허용 함수, 인자·단위)를 통과해야 한다.

## 7. 데이터·설정 파일 (`shared/`)

| 위치 | 쓰임 |
|---|---|
| `shared/schema/Schema_v5.yaml` | Part1 스키마 규칙(레이어) |
| `shared/aspen/inp_v1.yaml` | INP 문법 DB (Part2 INP 골격) |
| `shared/aspen/aspen_code_v3.yaml` | COM 함수 카탈로그 (Part2 계획·코드 생성, Part3 진단) |
| `shared/aspen/aspen_code_v1.py` | 공용 Aspen COM 헬퍼 |
| `shared/validators/` | Part2 검증기(`valid_*.py`), 규칙(`valid_*_rules.yaml`), 레지스트리 |

논문 텍스트·그림(`jinny_DB/parsed/…`)은 저작권 때문에 공개하지 않는다. 위치는 환경변수 `ASPEN_AGENT_PAPERS_DIR`, 공용 데이터는 `ASPEN_AGENT_DATA_DIR`로 지정한다(`src/paths.py`). 모델 설정은 기본 `gpt-5.5`, 그림 추출은 `gpt-5.2`이고 파트별로 `llm_config`로 바꿀 수 있다.

## 8. 실행 한 번의 산출물

```
src/run/attempt_chat_<타임스탬프>/
  runner_input.json                    # 세 파트 입력 설정
  part1/  Schema_proto.yaml, Schema_validated.yaml, part1_status.json,
          edc_assumption_ledger.json, *.pre_review.yaml (EDC 켰을 때)
  part2/  inp_proto.inp/.bkp, com_skeleton.py, com_completion_*.py,
          com_proto_initial.bkp / com_proto_repaired.bkp, .his, part2_result_iter1.json
  part3/  part3_iter*.{bkp,his,inp}, part3_result.json
```

## 9. 테스트

- `tests/`: 단위테스트(API 키·Aspen 불필요). 저장소 루트에서 `python -m unittest discover -s tests -t .`.
- 평가 스크립트와 정답 라벨, 실행 결과는 이 저장소에 포함하지 않는다.
