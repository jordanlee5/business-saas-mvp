from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import os
from datetime import datetime

from .database import engine, Base, SessionLocal
from . import models
from .models import User, BusinessRecord, UploadBatch
from .auth import verify_password, get_password_hash
from .excel_service import parse_business_excel

app = FastAPI(title="业务数据管理SaaS MVP")

# 创建数据库表
Base.metadata.create_all(bind=engine)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

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
    return templates.TemplateResponse(
        "query_record.html",
        {"request": request, "records": None}
    )


@app.post("/query-record", response_class=HTMLResponse)
def query_record(request: Request, keyword: str = Form(...)):
    db = SessionLocal()
    records = db.query(models.BusinessRecord).filter(
        (models.BusinessRecord.phone == keyword) |
        (models.BusinessRecord.name == keyword) |
        (models.BusinessRecord.plate_number == keyword)
    ).all()
    db.close()
    return templates.TemplateResponse(
        "query_record.html",
        {"request": request, "records": records, "keyword": keyword}
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

    new_partner = User(
        username=username,
        password_hash=get_password_hash(password),
        role="partner",
        service_rate=service_rate,
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
            "message": f"上传方账号 {username} 创建成功，服务费率为 {service_rate}%",
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


@app.get("/logout")
def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("user_id")
    return response


@app.get("/health")
def health():
    return {"status": "ok"}