"""Linux Docker only: create disposable PostgreSQL projects and compare 1/2 workers."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import threading
import time

from tools.staging_pg_backup import StagingProject, prepare_secrets

ROOT=Path(__file__).resolve().parents[1]


class DockerSocket(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost",timeout=10)
    def connect(self):
        self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        self.sock.settimeout(10)
        self.sock.connect("/var/run/docker.sock")


def docker_json(path):
    connection=DockerSocket()
    try:
        connection.request("GET",path)
        response=connection.getresponse()
        if response.status!=200:raise RuntimeError("Docker metrics unavailable.")
        return json.loads(response.read())
    finally:connection.close()


def run_case(workers,seconds,image,out,server_cpus=4,ocr_mode="inline",verify_ocr=False):
    project="saas-pg-test-mixed-"+secrets.token_hex(5)
    private=Path(tempfile.mkdtemp(prefix=project+"-"))
    prepare_secrets(private/"secrets")
    os.environ.update(STAGING_SECRET_DIR=str(private/"secrets"),
        STAGING_APP_IMAGE=image,STAGING_WEB_WORKERS=str(workers),STAGING_OCR_MODE=ocr_mode,WECHAT_MINIPROGRAM_APP_ID="wx0123456789abcdef")
    app_cpus=server_cpus*3//4-(1 if ocr_mode=="isolated" else 0)
    p=StagingProject(project)
    # Only resources created under this random project are ever removed.
    assert not p.run("ps","-a","-q").stdout.strip()
    override=private/"override.json"
    override.write_text(json.dumps(dict(services={
        "database":dict(cpus=server_cpus//4,mem_limit="1g",command=["postgres","-c","track_io_timing=on"]),
        "app":dict(cpus=app_cpus,mem_limit="4g",environment={"MIXED_LOAD_DISPOSABLE":"fresh-docker-only"},
            volumes=[str(ROOT/"tools")+":/app/tools:ro"],
            command=["python","-m","tools.mixed_load_fixture","serve"]),
    })))
    p.command += ["-f",str(override)]
    log=(out/("workers-"+str(workers)+".log")).open("wb")
    stopped=threading.Event()
    samples=[]
    errors=[]
    def command(*args):
        result=subprocess.run([*p.command,*args],capture_output=True)
        log.write(result.stderr);log.flush()
        if result.returncode:raise RuntimeError("Dedicated Compose operation failed; inspect local log.")
        return result.stdout
    def last_json(data):return json.loads(data.decode().strip().splitlines()[-1])
    def monitor(ids):
        previous={}
        i=0
        while not stopped.is_set():
            row={"at":time.time(),"containers":{}}
            try:
                for name,cid in ids.items():
                    data=docker_json("/containers/"+cid+"/stats?stream=false&one-shot=true")
                    cpu=data["cpu_stats"]["cpu_usage"]["total_usage"]
                    now=time.monotonic()
                    old=previous.get(name)
                    mem=data["memory_stats"]
                    usage=mem.get("usage",0)
                    inactive=mem.get("stats",{}).get("inactive_file",0)
                    row["containers"][name]=dict(cpu_cores=(cpu-old[0])/1e9/(now-old[1]) if old else 0,
                        memory_bytes=usage,working_set_bytes=max(0,usage-inactive),
                        throttling=data["cpu_stats"].get("throttling_data",{}),
                        networks=data.get("networks",{}),block_io=data.get("blkio_stats",{}))
                    previous[name]=(cpu,now)
                meminfo=Path("/proc/meminfo").read_text().splitlines()
                row["host_mem_kib"]={s.split(":")[0]:int(s.split()[1]) for s in meminfo
                                     if s.startswith(("MemAvailable:","SwapFree:"))}
                if i%5==0:
                    row["postgres"]=json.loads(p.sql("""select json_build_object(
                        'connections',count(*),'active',count(*) filter(where state='active'),
                        'lock_waiters',count(*) filter(where wait_event_type='Lock'))
                        from pg_stat_activity where datname='saas_staging' and usename='saas_staging'"""))
                samples.append(row);i+=1
            except Exception as exc:
                errors.append(type(exc).__name__)
            stopped.wait(1)
    thread=None
    result=None
    load=project+"-load-measure"
    try:
        command("up","-d","--wait","--wait-timeout","120","database")
        command("run","--rm","--no-deps","app","python","-m","tools.staging_postgres","initialize")
        seed=last_json(command("run","--rm","--no-deps","app","python","-m","tools.mixed_load_fixture","seed"))
        command("up","-d","--no-build","--wait","--wait-timeout","240","app")
        app,app_info=p.container("app")
        database,db_info=p.container("database")
        ocr,ocr_info=p.container("ocr")
        if ocr_mode=="inline":
            command("stop","ocr")  # Keep the measured server CPU budget unchanged.
        state=command("exec","-T","app","python","-m","tools.mixed_load_fixture","state")
        private_state=last_json(state)
        assert len(private_state["tokens"])==24
        started=time.time()
        # docker run -i attaches stdin at creation; no credentials in command args or logs.
        with (private/"client-output.json").open("wb") as output:
            proc=subprocess.Popen(["docker","run","-i","--name",load,"--network",project+"_frontend",
                "--label","mixed-load-project="+project,"--cpus","2","--memory","1g",
                "--user","10001:10001","--cap-drop","ALL","--security-opt","no-new-privileges:true",
                "-e","MIXED_LOAD_DISPOSABLE=fresh-docker-only","-v",str(ROOT/"tools")+":/app/tools:ro",
                image,"python","-m","tools.mixed_load_client",str(seconds)],
                stdin=subprocess.PIPE,stdout=output,stderr=log)
            proc.stdin.write((json.dumps(private_state)+"\n").encode());proc.stdin.close()
            for _ in range(100):
                probe=subprocess.run(["docker","inspect","--format","{{.State.Running}}",load],capture_output=True)
                if probe.stdout.strip()==b"true":break
                if proc.poll() is not None:break
                time.sleep(.1)
            ids={"app":app,"database":database,"load":load}
            if ocr_mode=="isolated":ids["ocr"]=ocr
            thread=threading.Thread(target=monitor,args=(ids,),daemon=True)
            thread.start()
            proc.wait(timeout=seconds*6+240)
        stopped.set();thread.join(20)
        if proc.returncode:
            raise RuntimeError("HTTP mixed load failed; inspect local evidence.")
        workload=last_json((private/"client-output.json").read_bytes())
        audit=last_json(command("exec","-T","app","python","-m","tools.mixed_load_fixture","audit"))
        logs=command("logs","--no-color","app").decode(errors="replace")
        worker_count=logs.count("Started server process")
        assert worker_count==workers,(worker_count,workers)
        pg=json.loads(p.sql("""select row_to_json(s) from (select numbackends,xact_commit,xact_rollback,
            blks_read,blks_hit,deadlocks,temp_files,temp_bytes,blk_read_time,blk_write_time
            from pg_stat_database where datname='saas_staging') s"""))
        states={name:json.loads(subprocess.check_output(["docker","inspect",cid]))[0]["State"]
                for name,cid in ids.items()}
        peaks={name:dict(cpu_cores=round(max(s["containers"][name]["cpu_cores"] for s in samples),3),
            memory_mib=round(max(s["containers"][name]["memory_bytes"] for s in samples)/2**20,2),
            working_set_mib=round(max(s["containers"][name]["working_set_bytes"] for s in samples)/2**20,2))
            for name in ids}
        result=dict(result="PASS",workers=workers,observed_workers=worker_count,seed=seed,
            started_unix=started,workload=workload,audit=audit,samples=len(samples),sample_errors=errors,
            sampled_peaks=peaks,pg_stats=pg,
            max_connections=max(s.get("postgres",{}).get("connections",0) for s in samples),
            max_lock_waiters=max(s.get("postgres",{}).get("lock_waiters",0) for s in samples),
            oom_killed={n:s["OOMKilled"] for n,s in states.items()},
            ocr_mode=ocr_mode,
            quota=dict(ocr_cpu=1 if ocr_mode=="isolated" else 0,ocr_memory_gib=1 if ocr_mode=="isolated" else 0,app_cpu=app_cpus,app_memory_gib=4,database_cpu=server_cpus//4,database_memory_gib=1,
                       client_cpu=2,client_memory_gib=1),
            published_ports=app_info["NetworkSettings"]["Ports"],
            image_id=app_info["Image"],load_separate_container=True,
            host_memory=Path("/proc/meminfo").read_text().splitlines()[:3])
        assert not errors and not any(result["oom_killed"].values())
        assert states["load"]["ExitCode"]==0
        if verify_ocr:
            assert ocr_mode=="isolated"
            from tools.ocr_resilience_checks import exercise
            result["resilience"]=exercise(p,private_state,log)
        return result
    except BaseException:
        if result:
            result["result"]="FAIL"
        raise
    finally:
        stopped.set()
        if thread:thread.join(20)
        (out/("workers-"+str(workers)+"-samples.json")).write_text(json.dumps(samples))
        application_logs=subprocess.run([*p.command,"logs","--no-color","app"],capture_output=True)
        (out/("workers-"+str(workers)+"-app.log")).write_bytes(application_logs.stdout+application_logs.stderr)
        ocr_logs=subprocess.run([*p.command,"logs","--no-color","ocr"],capture_output=True)
        (out/("workers-"+str(workers)+"-ocr.log")).write_bytes(ocr_logs.stdout+ocr_logs.stderr)
        final_states={}
        for service in ("app","database","ocr"):
            listed=p.run("ps","-a","-q",service).stdout.decode().split()
            if listed:
                inspected=json.loads(subprocess.check_output(["docker","inspect",listed[0]]))[0]
                final_states[service]={"state":inspected["State"],"restarts":inspected["RestartCount"]}
        (out/("workers-"+str(workers)+"-states.json")).write_text(json.dumps(final_states))
        probe=subprocess.run(["docker","inspect",load],capture_output=True)
        if probe.returncode==0:
            assert json.loads(probe.stdout)[0]["Config"]["Labels"]["mixed-load-project"]==project
            client_logs=subprocess.run(["docker","logs",load],capture_output=True)
            (out/("workers-"+str(workers)+"-client.log")).write_bytes(client_logs.stdout+client_logs.stderr)
            subprocess.run(["docker","rm","-f",load],check=True,stdout=log,stderr=log)
        command("down","--volumes","--remove-orphans")
        assert not p.run("ps","-a","-q").stdout.strip()
        log.close()
        if result:
            result["disposable_project_removed"]=True
            (out/("workers-"+str(workers)+"-result.json")).write_text(json.dumps(result,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True,help="Image built from the code being measured.")
    parser.add_argument("--seconds",type=int,default=60)
    parser.add_argument("--workers",default="1,2")
    parser.add_argument("--server-cpus",type=int,choices=(4,8),default=4)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--ocr-mode",choices=("inline","isolated"),default="inline")
    parser.add_argument("--verify-ocr-failures",action="store_true")
    args=parser.parse_args()
    workers=[int(x) for x in args.workers.split(",")]
    if not __debug__ or os.name!="posix" or not Path("/var/run/docker.sock").exists():
        parser.error("Requires Linux Docker and assertions enabled.")
    if not 15<=args.seconds<=300 or any(w not in (1,2,4) for w in workers):
        parser.error("Choose 15..300 seconds and workers 1,2,4.")
    args.output.mkdir(parents=True,exist_ok=False)
    for worker in workers:
        print("Starting",worker,"workers",flush=True)
        result=run_case(worker,args.seconds,args.image,args.output,args.server_cpus,args.ocr_mode,args.verify_ocr_failures)
        print(json.dumps({"workers":worker,"result":result["result"],"peaks":result["sampled_peaks"]}),flush=True)


if __name__=="__main__":
    main()
