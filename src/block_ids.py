"""block_ids.py -- keep Aspen block IDs short enough for every block model.

Why: an MHeatX block creates helper blocks named "$<id>HTR" (seen in the Aspen history file).  Aspen
block IDs are limited, so an MHeatX with a 5-character ID such as "B0102" fails in `block_add`
("Invalid Block/Model/Library Specification"), while 2-4 characters work (213 earlier runs used 2
characters; the one run with a 5-character ID failed, and the user knows from experience that 5 characters
only sometimes works).  The Part1 model chooses block IDs freely (B1..B4 in one run, B0101..B0104 in
the next), so the schema is normalised right after it is drafted: every block ID longer than MAX_LEN is
replaced by the smallest free `B<n>`.  IDs that are already short are never touched.

Only whole-string references are rewritten (the block's own id, edge endpoints, the topology lists in
`validation`), never text inside notes.  The old ID is kept in `specs.original_id` when the block has none.
Pure function, no I/O.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

MAX_LEN = 4
_SHORT = re.compile(rf"^[A-Za-z][A-Za-z0-9_]{{0,{MAX_LEN - 1}}}$")


def shorten_block_ids(schema: Dict[str, Any]) -> Dict[str, str]:
    """Rename too-long block IDs IN PLACE.  -> {old_id: new_id} (empty when nothing had to change)."""
    fs = schema.get("flowsheet_schema") if isinstance(schema, dict) else None
    blocks = (fs or {}).get("blocks")
    if not isinstance(blocks, list):
        return {}
    ids = [str(b.get("id")) for b in blocks if isinstance(b, dict) and b.get("id") is not None]
    long_ids = [i for i in ids if not _SHORT.match(i)]
    if not long_ids:
        return {}
    taken = {i for i in ids if _SHORT.match(i)}
    mapping: Dict[str, str] = {}
    n = 1
    for old in ids:
        if old not in long_ids or old in mapping:
            continue
        while f"B{n}" in taken:
            n += 1
        mapping[old] = f"B{n}"
        taken.add(f"B{n}")
    for b in blocks:
        if isinstance(b, dict) and str(b.get("id")) in mapping:
            old = str(b["id"])
            b["id"] = mapping[old]
            specs = b.get("specs")
            if isinstance(specs, dict) and not specs.get("original_id"):
                specs["original_id"] = old
    for e in ((fs.get("connectivity") or {}).get("edges")) or []:
        for end in ("from", "to"):
            p = e.get(end) if isinstance(e, dict) else None
            if isinstance(p, dict) and isinstance(p.get("block"), str) and p["block"] in mapping:
                p["block"] = mapping[p["block"]]
    topo = (schema.get("validation") or {}).get("topology_consistency")
    if isinstance(topo, dict):
        for k in ("block_ids_declared", "block_ids_referenced_in_edges"):
            if isinstance(topo.get(k), list):
                topo[k] = [mapping.get(x, x) if isinstance(x, str) else x for x in topo[k]]
    return mapping


def describe(mapping: Dict[str, str]) -> str:
    return ", ".join(f"{o}->{n}" for o, n in mapping.items())


def short_ids_in(ids: List[str]) -> bool:
    return all(_SHORT.match(i) for i in ids)
