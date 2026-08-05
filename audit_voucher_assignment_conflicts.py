from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from types import SimpleNamespace

from app.match_review_workflow import (
    APPROVED_REVIEW_STATUS,
    build_voucher_assignment_conflict_audit_groups,
)
from audit_match_review_allocations import (
    money,
    open_read_only_database,
    validate_schema,
)


BUSINESS_TOTALS_SQL = """
SELECT
    b.id AS business_id,
    COALESCE(
        NULLIF(TRIM(b.public_business_no), ''),
        NULLIF(TRIM(b.business_no), ''),
        'BR-' || b.id
    ) AS business_no,
    b.points_amount AS business_amount,
    ROUND(
        COALESCE(
            SUM(
                CASE
                    WHEN r.review_status = '已通过'
                     AND r.allocation_amount IS NOT NULL
                    THEN r.allocation_amount
                    ELSE 0
                END
            ),
            0
        ),
        2
    ) AS approved_amount,
    SUM(
        CASE
            WHEN r.review_status = '已通过'
             AND r.allocation_amount IS NULL
            THEN 1
            ELSE 0
        END
    ) AS missing_amount_count
FROM business_records AS b
LEFT JOIN match_reviews AS r
  ON r.business_record_id = b.id
GROUP BY
    b.id,
    b.public_business_no,
    b.business_no,
    b.points_amount
ORDER BY b.id
"""


CONTINUABLE_ALLOCATION_STATES = frozenset(
    {
        "未付款",
        "部分付款",
    }
)


MONEY_QUANTUM = Decimal("0.01")
ZERO_MONEY = Decimal("0.00")


NO_SAFE_CANDIDATE_ACTION = (
    "禁止审核通过，保持冻结并核对凭证或业务数据"
)
UNIQUE_SAFE_CANDIDATE_ACTION = (
    "仅有一个金额安全候选，仍须人工核对匹配要素"
)
MULTIPLE_SAFE_CANDIDATE_ACTION = (
    "存在多个金额安全候选，必须人工确定唯一归属"
)
HISTORICAL_DUPLICATE_ACTION = (
    "暂不在页面操作；人工确认唯一有效归属后再设计修复"
)


MANUAL_DISPOSITION_COLUMNS = (
    "案例编号",
    "案例类型",
    "凭证ID",
    "凭证文件名",
    "凭证金额",
    "审核记录ID",
    "审核状态",
    "本次核销金额",
    "审核记录创建时间",
    "业务ID",
    "公开业务单号",
    "业务金额",
    "已核销金额",
    "剩余金额",
    "业务核销状态",
    "金额安全性",
    "匹配分数",
    "匹配状态",
    "姓名匹配证据",
    "银行卡匹配证据",
    "金额匹配证据",
    "初审人ID",
    "初审结果",
    "初审时间",
    "复核人ID",
    "复核结果",
    "复核时间",
    "处置建议",
    "人工决定",
    "确认人",
    "确认时间",
    "备注",
)


def normalized_money(value):
    if value is None or isinstance(value, bool):
        return None

    try:
        amount = Decimal(str(value)).quantize(
            MONEY_QUANTUM,
            rounding=ROUND_HALF_UP,
        )
    except (InvalidOperation, ValueError):
        return None

    if not amount.is_finite():
        return None

    return amount


def has_valid_positive_amount(value):
    amount = normalized_money(value)

    return amount is not None and amount > ZERO_MONEY


def allocation_state(row):
    if (
        row["business_amount"] is None
        or row["business_amount"] < 0
        or row["approved_amount"] is None
        or row["missing_amount_count"] > 0
    ):
        return "金额异常"

    business_amount = round(row["business_amount"], 2)
    approved_amount = round(row["approved_amount"], 2)

    if approved_amount < 0:
        return "金额异常"

    if approved_amount > business_amount:
        return "超额核销"

    if approved_amount == business_amount:
        return "已结清"

    if approved_amount == 0:
        return "未付款"

    return "部分付款"


