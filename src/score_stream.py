"""score_stream.py -- stream-level structural F1 (Schema and Aspen model), the stricter companion of score_structure*.py.

    python score_stream.py

Block / Edge F1 only count block->block links, so extra, duplicated or dangling material streams are invisible to them (a run whose
FLASH2 got two placeholder product streams on the same outlet port scored 1.00).  Stream F1 counts every material stream that leaves a
block, as a MULTISET of (producer block, consumer block | PRODUCT):
  * a stream with a consumer block      -> (producer, consumer)
  * a stream nobody consumes            -> (producer, "PRODUCT")   (a real product / purge / vent)
  * feed streams (no producer) are not scored: how many external feeds a paper has is not read off the figure reliably.
Gold = the gold edges + the boundary outlets every unit must have (FLASH2 / splitter / multi-stream heat exchanger need two outlets, any
other unit one) that are not already consumed by a gold edge.  So a duplicate stream on one port, a missing product port or an extra
stream all lower precision / recall.  Orphan streams (declared in the generated code, never connected) count as extra streams; the exported .inp never contains them.
Aspen side: the exported .inp when it exists, otherwise the connect_stream() calls of the Part 2
skeleton (what the code tried to build, so a run that stopped at validation is still scored).
"""
from __future__ import annotations

import json
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

import yaml

from eval_dev import HERE, P13
from make_gold_sheet_holdout import final_schema
from score_structure import GOLD, PAPER_OF, match_blocks, runs_for
from score_structure_aspen import ASPEN_TO_SCHEMA, final_inp, parse_inp

TWO_OUTLETS = {"FLASH2", "SPLITTER", "MHEATX"}


def prf_multi(pred: Counter, gold: Counter):
    tp = sum((pred & gold).values())
    fp = sum((pred - gold).values())
    fn = sum((gold - pred).values())
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return (2 * p * r / (p + r) if p + r else 0.0), p, r


def pooled_f1(blocks_tp, blocks_fp, blocks_fn, pred: Counter, gold: Counter, spec_pred=frozenset(), spec_gold=frozenset()) -> float:
    """One structural F1: block, stream and spec items pooled into a single TP / FP / FN count."""
    tp = blocks_tp + sum((pred & gold).values()) + len(spec_pred & spec_gold)
    fp = blocks_fp + sum((pred - gold).values()) + len(spec_pred - spec_gold)
    fn = blocks_fn + sum((gold - pred).values()) + len(spec_gold - spec_pred)
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return 2 * p * r / (p + r) if p + r else 0.0


# ---- spec items ------------------------------------------------------------------------------------------------------------
# A "spec item" is (unit, field): an input an Aspen block needs to run.  Required fields per unit type (what Aspen accepts / needs, not
# what the paper says): heater / RGibbs / Flash2 = temperature (or duty) + pressure; pump / valve = pressure; compressor = pressure +
# efficiency (+ model type and isentropic/polytropic type in the Aspen file); splitter = the outlet fractions of all outlets but one;
# MIXER and MHEATX blocks and efficiencies are not scored (optional / plugin rules).  Every feed stream needs temperature, pressure, flow and composition.
# Over-specifying (a 2nd splitter fraction) is a false positive, a missing field a false negative.
REQ = {"HEATER": ("T", "P"), "RGIBBS": ("T", "P"), "FLASH2": ("T", "P"), "PUMP": ("P",), "VALVE": ("P",),
       "COMPRESSOR": ("P", "eff"), "MIXER": (), "MHEATX": (), "SPLITTER": ()}
REQ_ASPEN_EXTRA = {"COMPRESSOR": ("model_type", "type")}
FEED_FIELDS = ("T", "P", "flow", "z")
CODE_SETTERS = {"heater_set_temperature": "T", "rgibbs_set_temperature": "T", "flash_set_temperature": "T", "heater_set_duty": "T",
                "heater_set_pressure": "P", "rgibbs_set_pressure": "P", "flash_set_pressure": "P",
                "comp_set_eff": "eff", "comp_set_model_type": "model_type", "comp_set_type": "type"}
STREAM_SETTERS = {"stream_set_temperature": "T", "stream_set_pressure": "P", "stream_set_total_flow": "flow",
                  "stream_set_components": "z", "stream_set_component_value": "z"}
