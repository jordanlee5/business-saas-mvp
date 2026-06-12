from fastapi import FastAPI, Request, Form, UploadFile, File, Query
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_

import os
import hashlib
from datetime import datetime, time
import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .database import engine, Base, SessionLocal
from . import models
from .models import User, BusinessRecord, UploadBatch, VoucherRecord, MatchReview
from .auth import verify_password, get_password_hash
from .excel_service import parse_business_excel
from .ocr_service import ocr_image, match_ocr_with_records

app = FastAPI(title="业务数据管理SaaS MVP")

# 创建数据库表
Base.metadata.create_all(bind=engine)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

os.makedirs("uploads", exist_ok=True)
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")

templates = Jinja2Templates(directory="app/templates")


def get_current_user(request: Request):
    user_id = request.cookies.get("user_id")
    if not user_id:
        return None

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == int(user_id)).first()
        return user
    except Exception:
        return None
    finally:
        db.close()

def build_stats_data(partner_id: int = 0, start_date: str = "", end_date: str = ""):
    db = SessionLocal()

    partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()

    selected_partner_id = partner_id
    selected_start_date = start_date
    selected_end_date = end_date

    start_datetime = None
    end_datetime = None

    try:
        if selected_start_date:
            start_datetime = datetime.combine(
                datetime.strptime(selected_start_date, "%Y-%m-%d").date(),
                time.min,
            )

        if selected_end_date:
            end_datetime = datetime.combine(
                datetime.strptime(selected_end_date, "%Y-%m-%d").date(),
                time.max,
            )
    except Exception:
        start_datetime = None
        end_datetime = None
        selected_start_date = ""
        selected_end_date = ""

    records_query = db.query(BusinessRecord)
    batches_query = db.query(UploadBatch)

    if start_datetime:
        records_query = records_query.filter(BusinessRecord.created_at >= start_datetime)
        batches_query = batches_query.filter(UploadBatch.created_at >= start_datetime)

    if end_datetime:
        records_query = records_query.filter(BusinessRecord.created_at <= end_datetime)
        batches_query = batches_query.filter(UploadBatch.created_at <= end_datetime)

    if selected_partner_id != 0:
        records_query = records_query.filter(BusinessRecord.user_id == selected_partner_id)
        batches_query = batches_query.filter(UploadBatch.user_id == selected_partner_id)

    business_records = records_query.all()

    total_records = len(business_records)
    total_batches = batches_query.count()
    total_partners = db.query(User).filter(User.role == "partner").count()

    reviews_query = db.query(MatchReview)

    if start_datetime:
        reviews_query = reviews_query.filter(MatchReview.created_at >= start_datetime)

    if end_datetime:
        reviews_query = reviews_query.filter(MatchReview.created_at <= end_datetime)

    if selected_partner_id != 0:
        partner_record_ids = [r.id for r in business_records]
        if partner_record_ids:
            reviews_query = reviews_query.filter(MatchReview.business_record_id.in_(partner_record_ids))
        else:
            reviews_query = reviews_query.filter(MatchReview.id == -1)

    pending_reviews = reviews_query.filter(MatchReview.review_status == "待审核").count()
    approved_reviews = reviews_query.filter(MatchReview.review_status == "已通过").count()
    rejected_reviews = reviews_query.filter(MatchReview.review_status == "已驳回").count()

    total_points = 0
    total_receivable_fee = 0
    total_payable_cost = 0
    total_gross_profit = 0

    approved_settlement_count = 0
    approved_settlement_points = 0
    approved_settlement_receivable_fee = 0
    approved_settlement_payable_cost = 0
    approved_settlement_gross_profit = 0

    rows = []

    for record in business_records:
        uploader = db.query(User).filter(User.id == record.user_id).first()

        points_amount = record.points_amount or 0
        service_rate = uploader.service_rate if uploader else 0
        upstream_cost_rate = uploader.upstream_cost_rate if uploader else 0

        receivable_fee = points_amount * service_rate / 100
        payable_cost = points_amount * upstream_cost_rate / 100
        gross_profit = receivable_fee - payable_cost

        total_points += points_amount
        total_receivable_fee += receivable_fee
        total_payable_cost += payable_cost
        total_gross_profit += gross_profit

        latest_review = (
            db.query(MatchReview)
            .filter(MatchReview.business_record_id == record.id)
            .order_by(MatchReview.id.desc())
            .first()
        )

        if latest_review and latest_review.review_status == "已通过":
            approved_settlement_count += 1
            approved_settlement_points += points_amount
            approved_settlement_receivable_fee += receivable_fee
            approved_settlement_payable_cost += payable_cost
            approved_settlement_gross_profit += gross_profit

        review = (
            db.query(MatchReview)
            .filter(MatchReview.business_record_id == record.id)
            .order_by(MatchReview.id.desc())
            .first()
        )

        review_status = latest_review.review_status if latest_review else "未匹配"

        rows.append(
            {
                "上传方": uploader.username if uploader else "未知上传方",
                "姓名": record.name,
                "手机号": record.phone,
                "车牌号": record.plate_number,
                "积分金额": points_amount,
                "银行卡号": record.bank_card,
                "下游服务费率": service_rate,
                "应收服务费": round(receivable_fee, 2),
                "上游成本费率": upstream_cost_rate,
                "应付成本费": round(payable_cost, 2),
                "毛利": round(gross_profit, 2),
                "审核状态": review_status,
                "导入时间": record.created_at,
            }
        )

    db.close()

    return {
        "partners": partners,
        "selected_partner_id": selected_partner_id,
        "selected_start_date": selected_start_date,
        "selected_end_date": selected_end_date,
        "total_records": total_records,
        "total_batches": total_batches,
        "total_partners": total_partners,
        "pending_reviews": pending_reviews,
        "approved_reviews": approved_reviews,
        "rejected_reviews": rejected_reviews,
        "total_points": round(total_points, 2),
        "total_receivable_fee": round(total_receivable_fee, 2),
        "total_payable_cost": round(total_payable_cost, 2),
        "total_gross_profit": round(total_gross_profit, 2),

        "approved_settlement_count": approved_settlement_count,
        "approved_settlement_points": round(approved_settlement_points, 2),
        "approved_settlement_receivable_fee": round(approved_settlement_receivable_fee, 2),
        "approved_settlement_payable_cost": round(approved_settlement_payable_cost, 2),
        "approved_settlement_gross_profit": round(approved_settlement_gross_profit, 2),

        "rows": rows,
    }