def candidate_allocation_state(business):
    if business is None:
        return "金额异常"

    return allocation_state(business)


def continuable_business_ids(group, businesses):
    return tuple(
        business_id
        for business_id in group.pending_business_record_ids
        if candidate_allocation_state(
            businesses.get(business_id)
        )
        in CONTINUABLE_ALLOCATION_STATES
    )


def group_category(group, businesses):
    continuable_ids = continuable_business_ids(
        group,
        businesses,
    )

    if not continuable_ids:
        return "无可继续核销候选"

    if len(continuable_ids) == 1:
        return "仅一个可继续核销候选"

    return "多个可继续核销候选"


def remaining_amount(business):
    if (
        candidate_allocation_state(business)
        not in CONTINUABLE_ALLOCATION_STATES
    ):
        return None

    business_amount = normalized_money(
        business["business_amount"]
    )
    approved_amount = normalized_money(
        business["approved_amount"]
    )

    if business_amount is None or approved_amount is None:
        return None

    remaining = business_amount - approved_amount

    if remaining <= ZERO_MONEY:
        return None

    return remaining


def amount_safe_business_ids(
    group,
    businesses,
    voucher_amount,
):
    normalized_voucher_amount = normalized_money(
        voucher_amount
    )

    if (
        normalized_voucher_amount is None
        or normalized_voucher_amount <= ZERO_MONEY
    ):
        return ()

    safe_ids = []

    for business_id in group.pending_business_record_ids:
        remaining = remaining_amount(
            businesses.get(business_id)
        )

        if (
            remaining is not None
            and normalized_voucher_amount <= remaining
        ):
            safe_ids.append(business_id)

    return tuple(safe_ids)


def amount_safe_group_category(
    group,
    businesses,
    voucher_amount,
):
    safe_ids = amount_safe_business_ids(
        group,
        businesses,
        voucher_amount,
    )

    if not safe_ids:
        return "无金额安全候选"

    if len(safe_ids) == 1:
        return "仅一个金额安全候选"

    return "多个金额安全候选"


def unresolved_group_action(
    group,
    businesses,
    voucher_amount,
):
    safe_ids = amount_safe_business_ids(
        group,
        businesses,
        voucher_amount,
    )

    if not safe_ids:
        return NO_SAFE_CANDIDATE_ACTION

    if len(safe_ids) == 1:
        return UNIQUE_SAFE_CANDIDATE_ACTION

    return MULTIPLE_SAFE_CANDIDATE_ACTION


def checklist_remaining_amount(business):
    if business is None:
        return None

    business_amount = normalized_money(
        business["business_amount"]
    )
    approved_amount = normalized_money(
        business["approved_amount"]
    )

    if (
        business_amount is None
        or approved_amount is None
        or business["missing_amount_count"] > 0
    ):
        return None

    return business_amount - approved_amount


def amount_safety_label(voucher_amount, business):
    normalized_voucher_amount = normalized_money(
        voucher_amount
    )
    remaining = remaining_amount(business)

    if (
        normalized_voucher_amount is not None
        and normalized_voucher_amount > ZERO_MONEY
        and remaining is not None
        and normalized_voucher_amount <= remaining
    ):
        return "金额安全候选"

    return "非金额安全候选"