_CALL = re.compile(r"^\s*([a-z_0-9]+)\(\s*doc\s*,\s*[\"']([^\"']+)[\"']\s*(?:,\s*([^\n]*))?\)\s*$", re.M)
_CONNECT = re.compile(r"connect_stream\(\s*doc\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*\)")


def _present(v) -> bool:
    if isinstance(v, dict):
        return v.get("value") is not None
    return v is not None and v != ""


def spec_items_schema(schema, mapping) -> tuple:
    """-> (items, feed ids).  Items of blocks that are not mapped to a gold block carry the `extra:` prefix (false positives)."""
    items = set()
    f = schema.get("flowsheet_schema") or {}
    for b in f.get("blocks") or []:
        g = mapping.get(str(b.get("id")), f"extra:{b.get('id')}")
        specs = b.get("specs") if isinstance(b.get("specs"), dict) else {}
        for k, v in specs.items():
            if k in ("T_out_C", "T_C", "duty", "heat_duty_kW") and _present(v):
                items.add((g, "T"))
            elif k in ("P_bar", "P_out_bar", "ratio") and _present(v):
                items.add((g, "P"))
            elif k == "eff" and _present(v):
                items.add((g, "eff"))
            elif k == "split_fractions" and isinstance(v, dict):
                for i in range(len([x for x in v if _present(v[x])])):
                    items.add((g, f"frac{i}"))
    feeds = []
    for fd in (schema.get("io") or {}).get("feeds") or []:
        sid = str(fd.get("stream"))
        feeds.append(sid)
        for k, v in (fd.get("specs") or {}).items():
            field = {"T_C": "T", "P_bar": "P", "flow": "flow", "z": "z"}.get(k)
            if field and (bool(v) if k == "z" else _present(v)):          # z is a {component: fraction} mapping, not a provenance node
                items.add((f"FEED:{sid}", field))
    return items, feeds


def spec_items_code(code: str, mapping_ok: dict) -> tuple:
    items = set()
    produced, consumed = set(), set()
    for m in _CONNECT.finditer(code):
        blk, st, port = m.groups()
        (produced if port.upper().endswith("(OUT)") else consumed).add(st)
    feeds = sorted(consumed - produced)
    frac = Counter()
    for m in _CALL.finditer(code):
        fn, name, rest = m.group(1), m.group(2), m.group(3) or ""
        if fn in STREAM_SETTERS:
            if name in feeds:
                items.add((f"FEED:{name}", STREAM_SETTERS[fn]))
            continue
        g = mapping_ok.get(name, f"extra:{name}")
        if fn in CODE_SETTERS:
            items.add((g, CODE_SETTERS[fn]))
        elif fn in ("pump_set_spec", "comp_set_spec") and re.search(r"[\"'](PRES|DELP)[\"']", rest):
            items.add((g, "P"))
        elif fn == "splt_set_frac":
            items.add((g, f"frac{frac[g]}"))
            frac[g] += 1
    return items, feeds


def gold_specs(case: str, feeds: list, aspen: bool) -> set:
    g = GOLD[case]
    items = set()
    for b in g["blocks"]:
        req = list(REQ.get(b["type"], ()))
        if aspen:
            req += list(REQ_ASPEN_EXTRA.get(b["type"], ()))
        for r in req:
            items.add((b["id"], r))
        if b["type"] == "SPLITTER":
            for i in range(1):                         # two outlets (purge + recycle): one fraction, the other is left to Aspen
                items.add((b["id"], f"frac{i}"))
    for sid in feeds:
        for f in FEED_FIELDS:
            items.add((f"FEED:{sid}", f))
    return items


def _ignore_mheatx(items: set, case: str) -> set:
    """Items that are not scored: MHEATX / MIXER blocks (plugin rules / pressure optional) and efficiencies (Aspen has defaults)."""
    skip = {b["id"] for b in GOLD[case]["blocks"] if b["type"] in ("MHEATX", "MIXER")}
    return {x for x in items if x[0] not in skip and x[1] != "eff"}