def format_excel_file(writer):
    workbook = writer.book

    for worksheet in workbook.worksheets:
        worksheet.freeze_panes = "A2"

        for cell in worksheet[1]:
            cell.font = Font(bold=True)

        for column_cells in worksheet.columns:
            max_length = 0
            column_letter = get_column_letter(column_cells[0].column)

            for cell in column_cells:
                cell_value = cell.value
                if cell_value is None:
                    continue

                cell_length = len(str(cell_value))
                if cell_length > max_length:
                    max_length = cell_length

                if isinstance(cell_value, (int, float)):
                    cell.number_format = "#,##0.00"

                if "时间" in str(worksheet.cell(row=1, column=cell.column).value):
                    cell.number_format = "yyyy-mm-dd hh:mm:ss"

            worksheet.column_dimensions[column_letter].width = min(max_length + 4, 35)

@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "home.html",
        {
            "request": request,
            "title": "业务数据管理SaaS MVP",
        },
    )


@app.get("/query-record", response_class=HTMLResponse)
def query_record_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "query_record.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "records": None,
            "keyword": "",
        },
    )


@app.post("/query-record", response_class=HTMLResponse)
def query_record_submit(
    request: Request,
    keyword: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    keyword = keyword.strip()

    db = SessionLocal()

    query = db.query(BusinessRecord)

    # 权限隔离：
    # 管理员可以查所有数据
    # 上传方只能查自己上传的数据
    if user.role != "admin":
        query = query.filter(BusinessRecord.user_id == user.id)

    query_result = query.filter(
        or_(
            BusinessRecord.name == keyword,
            BusinessRecord.phone == keyword,
            BusinessRecord.plate_number == keyword,
            BusinessRecord.bank_card == keyword,
        )
    ).order_by(BusinessRecord.id.desc()).all()

    records = []

    for r in query_result:
        uploader = db.query(User).filter(User.id == r.user_id).first()

        service_rate = uploader.service_rate if uploader else 0
        upstream_cost_rate = uploader.upstream_cost_rate if uploader else 0

        points_amount = r.points_amount or 0

        receivable_fee = points_amount * service_rate / 100
        payable_cost = points_amount * upstream_cost_rate / 100
        gross_profit = receivable_fee - payable_cost

        records.append(
            {
                "id": r.id,
                "name": r.name,
                "phone": r.phone,
                "plate_number": r.plate_number,
                "points_amount": points_amount,
                "bank_card": r.bank_card,
                "created_at": r.created_at,
                "uploader_username": uploader.username if uploader else "未知上传方",
                "service_rate": service_rate,
                "upstream_cost_rate": upstream_cost_rate,
                "receivable_fee": round(receivable_fee, 2),
                "payable_cost": round(payable_cost, 2),
                "gross_profit": round(gross_profit, 2),
            }
        )

    db.close()

    return templates.TemplateResponse(
        "query_record.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "records": records,
            "keyword": keyword,
        },
    )


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
        },
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    db = SessionLocal()
    user = db.query(User).filter(User.username == username).first()
    db.close()

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "用户名或密码错误",
            },
        )

    response = RedirectResponse(url="/dashboard", status_code=302)
    response.set_cookie(
        key="user_id",
        value=str(user.id),
        httponly=True,
        max_age=60 * 60 * 8,
    )
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
        },
    )

