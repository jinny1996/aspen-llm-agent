#### 0. Aspen control ####

import win32com.client

def start_aspen_doc():
    """Start Aspen COM document"""
    doc = win32com.client.Dispatch("Apwn.Document")
    return doc

def open_aspen(doc, bkp_path=None, visible=True):
    """Set visibility and optionally load a .bkp file into Aspen doc"""
    if bkp_path:
        doc.InitFromFile2(bkp_path)
    else:
        doc.InitNew()
    if visible:
        doc.Visible = True
    return doc

def save_aspen(doc, path):
    """Save the current Aspen document as a .bkp file"""
    doc.SaveAs(path)

def run_aspen(doc):
    """Run the current Aspen simuation"""
    doc.Engine.Run2()

def export_inp(doc, filename):
    """Export the current Aspen simulation to an .inp file"""
    doc.Export(4, filename)

def export_cpm(doc, filename):
    """Export the current Aspen simulation to an .cpm (run messages) file"""
    doc.Export(6, filename)

def close_aspen(doc):
    """Close the Aspen document (False = do not save / True = save)"""
    doc.Close(False)


#### 1. Property method ####
def set_property_method(doc, method="PENG-ROB"):
    """Set the property method (e.g., Peng-ROB, NRTL, ELECNRTL, UNIQUAC, ENRTL-RK, RK-SOAVE)"""
    method_node = doc.Tree.FindNode(r"\Data\Properties\Specifications\Input\GOPSETNAME")
    method_node.Value = method


#### 2. Add and connect stream, block
def stream_add(doc, stream_name, stype="MATERIAL"):
    """
    Add a new stream (Material or Heat) to aspen
    
    Args:
        stream_name: e.g., "S1", "FEED1"
        stype: Stream type. common values:
            - "MATERIAL"
            - "HEAT"
    
    Example:
        add_stream(doc, "S1", "MATERIAL")
    """
    streams = doc.Tree.FindNode(r"\Data\Streams")
    streams.Elements.Add(f"{stream_name}!{stype.upper()}")

def _normalize_block_type(btype: str) -> str:
    """
    Normalize block type names used by generators.
    HeatX is treated as an alias of MHeatX to keep implementation generic.
    Splitter is treated as an alias of FSplit, the Aspen block model name.
    """
    raw = str(btype or "").strip()
    if raw.upper() == "HEATX":
        return "MHeatX"
    if raw.upper() in {"SPLITTER", "FSPLIT"}:
        return "FSplit"
    return raw


def block_add(doc, block_name, btype="Mixer"):
    """
    Add a new block to Aspen

    Args:
        block name: e.g., "B1", "R1", "H1", "F1", "M1"
        btype: Block type. common values:
            - "Mixer", "FSplit", "Heater", "Flash2", "RCSTR", "RPlug", "RYield", "RGibbs", "Radfrac", "MHeatX"
            - If "HeatX" is provided, it is normalized to "MHeatX".
            - If "Splitter" is provided, it is normalized to "FSplit".
    
    Example:
        block_add(doc, "B1", "Mixer")
    """
    blocks = doc.Tree.FindNode(r"\Data\Blocks")
    norm_btype = _normalize_block_type(btype)
    blocks.Elements.Add(f"{block_name}!{norm_btype}")

def connect_stream(doc, block_name, stream_name, port_name):
    """
    Connect a stream to a specific block port

    Args:
        block_name: e.g., "B1", "R-101"
        stream_name: e.g., "S1", "FEED1"
        port_name: Port name on the block. Examples:
            - Reactors: "F(IN)" for feed, "P(OUT)" for product
            - FLASH2: "F(IN)", "V(OUT)", "L(OUT)"
            - RADFRAC in this workflow: "F(IN)", "VD(OUT)", "LD(OUT)", "B(OUT)", optional "SP(OUT)"
            - Heat exchangers (HEATX, MHEATX): "CF(IN)", "CP(OUT)", "HF(IN)", "HP(OUT)"

    Example:
        connect_stream(doc, "R-101", "S1", "F(IN)")
    """
    block = doc.Tree.FindNode(r"\Data\Blocks")
    block.Elements(block_name).Elements("Ports").Elements(port_name).Elements.Add(stream_name)


def delete_stream(doc, stream_name, stype="MATERIAL"):
    """
    Aspen material/heat stream 추가
    name: 스트림 이름 (예: "S1")
    stype: "MATERIAL" (기본), "HEAT" 등
    """
    streams = doc.Tree.FindNode(r"\Data\Streams")
    streams.Elements.Remove(stream_name)

