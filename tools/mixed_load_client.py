"""Independent HTTP load process. Receives synthetic credentials over stdin."""
import asyncio
from collections import Counter, defaultdict
import hashlib
import io
import json
import math
import os
import secrets
import sys
import time

import httpx
from PIL import Image, ImageDraw, ImageFont

PREFIX="/api/miniprogram/v1"
ADDRESS=dict(recipient_name="虚拟收件人",phone="13800000001",province="测试省",
             city="测试市",district="测试区",detail="混合负载隔离地址")


def summarize(rows, elapsed):
    values=sorted(r[1] for r in rows)
    return dict(requests=len(rows),statuses=dict(Counter(str(r[0]) for r in rows)),
        p50_ms=round(values[math.ceil(len(values)*.50)-1],2),
        p95_ms=round(values[math.ceil(len(values)*.95)-1],2),
        max_ms=round(values[-1],2),requests_per_second=round(len(rows)/elapsed,2),
        response_bytes=sum(r[2] for r in rows),
        first_start_offset_s=round(min(r[3] for r in rows),3),
        last_end_offset_s=round(max(r[4] for r in rows),3))


def voucher(label):
    im=Image.new("RGB",(1000,700),"white")
    draw=ImageDraw.Draw(im)
    font=ImageFont.load_default(size=34)
    for y,line in enumerate(["SYNTHETIC TRANSFER RECEIPT","TEST ONLY - NO PAYMENT",
                             "Amount: 100.00","Reference: "+label,"Account: 1234567890"]):
        draw.text((45,65+y*100),line,fill="black",font=font)
    result=io.BytesIO();im.save(result,format="PNG")
    return result.getvalue()