@app.get("/partners", response_class=HTMLResponse)
def partners_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = SessionLocal()
    partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()
    db.close()

    return templates.TemplateResponse(
        "partners.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "partners": partners,
            "message": None,
            "error": None,
        },
    )


@app.post("/partners", response_class=HTMLResponse)
def create_partner(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    service_rate: float = Form(...),
    upstream_cost_rate: float = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = SessionLocal()

    existing_user = db.query(User).filter(User.username == username).first()
    if existing_user:
        partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()
        db.close()
        return templates.TemplateResponse(
            "partners.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "partners": partners,
                "message": None,
                "error": "该账号已存在，请换一个账号名",
            },
        )

    if service_rate < 0 or service_rate > 100:
        partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()
        db.close()
        return templates.TemplateResponse(
            "partners.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "partners": partners,
                "message": None,
                "error": "服务费率必须在 0 到 100 之间",
            },
        )

    if upstream_cost_rate < 0 or upstream_cost_rate > 100:
        partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()
        db.close()
        return templates.TemplateResponse(
            "partners.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "partners": partners,
                "message": None,
                "error": "上游成本费率必须在 0 到 100 之间",
            },
        )

    new_partner = User(
        username=username,
        password_hash=get_password_hash(password),
        role="partner",
        service_rate=service_rate,
        upstream_cost_rate=upstream_cost_rate,
    )

    db.add(new_partner)
    db.commit()

    partners = db.query(User).filter(User.role == "partner").order_by(User.id.desc()).all()
    db.close()

    return templates.TemplateResponse(
        "partners.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "partners": partners,
            "message": f"上传方账号 {username} 创建成功，下游服务费率为 {service_rate}%，上游成本费率为 {upstream_cost_rate}%",
            "error": None,
        },
    )


@app.get("/upload-excel", response_class=HTMLResponse)
def upload_excel_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    return templates.TemplateResponse(
        "upload_excel.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "message": None,
            "errors": None,
        },
    )