def delete_block(doc, block_name, btype="Mixer"):
    """
    Aspen block 추가
    name: 블록 이름 (예: "B1")
    btype: 블록 종류 (예: "Mixer", "Heater", "RCSTR", "RPlug", "RGibbs", "Flash2", "Radfrac", "Splitter", "RYield")
    """
    blocks = doc.Tree.FindNode(r"\Data\Blocks")
    blocks.Elements.Remove(block_name)

def disconnect_stream(doc, block_name, stream_name, port_name):
    """
    블록과 스트림 연결

    Args:
        block_name: 블록 이름 (예: "B1")
        stream_name: 스트림 이름 (예: "S1", "FEED1")
        port_name: 포트 이름 (예: 일반 블록 "F(IN)", "P(OUT)";
            FLASH2 "F(IN)", "V(OUT)", "L(OUT)";
            RADFRAC "F(IN)", "VD(OUT)", "LD(OUT)", "B(OUT)", optional "SP(OUT)";
            MHEATX "CF(IN)", "CP(OUT)", "HF(IN)", "HP(OUT)")
    """
    block = doc.Tree.FindNode(r"\Data\Blocks")
    block.Elements(block_name).Elements("Ports").Elements(port_name).Elements.Remove(stream_name)



#### 3. Input feed stream ####
# ===== Stream: Temperature =====
def stream_set_temperature(doc, stream_name, T):
    """Set stream temperature"""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\TEMP\MIXED")
    node.Value = T

# ===== Stream: Pressure =====
def stream_set_pressure(doc, stream_name, P):
    """Set stream pressure"""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\PRES\MIXED")
    node.Value = P

# ===== Stream: Total Flow =====
def stream_set_flow_base(doc, stream_name, base):
    """Set flow basis (MOLE or MASS)"""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\FLOWBASE\MIXED")
    node.Value = base.upper()

_KG_PER_HR_UNIT_CODE = 3          # kg/hr in the default unit set; verified at runtime below


def _is_kg_per_hr(unit_string):
    return str(unit_string).strip().lower().replace(" ", "") in {"kg/hr", "kg/h"}


def stream_set_total_flow(doc, stream_name, total_flow, base="MOLE"):
    """Set total flow.

    MOLE keeps the old behaviour (the number lands in the node's current unit, kmol/hr).
    MASS used to be ignored here, so a mass flow such as 303823.76 kg/h was silently entered as
    kmol/hr.  Now it sets an explicit kg/hr unit through SetValueUnitAndBasis (checked against
    Aspen V14 via COM: UnitString becomes 'kg/hr', exported INP gets MASS-FLOW) and raises if the
    unit did not take, instead of falling back to kmol/hr.  (fixed 2026-10-08; original kept as
    aspen_code_v1.py.bak_before_massfix_20261008)"""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\TOTFLOW\MIXED")
    if node is None:
        raise RuntimeError(f"TOTFLOW node not found for stream {stream_name!r}")
    if str(base).strip().upper() != "MASS":
        node.Value = total_flow
        return
    for code in (_KG_PER_HR_UNIT_CODE, *range(0, 20)):
        node.SetValueUnitAndBasis(float(total_flow), code, "MASS")
        if _is_kg_per_hr(node.UnitString):
            break
    else:
        raise RuntimeError(f"could not set a kg/hr unit on TOTFLOW of stream {stream_name!r}")
    if abs(float(node.Value) - float(total_flow)) > 1e-9 * max(1.0, abs(float(total_flow))):
        raise RuntimeError(f"TOTFLOW of {stream_name!r} reads {node.Value}, expected {total_flow}")

# ===== Stream: Composition =====
def stream_set_basis(doc, stream_name, basis):
    """Set composition basis (e.g., 'MOLE-FRAC', 'MASS-FRAC', 'MOLE-FLOW', 'MASS-FLOW')"""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\BASIS\MIXED")
    node.Value = basis.upper()

def stream_set_component_value(doc, stream_name, component, value):
    """Set a single component value depending on the current BASIS."""
    node = doc.Tree.FindNode(rf"\Data\Streams\{stream_name}\Input\FLOW\MIXED\{component}")
    node.Value = value

def stream_set_components(doc, stream_name, comp_dict, basis="MOLE-FRAC"):
    """
    Set multiple component values at once.

    Args:
        comp_dict: Dictionary of components, e.g., {"CH4": 0.8, "H2O": 0.2}
        basis: Composition basis (default: MOLE-FRAC)
    """
    stream_set_basis(doc, stream_name, basis)
    for comp, val in comp_dict.items():
        stream_set_component_value(doc, stream_name, comp, val)

