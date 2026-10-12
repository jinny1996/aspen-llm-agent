## Stream F1, Schema 기준 (Part 1 결과)

| Case | 첫 요청 | 실행 수 | Stream F1 (평균, 범위) | 최악 사례의 초과 / 누락 스트림 |
|---|---|---|---|---|
| A | 유닛 순서 없음 | 10 | 1.00 (1.00–1.00) | - |
| B | 유닛 순서 포함 | 7 | 1.00 (1.00–1.00) | - |
| B | 유닛 순서 없음 | 1 | 1.00 (1.00–1.00) | - |
| C | 유닛 순서 포함 | 17 | 1.00 (1.00–1.00) | - |
| C | 유닛 순서 없음 | 1 | 1.00 (1.00–1.00) | - |
| D | 유닛 순서 포함 | 6 | 0.96 (0.96–0.96) | 초과 [('Separator1', 'PRODUCT')] / 누락 [] |
| D | 유닛 순서 없음 | 1 | 0.96 (0.96–0.96) | 초과 [('Separator1', 'PRODUCT')] / 누락 [] |
| E | 유닛 순서 포함 | 5 | 1.00 (1.00–1.00) | - |
| E | 유닛 순서 없음 | 3 | 0.85 (0.54–1.00) | 초과 [('HTS', 'LTS'), ('LTS', 'SEP-1'), ('REFORM', 'HTS')] / 누락 [('CWC-1', 'SEP-1'), ('HPC-1', 'HTS'), ('HPC-2', 'MPC-1')] |
| F | 유닛 순서 포함 | 7 | 0.95 (0.95–1.00) | 초과 [('SEP-2', 'PRODUCT'), ('SEP-2', 'PRODUCT')] / 누락 [] |
| F | 유닛 순서 없음 | 3 | 0.83 (0.58–0.95) | 초과 [('MIX-4', 'NH3SYN'), ('NH3SYN', 'SEP-2'), ('SEP-2', 'PRODUCT')] / 누락 [('CWC-2', 'RFC-1'), ('FHH-2', 'NH3SYN'), ('HPC-4', 'MPC-4')] |

## Stream F1, Aspen 모델 기준 (Part 2 이후; .inp가 없으면 Part 2 생성 코드)

| Case | 실행 | 출처 | Stream F1 | 초과 / 누락 스트림 |
|---|---|---|---|---|
| C | 20261006_214246 | com_proto_initial_run.inp | 1.00 | - / - |
| C | 20261006_220801 | com_proto_initial_run.inp | 1.00 | - / - |
| A | 20261006_225749 | com_proto_initial_run.inp | 1.00 | - / - |
| C | 20261007_174708 | com_proto_initial_run.inp | 1.00 | - / - |
| C | 20261007_175417 | com_proto_initial_run.inp | 1.00 | - / - |
| C | 20261007_191032 | com_proto_initial_run.inp | 1.00 | - / - |
| C | 20261007_222124 | com_proto_initial_run.inp | 1.00 | - / - |
| B | 20261007_224705 | final_export_run.inp | 1.00 | - / - |
| B | 20261007_230708 | final_export_run.inp | 1.00 | - / - |
| B | 20261007_232130 | com_skeleton.py | 1.00 | - / - |
| B | 20261007_235813 | final_export_run.inp | 1.00 | - / - |
| B | 20261008_000600 | final_export_run.inp | 1.00 | - / - |
| B | 20261008_134658 | final_export_run.inp | 1.00 | - / - |
| D | 20261008_215900 | com_proto_repaired_run.inp | 1.00 | - / - |
| D | 20261008_222133 | com_proto_repaired_run.inp | 1.00 | - / - |
| D | 20261008_223038 | final_export_run.inp | 1.00 | - / - |
| E | 20261008_224631 | com_proto_initial_run.inp | 1.00 | - / - |
| E | 20261008_230526 | com_proto_initial_run.inp | 1.00 | - / - |
| F | 20261009_001754 | com_skeleton.py | 0.95 | [('SEP-2', 'PRODUCT'), ('SEP-2', 'PRODUCT')] / - |
| F | 20261009_004412 | com_skeleton.py | 1.00 | - / - |