@app.post("/upload-excel", response_class=HTMLResponse)
async def upload_excel_submit(
    request: Request,
    file: UploadFile = File(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if not file.filename.endswith((".xlsx", ".xls")):
        return templates.TemplateResponse(
            "upload_excel.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "message": None,
                "errors": ["只允许上传 Excel 文件：.xlsx 或 .xls"],
            },
        )

    os.makedirs("uploads/excel", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_filename = f"{user.id}_{timestamp}_{file.filename}"
    file_path = os.path.join("uploads", "excel", safe_filename)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    records, errors = parse_business_excel(file_path)

    db = SessionLocal()

    batch = UploadBatch(
        user_id=user.id,
        filename=file.filename,
        total_rows=len(records) + len(errors),
        success_rows=len(records),
        failed_rows=len(errors),
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)

    for item in records:
        record = BusinessRecord(
            user_id=user.id,
            batch_id=batch.id,
            name=item["name"],
            phone=item["phone"],
            plate_number=item["plate_number"],
            points_amount=item["points_amount"],
            bank_card=item["bank_card"],
        )
        db.add(record)

    db.commit()
    db.close()

    message = f"上传成功：共读取 {len(records) + len(errors)} 行，成功导入 {len(records)} 行，失败 {len(errors)} 行。"

    return templates.TemplateResponse(
        "upload_excel.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "message": message,
            "errors": errors,
        },
    )

@app.get("/upload-voucher", response_class=HTMLResponse)
def upload_voucher_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # 第一版只允许管理员上传凭证
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    return templates.TemplateResponse(
        "upload_voucher.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "ocr_text": None,
            "match_results": None,
            "error": None,
        },
    )


@app.post("/upload-voucher", response_class=HTMLResponse)
async def upload_voucher_submit(
    request: Request,
    file: UploadFile = File(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    # 第一版只允许管理员上传凭证
    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    if not file.filename.lower().endswith((".png", ".jpg", ".jpeg")):
        return templates.TemplateResponse(
            "upload_voucher.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "ocr_text": None,
                "match_results": None,
                "error": "只允许上传 PNG / JPG / JPEG 图片",
            },
        )

    os.makedirs("uploads/vouchers", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    safe_filename = f"{user.id}_{timestamp}_{file.filename}"
    file_path = os.path.join("uploads", "vouchers", safe_filename)

    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    file_hash = hashlib.sha256(content).hexdigest()

    db = SessionLocal()

    existing_voucher = (
        db.query(VoucherRecord)
        .filter(VoucherRecord.file_hash == file_hash)
        .first()
    )

    if existing_voucher:
        db.close()
        return templates.TemplateResponse(
            "upload_voucher.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "ocr_text": existing_voucher.ocr_text,
                "match_results": [],
                "error": f"该凭证图片已上传过，原凭证文件：{existing_voucher.filename}，本次未重复生成审核记录。",
            },
        )

    try:
        ocr_text = ocr_image(file_path)
    except Exception as e:
        return templates.TemplateResponse(
            "upload_voucher.html",
            {
                "request": request,
                "username": user.username,
                "role": user.role,
                "ocr_text": None,
                "match_results": None,
                "error": f"OCR识别失败：{e}",
            },
        )



    # 第一版：管理员上传凭证时，和所有业务数据匹配
    voucher_record = VoucherRecord(
    uploader_id=user.id,
    filename=file.filename,
    file_path=file_path,
    file_hash=file_hash,
    ocr_text=ocr_text,
    )
    db.add(voucher_record)
    db.commit()
    db.refresh(voucher_record)

    records = db.query(BusinessRecord).all()
    raw_match_results = match_ocr_with_records(ocr_text, records)

    match_results = []

    for item in raw_match_results:
        record = item["record"]

        existing_review = (
            db.query(MatchReview)
            .filter(MatchReview.voucher_id == voucher_record.id)
            .filter(MatchReview.business_record_id == record.id)
            .first()
        )

        if not existing_review:
            review = MatchReview(
                voucher_id=voucher_record.id,
                business_record_id=record.id,
                match_status=item["status"],
                name_match=item.get("name_detail", "未知"),
                bank_match="是" if item["bank_match"] else "否",
                amount_match="是" if item["amount_match"] else "否",
                score=item["score"],
                review_status="待审核",
            )
            db.add(review)

        match_results.append(
            {
                "status": item["status"],
                "name_match": item["name_match"],
                "name_detail": item.get("name_detail", "未知"),
                "bank_match": item["bank_match"],
                "amount_match": item["amount_match"],
                "score": item["score"],
                "record": {
                    "id": record.id,
                    "name": record.name,
                    "phone": record.phone,
                    "plate_number": record.plate_number,
                    "points_amount": record.points_amount,
                    "bank_card": record.bank_card,
                },
            }
        )

    db.commit()
    db.close()
    
    return templates.TemplateResponse(
        "upload_voucher.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "ocr_text": ocr_text,
            "match_results": match_results,
            "error": None,
        },
    )