##
def stream_define(doc, stream_name, T, P, total_flow, comp_dict, basis="MOLE-FRAC"):
    """
    Define a new material stream with required inputs.

    Args:
        stream_name: Stream ID, e.g., "S1"
        T: Temperature (default °C)
        P: Pressure (default bar)
        total_flow: Flow rate value
        comp_dict: Component dictionary, e.g., {"CH4": 0.8, "H2O": 0.2}
        basis: Composition basis, default "MOLE-FRAC"

    Example:
        stream_define(doc, "S1", T=300, P=1.5, total_flow=100,
                      comp_dict={"CH4": 0.8, "H2O": 0.2}, basis="MOLE-FRAC")
    """
    stream_set_temperature(doc, stream_name, T)
    stream_set_pressure(doc, stream_name, P)
    stream_set_total_flow(doc, stream_name, total_flow, base="MOLE")
    stream_set_components(doc, stream_name, comp_dict, basis=basis)


#### 4. Block setting ####
# ===== Block: Splitter =====
def splt_set_frac(doc, block_name, outlet_stream, value):
    """
    Set split fraction for an outlet stream

    Args:
        block_name: Splitter block ID (e.g., "SPLT1")
        outlet_stream: Outlet stream ID (e.g., "S2")
        value: Fraction value (0-1)
    
    Example:
        splt_set_frac(doc, "SPLT1", "S2", 0.5)
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\FRAC\{outlet_stream}")
    node.Value = value


# ===== Block: Flash =====
def flash_set_spec(doc, block_name, spec_opt):
    """
    Set flash specification option.

    Args:
        spec_opt: Specification option, one of:
            - "TP", "TV", "PV", "TD", "PD", "PQ", "TQ"

    Example:
        flash_set_spec(doc, "FLASH-101", "TP")
    """
    spec_opt = str(spec_opt).upper()
    allowed = {"TP","TV","PV","TD","PD","PQ","TQ"}
    if spec_opt not in allowed:
        raise ValueError(f"[FLASH] SPEC_OPT must be one of {sorted(allowed)}")
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\SPEC_OPT")
    node.Value = spec_opt

def flash_set_temperature(doc, block_name, T):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TEMP")
    node.Value = T

def flash_set_pressure(doc, block_name, P):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES")
    node.Value = P

def flash_set_duty(doc, block_name, Q):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\DUTY")
    node.Value = Q

def flash_set_vapor_frac(doc, block_name, vfrac):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\VFRAC")
    node.Value = vfrac

##
def flash_config(doc, block_name, *,
                 spec_opt=None, T=None, P=None, duty=None, vfrac=None):
    """
    Configure a flash block with multiple optional settings.

    Args:
        block_name: Flash block ID
        spec_opt: Specification option
        T: Temperature (C)
        P: Pressure (bar)
        duty: Heat duty (cal/sec)
        vfrac: Vapor fraction (0-1)

    Example:
        configure_flash(doc, "FLASH-101", spec_opt="TP", T=350, P=5)
    """
    if spec_opt is not None:
        flash_set_spec(doc, block_name, spec_opt)
    if T is not None:
        flash_set_temperature(doc, block_name, T)
    if P is not None:
        flash_set_pressure(doc, block_name, P)
    if duty is not None:
        flash_set_duty(doc, block_name, duty)
    if vfrac is not None:
        flash_set_vapor_frac(doc, block_name, vfrac)


# ===== Block: Heater =====
def heater_set_spec(doc, block_name, spec_opt):
    """
    Set heater specification option.

    Args:
        block_name: Heater block ID.
        spec_opt: One of:
            TP, TV, TD, TDPPARM, PV, PD, PDT, PDEGSUP, PDEGSUB,
            VDPPARM, DDPPARM, DTV, DTD, DTDPPARM,
            DEGSUPDPPARM, DEGSUBDPPARM, PQ, TQ, DTQ, DPPARMQ.

    Example:
        heater_set_spec(doc, "HEAT-101", "TP")
    """
    spec_opt = str(spec_opt).upper()
    allowed = {
        "TP","TV","TD","TDPPARM","PV","PD","PDT","PDEGSUP","PDEGSUB",
        "VDPPARM","DDPPARM","DTV","DTD","DTDPPARM","DEGSUPDPPARM","DEGSUBDPPARM",
        "PQ","TQ","DTQ","DPPARMQ"
    }
    if spec_opt not in allowed:
        raise ValueError(f"[HEATER] SPEC_OPT must be one of: {sorted(allowed)}")
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\SPEC_OPT")
    node.Value = spec_opt

def heater_set_temperature(doc, block_name, T):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TEMP")
    node.Value = T

def heater_set_pressure(doc, block_name, P):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES")
    node.Value = P

def heater_set_duty(doc, block_name, Q):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\DUTY")
    node.Value = Q

def heater_set_vapor_frac(doc, block_name, vfrac):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\VFRAC")
    node.Value = vfrac

##
def heater_config(doc, block_name, *,
                  spec_opt=None, T=None, P=None, duty=None, vfrac=None):
    """
    Configure multiple settings for a heater block.

    Args:
        block_name: Heater block ID
        spec_opt: Specification option
        T: Temperature (C)
        P: Pressure (bar)
        duty: Heat duty (cal/sec)
        vfrac: Vapor fraction (0-1)

    Example:
        heater_configure(doc, "HEAT-101", spec_opt="TP", T=350, P=5)
    """
    if spec_opt is not None:
        heater_set_spec(doc, block_name, spec_opt)
    if T is not None:
        heater_set_temperature(doc, block_name, T)
    if P is not None:
        heater_set_pressure(doc, block_name, P)
    if duty is not None:
        heater_set_duty(doc, block_name, duty)
    if vfrac is not None:
        heater_set_vapor_frac(doc, block_name, vfrac)


# ===== Block: RStoic =====
def rstoic_set_spec(doc, block_name, spec_opt):
    """
    Set RStoic specification option.

    Args:
        block_name: RStoic block ID.
        spec_opt: One of: TP, TV, PV, TD, PD, PQ, TQ.
    """
    spec_opt = str(spec_opt).upper()
    allowed = {"TP", "TV", "PV", "TD", "PD", "PQ", "TQ"}
    if spec_opt not in allowed:
        raise ValueError(f"[RSTOIC] SPEC_OPT must be one of {sorted(allowed)}")
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\SPEC_OPT")
    node.Value = spec_opt

def rstoic_set_temperature(doc, block_name, T):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TEMP")
    node.Value = T

def rstoic_set_pressure(doc, block_name, P):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES")
    node.Value = P

def rstoic_set_duty(doc, block_name, Q):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\DUTY")
    node.Value = Q

def rstoic_set_vapor_frac(doc, block_name, vfrac):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\VFRAC")
    node.Value = vfrac

##
def rstoic_config(doc, block_name, *, spec_opt=None, T=None, P=None, duty=None):
    """
    Configure multiple settings for an RStoic block.

    Args:
        block_name: RStoic block ID.
        spec_opt: Specification option (TP, TV, PV, TD, PD, PQ, TQ).
        T: Temperature (C)
        P: Pressure (bar)
        duty: Heat duty (cal/sec)
    """
    if spec_opt is not None:
        rstoic_set_spec(doc, block_name, spec_opt)
    if T is not None:
        rstoic_set_temperature(doc, block_name, T)
    if P is not None:
        rstoic_set_pressure(doc, block_name, P)
    if duty is not None:
        rstoic_set_duty(doc, block_name, duty)


# ===== Block: RGibbs =====
def rgibbs_set_opt_chemeq(doc, block_name, mode="PHASE-CHEM"):
    """
    Set RGibbs chemical equilibrium option (OPT_CHEMEQ).

    Args:
        block_name: RGibbs block ID.
        mode: One of:
            - "PHASE-ONLY"
            - "PHASE-CHEM" (default)
            - "RESTRICT"
            - "CALC-TAPP"
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\OPT_CHEMEQ")
    node.Value = mode.upper()