async def run(state, seconds):
    if os.environ.get("MIXED_LOAD_DISPOSABLE")!="fresh-docker-only":
        raise RuntimeError("Use the disposable runner.")
    # Host name is fixed by our isolated Compose project, no arbitrary destination.
    origin="http://app:8000"
    # Recycle idle client sockets before Uvicorn's default 5s keep-alive cutoff.
    limits=httpx.Limits(max_connections=160,max_keepalive_connections=140,keepalive_expiry=2)
    async with httpx.AsyncClient(base_url=origin,trust_env=False,timeout=60,limits=limits) as client:
        login=await client.post("/login",data=dict(username="load-operator",password=state["password"]))
        assert login.status_code==302 and login.headers["location"]=="/dashboard"
        admin={"Cookie":"; ".join(k+"="+v for k,v in client.cookies.items())}
        client.cookies.clear()
        tokens=[{"Authorization":"Bearer "+token} for token in state["tokens"]]
        bodies=[]
        lines=[dict(product_public_id="PRD-LOAD-0",sku_code="LOAD-0-0",quantity=1,expected_points_price="8.00")]
        for headers in tokens:
            address=await client.post(PREFIX+"/addresses",headers=headers,json=ADDRESS)
            assert address.status_code==201
            notice=await client.post(PREFIX+"/orders/fulfillment-preview",headers=headers,json={"lines":lines})
            assert notice.status_code==200 and notice.json()["ready"]
            bodies.append(dict(address_public_id=address.json()["address_public_id"],expected_address=ADDRESS,
                               lines=lines,fulfillment_digest=notice.json()["digest"]))
        im=await client.get("/uploads/mall_products/mixed/main.jpg")
        assert im.status_code==200
        image_hash=hashlib.sha256(im.content).hexdigest()
        history=[]
        for rep in range(2):
            for mixed in (False,True):
                records=defaultdict(list)
                begin=time.perf_counter()
                deadline=begin+(seconds if mixed else max(15,seconds//2))
                async def request(label,method,path,expected=(200,),**kwargs):
                    start=time.perf_counter()
                    try:
                        response=await client.request(method,path,**kwargs)
                    except httpx.HTTPError as exc:
                        records[label].append((0,(time.perf_counter()-start)*1000,0,start-begin,time.perf_counter()-begin))
                        raise RuntimeError("Load HTTP transport failed: "+type(exc).__name__) from None
                    records[label].append((response.status_code,(time.perf_counter()-start)*1000,len(response.content),start-begin,time.perf_counter()-begin))
                    assert response.status_code in expected,(label,response.status_code)
                    return response
                async def browser(index):
                    n=index
                    while time.perf_counter()<deadline:
                        if mixed and n%5==0:
                            r=await request("image","GET","/uploads/mall_products/mixed/PRD-LOAD-"+str(n%400)+".jpg")
                            assert hashlib.sha256(r.content).hexdigest()==image_hash
                        elif mixed and n%5==1:
                            r=await request("detail","GET",PREFIX+"/products/PRD-LOAD-"+str(n%400))
                            assert len(r.json()["skus"])==10
                        else:
                            r=await request("catalog","GET",PREFIX+"/products?page="+str(n%20+1))
                            assert r.json()["total"]==400
                        n+=1
                async def operations():
                    n=0
                    while time.perf_counter()<deadline:
                        await request("admin_read","GET","/mall-catalog?page="+str(n%20+1),headers=admin)
                        slug="mixed-"+secrets.token_hex(10)
                        r=await request("admin_write","POST","/mall-catalog/categories/create",
                            expected=(303,),headers=admin,data=dict(name=slug,slug=slug,is_active="true"))
                        assert "error=" not in r.headers["location"]
                        n+=1
                        await asyncio.sleep(2)
                async def ocr():
                    # Two real OCR uploads per phase, serialized operational workload.
                    for n in range(2):
                        label=str(rep)+"-"+str(n)+"-"+secrets.token_hex(5)
                        payload=voucher(label)
                        await request("ocr_upload","POST","/upload-voucher",headers=admin,
                            data={"partner_id":str(state["partner_id"]),"selected_business_batch_id":"0"},
                            files={"files":(label+".png",payload,"image/png")})
                        await asyncio.sleep(5)
                async def activate():
                    await asyncio.sleep(2)
                    async def one(i):
                        number,code=state["credentials"][i]
                        r=await request("activation","POST",PREFIX+"/activation/redeem",json=dict(
                            business_public_no=number,phone="13800000001",plate_number="京A00001",
                            activation_code=code,wechat_code="mixed-"+str(i)+"_"+secrets.token_hex(6)))
                        login=await request("member_login","POST",PREFIX+"/auth/wechat-session",
                            json={"code":"mixed-"+str(i)+"_"+secrets.token_hex(6)})
                        assert login.json().get("token")
                    await asyncio.gather(*(one(i) for i in range(24+rep*24,48+rep*24)))
                async def checkout():
                    await asyncio.sleep(5)
                    submissions=[{**body,"request_id":secrets.token_hex(16)} for body in bodies]
                    responses=await asyncio.gather(*(request("checkout","POST",PREFIX+"/orders",
                        expected=(200,201,409),headers=h,json=b) for h,b in zip(tokens,submissions)))
                    winners=[(i,r.json()["order_public_id"]) for i,r in enumerate(responses) if r.status_code in (200,201)]
                    assert len(winners)==12
                    async def replay(i,order):
                        for _ in range(2):
                            r=await request("checkout_replay","POST",PREFIX+"/orders",headers=tokens[i],json=submissions[i])
                            assert r.json()["order_public_id"]==order
                        await request("cross_member","GET",PREFIX+"/orders/"+order,
                                      expected=(404,),headers=tokens[(i+1)%24])
                        for _ in range(2):
                            r=await request("cancel","POST",PREFIX+"/orders/"+order+"/cancel",headers=tokens[i])
                            assert r.json()["status"]=="CANCELLED"
                    await asyncio.gather(*(replay(i,o) for i,o in winners))
                tasks=[browser(i) for i in range(100)]
                if mixed:tasks += [operations(),ocr(),activate(),checkout()]
                running=[asyncio.create_task(task) for task in tasks]
                try:
                    await asyncio.gather(*running)
                finally:
                    for task in running:
                        if not task.done():task.cancel()
                    await asyncio.gather(*running,return_exceptions=True)
                elapsed=time.perf_counter()-begin
                row=dict(repetition=rep+1,scenario="mixed" if mixed else "browse",
                    browser_concurrency=100,seconds=round(elapsed,3),
                    metrics={k:summarize(v,elapsed) for k,v in records.items()})
                history.append(row)
                print(json.dumps(row),file=sys.stderr,flush=True)
        assert (await client.get("/logout",headers=admin)).status_code==302
        assert (await client.get("/mall-catalog",headers=admin)).status_code==302
        return dict(result="PASS",phases=history,image_sha256=image_hash,logout_revoked=True,
                    model="100 closed-loop browsing clients plus parallel operational tasks; no artificial browser think time",
                    measured_latency="request start to complete body; includes httpx pool wait, excludes next closed-loop iteration")


if __name__=="__main__":
    if not __debug__:raise RuntimeError("Assertions required.")
    state=json.loads(sys.stdin.readline())
    print(json.dumps(asyncio.run(run(state,int(sys.argv[1]))),ensure_ascii=False))
