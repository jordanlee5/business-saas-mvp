"""Record a virtual-only HTTP access matrix; never open a real database.

Run inside the isolated test image. The fixture owns a disposable database and
patches every business Session/identity provider; credentials are never recorded.
"""
import argparse
import copy
import json
import sys
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_member_catalog_access as cases
import test_order_service as order_fixture
from app.api import catalog_access as access
from app.mall.parcel_tracking import TrackingService, tracking_service
from sqlalchemy.exc import OperationalError


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--output",required=True)
    output=Path(parser.parse_args().output).resolve();output.mkdir(parents=True,exist_ok=True)
    case=cases.MemberCatalogAccessTests("runTest")
    matrix=[]
    try:
        case.setUp()
        paths={"categories":cases.PREFIX+"/categories","catalog":cases.PREFIX+"/products?page_size=1",
               "detail":cases.PREFIX+"/products/PRD-ORDER-001","original":case.url,"thumbnail":case.thumb}
        def check(label,kind,path,status,**kwargs):
            response=case.request(path,**kwargs)
            assert response.status_code==status,(label,kind,response.status_code)
            matrix.append(dict(scenario=label,resource=kind,status=response.status_code,
                               cache_control=response.headers.get("cache-control")))
            return response
        for owner,label,status in ((None,"anonymous",401),(1,"not_on_confirmed_list",403),(0,"eligible",200)):
            for kind,path in paths.items(): check(label,kind,path,status,owner=owner)
        for method,headers in (("HEAD",{}),("GET",{"Range":"bytes=0-9"})):
            check("anonymous_direct_image",method,case.thumb,401,method=method,headers=headers,owner=None)
        check("eligible_range","thumbnail",case.thumb,206,headers={"Range":"bytes=0-9"})
        check("copied_url_token_ignored","thumbnail",case.thumb+"?token="+case.case.tokens[0],401,owner=None)
        order=case.case.place();oid=order["order_public_id"]
        case.f.fulfill(oid);case.f.ship(oid)
        parcel=case.case.call("GET","/"+oid).json()["parcels"][0]["parcel_id"]
        service=TrackingService(cases.VirtualProvider())
        from app.main import app
        app.dependency_overrides[tracking_service]=lambda:service
        case.addCleanup(lambda:app.dependency_overrides.pop(tracking_service,None))
        case.change("DISABLE");before=case.balances()
        for kind,path in paths.items(): check("revoked",kind,path,403)
        check("revoked_history","orders",cases.PREFIX+"/orders",200)
        check("revoked_history","order_detail",cases.PREFIX+"/orders/"+oid,200)
        track=cases.PREFIX+"/orders/"+oid+"/parcels/"+str(parcel)+"/tracking"
        check("revoked_history","tracking",track,200)
        check("other_member_history","order_detail",cases.PREFIX+"/orders/"+oid,404,owner=1)
        check("other_member_history","tracking",track,404,owner=1)
        rights=check("revoked_history","rights_submit",cases.PREFIX+"/rights-requests",201,method="POST",
            json=dict(request_key="d"*32,kind="ACCESS",description="虚拟资格停用后的权益申请"))
        rid=rights.json()["public_id"]
        check("revoked_history","rights_result",cases.PREFIX+"/rights-requests/"+rid,200)
        check("other_member_history","rights_result",cases.PREFIX+"/rights-requests/"+rid,404,owner=1)
        check("revoked","order_exact_replay",cases.PREFIX+"/orders",200,method="POST",json=case.case.body)
        body=copy.deepcopy(case.case.body);body["request_id"]="e"*32
        check("revoked","new_checkout",cases.PREFIX+"/orders",403,method="POST",json=body)
        assert case.balances()==before
        case.change("ENABLE")
        case.change("VALIDITY",valid_from=order_fixture.NOW-timedelta(days=1),valid_until=order_fixture.NOW)
        for kind,path in paths.items(): check("expired",kind,path,403)
        check("expired_history","orders",cases.PREFIX+"/orders",200)
        check("expired_history","tracking",track,200)
        check("expired_history","rights_result",cases.PREFIX+"/rights-requests/"+rid,200)
        check("expired","new_checkout",cases.PREFIX+"/orders",403,method="POST",json=body)
        case.change("VALIDITY",valid_from=order_fixture.NOW-timedelta(days=1),valid_until=order_fixture.NOW+timedelta(days=1))
        for kind,path in paths.items(): check("restored",kind,path,200)
        with patch.object(access,"require_qualification",side_effect=OperationalError("virtual-private",{},Exception("virtual-private"))):
            check("database_failure","catalog",paths["catalog"],503)
            check("database_failure","thumbnail",case.thumb,503)
        check("recovered","catalog",paths["catalog"],200)
        result=dict(result="PASS",cases=len(matrix),matrix=matrix,credentials_recorded=False,
            real_database_touched=False,real_wechat_called=False,real_logistics_called=False,
            revoked_new_checkout_balances_unchanged=True,history_remains_owner_only=True,
            scope="real FastAPI handlers over in-process HTTP, disposable virtual data; native miniapp UI is next round")
        (output/"result.json").write_text(json.dumps(result,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
        print(f"PASS: {len(matrix)} virtual HTTP access checks; no real credentials/data/providers")
    finally:
        case.doCleanups()


if __name__=="__main__":main()