def rgibbs_set_spec(doc, block_name, spec_opt):
    """
    Set RGibbs specification option

    Args:
        block_name: RGibbs block ID
        spec_opt: One of "TEMP" and "DUTY". "Pressure" is requied.
    """
    spec_opt = str(spec_opt).upper()
    allowed = {"TEMP","DUTY"}
    if spec_opt not in allowed:
        raise ValueError(f"[RGIBBS] SPEC_OPT must be one of {allowed}")
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\SPEC_OPT")
    node.Value = spec_opt

def rgibbs_set_temperature(doc, block_name, T):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TEMP")
    node.Value = T

def rgibbs_set_pressure(doc, block_name, P):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES")
    node.Value = P   # PRES는 필수 옵션

def rgibbs_set_duty(doc, block_name, Q):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\DUTY")
    node.Value = Q

# ===== RGibbs: Reaction coefficients =====
def rgibbs_set_reactant_coef(doc, block_name, rxn_idx, comp, value_neg):
    """
    Set stoichiometric coefficient for a reactant (negative value).

    Args:
        rxn_idx: Reaction index.
        comp: Component ID.
        value_neg: Negative coefficient.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\COEF\{rxn_idx}\{comp}")
    node.Value = value_neg

def rgibbs_set_product_coef(doc, block_name, rxn_idx, comp, value_pos):
    """
    Set stoichiometric coefficient for a product (positive value).

    Args:
        rxn_idx: Reaction index.
        comp: Component ID.
        value_pos: Positive coefficient.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\COEF1\{rxn_idx}\{comp}")
    node.Value = value_pos

