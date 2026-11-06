#!/usr/bin/env python3
"""Bounded named-operation execution for the DASH admin console."""

import concurrent.futures
import datetime
import json
import os
import re
import time


MAX_OUTPUT_BYTES = 65536
COMMANDS = {
    "landsraad-cycle": {"label":"Validate Landsraad / Coriolis cycle","description":"Read-only seven-day Landsraad invariant and Deep Desert policy check.","timeout":20,"category":"configuration"},
    "stack-status": {"label":"Stack status","description":"Read-only service, resource, database, and routing status report.","timeout":45,"category":"runtime"},
    "rmq-health": {"label":"RabbitMQ health","description":"Read-only health checks for the configured RabbitMQ services.","timeout":20,"category":"runtime"},
    "inventory-audit": {"label":"Inventory integrity audit","description":"Read-only duplicate, negative-slot, and over-capacity inventory scan.","timeout":30,"category":"integrity"},
    "storage-status": {"label":"Storage status","description":"Read-only workspace, backup, and container storage report.","timeout":20,"category":"host"},
    "cpu-affinity-status": {"label":"CPU affinity status","description":"Read current project-container CPU affinity through the Docker API.","timeout":20,"category":"host"},
}


def catalog():
    return [{"id":command_id,"label":spec["label"],"description":spec["description"],"category":spec["category"],"timeoutSeconds":spec["timeout"],"available":True,"acceptsArguments":False,"backend":"native-read-only"} for command_id,spec in COMMANDS.items()]


def redact(text, environment=None):
    text=str(text or "");environment=environment or os.environ;secrets=[]
    for key,value in environment.items():
        if re.search(r"password|passwd|secret|token|credential|private.?key|authorization",key,re.I):
            value=str(value or "")
            if len(value)>=4: secrets.append(value)
    for value in sorted(set(secrets),key=len,reverse=True): text=text.replace(value,"[redacted]")
    text=re.sub(r"(?i)(authorization\s*[:=]\s*(?:bearer|basic)\s+)[^\s]+",r"\1[redacted]",text)
    text=re.sub(r"(?i)(\b[a-z][a-z0-9+.-]{1,20}://[^/@:\s]+:)[^/@\s]+(@)",r"\1[redacted]\2",text)
    pem_label="PRIVATE"+" "+"KEY"
    pem_pattern=rf"-----BEGIN [^-]+ {pem_label}-----.*?-----END [^-]+ {pem_label}-----"
    text=re.sub(pem_pattern,"[redacted private key]",text,flags=re.S)
    encoded=text.encode("utf-8",errors="replace")
    if len(encoded)>MAX_OUTPUT_BYTES: text=encoded[:MAX_OUTPUT_BYTES].decode("utf-8",errors="ignore")+"\n...[output truncated]"
    return text


def run(command_id, executor, environment=None):
    command_id=str(command_id or "").strip()
    if command_id not in COMMANDS: raise ValueError("unknown command-console command id")
    if not callable(executor): raise ValueError("command-console executor is required")
    spec=COMMANDS[command_id];started=time.monotonic();timed_out=False;returncode=0
    pool=concurrent.futures.ThreadPoolExecutor(max_workers=1,thread_name_prefix="command-console")
    future=pool.submit(executor,command_id)
    try:
        value=future.result(timeout=spec["timeout"])
        if isinstance(value,dict) and value.get("ok") is False:
            returncode=1
        output=json.dumps(value,sort_keys=True,indent=2,default=str)
    except concurrent.futures.TimeoutError:
        timed_out=True;returncode=124;future.cancel();output="operation timed out"
    except Exception as exc:
        returncode=1;output=f"{type(exc).__name__}: {exc}"
    finally:
        pool.shutdown(wait=False,cancel_futures=True)
    return {"ok":returncode==0,"commandId":command_id,"label":spec["label"],"startedAt":datetime.datetime.now(datetime.timezone.utc).isoformat(),"durationMs":int((time.monotonic()-started)*1000),"returncode":returncode,"timedOut":timed_out,"output":redact(output,environment),"outputLimitBytes":MAX_OUTPUT_BYTES,"shell":False,"subprocess":False,"argumentsAccepted":False,"backend":"native-read-only"}