def build_manual_disposition_row(
    *,
    case_no,
    case_type,
    voucher_id,
    voucher,
    review,
    business,
    action,
):
    voucher_amount = (
        voucher["voucher_amount"]
        if voucher is not None
        else None
    )
    business_id = getattr(
        review,
        "business_record_id",
        None,
    )

    if business_id is None and business is not None:
        business_id = business["business_id"]

    business_amount = (
        normalized_money(business["business_amount"])
        if business is not None
        else None
    )
    approved_amount = (
        normalized_money(business["approved_amount"])
        if business is not None
        else None
    )
    business_no = (
        business["business_no"]
        if business is not None
        else None
    )

    return {
        "案例编号": case_no,
        "案例类型": case_type,
        "凭证ID": voucher_id,
        "凭证文件名": (
            voucher["filename"]
            if voucher is not None
            else None
        ),
        "凭证金额": normalized_money(voucher_amount),
        "审核记录ID": getattr(review, "id", None),
        "审核状态": getattr(
            review,
            "review_status",
            None,
        ),
        "本次核销金额": normalized_money(
            getattr(review, "allocation_amount", None)
        ),
        "审核记录创建时间": getattr(
            review,
            "created_at",
            None,
        ),
        "业务ID": business_id,
        "公开业务单号": business_no,
        "业务金额": business_amount,
        "已核销金额": approved_amount,
        "剩余金额": checklist_remaining_amount(
            business
        ),
        "业务核销状态": candidate_allocation_state(
            business
        ),
        "金额安全性": amount_safety_label(
            voucher_amount,
            business,
        ),
        "匹配分数": getattr(review, "score", None),
        "匹配状态": getattr(
            review,
            "match_status",
            None,
        ),
        "姓名匹配证据": getattr(
            review,
            "name_match",
            None,
        ),
        "银行卡匹配证据": getattr(
            review,
            "bank_match",
            None,
        ),
        "金额匹配证据": getattr(
            review,
            "amount_match",
            None,
        ),
        "初审人ID": getattr(
            review,
            "primary_reviewer_id",
            None,
        ),
        "初审结果": getattr(
            review,
            "primary_review_result",
            None,
        ),
        "初审时间": getattr(
            review,
            "primary_reviewed_at",
            None,
        ),
        "复核人ID": getattr(
            review,
            "secondary_reviewer_id",
            None,
        ),
        "复核结果": getattr(
            review,
            "secondary_review_result",
            None,
        ),
        "复核时间": getattr(
            review,
            "secondary_reviewed_at",
            None,
        ),
        "处置建议": action,
        "人工决定": "",
        "确认人": "",
        "确认时间": "",
        "备注": "",
    }