@app.get("/match-reviews", response_class=HTMLResponse)
def match_reviews_page(
    request: Request,
    status_filter: str = Query("全部"),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = SessionLocal()

    reviews = db.query(MatchReview).order_by(MatchReview.id.desc()).all()

    latest_reviews = []
    seen_business_record_ids = set()

    for review in reviews:
        if review.business_record_id in seen_business_record_ids:
            continue

        seen_business_record_ids.add(review.business_record_id)
        latest_reviews.append(review)

    if status_filter in ["待审核", "已通过", "已驳回"]:
        latest_reviews = [
            review for review in latest_reviews
            if review.review_status == status_filter
        ]

    review_items = []

    for review in latest_reviews:
        voucher = db.query(VoucherRecord).filter(VoucherRecord.id == review.voucher_id).first()
        record = db.query(BusinessRecord).filter(BusinessRecord.id == review.business_record_id).first()

        if voucher and record:
            uploader = db.query(User).filter(User.id == record.user_id).first()

            voucher_url = ""

            if voucher.file_path:
                normalized_path = voucher.file_path.replace("\\", "/")

                if "/uploads/" in normalized_path:
                    relative_path = normalized_path.split("/uploads/", 1)[1]
                    voucher_url = "/uploads/" + relative_path
                elif normalized_path.startswith("uploads/"):
                    voucher_url = "/" + normalized_path
                else:
                    voucher_url = "/uploads/" + os.path.basename(normalized_path)
            elif voucher.filename:
                voucher_url = "/uploads/vouchers/" + voucher.filename
            
            service_rate = uploader.service_rate if uploader else 0
            upstream_cost_rate = uploader.upstream_cost_rate if uploader else 0
            points_amount = record.points_amount or 0

            receivable_fee = points_amount * service_rate / 100
            payable_cost = points_amount * upstream_cost_rate / 100
            gross_profit = receivable_fee - payable_cost

            review_items.append(
                {
                    "review": review,
                    "voucher": voucher,
                    "record": record,
                    "voucher_url": voucher_url,
                    "uploader_username": uploader.username if uploader else "未知上传方",
                    "service_rate": service_rate,
                    "upstream_cost_rate": upstream_cost_rate,
                    "receivable_fee": round(receivable_fee, 2),
                    "payable_cost": round(payable_cost, 2),
                    "gross_profit": round(gross_profit, 2),
                }
            )

    db.close()

    return templates.TemplateResponse(
        "match_reviews.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "reviews": review_items,
            "status_filter": status_filter,
        },
    )