def rgibbs_define_reaction(doc, block_name, rxn_idx, stoich_dict):
    """
    Define reaction stoichiometry from a dictionary.

    Args:
        stoich_dict: Dict of {component: coeff}.
            Negative = reactant, Positive = product.
            Example: {"CH4": -1, "H2O": -1, "H2": 3, "CO": 1}
    """
    for comp, coef in stoich_dict.items():
        if coef < 0:
            rgibbs_set_reactant_coef(doc, block_name, rxn_idx, comp, coef)
        elif coef > 0:
            rgibbs_set_product_coef(doc, block_name, rxn_idx, comp, coef)

##
def rgibbs_config(doc, block_name, *, opt_chemeq=None, spec_opt=None, T=None, P=None, duty=None):
    """
    Configure multiple settings for an RGibbs block.

    Args:
        block_name: RGibbs block ID
        opt_chemeq: Chemical equilibrium option (PHASE-ONLY, PHASE-CHEM(default), RESTRICT, CALC-TAPP)
        spec_opt: Specification option (one of TEMP and DUTY/ PRES is required).
        T: Temperature.
        P: Pressure (required in most cases).
        duty: Heat duty.
    """
    if opt_chemeq is not None:
        rgibbs_set_opt_chemeq(doc, block_name, opt_chemeq)
    if spec_opt is not None:
        rgibbs_set_spec(doc, block_name, spec_opt)
    if T is not None:
        rgibbs_set_temperature(doc, block_name, T)
    if P is not None:
        rgibbs_set_pressure(doc, block_name, P)
    if duty is not None:
        rgibbs_set_duty(doc, block_name, duty)


# ===== Block: RPlug / TYPE =====

# NOTE:
# - TYPE별 필수 입력 요약
#   * T-SPEC: OPT_TSPEC + REAC_TEMP or LOC profile
#   * ADIABATIC: P, dP (온도는 계산됨)
#   * 기타 TYPE: Aspen GUI 참고

def rplug_set_type(doc, block_name, type_opt):
    """
    Set reactor type option (TYPE).

    Args:
        block_name: RPlug block ID.
        type_opt: One of:
            "T-SPEC", "ADIABATIC", "TCOOL-SPEC", "CO-COOL",
            "COUNTER-COOL", "TCOOL-PROF", "QFLUX-PROF".
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TYPE")
    node.Value = type_opt.upper()

def rplug_set_opt_tspec(doc, block_name, opt):
    """
    Set OPT_TSPEC (valid when TYPE = T-SPEC).

    Args:
        opt: One of: "INLET-TEMP", "CONST-TEMP", "TEMP-PROF".
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\OPT_TSPEC")
    node.Value = opt.upper()

def rplug_set_reactor_temp(doc, block_name, T):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\REAC_TEMP")
    node.Value = T