def main():
    connection = open_read_only_database()

    try:
        validate_schema(connection)
        review_rows = connection.execute(
            """
            SELECT
                id,
                voucher_id,
                business_record_id,
                review_status,
                allocation_amount,
                match_status,
                name_match,
                bank_match,
                amount_match,
                score,
                primary_reviewer_id,
                primary_review_result,
                primary_reviewed_at,
                secondary_reviewer_id,
                secondary_review_result,
                secondary_reviewed_at,
                created_at
            FROM match_reviews
            ORDER BY id
            """
        ).fetchall()
        reviews = [
            SimpleNamespace(**dict(row))
            for row in review_rows
        ]
        reviews_by_id = {
            review.id: review
            for review in reviews
        }
        business_rows = connection.execute(
            BUSINESS_TOTALS_SQL
        ).fetchall()
        businesses = {
            row["business_id"]: row
            for row in business_rows
        }
        voucher_rows = connection.execute(
            """
            SELECT id, filename, voucher_amount
            FROM voucher_records
            ORDER BY id
            """
        ).fetchall()
        vouchers = {
            row["id"]: row
            for row in voucher_rows
        }
        completed_business_ids = frozenset(
            row["business_id"]
            for row in business_rows
            if allocation_state(row) == "已结清"
        )
        all_groups = (
            build_voucher_assignment_conflict_audit_groups(
                reviews,
                completed_business_ids,
            )
        )
        unresolved_groups = tuple(
            group
            for group in all_groups
            if group.unresolved_review_ids
        )
        high_risk_groups = tuple(
            group
            for group in all_groups
            if group.has_multiple_approved_businesses
        )
        categories = Counter(
            group_category(group, businesses)
            for group in unresolved_groups
        )
        amount_safe_categories = Counter(
            amount_safe_group_category(
                group,
                businesses,
                (
                    vouchers[group.voucher_id]["voucher_amount"]
                    if group.voucher_id in vouchers
                    else None
                ),
            )
            for group in unresolved_groups
        )
        invalid_voucher_amount_group_count = sum(
            1
            for group in unresolved_groups
            if not has_valid_positive_amount(
                vouchers[group.voucher_id]["voucher_amount"]
                if group.voucher_id in vouchers
                else None
            )
        )
        candidate_states = Counter(
            candidate_allocation_state(
                businesses.get(business_id)
            )
            for group in unresolved_groups
            for business_id in group.pending_business_record_ids
        )
        distribution = Counter(
            len(group.pending_business_record_ids)
            for group in unresolved_groups
        )

        print("凭证归属冲突只读审计")
        print("数据库模式：只读（mode=ro + query_only）")
        print()
        print("A. 当前未决归属冲突池")
        print(
            "未决候选审核记录数："
            f"{sum(len(group.unresolved_review_ids) for group in unresolved_groups)}"
        )
        print(f"冲突凭证组数：{len(unresolved_groups)}")
        print("每张凭证的候选业务数分布：")

        for business_count in sorted(distribution):
            print(
                f"  {business_count} 个业务："
                f"{distribution[business_count]} 组"
            )

        if not distribution:
            print("  无")

        print()
        print("B. 未决冲突组可继续核销分类")

        for category in (
            "无可继续核销候选",
            "仅一个可继续核销候选",
            "多个可继续核销候选",
        ):
            print(f"{category}：{categories[category]} 组")

        print("候选业务状态（按冲突组内候选项计数）：")

        for state in (
            "未付款",
            "部分付款",
            "已结清",
            "超额核销",
            "金额异常",
        ):
            print(f"  {state}：{candidate_states[state]} 项")

        print(
            "金额安全分类"
            "（凭证金额必须大于 0 且不超过业务剩余金额）："
        )

        for category in (
            "无金额安全候选",
            "仅一个金额安全候选",
            "多个金额安全候选",
        ):
            print(
                f"  {category}："
                f"{amount_safe_categories[category]} 组"
            )

        print(
            "  凭证金额异常或缺失："
            f"{invalid_voucher_amount_group_count} 组"
        )

        print()
        print("C. 历史高风险检查")
        print(
            "同一凭证已在多个业务通过："
            f"{len(high_risk_groups)} 组"
        )
        print()
        print("D. 当前未决冲突组明细")

        if not unresolved_groups:
            print("无")

        for index, group in enumerate(
            unresolved_groups,
            start=1,
        ):
            voucher = vouchers.get(group.voucher_id)
            filename = (
                voucher["filename"]
                if voucher is not None
                else "凭证记录缺失"
            )
            voucher_amount = (
                voucher["voucher_amount"]
                if voucher is not None
                else None
            )
            print()
            print(
                f"[D{index}] 凭证#{group.voucher_id} / "
                f"{filename} / 金额 {money(voucher_amount)}"
            )
            print(
                "  状态分类："
                f"{group_category(group, businesses)}"
            )
            print(
                "  金额安全分类："
                f"{amount_safe_group_category(group, businesses, voucher_amount)}"
            )
            print(
                "  处置建议："
                f"{unresolved_group_action(group, businesses, voucher_amount)}"
            )
            safe_business_ids = set(
                amount_safe_business_ids(
                    group,
                    businesses,
                    voucher_amount,
                )
            )
            print(
                "  未决审核 ID："
                f"{','.join(map(str, group.unresolved_review_ids))}"
            )

            unresolved_reviews_by_business_id = {}

            for review_id in group.unresolved_review_ids:
                review = reviews_by_id.get(review_id)

                if review is None:
                    continue

                unresolved_reviews_by_business_id.setdefault(
                    review.business_record_id,
                    review,
                )

            for business_id in group.pending_business_record_ids:
                business = businesses.get(business_id)

                if business is None:
                    print(f"    - 缺失业务#{business_id}")
                    continue

                remaining = (
                    max(
                        round(business["business_amount"], 2)
                        - round(business["approved_amount"], 2),
                        0,
                    )
                    if business["business_amount"] is not None
                    else None
                )
                print(
                    "    - "
                    f"{business['business_no']} "
                    f"(ID {business_id}) / "
                    f"业务金额 {money(business['business_amount'])} / "
                    f"已核销 {money(business['approved_amount'])} / "
                    f"剩余 {money(remaining)} / "
                    f"{allocation_state(business)} / "
                    + (
                        "金额安全候选"
                        if business_id in safe_business_ids
                        else "非金额安全候选"
                    )
                )
                review = unresolved_reviews_by_business_id.get(
                    business_id
                )

                if review is None:
                    print("      证据：对应未决审核记录缺失")
                    continue

                score_text = (
                    review.score
                    if review.score is not None
                    else "-"
                )
                print(
                    f"      证据：MR#{review.id} / "
                    f"分数 {score_text} / "
                    f"匹配状态 {review.match_status or '-'} / "
                    f"姓名 {review.name_match or '-'} / "
                    f"银行卡 {review.bank_match or '-'} / "
                    f"金额 {review.amount_match or '-'}"
                )
        print()
        print("E. 同一凭证多业务已通过明细")

        if not high_risk_groups:
            print("无")

        for index, group in enumerate(
            high_risk_groups,
            start=1,
        ):
            voucher = vouchers.get(group.voucher_id)
            filename = (
                voucher["filename"]
                if voucher is not None
                else "凭证记录缺失"
            )
            voucher_amount = (
                voucher["voucher_amount"]
                if voucher is not None
                else None
            )

            print()
            print(
                f"[E{index}] 凭证#{group.voucher_id} / "
                f"{filename} / 金额 {money(voucher_amount)}"
            )
            print(
                "  处置建议：暂不在页面操作；"
                "人工确认唯一有效归属后再设计修复"
            )

            for review_id in group.review_ids:
                review = reviews_by_id.get(review_id)

                if (
                    review is None
                    or review.review_status
                    != APPROVED_REVIEW_STATUS
                ):
                    continue

                business = businesses.get(
                    review.business_record_id
                )
                business_no = (
                    business["business_no"]
                    if business is not None
                    else f"缺失业务#{review.business_record_id}"
                )

                print(
                    f"    - MR#{review.id} / "
                    f"{business_no} / "
                    f"核销 {money(review.allocation_amount)} / "
                    f"分数 "
                    f"{review.score if review.score is not None else '-'}"
                )
                print(
                    f"      匹配：{review.match_status or '-'} / "
                    f"姓名 {review.name_match or '-'} / "
                    f"银行卡 {review.bank_match or '-'} / "
                    f"金额 {review.amount_match or '-'}"
                )
                print(
                    "      审核链："
                    f"初审人 {review.primary_reviewer_id or '-'} / "
                    f"结果 {review.primary_review_result or '-'} / "
                    f"时间 {review.primary_reviewed_at or '-'}；"
                    f"复核人 {review.secondary_reviewer_id or '-'} / "
                    f"结果 {review.secondary_review_result or '-'} / "
                    f"时间 {review.secondary_reviewed_at or '-'}"
                )

        quick_check = connection.execute(
            "PRAGMA quick_check"
        ).fetchone()[0]

        if quick_check != "ok":
            raise RuntimeError(
                f"数据库完整性检查失败：{quick_check}"
            )

        if connection.total_changes != 0:
            raise RuntimeError(
                "审计期间检测到数据库写入"
            )

        print()
        print("F. 只读保护与完整性检查")
        print("PRAGMA quick_check：ok")
        print("数据库写入次数：0")
        print("审计完成：未修改任何数据库记录")
    finally:
        connection.close()


if __name__ == "__main__":
    main()