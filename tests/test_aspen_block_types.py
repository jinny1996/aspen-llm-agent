"""test_aspen_block_types.py -- which block type names does THIS Aspen accept for a compressor?  (needs the Aspen license; ~1 min)

    python test_aspen_block_types.py

Opens a NEW empty Aspen document (nothing is saved), tries block_add with several spellings, then removes the test block again.
Prints one line per spelling: ACCEPTED / REJECTED (+ Aspen's message).  Other pressure changers are included for comparison.
"""
import sys

import win32com.client

CANDIDATES = ["Compr", "COMPR", "Compressor", "COMPRESSOR", "MCompr", "Pump", "PUMP", "Valve", "VALVE", "FSplit", "Heater", "HEATER"]


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    doc = win32com.client.Dispatch("Apwn.Document")
    try:
        doc.InitNew()
        blocks = doc.Tree.FindNode(r"\Data\Blocks")
        for i, t in enumerate(CANDIDATES, 1):
            name = f"T{i}"
            try:
                blocks.Elements.Add(f"{name}!{t}")
                print(f"{t:<12} ACCEPTED")
                try:
                    blocks.Elements.Remove(name)
                except Exception:
                    pass
            except Exception as e:
                msg = str(e).replace("\n", " ")[:110]
                print(f"{t:<12} REJECTED  {msg}")
    finally:
        try:
            doc.Close(False)
        except Exception:
            pass


if __name__ == "__main__":
    main()
