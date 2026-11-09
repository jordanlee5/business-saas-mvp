"""Filtered admin list exports; bounded, consistent reads and audited downloads."""
from datetime import datetime
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE

from ..admin_permissions import can_perform_mall_audit_action
from ..models import AdminActionLog, User
from ..time_utils import format_utc8, utc8_now
from .audit import MallAuditActionType as Action
from .catalog_admin_service import catalog_listing, PRODUCT_STATUS_LABELS
from .member_points_service import list_member_points
from .order_reporting_service import list_mall_orders

MAX_ROWS = 10000
ACTIONS = {"catalog": Action.CATALOG_EXPORT, "orders": Action.ORDER_EXPORT,
           "points": Action.MEMBER_POINTS_LIST_EXPORT}
TITLES = {"catalog": "商品目录", "orders": "商城订单", "points": "会员积分"}


def build_list_export(db, *, actor_id, kind, keyword="", status="ALL"):
    # Authorization is checked again here so direct callers cannot bypass it.
    actor = db.get(User, actor_id)
    if kind not in ACTIONS or not actor or not actor.is_active or not can_perform_mall_audit_action(actor, ACTIONS[kind]):
        raise PermissionError("当前账号无权导出")
    when = utc8_now()
    if kind == "catalog":
        keyword = keyword.strip()[:160]
        fetch = lambda page: catalog_listing(db, keyword, page)
        headers = ["商品名称", "SKU 编码", "规格名称", "供应商", "可售库存", "成本（元）", "积分售价", "商品状态", "SKU 状态"]
        values = lambda x: [x.product_name, x.sku_code, x.sku_name, x.supplier_name,
            x.available if x.sku_id else None, x.cost_price, x.points_price,
            PRODUCT_STATUS_LABELS.get(x.status, x.status), ("启用" if x.is_active else "停用") if x.sku_id else "待建 SKU"]
    elif kind == "orders":
        fetch = lambda page: list_mall_orders(db, keyword=keyword, status=status, page=page, page_size=100)
        headers = ["订单编号", "会员编号", "状态", "积分合计", "商品成本（元）", "数量", "创建时间（北京时间）"]
        values = lambda x: [x.order_public_id, x.member_public_id, x.status_label, x.total_points,
                             x.total_cost_amount, x.total_quantity, x.created_at]
    else:
        fetch = lambda page: list_member_points(db, keyword=keyword, member_status=status, page=page, page_size=50, now=when)
        headers = ["会员公开编号", "状态", "可用积分", "预占积分", "积分批次", "流水", "最近到期（北京时间）", "账本一致性"]
        values = lambda x: [x.member_public_id, "启用" if x.is_active else "停用", x.available_points,
            x.reserved_points, x.grant_count, x.ledger_count, x.nearest_expires_at, "一致" if x.is_consistent else "异常"]
    first = fetch(1)
    total = first["total"] if isinstance(first, dict) else first.total
    if total > MAX_ROWS:
        raise ValueError(f"匹配记录超过 {MAX_ROWS} 条，请缩小筛选范围后导出")
    pages = first["pages"] if isinstance(first, dict) else first.total_pages
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = TITLES[kind]
    sheet.append(headers)
    count = 0
    for page in range(1, pages + 1):
        result = first if page == 1 else fetch(page)
        for item in (result["rows"] if isinstance(result, dict) else result.items):
            count += 1
            if count > MAX_ROWS:
                raise ValueError("匹配记录过多，请缩小筛选范围后导出")
            for col, value in enumerate(values(item), 1):
                if isinstance(value, datetime):
                    value = format_utc8(value)
                if isinstance(value, str):
                    value = ILLEGAL_CHARACTERS_RE.sub("", value)
                cell = sheet.cell(count + 1, col, value)
                # Explicit text type protects formulas, identifiers and leading zeroes.
                if isinstance(value, str):
                    cell.data_type = "s"
                elif isinstance(value, Decimal):
                    cell.number_format = "#,##0.00"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2563EB")
        sheet.column_dimensions[cell.column_letter].width = 26
    info = workbook.create_sheet("导出说明")
    info.append(["项目", "内容"])
    for key, value in [("范围", "当前筛选条件下的全部记录，不限当前页"), ("导出条数", count),
                       ("导出时间（北京时间）", format_utc8(when)), ("字段范围", "与列表一致，不含登录凭据、收货地址和客户身份资料")]:
        info.append([key, value])
    info.column_dimensions["A"].width = 28
    info.column_dimensions["B"].width = 65
    output = BytesIO()
    workbook.save(output)
    output.seek(0)
    db.add(AdminActionLog(admin_id=actor.id, action_type=ACTIONS[kind].value, target_type=kind,
        description=f"导出{TITLES[kind]}筛选列表，共 {count} 条；不含客户身份资料", created_at=when))
    db.flush()
    return output
