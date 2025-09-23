from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import os

from . import db_cases, warranty
from .vision_openai import ocr_image_openai
from s3_storage import (
    presign_upload as s3_presign_upload,
    public_url as s3_public_url,
    available as s3_available,
)

router = APIRouter(prefix="/v1/cases", tags=["cases"])


class IntakeBody(BaseModel):
    source: str
    client_id: Optional[str] = None
    contract_id: Optional[str] = None
    text: Optional[str] = None


@router.post("/intake")
def intake_case(body: IntakeBody):
    if body.source not in {"cliente", "taller", "whatsapp", "make", "api"}:
        raise HTTPException(status_code=400, detail="source inválido")
    cid = db_cases.create_case(body.source, body.client_id, body.contract_id)
    if not cid:
        raise HTTPException(status_code=500, detail="DB no disponible")
    return {"case_id": cid}


class PresignBody(BaseModel):
    case_id: str
    filename: str
    kind: str  # circulacion | vin_plate | odometro | evidencia | reporte_taller


@router.post("/{case_id}/attachments/presign")
def presign(case_id: str, body: PresignBody):
    if not s3_available():
        raise HTTPException(status_code=400, detail="S3 no configurado")
    res = s3_presign_upload(case_id, body.filename, body.kind)
    if not res:
        raise HTTPException(status_code=500, detail="No fue posible generar URL de subida")
    return res


class ConfirmBody(BaseModel):
    case_id: str
    kind: str
    object_key: Optional[str] = None
    url: Optional[str] = None
    run_ocr: bool = True


@router.post("/{case_id}/attachments/confirm")
def confirm_upload(case_id: str, body: ConfirmBody):
    # Resolver URL
    url = body.url
    if not url and body.object_key:
        url = s3_public_url(body.object_key)
    if not url:
        raise HTTPException(status_code=400, detail="Falta url u object_key")
    # OCR opcional
    ocr = None
    if body.run_ocr:
        ocr = ocr_image_openai(url, body.kind)
    res = db_cases.add_attachment(body.case_id, body.kind, url, ocr=ocr, meta={"object_key": body.object_key})
    if not res:
        raise HTTPException(status_code=500, detail="No se pudo guardar adjunto en DB")
    inserted = bool(res.get('inserted')) if isinstance(res, dict) else True
    payload = {"ok": True, "url": url, "ocr": ocr, "duplicate": not inserted}
    if isinstance(res, dict) and res.get('url_hash'):
        payload["url_hash"] = res.get('url_hash')
    return payload


class ClassifyBody(BaseModel):
    falla_tipo: str


@router.post("/{case_id}/classify")
def classify_case(case_id: str, body: ClassifyBody):
    ok = db_cases.upsert_case_meta(case_id, falla_tipo=body.falla_tipo)
    if not ok:
        raise HTTPException(status_code=500, detail="No se pudo actualizar caso")
    return {"ok": True}


class DeliveredAtBody(BaseModel):
    delivered_at: str  # YYYY-MM-DD


@router.post("/{case_id}/delivered_at")
def set_delivered_at(case_id: str, body: DeliveredAtBody):
    ok = db_cases.upsert_case_meta(case_id, delivered_at=body.delivered_at)
    if not ok:
        raise HTTPException(status_code=500, detail="No se pudo actualizar delivered_at")
    return {"ok": True, "delivered_at": body.delivered_at}


@router.get("/{case_id}/warranty/check")
def warranty_check(case_id: str):
    # TODO: agregar provided local si se integra contacto->case_id
    res = warranty.check(case_id)
    return res


@router.get("/{case_id}/warranty/report")
def warranty_report(case_id: str):
    rep = warranty.build_report(case_id)
    # log action
    try:
        db_cases.add_action(case_id, 'warranty_report_generated', payload=rep, created_by='api')
    except Exception:
        pass
    return rep


@router.get("/{case_id}/warranty/evaluate")
def warranty_evaluate(case_id: str, category: Optional[str] = None, problem: Optional[str] = None):
    res = warranty.policy_evaluate(case_id, fallback_category=category, problem=problem)
    # persist suggested status
    try:
        status = res.get('eligibility')
        if status in ('eligible','review','no_eligible'):
            db_cases.upsert_case_meta(case_id, warranty_status=status)
    except Exception:
        pass
    return res