def gold_streams(case: str) -> Counter:
    g = GOLD[case]
    c = Counter(tuple(e) for e in g["edges"])
    out_deg = Counter(e[0] for e in g["edges"])
    for b in g["blocks"]:
        need = 2 if b["type"] in TWO_OUTLETS else 1
        for _ in range(max(need - out_deg[b["id"]], 0)):
            c[(b["id"], "PRODUCT")] += 1
    return c


def schema_streams(schema, mapping) -> Counter:
    c = Counter()
    for e in (schema.get("flowsheet_schema") or {}).get("connectivity", {}).get("edges") or []:
        fb = (e.get("from") or {}).get("block") if isinstance(e.get("from"), dict) else None
        tb = (e.get("to") or {}).get("block") if isinstance(e.get("to"), dict) else None
        if not fb:
            continue
        c[(mapping.get(str(fb), f"extra:{fb}"), mapping.get(str(tb), f"extra:{tb}") if tb else "PRODUCT")] += 1
    return c


def streams_from_inp(text: str, ok: dict) -> Counter:
    ins, outs = {}, {}
    in_flow = False
    for ln in text.splitlines():
        s = ln.strip()
        if re.match(r"^FLOWSHEET\b", s, re.I):
            in_flow = True
            continue
        if in_flow:
            m = re.match(r"^BLOCK\s+(\S+)\s+(?:IN=(.*?))?\s*(?:OUT=(.*))?$", s, re.I)
            if m:
                ins[m.group(1)] = (m.group(2) or "").split()
                outs[m.group(1)] = (m.group(3) or "").split()
                continue
            if s == "":
                in_flow = False
    return _streams(ins, outs, ok)


def streams_from_code(text: str, ok: dict) -> Counter:
    ins, outs = {}, {}
    for m in re.finditer(r"connect_stream\(\s*doc\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*\)", text):
        blk, st, port = m.groups()
        (outs if port.upper().endswith("(OUT)") else ins).setdefault(blk, []).append(st)
    c = _streams(ins, outs, ok)
    used = {s for lst in list(ins.values()) + list(outs.values()) for s in lst}
    for m in re.finditer(r"stream_add\(\s*doc\s*,\s*[\"']([^\"']+)[\"']", text):
        if m.group(1) not in used:                       # declared but never connected: an orphan stream is an extra stream
            c[(f"orphan:{m.group(1)}", "PRODUCT")] += 1
    return c


def _streams(ins, outs, ok) -> Counter:
    c = Counter()
    for x, olist in outs.items():
        for st in olist:
            consumers = [y for y, il in ins.items() if y != x and st in il]
            gx = ok.get(x, f"extra:{x}")
            if not consumers:
                c[(gx, "PRODUCT")] += 1
            for y in consumers:
                c[(gx, ok.get(y, f"extra:{y}"))] += 1
    return c


def block_types_from_code(text: str) -> dict:
    return {m.group(1): m.group(2).upper() for m in re.finditer(r"block_add\(\s*doc\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']", text)}