def rplug_set_temp_profile(doc, block_name, loc_index, T):
    """
    Set temperature profile at a given location index.

    Args:
        loc_index: Location index (0, 1, 2, ...).
        T: Temperature value.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\LOC\#{loc_index}")
    node.Value = T

# ===== Block: RPlug / Config =====
def rplug_set_multitube(doc, block_name, yesno="NO"):
    """Enable or disable multitube option (YES/NO)."""
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CHK_NTUBE")
    node.Value = yesno.upper()

def rplug_set_ntube(doc, block_name, n):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NTUBE")
    node.Value = n

def rplug_set_length(doc, block_name, length):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\LENGTH")
    node.Value = length

def rplug_set_diameter(doc, block_name, diam):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\DIAM")
    node.Value = diam

def rplug_set_phase(doc, block_name, nphase, phase_opt):
    """
    Set reactor phases.

    Args:
        nphase: Number of phases (1, 2, or 3)
        phase_opt: Phase option, e.g., "V", "L"
    
    Example:
        Vapor phase: 1, V
        Liquid phase: 1, L
        Vapor-liquid phase: 2, None
    """
    node1 = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NPHASE")
    node1.Value = nphase
    node2 = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PHASE")
    node2.Value = phase_opt.upper()


# ===== Block: RPlug / Reaction =====
## Missing -> control using inp file


# ===== Block: RPlug / Pressure =====
def rplug_set_pressure(doc, block_name, P):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES")
    node.Value = P

def rplug_set_pressure_drop(doc, block_name, dP):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES_DROP")
    node.Value = dP


# ===== Block: RPlug / Catalyst =====
def rplug_set_catalyst_presence(doc, block_name, yesno="NO"):
    """Enable or disable catalyst presence (YES/NO)."""
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CAT_PRESENT")
    node.Value = yesno.upper()

def rplug_set_catalyst_props(doc, block_name, catwt=None, cat_rho=None, bed_voidage=None):
    """
    Set catalyst properties.

    Args:
        catwt: Catalyst weight.
        cat_rho: Catalyst density.
        bed_voidage: Bed void fraction.
    """
    if catwt is not None:
        doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CATWT").Value = catwt
    if cat_rho is not None:
        doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CAT_RHO").Value = cat_rho
    if bed_voidage is not None:
        doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\BED_VOIDAGE").Value = bed_voidage



# ===== Block: Pump =====
def pump_set_type(doc, block_name, pump_type="PUMP"):
    doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PUMP_TYPE").Value = pump_type

def pump_set_spec(doc, block_name, opt_spec="PRES", value=None):
    """
    Set pump specification option and value.

    Args:
        opt_spec: One of: "PRES", "DELP", "POWER".
        value: Corresponding value.
    """
    base = rf"\Data\Blocks\{block_name}\Input"
    node = doc.Tree.FindNode(base + r"\OPT_SPEC")
    node.Value = opt_spec.upper()

    if opt_spec == "PRES" and value is not None:
        doc.Tree.FindNode(base + r"\PRES").Value = value
    elif opt_spec == "DELP" and value is not None:
        doc.Tree.FindNode(base + r"\DELP").Value = value
    elif opt_spec == "POWER" and value is not None:
        doc.Tree.FindNode(base + r"\POWER").Value = value

def pump_set_eff(doc, block_name, eff=None, deff=None):
    """
    Set pump efficiency.

    Args:
        eff: Efficiency (EFF).
        deff: Driver efficiency (DEFF).
    """
    base = rf"\Data\Blocks\{block_name}\Input"
    if eff is not None:
        doc.Tree.FindNode(base + r"\EFF").Value = eff
    if deff is not None:
        doc.Tree.FindNode(base + r"\DEFF").Value = deff

def pump_set_nphase(doc, block_name, nphase=1):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NPHASE")
    node.Value = nphase


##
def pump_configure(doc, block_name, *, pump_type="PUMP", spec="PRES", value=None, eff=None, deff=None, nphase=1):
    """
    Configure a pump block with multiple settings.

    Args:
        pump_type: Pump type (default: PUMP).
        spec: Specification option (PRES, DELP, POWER).
        value: Value for the spec option.
        eff: Efficiency (EFF).
        deff: Differential efficiency (DEFF).
        nphase: Number of phases.
    """
    pump_set_type(doc, block_name, pump_type)
    pump_set_spec(doc, block_name, spec, value)
    pump_set_eff(doc, block_name, eff, deff)
    pump_set_nphase(doc, block_name, nphase)
    


# ===== Block: Compressor =====
def comp_set_model_type(doc, block_name, model_type="COMPRESSOR"):
    """Set compressor model type (default: COMPRESSOR)."""
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\MODEL_TYPE")
    node.Value = model_type.upper()

def comp_set_type(doc, block_name, comp_type="ISENTROPIC"):
    """Set compressor calculation type (e.g., ISENTROPIC)."""
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\TYPE")
    node.Value = comp_type.upper()

def comp_set_spec(doc, block_name, opt_spec="PRES", value=None):
    """
    Set compressor specification option and value.

    Args:
        opt_spec: One of: "PRES", "DELP", "POWER".
        value: Corresponding value.
    """
    base = rf"\Data\Blocks\{block_name}\Input"
    node = doc.Tree.FindNode(base + r"\OPT_SPEC")
    node.Value = opt_spec.upper()

    if opt_spec == "PRES" and value is not None:
        doc.Tree.FindNode(base + r"\PRES").Value = value
    elif opt_spec == "DELP" and value is not None:
        doc.Tree.FindNode(base + r"\DELP").Value = value
    elif opt_spec == "POWER" and value is not None:
        doc.Tree.FindNode(base + r"\POWER").Value = value

def comp_set_eff(doc, block_name, seff=None, peff=None, meff=None):
    """
    Set compressor efficiency values.

    Args:
        seff: Isentropic efficiency (SEFF).
        peff: Polytropic efficiency (PEFF).
        meff: Mechanical efficiency (MEFF).
    """
    base = rf"\Data\Blocks\{block_name}\Input"
    if seff is not None:
        doc.Tree.FindNode(base + r"\SEFF").Value = seff
    if peff is not None:
        doc.Tree.FindNode(base + r"\PEFF").Value = peff
    if meff is not None:
        doc.Tree.FindNode(base + r"\MEFF").Value = meff

def comp_set_nphase(doc, block_name, nphase=1):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NPHASE")
    node.Value = nphase

##
def comp_configure(
    doc, block_name, *,
    model_type="COMPRESSOR", comp_type="ISENTROPIC",
    spec="PRES", value=None,
    seff=None, peff=None, meff=None,
    nphase=1
):
    """
    Configure a compressor block with multiple settings.

    Args:
        model_type: Compressor model type (default: COMPRESSOR).
        comp_type: Calculation type (e.g., ISENTROPIC).
        spec: Specification option (PRES, DELP, POWER).
        value: Value for the spec option.
        seff: Isentropic efficiency.
        peff: Polytropic efficiency.
        meff: Mechanical efficiency.
        nphase: Number of phases.
    """
    comp_set_model_type(doc, block_name, model_type)
    comp_set_type(doc, block_name, comp_type)
    comp_set_spec(doc, block_name, spec, value)
    comp_set_eff(doc, block_name, seff, peff, meff)
    comp_set_nphase(doc, block_name, nphase)



# ===== Block: MHeatX =====
def mheatx_set_outlet_stream(doc, block_name, in_stream, out_stream):
    """
    Connect an outlet stream for MHeatX.

    Args:
        block_name: MHeatX block ID.
        in_stream: Inlet stream name.
        out_stream: Outlet stream name to connect.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\OUT\{in_stream}")
    node.Value = out_stream