@app.post("/match-reviews/{review_id}/approve")
def approve_match_review(review_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = SessionLocal()
    review = db.query(MatchReview).filter(MatchReview.id == review_id).first()

    if review:
        review.review_status = "已通过"
        db.commit()

    db.close()

    return RedirectResponse(url="/match-reviews", status_code=302)


@app.post("/match-reviews/{review_id}/reject")
def reject_match_review(review_id: int, request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    db = SessionLocal()
    review = db.query(MatchReview).filter(MatchReview.id == review_id).first()

    if review:
        review.review_status = "已驳回"
        db.commit()

    db.close()

    return RedirectResponse(url="/match-reviews", status_code=302)

@app.post("/match-reviews/batch-review")
def batch_review_match_reviews(
    request: Request,
    review_ids: list[int] = Form([]),
    action: str = Form(...),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    if action not in ["approve", "reject"]:
        return RedirectResponse(url="/match-reviews", status_code=302)

    new_status = "已通过" if action == "approve" else "已驳回"

    db = SessionLocal()

    if review_ids:
        db.query(MatchReview).filter(MatchReview.id.in_(review_ids)).update(
            {MatchReview.review_status: new_status},
            synchronize_session=False,
        )
        db.commit()

    db.close()

    return RedirectResponse(url="/match-reviews", status_code=302)

@app.get("/upload-batches", response_class=HTMLResponse)
def upload_batches_page(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    db = SessionLocal()

    query = db.query(UploadBatch)

    # 权限隔离：
    # 管理员可以查看全部上传记录
    # 上传方只能查看自己的上传记录
    if user.role != "admin":
        query = query.filter(UploadBatch.user_id == user.id)

    batches = query.order_by(UploadBatch.id.desc()).all()

    batch_items = []

    for batch in batches:
        uploader = db.query(User).filter(User.id == batch.user_id).first()

        batch_items.append(
            {
                "id": batch.id,
                "username": uploader.username if uploader else "未知用户",
                "filename": batch.filename,
                "total_rows": batch.total_rows,
                "success_rows": batch.success_rows,
                "failed_rows": batch.failed_rows,
                "created_at": batch.created_at,
            }
        )

    db.close()

    return templates.TemplateResponse(
        "upload_batches.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "batches": batch_items,
        },
    )

@app.get("/stats-dashboard", response_class=HTMLResponse)
def stats_dashboard_page(
    request: Request,
    partner_id: int = Query(0),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    stats = build_stats_data(
        partner_id=partner_id,
        start_date=start_date,
        end_date=end_date,
    )

    return templates.TemplateResponse(
        "stats_dashboard.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            **stats,
        },
    )

@app.get("/stats-dashboard/export")
def export_stats_dashboard(
    request: Request,
    partner_id: int = Query(0),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "admin":
        return RedirectResponse(url="/dashboard", status_code=302)

    stats = build_stats_data(
        partner_id=partner_id,
        start_date=start_date,
        end_date=end_date,
    )

    os.makedirs("exports", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    export_path = os.path.join("exports", f"settlement_summary_{timestamp}.xlsx")

    selected_partner_name = "全部上传方"

    if partner_id != 0:
        for partner in stats["partners"]:
            if partner.id == partner_id:
                selected_partner_name = partner.username
                break

    export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_rows = [
        {"指标": "报表名称", "数值": "结算汇总报表"},
        {"指标": "导出时间", "数值": export_time},
        {"指标": "上传方范围", "数值": selected_partner_name},
        {"指标": "开始日期", "数值": start_date if start_date else "全部"},
        {"指标": "结束日期", "数值": end_date if end_date else "全部"},
        {"指标": "", "数值": ""},
        {"指标": "业务数据总条数", "数值": stats["total_records"]},
        {"指标": "上传批次数", "数值": stats["total_batches"]},
        {"指标": "上传方账号数", "数值": stats["total_partners"]},
        {"指标": "待审核数量", "数值": stats["pending_reviews"]},
        {"指标": "已通过数量", "数值": stats["approved_reviews"]},
        {"指标": "已驳回数量", "数值": stats["rejected_reviews"]},
        {"指标": "总积分金额", "数值": stats["total_points"]},
        {"指标": "应收服务费合计", "数值": stats["total_receivable_fee"]},
        {"指标": "应付成本费合计", "数值": stats["total_payable_cost"]},
        {"指标": "毛利合计", "数值": stats["total_gross_profit"]},
        {"指标": "", "数值": ""},
        {"指标": "已通过结算条数", "数值": stats["approved_settlement_count"]},
        {"指标": "已通过结算金额", "数值": stats["approved_settlement_points"]},
        {"指标": "已通过应收服务费", "数值": stats["approved_settlement_receivable_fee"]},
        {"指标": "已通过应付成本费", "数值": stats["approved_settlement_payable_cost"]},
        {"指标": "已通过毛利", "数值": stats["approved_settlement_gross_profit"]},       
    ]

    detail_df = pd.DataFrame(stats["rows"])

    partner_summary_rows = []

    if not detail_df.empty:
        grouped = detail_df.groupby("上传方")

        for partner_name, group in grouped:
            total_points = group["积分金额"].sum()

            service_rate = group["下游服务费率"].iloc[0]
            upstream_cost_rate = group["上游成本费率"].iloc[0]

            total_receivable_fee = total_points * service_rate / 100
            total_payable_cost = total_points * upstream_cost_rate / 100
            total_gross_profit = total_receivable_fee - total_payable_cost
            approved_group = group[group["审核状态"] == "已通过"]

            approved_points = approved_group["积分金额"].sum()
            approved_receivable_fee = approved_points * service_rate / 100
            approved_payable_cost = approved_points * upstream_cost_rate / 100
            approved_gross_profit = approved_receivable_fee - approved_payable_cost

            partner_summary_rows.append(
                {
                    "上传方": partner_name,
                    "业务条数": len(group),
                    "总积分金额": round(total_points, 2),
                    "下游服务费率": service_rate,
                    "应收服务费合计": round(total_receivable_fee, 2),
                    "上游成本费率": upstream_cost_rate,
                    "应付成本费合计": round(total_payable_cost, 2),
                    "毛利合计": round(total_gross_profit, 2),
                    "已通过结算条数": len(approved_group),
                    "已通过结算金额": round(approved_points, 2),
                    "已通过应收服务费": round(approved_receivable_fee, 2),
                    "已通过应付成本费": round(approved_payable_cost, 2),
                    "已通过毛利": round(approved_gross_profit, 2),
                }
            )

    with pd.ExcelWriter(export_path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="汇总", index=False)
        pd.DataFrame(partner_summary_rows).to_excel(writer, sheet_name="上传方汇总", index=False)
        detail_df.to_excel(writer, sheet_name="明细", index=False)
        
        format_excel_file(writer)

    return FileResponse(
        export_path,
        filename=f"结算汇总_{timestamp}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

@app.get("/my-settlement", response_class=HTMLResponse)
def my_settlement_page(
    request: Request,
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "partner":
        return RedirectResponse(url="/dashboard", status_code=302)

    stats = build_stats_data(
        partner_id=user.id,
        start_date=start_date,
        end_date=end_date,
    )

    return templates.TemplateResponse(
        "my_settlement.html",
        {
            "request": request,
            "username": user.username,
            "role": user.role,
            "start_date": start_date,
            "end_date": end_date,
            "total_records": stats["total_records"],
            "total_points": stats["total_points"],
            "approved_settlement_count": stats["approved_settlement_count"],
            "approved_settlement_points": stats["approved_settlement_points"],
            "settlement_service_fee": stats["approved_settlement_receivable_fee"],
        },
    )

@app.get("/my-settlement/export")
def export_my_settlement(
    request: Request,
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = get_current_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    if user.role != "partner":
        return RedirectResponse(url="/dashboard", status_code=302)

    stats = build_stats_data(
        partner_id=user.id,
        start_date=start_date,
        end_date=end_date,
    )

    os.makedirs("exports", exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    export_path = os.path.join("exports", f"my_settlement_{user.id}_{timestamp}.xlsx")

    export_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    summary_rows = [
        {"指标": "报表名称", "数值": "我的结算报表"},
        {"指标": "导出时间", "数值": export_time},
        {"指标": "上传方", "数值": user.username},
        {"指标": "开始日期", "数值": start_date if start_date else "全部"},
        {"指标": "结束日期", "数值": end_date if end_date else "全部"},
        {"指标": "", "数值": ""},
        {"指标": "业务数据总条数", "数值": stats["total_records"]},
        {"指标": "总积分金额", "数值": stats["total_points"]},
        {"指标": "已通过结算条数", "数值": stats["approved_settlement_count"]},
        {"指标": "已通过结算金额", "数值": stats["approved_settlement_points"]},
        {"指标": "结算服务费合计", "数值": stats["approved_settlement_receivable_fee"]},
    ]

    internal_columns = [
        "姓名",
        "手机号",
        "车牌号",
        "积分金额",
        "银行卡号",
        "下游服务费率",
        "应收服务费",
        "导入时间",
    ]

    customer_column_names = {
        "下游服务费率": "结算费率",
        "应收服务费": "结算服务费",
    }

    detail_df = pd.DataFrame(stats["rows"])

    if detail_df.empty:
        detail_df = pd.DataFrame(
            columns=[
                "姓名",
                "手机号",
                "车牌号",
                "积分金额",
                "银行卡号",
                "结算费率",
                "结算服务费",
                "导入时间",
            ]
        )
    else:
        detail_df = detail_df[internal_columns]
        detail_df = detail_df.rename(columns=customer_column_names)

    with pd.ExcelWriter(export_path, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="汇总", index=False)
        detail_df.to_excel(writer, sheet_name="明细", index=False)

        format_excel_file(writer)

    return FileResponse(
        export_path,
        filename=f"我的结算报表_{timestamp}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("user_id")
    return response


@app.get("/health")
def health():
    return {"status": "ok"}