def score_aspen(run: Path, case: str):
    sp = final_schema(run)
    if sp is None:
        return None
    schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
    g = GOLD[case]
    mapping = match_blocks(schema, g["blocks"])
    gold_type = {b["id"]: b["type"] for b in g["blocks"]}
    inp = final_inp(run)
    if inp is not None:
        types, _ = parse_inp(inp.read_text(encoding="utf-8", errors="replace"))
        ok = {a: m for a, m in mapping.items() if ASPEN_TO_SCHEMA.get(types.get(a, ""), "") == gold_type[m]}
        pred = streams_from_inp(inp.read_text(encoding="utf-8", errors="replace"), ok)
        source = inp.name
    else:
        sk = run / "part2" / "com_skeleton.py"
        if not sk.exists():
            return None
        code = sk.read_text(encoding="utf-8", errors="replace")
        types = block_types_from_code(code)
        ok = {a: m for a, m in mapping.items() if ASPEN_TO_SCHEMA.get(types.get(a, ""), types.get(a, "")) == gold_type[m]}
        pred = streams_from_code(code, ok)
        source = "com_skeleton.py"
    gold = gold_streams(case)
    f = prf_multi(pred, gold)
    n_ok = len(ok)
    skp = run / "part2" / "com_skeleton.py"
    sp_items, sp_feeds = spec_items_code(skp.read_text(encoding="utf-8", errors="replace"), ok) if skp.exists() else (set(), [])
    sp_items = _ignore_mheatx(sp_items, case)
    spec_gold = _ignore_mheatx(gold_specs(case, sp_feeds, True), case)
    sf = pooled_f1(n_ok, len(types) - n_ok, len(gold_type) - n_ok, pred, gold, sp_items, spec_gold)
    return {"run": run.name, "source": source, "stream_f1": f[0], "struct_f1": sf, "spec_extra": sorted(sp_items - spec_gold, key=str), "spec_missing": sorted(spec_gold - sp_items, key=str), "p": f[1], "r": f[2], "extra": sorted((pred - gold).elements(), key=str), "missing": sorted((gold - pred).elements(), key=str)}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    md = ["## Stream F1, Schema 기준 (Part 1 결과)", "", "| Case | 첫 요청 | 실행 수 | Stream F1 (평균, 범위) | 최악 사례의 초과 / 누락 스트림 |", "|---|---|---|---|---|"]
    out = {}
    for case, seq in [(c, s) for c in "ABCDEF" for s in (True, False)]:
        res = []
        for d, sp in runs_for(case, seq):
            schema = yaml.safe_load(sp.read_text(encoding="utf-8"))
            mapping = match_blocks(schema, GOLD[case]["blocks"])
            gs, ps = gold_streams(case), schema_streams(schema, mapping)
            f = prf_multi(ps, gs)
            n_sb = len((schema.get("flowsheet_schema") or {}).get("blocks") or [])
            sp_items, sp_feeds = spec_items_schema(schema, mapping)
            sf = pooled_f1(len(mapping), n_sb - len(mapping), len(GOLD[case]["blocks"]) - len(mapping), ps, gs,
                           _ignore_mheatx(sp_items, case), _ignore_mheatx(gold_specs(case, sp_feeds, False), case))
            res.append((d.name, f[0], sorted((ps - gs).elements(), key=str), sorted((gs - ps).elements(), key=str), sf))
        if not res:
            continue
        label = "유닛 순서 포함" if seq else "유닛 순서 없음"
        v = [r[1] for r in res]
        worst = min(res, key=lambda x: x[1])
        extra = f"초과 {worst[2][:3]} / 누락 {worst[3][:3]}" if (worst[2] or worst[3]) else "-"
        md.append(f"| {case} | {label} | {len(res)} | {statistics.mean(v):.2f} ({min(v):.2f}–{max(v):.2f}) | {extra} |")
        out[f"schema:{case}{'' if seq else 'n'}"] = [{"run": n, "stream_f1": x, "extra": e, "missing": m, "struct_f1": sf} for n, x, e, m, sf in res]
    md += ["", "## Stream F1, Aspen 모델 기준 (Part 2 이후; .inp가 없으면 Part 2 생성 코드)", "",
           "| Case | 실행 | 출처 | Stream F1 | 초과 / 누락 스트림 |", "|---|---|---|---|---|"]
    for root in (P13 / "run", HERE / "run"):
        for d in sorted(root.glob("attempt_chat_*")):
            if not (d / "part2").is_dir() or not any((d / "part2").iterdir()):
                continue
            try:
                pin = json.loads((d / "runner_input.json").read_text(encoding="utf-8"))["part1_input"]
            except Exception:
                continue
            path = pin["paper_text_path"].replace("\\", "/")
            case = next((c for c in "ABCDEF" if PAPER_OF[c] in path and (c not in "EF" or ("Haber" in pin.get("user_input_text", "")) == (c == "F"))), None)
            if case is None or ("->" not in pin.get("user_input_text", "") and case != "A"):
                continue
            r = score_aspen(d, case)
            if r:
                md.append(f'| {case} | {r["run"][-15:]} | {r["source"]} | {r["stream_f1"]:.2f} | {r["extra"][:3] or "-"} / {r["missing"][:3] or "-"} |')
                out.setdefault(f"aspen:{case}", []).append(r)
    text = "\n".join(md)
    print(text)
    (HERE / "meeting" / "STREAM_F1.md").write_text(text + "\n", encoding="utf-8")
    (HERE / "meeting" / "stream_f1_detail.json").write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


if __name__ == "__main__":
    main()