def mheatx_set_spec(doc, block_name, in_stream, spec="TEMP"):
    """
    Set MHeatX specification option.

    Args:
        block_name: MHeatX block ID.
        spec: One of:
            TEMP, VFRAC, DELT, DEGSUP, DEGSUB,
            DUTY, MOLAR-Q, MASS-Q, QFRAC.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\SPEC\{in_stream}")
    node.Value = spec.upper()

def mheatx_set_value(doc, block_name, in_stream, value):
    """
    Set value corresponding to the selected specification.

    Args:
        block_name: MHeatX block ID.
        value: Numeric value for the spec option.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\VALUE\{in_stream}")
    node.Value = value

##
def mheatx_configure(doc, block_name, *, outlet_streams=None, spec=None, value=None):
    """
    Configure an MHeatX block with multiple settings.

    Args:
        block_name: MHeatX block ID.
        outlet_streams: Dict of {in_stream: out_stream}, e.g., {"FEED": "FEED-OUT"}.
        spec: Specification option (TEMP, VFRAC, DELT, ...), or {in_stream: spec}.
        value: Value corresponding to the spec option, or {in_stream: value}.

    Example:
        mheatx_configure(
            doc, "HX-101",
            outlet_streams={"FEED": "FEED-OUT", "COOL": "COOL-OUT"},
            spec="TEMP", value=350
        )
    """
    if outlet_streams:
        for in_stream, out_stream in outlet_streams.items():
            mheatx_set_outlet_stream(doc, block_name, in_stream, out_stream)

        inlet_streams = list(outlet_streams.keys())
    else:
        inlet_streams = []

    if isinstance(spec, dict):
        spec_streams = list(spec.keys())
    elif isinstance(value, dict):
        spec_streams = list(value.keys())
    else:
        # MHeatX with N connected stream pairs requires N-1 specifications.
        spec_streams = inlet_streams[:-1] if len(inlet_streams) > 1 else inlet_streams

    for in_stream in spec_streams:
        if spec is not None:
            spec_value = spec.get(in_stream) if isinstance(spec, dict) else spec
            if spec_value is not None:
                mheatx_set_spec(doc, block_name, in_stream, spec_value)
        if value is not None:
            numeric_value = value.get(in_stream) if isinstance(value, dict) else value
            if numeric_value is not None:
                mheatx_set_value(doc, block_name, in_stream, numeric_value)


