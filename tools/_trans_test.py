"""测 mihomo 认不认 network: httpupgrade / xhttp。"""
import json, os, subprocess, time, urllib.request
ROOT="/opt/nodehub"; EXE=os.path.join(ROOT,"bin","mihomo")
WD=os.path.join(ROOT,"data","transtest"); API=38891
os.makedirs(WD, exist_ok=True)
for net in ("tcp","http","httpupgrade","xhttp"):
    p={"type":"vless","name":"t1","server":"127.0.0.1","port":1,
       "uuid":"0b3e0b1e-1111-2222-3333-444455556666","network":net,"tls":True,
       "servername":"x.example"}
    if net in ("ws","httpupgrade","xhttp"): p["ws-opts"]={"path":"/"}
    if net=="http": p["http-opts"]={"path":["/"]}
    conf={"mixed-port":38890,"allow-lan":False,"mode":"rule","log-level":"silent",
          "external-controller":f"127.0.0.1:{API}","proxies":[p],"rules":["MATCH,DIRECT"]}
    path=os.path.join(WD,"c.yaml"); json.dump(conf, open(path,"w"))
    proc=subprocess.Popen([EXE,"-d",WD,"-f",path],stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT,text=True)
    up=False
    for _ in range(24):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{API}/version",timeout=1).read(); up=True; break
        except Exception:
            if proc.poll() is not None: break
            time.sleep(0.4)
    err=""
    if not up:
        proc.terminate()
        try: err=(proc.communicate(timeout=5)[0] or "").strip().splitlines()[-1][:110]
        except Exception: pass
    print(f"  network={net:12} -> {'加载成功' if up else '加载失败: '+err}")
    if up: proc.terminate()
    try: proc.wait(5)
    except Exception: proc.kill()
    time.sleep(0.3)
