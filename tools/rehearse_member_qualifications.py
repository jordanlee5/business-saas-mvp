"""Render virtual admin acceptance pages through real HTTP handlers.

Run from the repository root, with --output pointing at a private evidence
directory. Uses only an in-memory fixture; does not migrate or connect the
configured business database, call WeChat, or change product access.
"""
import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app import models as m
from app.main import app
from app.mall import qualification_service as q
from test_member_qualifications import seed, activation, NOW, START, END


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    output=Path(parser.parse_args().output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    engine=create_engine("sqlite://", connect_args={"check_same_thread":False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    Session=sessionmaker(bind=engine, expire_on_commit=False)
    try:
        with Session.begin() as db:
            actor,partner,batch,business=seed(db)
            batch_id,business_id=batch.id,business.id
        # Without entering TestClient's lifespan context, all route sessions are
        # patched to the in-memory fixture and no configured DB is opened.
        client=TestClient(app)
        try:
            with patch("app.main.get_current_user",return_value=actor), patch("app.main.SessionLocal",Session), patch.object(q,"utc8_now",return_value=NOW):
                def save(name, url):
                    response=client.get(url)
                    assert response.status_code==200
                    assert response.headers["cache-control"]=="no-store, private"
                    assert "13800000001" not in response.text
                    (output/(name+".html")).write_text(response.text,encoding="utf-8")
                save("01-list-before",f"/mall-qualifications?batch_id={batch_id}")
                token=client.cookies.get("mall_qualifications_csrf")
                form=dict(batch_id=batch_id,business_ids=[business_id],valid_from=START.strftime("%Y-%m-%dT%H:%M"),valid_until=END.strftime("%Y-%m-%dT%H:%M"),reason="虚拟甲方确认本期名单",csrf_token=token)
                response=client.post("/mall-qualifications/confirm",data=form)
                assert "新增确认 1 条" in response.text
                with Session() as db:
                    qualification_id=db.query(m.MemberQualification).one().id
                url=f"/mall-qualifications?qualification_id={qualification_id}"
                save("02-confirmed-unbound",url)
                with Session.begin() as db:
                    assert activation(db,db.get(m.BusinessRecord,business_id)).success
                save("03-bound-detail",url)
                with Session() as db:
                    row=db.get(m.MemberQualification,qualification_id)
                    version,member_id=row.version,row.member_id
                    assert q.has_effective_qualification(db,member_id,now=NOW)
                response=client.post(f"/mall-qualifications/{qualification_id}/update",data=dict(version=version,action="DISABLE",reason="虚拟甲方通知暂停资格",csrf_token=token))
                assert "已停用" in response.text
                save("04-disabled-detail",url)
                with Session() as db:
                    assert not q.has_effective_qualification(db,member_id,now=NOW)
                    assert db.get(m.Member,member_id).is_active
                    row=db.get(m.MemberQualification,qualification_id)
                    version=row.version
                response=client.post(f"/mall-qualifications/{qualification_id}/update",data=dict(version=version,action="ENABLE",reason="虚拟甲方确认恢复资格",csrf_token=token))
                assert "已更新并记录经办人和时间" in response.text
                save("05-restored-detail",url)
                save("06-list-bound",f"/mall-qualifications?batch_id={batch_id}")
                with Session() as db:
                    assert q.has_effective_qualification(db,member_id,now=NOW)
                    events=db.query(m.MemberQualificationEvent).order_by(m.MemberQualificationEvent.id).all()
                    result={"fixture":"virtual in-memory only", "http_flow":"confirm -> activate -> disable -> enable",
                        "pages":6,"events":[e.action for e in events],"member_remains_active":True,
                        "catalog_access_gated":False,"real_wechat_called":False,"real_database_touched":False}
                    assert result["events"]==["CONFIRM","BIND","DISABLE","ENABLE"]
                    (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
                print("PASS: six virtual HTTP pages; confirm, verified-activation linkage, disable and restore with attributable history")
        finally:
            client.close()
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