# ===== Block: Radfrac =====
# --- Configuration ---
def radfrac_set_calc_mode(doc, block_name, mode="EQUILIBRIUM"):
    """
    Set calculation mode.

    Args:
        mode: One of {"EQUILIBRIUM", "RIG-RATE"}. (Equilibrium mode is default)
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CALC_MODE")
    node.Value = mode.upper()

def radfrac_set_nstage(doc, block_name, nstage):
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NSTAGE")
    node.Value = nstage

def radfrac_set_condenser(doc, block_name, condenser="TOTAL"):
    """
    Set condenser type.

    Args:
        condenser: One of {"NONE", "TOTAL", "PARTIAL-V", "PARTIAL-V-L"}.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\CONDENSER")
    node.Value = condenser.upper()

def radfrac_set_reboiler(doc, block_name, reboiler="KETTLE"):
    """
    Set reboiler type.

    Args:
        reboiler: One of {"NONE", "KETTLE", "THERMOSIPHON"}.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\REBOILER")
    node.Value = reboiler.upper()

def radfrac_set_no_phase(doc, block_name, no_phase="2"):
    """
    Set phase option.

    Args:
        no_phase: "2" for vapor-liquid (default).
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\NO_PHASE")
    node.Value = no_phase


# --- Operating specs ---
def radfrac_set_spec(doc, block_name, spec_name, value):
    """
    Set Radfrac operating spec.

    Args:
        spec_name: e.g., BASIS_D, BASIS_B, BASIS_L1, BASIS_RR,
                   BASIS_VN, BASIS_BR, D:F, B:F, Q1, QN.
        value: Numeric value.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\{spec_name}")
    node.Value = value


# --- Streams ---
def radfrac_set_feed_stage(doc, block_name, stream_name, stage):
    """
    Assign a feed stream to a specific stage.

    Args:
        stream_name: Feed stream ID.
        stage: Stage number (1-based).
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\FEED_STAGE\{stream_name}")
    node.Value = stage


# --- Pressure ---
def radfrac_set_stage_pressure(doc, block_name, stage, pressure):
    """
    Set stage pressure.

    Args:
        stage: Stage number (1-based).
        pressure: Pressure value.

    Note:
        Stage 1 corresponds to PRES1, stage 2(optional) corresponds to PRES2, etc.
    """
    node = doc.Tree.FindNode(rf"\Data\Blocks\{block_name}\Input\PRES{stage}")
    node.Value = pressure


##
def radfrac_configure(
    doc, block_name, *,
    calc_mode="EQUILIBRIUM",
    nstage=None,
    condenser="TOTAL",
    reboiler="KETTLE",
    no_phase="2",
    specs=None,
    feed_stages=None,
    stage_pressures=None
):
    """
    Configure a Radfrac block with multiple settings.

    Args:
        calc_mode: Calculation mode, one of {"EQUILIBRIUM", "RIG-RATE"}.
        nstage: Number of stages (int).
        condenser: Condenser type, one of {"NONE", "TOTAL", "PARTIAL-V", "PARTIAL-V-L"}.
        reboiler: Reboiler type, one of {"NONE", "KETTLE", "THERMOSIPHON"}.
        no_phase: Phase option ("2" = vapor-liquid, default).
        specs: Dict of {spec_name: value}, e.g., {"D:F": 0.95, "BASIS_D": "MOLFRAC"}.
        feed_stages: Dict of {stream_name: stage}, e.g., {"FEED1": 5, "FEED2": 10}.
        stage_pressures: Dict of {stage: pressure}, e.g., {1: 1.0, 10: 1.2}.
    """
    if calc_mode:
        radfrac_set_calc_mode(doc, block_name, calc_mode)
    if nstage is not None:
        radfrac_set_nstage(doc, block_name, nstage)
    if condenser:
        radfrac_set_condenser(doc, block_name, condenser)
    if reboiler:
        radfrac_set_reboiler(doc, block_name, reboiler)
    if no_phase:
        radfrac_set_no_phase(doc, block_name, no_phase)

    if specs:
        for spec_name, value in specs.items():
            radfrac_set_spec(doc, block_name, spec_name, value)

    if feed_stages:
        for stream, stage in feed_stages.items():
            radfrac_set_feed_stage(doc, block_name, stream, stage)

    if stage_pressures:
        for stage, pressure in stage_pressures.items():
            radfrac_set_stage_pressure(doc, block_name, stage, pressure)






