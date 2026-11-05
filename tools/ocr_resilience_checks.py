"""Virtual-only HTTP fault checks invoked by the mixed-load runner."""
import asyncio
import json
import os
import subprocess
import sys


async def client_check(phase, state):
    import httpx
    from tools.mixed_load_client import voucher, ADDRESS, PREFIX
    if os.environ.get("MIXED_LOAD_DISPOSABLE") != "fresh-docker-only":
        raise RuntimeError("Disposable checks only")
    payload=voucher("OCR RECOVERY VIRTUAL RECEIPT")
    if phase=="worker":
        async with httpx.AsyncClient(timeout=40,trust_env=False) as c:
            async def recognize():
                return await c.post("http://ocr:8001/recognize",content=payload,
                                    headers={"Content-Type":"application/octet-stream"})
            responses=await asyncio.gather(recognize(),recognize())
            assert sorted(r.status_code for r in responses)==[200,429]
            assert next(r for r in responses if r.status_code==200).json()["text"].strip()
            assert (await c.get("http://ocr:8001/health")).status_code==200
            bad=await c.post("http://ocr:8001/recognize",content=b"invalid image",
                             headers={"Content-Type":"application/octet-stream"})
            assert bad.status_code==422
        return {"concurrent":[200,429],"invalid_image":422,"health":200}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000",timeout=45,trust_env=False) as c:
        assert (await c.post("/login",data={"username":"load-operator","password":state["password"]})).status_code==302
        result=await c.post("/upload-voucher",data={"partner_id":state["partner_id"],"selected_business_batch_id":0},
                            files={"files":("recovery.png",payload,"image/png")})
        assert result.status_code==200
        if phase=="down":
            assert "识别服务暂不可用" in result.text
        else:
            assert "处理失败" not in result.text
        member={"Authorization":"Bearer "+state["tokens"][0]}
        assert (await c.get(PREFIX+"/products")).status_code==200
        assert (await c.get(PREFIX+"/points/summary",headers=member)).status_code==200
        if phase=="down":
            # Actual business write and cancellation while OCR is stopped.
            address=await c.post(PREFIX+"/addresses",headers=member,json=ADDRESS)
            assert address.status_code==201
            lines=[dict(product_public_id="PRD-LOAD-0",sku_code="LOAD-0-0",quantity=1,expected_points_price="8.00")]
            notice=await c.post(PREFIX+"/orders/fulfillment-preview",headers=member,json={"lines":lines})
            assert notice.status_code==200
            body=dict(address_public_id=address.json()["address_public_id"],expected_address=ADDRESS,lines=lines,
                      fulfillment_digest=notice.json()["digest"],request_id="a"*32)
            created=await c.post(PREFIX+"/orders",headers=member,json=body)
            assert created.status_code==201
            order=created.json()["order_public_id"]
            other={"Authorization":"Bearer "+state["tokens"][1]}
            assert (await c.get(PREFIX+"/orders/"+order,headers=other)).status_code==404
            assert (await c.post(PREFIX+"/orders/"+order+"/cancel",headers=member)).status_code==200
        assert (await c.get("/recognize")).status_code==404
    return {"phase":phase,"upload_http":200,"catalog_and_points":200,
            "checkout_and_cancel_during_outage":phase=="down","public_ocr_route":404}


def exercise(project,state,log):
    _,info=project.container("ocr")
    assert not info["NetworkSettings"]["Ports"]
    assert len(info["NetworkSettings"]["Networks"])==1
    assert next(iter(info["NetworkSettings"]["Networks"]))==project.name+"_recognition"
    assert all(m["Type"]=="tmpfs" and m["Destination"]=="/tmp" for m in info["Mounts"])
    assert info["HostConfig"]["ReadonlyRootfs"] and info["Config"]["User"]=="10001:10001"
    assert not any("PASSWORD" in item or "SECRET" in item or "DATABASE_URL" in item for item in info["Config"]["Env"])
    result={"restricted_worker":True}
    def run(phase):
        response=project.run("exec","-T","-e","MIXED_LOAD_DISPOSABLE=fresh-docker-only",
            "app","python","-m","tools.ocr_resilience_checks",phase,
            input=(json.dumps(state)+"\n").encode())
        log.write(response.stderr);log.flush()
        return json.loads(response.stdout.decode().strip().splitlines()[-1])
    result["worker"]=run("worker")
    before=int(project.sql("select count(*) from voucher_records"))
    project.run("stop","ocr")
    result["down"]=run("down")
    assert int(project.sql("select count(*) from voucher_records"))==before
    assert project.sql("select sum(failed_files) from voucher_upload_batches")=="1"
    project.run("up","-d","--no-build","--wait","--wait-timeout","120","ocr")
    result["recovered"]=run("recovered")
    assert int(project.sql("select count(*) from voucher_records"))==before+1
    result["duplicate"]=run("duplicate")
    assert int(project.sql("select count(*) from voucher_records"))==before+1
    assert project.sql("select sum(duplicate_files) from voucher_upload_batches")=="1"
    assert project.sql("select count(*) from orders where status<>'CANCELLED'")=="0"
    assert project.sql("select count(*) from points_accounts where available_points<>100 or reserved_points<>0")=="0"
    assert project.sql("select count(*) from inventory_balances where reserved_quantity<>0")=="0"
    result["final_vouchers"]=before+1
    result["failed_batches"]=1
    result["duplicate_uploads"]=1
    result["orders"]=int(project.sql("select count(*) from orders"))
    result["outage_and_recovery_balances_correct"]=True
    return result


if __name__=="__main__":
    if not __debug__:raise RuntimeError("Assertions required")
    print(json.dumps(asyncio.run(client_check(sys.argv[1],json.loads(sys.stdin.readline())))))
