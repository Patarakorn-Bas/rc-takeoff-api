# ============================================================
#  RC Quantity Takeoff API — main.py v6.0
#  ภัทรกร สุทธิผล | สาขาวิศวกรรมโยธา
#  มาตรฐาน: ว.ส.ท. 1011-48 + มยผ. 1101-64
#
#  วิธีรัน:
#    1. cd C:\Users\User\Python
#    2. venv\Scripts\activate
#    3. pip install fastapi uvicorn pydantic
#    4. uvicorn main:app --reload --port 8000
#
#  Endpoints:
#    POST /calculate  — คำนวณปริมาณงาน RC
#    POST /debug      — ตรวจสอบ input ก่อนคำนวณ
#    GET  /logs       — ดูสรุปเปรียบเทียบ Token/เวลา 4 Version
#    GET  /           — Health check
# ============================================================

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List
import math
import time
import datetime

app = FastAPI(
    title="RC Quantity Takeoff API",
    description="คำนวณปริมาณงานคอนกรีตเสริมเหล็กตาม ว.ส.ท. 1011-48 + มยผ. 1101-64",
    version="6.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── น้ำหนักเหล็กต่อเมตร (กก./ม.) ──────────────────────────
STEEL_WEIGHT = {
    "DB6":  0.222, "RB6":  0.222,
    "DB9":  0.499, "RB9":  0.499,
    "DB12": 0.888, "RB12": 0.888,
    "DB16": 1.578, "RB16": 1.578,
    "DB20": 2.466, "RB20": 2.466,
    "DB25": 3.853, "RB25": 3.853,
    "DB28": 4.834, "RB28": 4.834,
    "DB32": 6.313, "RB32": 6.313,
}

# ── Waste Factor (%) ────────────────────────────────────────
CONCRETE_WASTE  = 0.05   # +5%
FORMWORK_WASTE  = 0.10   # +10%
STEEL_WASTE = {
    "DB6": 0.15, "RB6": 0.15,
    "DB9": 0.15, "RB9": 0.15,
    "DB12": 0.10, "RB12": 0.10,
    "DB16": 0.10, "RB16": 0.10,
    "DB20": 0.08, "RB20": 0.08,
    "DB25": 0.07, "RB25": 0.07,
    "DB28": 0.07, "RB28": 0.07,
    "DB32": 0.05, "RB32": 0.05,
}

# ── ระยะหุ้มเหล็กตาม มยผ. 1101-64 ตารางที่ 13 ─────────────
COVER = {
    "indoor":      {"main": 0.030, "stirrup": 0.035},
    "outdoor":     {"main": 0.040, "stirrup": 0.040},
    "below_ground":{"main": 0.050, "stirrup": 0.050},
}

# ── Log Storage ─────────────────────────────────────────────
calculation_logs: List[dict] = []


# ── Input Models ────────────────────────────────────────────
class SteelBar(BaseModel):
    size: str           = Field(..., example="DB16", description="ขนาดเหล็ก เช่น DB16, RB6")
    count: int          = Field(..., example=4,      description="จำนวนเส้น")
    length_override: Optional[float] = Field(None,   description="ความยาวพิเศษ (ม.) ถ้าไม่ระบุใช้ความยาวคาน")

class StirrupData(BaseModel):
    size: str           = Field(..., example="RB6")
    spacing: float      = Field(..., example=0.20, description="ระยะเรียง (ม.)")

class BeamInput(BaseModel):
    beam_type: str      = Field(..., example="B1",   description="ชื่อชนิดคาน")
    b: float            = Field(..., example=0.20,   description="ความกว้าง (ม.)")
    h: float            = Field(..., example=0.40,   description="ความสูง (ม.)")
    L: float            = Field(..., example=41.20,  description="ความยาวรวม (ม.) Center-to-Center")
    exposure: str       = Field("indoor", example="indoor", description="indoor / outdoor / below_ground")
    main_bars: List[SteelBar] = Field(...,           description="รายการเหล็กหลัก")
    stirrups: StirrupData = Field(...,               description="เหล็กปลอก")

class CalculateRequest(BaseModel):
    version: str        = Field(..., example="A",    description="Version A/B/C/D")
    beams: List[BeamInput] = Field(...,              description="รายการคานทั้งหมด")
    token_input: Optional[int]  = Field(0,           description="Token input จาก GPT")
    token_output: Optional[int] = Field(0,           description="Token output จาก GPT")

class DebugRequest(BaseModel):
    version: str
    beams: List[BeamInput]


# ── คำนวณปริมาตรคอนกรีต ────────────────────────────────────
def calc_concrete(b: float, h: float, L: float) -> dict:
    net = round(b * h * L, 4)
    order = round(net * (1 + CONCRETE_WASTE), 4)
    return {"net_m3": net, "order_m3": order}


# ── คำนวณแบบหล่อ ────────────────────────────────────────────
def calc_formwork(b: float, h: float, L: float) -> dict:
    net = round((2 * h + b) * L, 4)
    order = round(net * (1 + FORMWORK_WASTE), 4)
    return {"net_m2": net, "order_m2": order}


# ── คำนวณเหล็กหลัก ─────────────────────────────────────────
def calc_main_steel(bars: List[SteelBar], L: float) -> dict:
    results = []
    total_net = 0.0
    total_order = 0.0
    for bar in bars:
        size = bar.size.upper()
        if size not in STEEL_WEIGHT:
            raise HTTPException(400, f"ไม่รู้จักขนาดเหล็ก: {size}")
        bar_L = bar.length_override if bar.length_override else L
        wt_per_m = STEEL_WEIGHT[size]
        waste = STEEL_WASTE.get(size, 0.10)
        net   = round(wt_per_m * bar.count * bar_L, 3)
        order = round(net * (1 + waste), 3)
        total_net   += net
        total_order += order
        results.append({
            "size": size, "count": bar.count,
            "length_m": bar_L, "wt_per_m": wt_per_m,
            "net_kg": net, "order_kg": order,
        })
    return {
        "bars": results,
        "total_net_kg": round(total_net, 3),
        "total_order_kg": round(total_order, 3),
    }


# ── คำนวณเหล็กปลอก ─────────────────────────────────────────
def calc_stirrup(stirrup: StirrupData, b: float, h: float,
                 L: float, exposure: str) -> dict:
    size = stirrup.size.upper()
    if size not in STEEL_WEIGHT:
        raise HTTPException(400, f"ไม่รู้จักขนาดเหล็กปลอก: {size}")

    cover_data = COVER.get(exposure.lower(), COVER["indoor"])
    c = cover_data["stirrup"]
    perimeter = 2 * ((b - 2*c) + (h - 2*c))
    dia_map = {"DB6":0.006,"RB6":0.006,"DB9":0.009,"RB9":0.009,
               "DB12":0.012,"RB12":0.012,"DB16":0.016,"RB16":0.016,
               "DB20":0.020,"RB20":0.020,"DB25":0.025,"RB25":0.025,
               "DB28":0.028,"RB28":0.028,"DB32":0.032,"RB32":0.032}
    hook_len  = 2 * 6 * dia_map.get(size, 0.010)
    stirrup_L  = round(perimeter + hook_len, 4)
    n_stirrups = math.ceil(L / stirrup.spacing) + 1
    wt_per_m   = STEEL_WEIGHT[size]
    waste      = STEEL_WASTE.get(size, 0.15)
    net        = round(wt_per_m * stirrup_L * n_stirrups, 3)
    order      = round(net * (1 + waste), 3)

    return {
        "size": size,
        "spacing_m": stirrup.spacing,
        "stirrup_length_m": stirrup_L,
        "count": n_stirrups,
        "net_kg": net,
        "order_kg": order,
    }


# ── Endpoint: POST /calculate ───────────────────────────────
@app.post("/calculate")
def calculate(req: CalculateRequest):
    start = time.time()

    if req.version.upper() not in ["A", "B", "C", "D"]:
        raise HTTPException(400, "version ต้องเป็น A, B, C หรือ D เท่านั้น")

    beam_results = []
    total_concrete_net   = 0.0
    total_concrete_order = 0.0
    total_formwork_net   = 0.0
    total_formwork_order = 0.0
    total_steel_net      = 0.0
    total_steel_order    = 0.0

    for beam in req.beams:
        concrete = calc_concrete(beam.b, beam.h, beam.L)
        formwork = calc_formwork(beam.b, beam.h, beam.L)
        main_st  = calc_main_steel(beam.main_bars, beam.L)
        stirrup  = calc_stirrup(beam.stirrups, beam.b, beam.h, beam.L, beam.exposure)

        steel_net   = round(main_st["total_net_kg"]   + stirrup["net_kg"],   3)
        steel_order = round(main_st["total_order_kg"] + stirrup["order_kg"], 3)

        total_concrete_net   += concrete["net_m3"]
        total_concrete_order += concrete["order_m3"]
        total_formwork_net   += formwork["net_m2"]
        total_formwork_order += formwork["order_m2"]
        total_steel_net      += steel_net
        total_steel_order    += steel_order

        beam_results.append({
            "beam_type":      beam.beam_type,
            "dimensions":     f"{beam.b*1000:.0f}x{beam.h*1000:.0f} มม.",
            "L_m":            beam.L,
            "exposure":       beam.exposure,
            "concrete_net_m3":    concrete["net_m3"],
            "concrete_order_m3":  concrete["order_m3"],
            "formwork_net_m2":    formwork["net_m2"],
            "formwork_order_m2":  formwork["order_m2"],
            "main_steel":         main_st,
            "stirrup":            stirrup,
            "steel_net_kg":       steel_net,
            "steel_order_kg":     steel_order,
        })

    elapsed = round(time.time() - start, 4)
    token_total = (req.token_input or 0) + (req.token_output or 0)

    # บันทึก log
    log_entry = {
        "timestamp":        datetime.datetime.now().isoformat(),
        "version":          req.version.upper(),
        "token_input":      req.token_input or 0,
        "token_output":     req.token_output or 0,
        "token_total":      token_total,
        "elapsed_sec":      elapsed,
        "total_L_m":        round(sum(b.L for b in req.beams), 3),
        "concrete_net_m3":  round(total_concrete_net,   4),
        "formwork_net_m2":  round(total_formwork_net,   4),
        "steel_net_kg":     round(total_steel_net,      3),
    }
    calculation_logs.append(log_entry)

    return {
        "version":   req.version.upper(),
        "timestamp": log_entry["timestamp"],
        "summary": {
            "concrete_net_m3":    round(total_concrete_net,   4),
            "concrete_order_m3":  round(total_concrete_order, 4),
            "formwork_net_m2":    round(total_formwork_net,   4),
            "formwork_order_m2":  round(total_formwork_order, 4),
            "steel_net_kg":       round(total_steel_net,      3),
            "steel_order_kg":     round(total_steel_order,    3),
        },
        "beams":     beam_results,
        "meta": {
            "token_input":   req.token_input or 0,
            "token_output":  req.token_output or 0,
            "token_total":   token_total,
            "elapsed_sec":   elapsed,
        }
    }


# ── Endpoint: POST /debug ───────────────────────────────────
@app.post("/debug")
def debug(req: DebugRequest):
    issues = []
    warnings = []

    if req.version.upper() not in ["A","B","C","D"]:
        issues.append(f"version '{req.version}' ไม่ถูกต้อง ต้องเป็น A, B, C หรือ D")

    for i, beam in enumerate(req.beams):
        prefix = f"Beam {i+1} ({beam.beam_type})"
        if beam.b <= 0 or beam.b > 2.0:
            issues.append(f"{prefix}: b={beam.b} ม. ดูผิดปกติ (ปกติ 0.10–1.00 ม.)")
        if beam.h <= 0 or beam.h > 5.0:
            issues.append(f"{prefix}: h={beam.h} ม. ดูผิดปกติ")
        if beam.L <= 0:
            issues.append(f"{prefix}: L={beam.L} ม. ต้องมากกว่า 0")
        if beam.L > 500:
            warnings.append(f"{prefix}: L={beam.L} ม. ดูสูงมาก ตรวจสอบด้วย")
        if beam.exposure.lower() not in ["indoor","outdoor","below_ground"]:
            issues.append(f"{prefix}: exposure='{beam.exposure}' ไม่รู้จัก ใช้ indoor/outdoor/below_ground")
        for bar in beam.main_bars:
            if bar.size.upper() not in STEEL_WEIGHT:
                issues.append(f"{prefix}: main bar size '{bar.size}' ไม่รู้จัก")
        if beam.stirrups.size.upper() not in STEEL_WEIGHT:
            issues.append(f"{prefix}: stirrup size '{beam.stirrups.size}' ไม่รู้จัก")
        if beam.stirrups.spacing <= 0 or beam.stirrups.spacing > 1.0:
            warnings.append(f"{prefix}: stirrup spacing={beam.stirrups.spacing} ม. ตรวจสอบด้วย")

    status = "OK" if not issues else "ERROR"
    return {
        "status":   status,
        "issues":   issues,
        "warnings": warnings,
        "message":  "ข้อมูลถูกต้อง พร้อมคำนวณ" if status == "OK" else f"พบ {len(issues)} ปัญหา กรุณาแก้ไขก่อนเรียก /calculate",
    }


# ── Endpoint: GET /logs ──────────────────────────────────────
@app.get("/logs")
def get_logs():
    if not calculation_logs:
        return {"message": "ยังไม่มีการคำนวณ", "logs": []}

    # สรุปแยก Version
    summary = {}
    for log in calculation_logs:
        v = log["version"]
        if v not in summary:
            summary[v] = {
                "version": v, "count": 0,
                "avg_token_total": 0, "avg_elapsed_sec": 0,
                "last_concrete_net_m3": 0,
                "last_formwork_net_m2": 0,
                "last_steel_net_kg": 0,
            }
        summary[v]["count"] += 1
        summary[v]["avg_token_total"] += log["token_total"]
        summary[v]["avg_elapsed_sec"] += log["elapsed_sec"]
        summary[v]["last_concrete_net_m3"] = log["concrete_net_m3"]
        summary[v]["last_formwork_net_m2"] = log["formwork_net_m2"]
        summary[v]["last_steel_net_kg"]    = log["steel_net_kg"]

    for v in summary:
        n = summary[v]["count"]
        summary[v]["avg_token_total"] = round(summary[v]["avg_token_total"] / n, 1)
        summary[v]["avg_elapsed_sec"] = round(summary[v]["avg_elapsed_sec"] / n, 4)

    return {
        "total_calculations": len(calculation_logs),
        "version_summary": list(summary.values()),
        "all_logs": calculation_logs,
    }


# ── Endpoint: GET / (Health Check) ──────────────────────────
@app.get("/")
def health():
    return {
        "status": "running",
        "service": "RC Quantity Takeoff API v6.0",
        "standards": "ว.ส.ท. 1011-48 + มยผ. 1101-64",
        "endpoints": {
            "POST /calculate": "คำนวณปริมาณงาน RC",
            "POST /debug":     "ตรวจสอบ input ก่อนคำนวณ",
            "GET  /logs":      "ดูสรุปเปรียบเทียบ Token/เวลา",
        }
    }
