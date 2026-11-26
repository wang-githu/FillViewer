# -*- coding: utf-8 -*-
"""
FillViewer 本地服务
- 静态文件：本文件所在目录下的所有文件
- GET  /api/data   读 data/data.json（WinCC 定时写出的数据）
- POST /api/click  接收页面点击，追加到 data/clicks.json（WinCC 定时读取）
默认只监听 127.0.0.1:8099（可用 FILLVIEWER_HOST/PORT 覆盖），不对外暴露。
"""
import subprocess
import io
import json
import hashlib
import os
import shutil
import sys
import time
import urllib.error
import urllib.parse
import base64
import ssl
import urllib.request
import socket
import re
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
CLICK_FILE = os.path.join(DATA_DIR, "clicks.json")
DATA_FILE = os.path.join(DATA_DIR, "data.json")
MAP_FILE  = os.path.join(DATA_DIR, "mapping.json")
TAGS_FILE = os.path.join(DATA_DIR, "tags.txt")

# ---- WinCC REST 服务（计算机属性里勾了“REST 服务”才会真的监听）----
# 实测可用地址（HTTPS + 自签名证书，Basic 认证，账号是 WinCC 用户）
REST_BASE = "https://YOUR-WINCC-HOST:8092/WinCCRestService"
REST_HOST, REST_PORT = "YOUR-WINCC-HOST", 8092
REST_USER, REST_PASS = "", ""
# 真正的 TLS 上下文由 _ssl_ctx() 按 config.json 生成（默认开启证书校验）。
# 保留这个变量名只为兼容旧代码里的 _SSL 引用（值为 None 时一律走 _ssl_ctx()）。
_SSL = None
# 安全策略默认值（都可在 config.json 里覆盖）
SEC_DEFAULTS = {
    "restInsecure": False,      # 自签名证书现场需显式设 true（会打印告警）
    "restCaFile": "",           # 可选：自签名 CA 证书路径（比 restInsecure 更安全）
    "restSkipHostname": False,  # true = 校验证书链但跳过主机名（自签证书 CN/SAN 与访问名不一致时用）
    "readOnly": False,          # true = 服务端一律拒绝写值
    "requireWriteToken": True,  # POST /api/write 必须带一次性 token
    "allowOrigins": [],         # 额外允许的 Origin（默认只允许本机同 Host）
    "maxUploadMB": 256,         # 单次模型上传上限
    "writeRatePer10s": 10,      # 每客户端 10 秒内最多写几次
    "logMaxMB": 5,              # logs/server.log 超过该大小就滚存
}
REST_TTL = 0.2          # 秒：REST 结果缓存默认值（可被 config.liveTtl 或 ?ttl= 覆盖）
REST_RETRY_AFTER = 15.0  # 秒：REST 连不上后，隔这么久再试，避免每次轮询都卡住
_rest_cache = {"t": 0, "data": None, "path": None}
_rest_down_until = 0.0

os.makedirs(DATA_DIR, exist_ok=True)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ---- 可换机器的配置（REST 地址/账号）：config.json ----
CONFIG_FILE = os.path.join(ROOT, "config.json")
_cfg = _read_json(CONFIG_FILE, {}) if os.path.isfile(CONFIG_FILE) else {}


def _backup_before_write(path, keep=5):
    """写文件前把已存在的文件备份成 path + .bak_<时间戳>，只保留最近 keep 份。"""
    try:
        if not os.path.isfile(path):
            return
        stamp = time.strftime("%Y%m%d_%H%M%S")
        dst = "%s.bak_%s" % (path, stamp)
        shutil.copyfile(path, dst)
        d = os.path.dirname(path)
        base = os.path.basename(path) + ".bak_"
        cand = sorted([f for f in os.listdir(d) if f.startswith(base)])
        for f in cand[:-keep]:
            try:
                os.remove(os.path.join(d, f))
            except OSError:
                pass
    except Exception:
        pass



def cfg_rest_base():
    b = (_cfg.get("restBase") or "").strip()
    return b.rstrip("/") if b else REST_BASE


def cfg_rest_auth():
    u = _cfg.get("restUser") or REST_USER
    p = _cfg.get("restPass") or REST_PASS
    return "Basic " + base64.b64encode((u + ":" + p).encode()).decode()


def reload_cfg():
    global _cfg
    _cfg = _read_json(CONFIG_FILE, {}) if os.path.isfile(CONFIG_FILE) else {}
    _rest_cache["t"] = 0
    _rest_cache["data"] = None
    return _cfg


# ---- 安全策略读取 ----
def sec(key):
    """取安全策略项，类型跟随 SEC_DEFAULTS。"""
    dflt = SEC_DEFAULTS.get(key)
    if key not in _cfg:
        return dflt
    v = _cfg.get(key)
    if isinstance(dflt, bool):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    if isinstance(dflt, (int, float)):
        try:
            return type(dflt)(v)
        except Exception:
            return dflt
    return v if v is not None else dflt


# ---- REST 地址统一解析（P0-1：探测/读取/写值都从这里取 host/port）----
def cfg_rest_addr():
    """把 cfg_rest_base() 解析成 (scheme, host, port)。"""
    u = urllib.parse.urlparse(cfg_rest_base())
    scheme = (u.scheme or "https").lower()
    host = u.hostname or REST_HOST
    port = u.port or (443 if scheme == "https" else 80)
    return scheme, host, int(port)


_ssl_cache = {"key": None, "ctx": None}


def cfg_ssl_context():
    """按配置生成 TLS 上下文：默认校验证书（P1-1）。"""
    ctx = ssl.create_default_context()
    ca = (sec("restCaFile") or "").strip()
    if ca:
        if os.path.isfile(ca):
            try:
                ctx.load_verify_locations(ca)
            except Exception as e:
                print("[rest] 载入自定义 CA 失败（仍用系统信任链）：%s" % e, flush=True)
        else:
            print("[rest] restCaFile 指定的文件不存在：%s" % ca, flush=True)
    if sec("restSkipHostname") and not sec("restInsecure"):
        # 只跳过"证书名是否匹配访问主机"，证书链仍然校验（比 restInsecure 安全）
        ctx.check_hostname = False
        if not _ssl_cache.get("warnedSkip"):
            print("[rest][提示] 已按 restSkipHostname=true 跳过主机名校验（证书链仍校验）。",
                  flush=True)
            _ssl_cache["warnedSkip"] = True
    if sec("restInsecure"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        if not _ssl_cache.get("warned"):
            print("[rest][警告] 已按 restInsecure=true 关闭证书校验；"
                  "建议改用 restCaFile 导入自签名 CA。", flush=True)
            _ssl_cache["warned"] = True
    return ctx


def _ssl_ctx():
    key = (bool(sec("restInsecure")), bool(sec("restSkipHostname")),
           (sec("restCaFile") or "").strip())
    if _ssl_cache["key"] != key or _ssl_cache["ctx"] is None:
        _ssl_cache["key"] = key
        _ssl_cache["ctx"] = cfg_ssl_context()
    return _ssl_cache["ctx"]


def rest_state():
    """当前 REST 生效地址与熔断状态，给 UI 显示用。"""
    scheme, host, port = cfg_rest_addr()
    left = max(0.0, _rest_down_until - time.time())
    return {"scheme": scheme, "host": host, "port": port,
            "base": cfg_rest_base(),
            "insecure": bool(sec("restInsecure")),
            "down": left > 0, "downLeft": round(left, 1),
            "path": _rest_cache.get("path")}


def _tcp_connect(host, port, total=3.0):
    """带总时限的 TCP 连接：多地址时按剩余时间分摊，避免逐个 3s 叠加。"""
    t0 = time.time()
    deadline = t0 + max(0.4, float(total))
    try:
        infos = socket.getaddrinfo(host, port, 0, socket.SOCK_STREAM)
    except Exception as e:
        return False, e, 0
    seen = set()
    tries = 0
    last = None
    for fam, typ, proto, _cn, sa in infos:
        key = (fam, sa)
        if key in seen:
            continue
        seen.add(key)
        left = deadline - time.time()
        if left <= 0.05:
            break
        tries += 1
        s = None
        try:
            s = socket.socket(fam, typ, proto)
            s.settimeout(max(0.25, min(1.2, left)))
            s.connect(sa)
            return True, None, tries
        except Exception as e:
            last = e
        finally:
            if s is not None:
                try:
                    s.close()
                except Exception:
                    pass
    if last is None:
        last = socket.timeout("连接超时（多地址全部耗尽时间预算）")
    return False, last, tries


def _cert_info(pem_path):
    """解析 PEM 证书的关键字段（给界面和日志看）。"""
    try:
        d = ssl._ssl._test_decode_cert(pem_path)
        sub = "; ".join("%s=%s" % (k, v) for tup in (d.get("subject") or ()) for k, v in tup)
        iss = "; ".join("%s=%s" % (k, v) for tup in (d.get("issuer") or ()) for k, v in tup)
        san = ", ".join("%s:%s" % (t, v) for t, v in (d.get("subjectAltName") or ()))
        return {"subject": sub, "issuer": iss,
                "notBefore": d.get("notBefore") or "", "notAfter": d.get("notAfter") or "",
                "san": san}
    except Exception as e:
        return {"err": str(e)}


def trust_rest_cert():
    """抓取当前 REST 服务端证书 → 存 certs/wincc-rest.pem → 让配置信任它（跳过主机名）。"""
    scheme, host, port = cfg_rest_addr()
    if scheme != "https":
        raise ValueError("当前 REST 地址不是 https，无需信任证书")
    ctx = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout=5) as s:
        with ctx.wrap_socket(s, server_hostname=host) as ss:
            der = ss.getpeercert(binary_form=True)
    if not der:
        raise ValueError("没有从服务端取到证书")
    pem = ssl.DER_cert_to_PEM_cert(der)
    d = os.path.join(ROOT, "certs")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "wincc-rest.pem")
    with open(path, "w", encoding="ascii", newline="\n") as f:
        f.write(pem)
    _cfg["restCaFile"] = path
    _cfg["restSkipHostname"] = True
    info = _cert_info(path)
    info["path"] = path
    info["bytes"] = len(pem)
    info["host"] = host
    info["port"] = port
    print("[rest] 已信任 %s:%d 的证书（%d B），证书链校验开启、主机名校验跳过。"
          % (host, port, len(pem)), flush=True)
    return info


def rest_probe(timeout=4.0):
    """保存配置后的连通性自检：先 TCP，再打一次 REST 端点。"""
    global _rest_down_until
    t0 = time.time()
    scheme, host, port = cfg_rest_addr()
    ok_tcp, err_tcp, tries = _tcp_connect(host, port, total=min(3.0, timeout))
    if not ok_tcp:
        _rest_down_until = time.time() + REST_RETRY_AFTER
        return {"ok": False, "stage": "tcp", "host": host, "port": port,
                "err": str(err_tcp)[:160], "tries": tries,
                "ms": int((time.time() - t0) * 1000)}
    try:
        # 先用 REST 根路径 GET（返回服务描述 JSON，200），对 WinCC 最无害；
        # 个别版本根路径不可用，再退回"空变量列表 POST"探活。
        code, j = _rest_call("GET", "", timeout=timeout)
        if code != 200:
            code, j = _rest_call("POST", "/tagManagement/Values",
                                 body={"variableNames": []}, timeout=timeout)
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        is_tls = ("certificate_verify_failed" in low or "hostname mismatch" in low
                  or "certificate verify failed" in low or "ssl:" in low)
        _rest_down_until = time.time() + REST_RETRY_AFTER
        out = {"ok": False, "stage": "tls" if is_tls else "http",
               "host": host, "port": port, "err": msg[:200],
               "ms": int((time.time() - t0) * 1000)}
        if is_tls:
            out["canTrust"] = True
            out["hint"] = ("WinCC 的 REST 用自签证书，证书名（CN/SAN）通常不含你访问用的主机名，"
                           "所以会报 Hostname mismatch。点「信任当前证书」：程序会把该证书存到 "
                           "certs/ 并只跳过主机名校验（证书链仍校验）。")
        return out
    _rest_down_until = 0.0
    _rest_cache["t"] = 0
    note = ""
    if code == 0:
        note = "TCP 通但 HTTPS 握手/请求失败（多为证书或账号）"
    elif code in (401, 403):
        note = "账号或授权问题（HTTP %s）" % code
    return {"ok": code != 0, "stage": "rest", "host": host, "port": port,
            "http": code, "err": (j or {}).get("error") if isinstance(j, dict) else None,
            "note": note, "ms": int((time.time() - t0) * 1000)}


# ---- 写值授权（P0-3）----
WRITE_ALLOW_FILE = os.path.join(DATA_DIR, "write_allow.json")
WRITE_AUDIT_FILE = os.path.join(os.path.dirname(CONFIG_FILE), "logs", "write_audit.log")
_wtok = {}
_wrate = {}


def write_allow_names():
    try:
        d = _read_json(WRITE_ALLOW_FILE, {"names": []}) or {}
        return set(x for x in (d.get("names") or []) if isinstance(x, str) and x.strip())
    except Exception:
        return set()


def write_permitted(name):
    """返回 (是否允许, 依据)。依据：readOnly / not-in-vartable / vartable / allowlist"""
    if sec("readOnly"):
        return False, "readOnly"
    if not name:
        return False, "missing"
    if name in write_allow_names():
        return True, "allowlist"
    try:
        vt = _read_json(VARTABLE, {}) or {}
        for v in (vt.get("vars") or []):
            if isinstance(v, dict) and v.get("n") == name:
                return (bool(v.get("w")), "vartable" if v.get("w") else "vartable-readonly")
    except Exception:
        pass
    return False, "not-in-vartable"


def write_token_new(ip):
    tok = base64.urlsafe_b64encode(os.urandom(12)).decode().rstrip("=")
    now = time.time()
    for k, (i, exp) in list(_wtok.items()):
        if exp < now:
            _wtok.pop(k, None)
    _wtok[tok] = (ip, now + 120)
    return tok


def write_token_ok(tok, ip):
    it = _wtok.pop(tok, None)
    if not it:
        return False
    i, exp = it
    return i == ip and exp >= time.time()


def write_rate_ok(ip):
    """每客户端 10 秒内最多 writeRatePer10s 次。"""
    limit = int(sec("writeRatePer10s") or 10)
    now = time.time()
    q = [t for t in (_wrate.get(ip) or []) if now - t < 10.0]
    if len(q) >= limit:
        _wrate[ip] = q
        return False
    q.append(now)
    _wrate[ip] = q
    return True


def write_audit(ip, name, value, old, result):
    try:
        os.makedirs(os.path.dirname(WRITE_AUDIT_FILE), exist_ok=True)
        rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": ip, "name": name,
               "old": old, "new": value, "result": result}
        with open(WRITE_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ---- 模型文件名与路径安全（P0-4）----
GLB_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.glb$", re.IGNORECASE)


def safe_glb_name(name):
    n = (name or "").strip()
    if not n or len(n) > 128:
        raise ValueError("文件名无效")
    if "/" in n or "\\" in n or ".." in n:
        raise ValueError("文件名不能包含路径分隔符或 ..")
    if not GLB_NAME_RE.match(n):
        raise ValueError("文件名只能用字母/数字/._-，且必须以 .glb 结尾")
    return n


def inside(parent, child):
    """child 的 realpath 是否仍在 parent 内。"""
    try:
        p = os.path.realpath(parent)
        c = os.path.realpath(child)
        return os.path.commonpath([p, c]) == p
    except Exception:
        return False

# ---- 运行选项（开机自启 / 启动后自动运行 / 默认项目与模型） ----
RT_DEFAULTS = {"autostart": False, "autostartMode": "none", "autoopen": False,
               "kiosk": False, "project": "", "model": ""}
AUTOSTART_NAME = "FillViewer_autostart.cmd"


def runtime_cfg():
    rt = dict(RT_DEFAULTS)
    raw = _cfg.get("runtime") or {}
    if isinstance(raw, dict):
        for k in RT_DEFAULTS:
            if k in raw:
                rt[k] = raw[k]
    rt["autostart"] = bool(rt.get("autostart"))
    if rt.get("autostartMode") not in ("none", "user", "service"):
        rt["autostartMode"] = "user" if rt["autostart"] else "none"
    rt["autoopen"] = bool(rt.get("autoopen"))
    rt["kiosk"] = bool(rt.get("kiosk"))
    rt["project"] = str(rt.get("project") or "")
    rt["model"] = str(rt.get("model") or "")
    return rt


def startup_dir():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Microsoft", "Windows", "Start Menu",
                        "Programs", "Startup")


def autostart_path():
    return os.path.join(startup_dir(), AUTOSTART_NAME)


def autostart_enabled():
    return os.path.isfile(autostart_path())


def apply_autostart(on):
    """写/删启动文件夹里的自启脚本；不需要管理员权限。"""
    p = autostart_path()
    if on:
        d = startup_dir()
        if not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        body = ("@echo off\r\n"
                "rem FillViewer auto start (generated by studio)\r\n"
                "call \"" + os.path.join(ROOT, "tools", "start_server.cmd") + "\"\r\n")
        with open(p, "w", encoding="gbk", newline="") as f:
            f.write(body)
    elif os.path.isfile(p):
        os.remove(p)
    return autostart_enabled()


def list_models():
    d = os.path.join(ROOT, "models")
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith((".glb", ".gltf")):
                out.append(f)
    return out


def save_runtime(rec):
    rt = runtime_cfg()
    for k in ("autoopen", "kiosk"):
        if k in rec:
            rt[k] = bool(rec[k])
    if "project" in rec:
        pid = str(rec.get("project") or "")
        rt["project"] = pid if (pid and os.path.isdir(proj_dir(pid))) else ""
    if "model" in rec:
        rt["model"] = str(rec.get("model") or "")
    mode = rec.get("autostartMode")
    if mode is None and "autostart" in rec:
        mode = "user" if rec.get("autostart") else "none"
    if mode in ("none", "user", "service"):
        # 先清掉另一种方式，保证只有一种生效
        if mode != "user":
            apply_autostart(False)
        if mode != "service":
            apply_service_autostart(False)
        if mode == "user":
            apply_autostart(True)
        if mode == "service":
            ok, note, _ = apply_service_autostart(True)
            rt["lastServiceNote"] = note
        rt["autostartMode"] = mode
        rt["autostart"] = (mode != "none")
    else:
        rt["autostart"] = autostart_enabled()
    cur = dict(_cfg)
    cur["runtime"] = rt
    _write_json(CONFIG_FILE, cur)
    reload_cfg()
    return rt


# ---- 服务级自启（计划任务，开机即启、无需登录；创建任务需管理员）----
SERVICE_TASK = "FillViewer服务"
SERVICE_EXE_ARGS = "-nobrowser"


def _task_cmd(args):
    """返回 (ok, 输出)。sysnative 无关，直接调 schtasks。"""
    try:
        p = subprocess.run(["schtasks"] + args, capture_output=True, text=True,
                           timeout=20, encoding="gbk", errors="ignore")
        return (p.returncode == 0), ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:
        return False, str(e)


def service_task_exists():
    ok, _ = _task_cmd(["/query", "/tn", SERVICE_TASK])
    return ok


def service_cmds():
    """生成「右键以管理员运行」的两个脚本，保证在无提权时也能让用户自助完成。"""
    tools = os.path.join(ROOT, "tools")
    os.makedirs(tools, exist_ok=True)
    exe = os.path.join(ROOT, "FillViewer.exe")
    tr = '\"%s\" %s' % (exe, SERVICE_EXE_ARGS)
    on = os.path.join(tools, "启用服务级自启(需管理员).cmd")
    off = os.path.join(tools, "关闭服务级自启(需管理员).cmd")
    body_on = ("@echo off\r\n"
               "rem 需要管理员权限：右键 → 以管理员身份运行\r\n"
               "schtasks /create /tn \"%s\" /tr \"%s\" /sc onstart /ru SYSTEM /rl HIGHEST /f\r\n"
               "if errorlevel 1 (echo 创建失败：请确认已用管理员运行) else (echo 已创建开机任务：%s)\r\n"
               "pause\r\n") % (SERVICE_TASK, tr, SERVICE_TASK)
    body_off = ("@echo off\r\n"
                "schtasks /delete /tn \"%s\" /f\r\n"
                "pause\r\n") % SERVICE_TASK
    with open(on, "w", encoding="gbk", newline="") as f:
        f.write(body_on)
    with open(off, "w", encoding="gbk", newline="") as f:
        f.write(body_off)
    return on, off


def apply_service_autostart(on):
    """尝试直接创建/删除计划任务；失败时返回提示（不影响已生成的 cmd）。"""
    cmds = service_cmds()
    if on:
        exe = os.path.join(ROOT, "FillViewer.exe")
        if not os.path.isfile(exe):
            return False, "尚未生成 FillViewer.exe：请先点「生成 FillViewer.exe」", cmds
        ok, out = _task_cmd(["/create", "/tn", SERVICE_TASK,
                             "/tr", '"%s" %s' % (exe, SERVICE_EXE_ARGS),
                             "/sc", "onstart", "/ru", "SYSTEM", "/rl", "HIGHEST", "/f"])
        if ok:
            return True, "已创建开机任务", cmds
        return False, "创建失败（多半需要管理员）：请右键运行 tools\\%s" % os.path.basename(cmds[0]), cmds
    ok, out = _task_cmd(["/delete", "/tn", SERVICE_TASK, "/f"])
    return ok, ("已删除开机任务" if ok else "任务不存在或删除失败"), cmds


def exe_info():
    p = os.path.join(ROOT, "FillViewer.exe")
    if os.path.isfile(p):
        return {"path": p, "exists": True, "size": os.path.getsize(p)}
    return {"path": p, "exists": False, "size": 0}


def build_exe():
    import importlib.util
    tool = os.path.join(ROOT, "tools", "_build_exe.py")
    if not os.path.isfile(tool):
        raise RuntimeError("缺少 tools\\_build_exe.py")
    spec = importlib.util.spec_from_file_location("_fillviewer_build_exe", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    exe, size = mod.build(ROOT)
    return exe, size






def read_tags():
    """页面写下的变量清单（UTF-16，给 WinCC 的 FSO 读的同一份）"""
    try:
        with open(TAGS_FILE, encoding="utf-16") as f:
            raw = f.read()
    except Exception:
        return []
    out = []
    for line in raw.splitlines():
        t = line.strip()
        if t and t not in out:
            out.append(t)
    return out


_REST_TPL = [
    "/tagManagement/Value/%s",
]


def _rest_alive():
    """先花 0.3 秒探一下端口；不通就熔断 15 秒。
    注意：本机装了远控/虚拟网卡类软件，连被拒绝的端口也要 ~2 秒才返回，
    所以必须用带超时的小探测，不能直接发 HTTP。"""
    global _rest_down_until
    if time.time() < _rest_down_until:
        return False
    # P0-1：探测用的 host/port 必须与 cfg_rest_base() 一致，否则换机后仍探旧主机
    _scheme, _host, _port = cfg_rest_addr()
    try:
        s = socket.create_connection((_host, _port), timeout=0.3)
        s.close()
        return True
    except Exception:
        _rest_down_until = time.time() + REST_RETRY_AFTER
        return False


def _pick_value(obj):
    if isinstance(obj, (int, float, bool, str)):
        return obj
    if isinstance(obj, dict):
        for k in ("value", "onlineValue", "Value", "OnlineValue", "v", "result"):
            if k in obj:
                return _pick_value(obj[k])
    return obj


def _rest_get(tag, tpl, timeout=2.0):
    """单读一个变量；失败抛异常（走模板回退）"""
    path = tpl % urllib.parse.quote(tag, safe="")
    code, j = _rest_call("GET", path, timeout=timeout)
    if code != 200:
        raise RuntimeError("HTTP %s" % code)
    if isinstance(j, dict) and j.get("error"):
        raise RuntimeError(str(j.get("error")))
    return j


def rest_read_batch(tags, timeout=8.0):
    """批量读：一次 POST /tagManagement/Values。成功返回 {name: {"v","q","ts"}}，失败返回 None"""
    if not tags:
        return {}
    body = {"variableNames": list(tags)}
    code, j = _rest_call("POST", "/tagManagement/Values", body=body, timeout=timeout)
    if code != 200 or not isinstance(j, list):
        return None
    out = {}
    for it in j:
        if not isinstance(it, dict):
            continue
        n = it.get("variableName")
        if not n:
            continue
        if it.get("error") or it.get("errorcode") not in (None, 0):
            out[n] = {"v": None, "q": None, "ts": None, "err": it.get("error") or ("errorcode %s" % it.get("errorcode"))}
        else:
            out[n] = {"v": it.get("value"), "q": it.get("qualitycode"), "ts": it.get("timestamp"), "err": None}
    return out


_last_errs = {}


def rest_read_all(tags):
    """返回 (values_dict, path_used)；连不上返回 (None, None)

    顺带把每个失败变量的原因记进 _last_errs，供 /api/live 回给前端。"""
    _last_errs.clear()
    if not tags or not _rest_alive():
        return None, None
    # ① 批量端点（一次请求，手册第 28-29 页）
    try:
        if len(tags) >= 2:
            b = rest_read_batch(tags)
            if b:
                # 首轮批量读偶发只回一部分（一次带 100+ 个变量时尤其明显）：
                # 对「没数据但也没报错」的项补读一次，避免前端误报缺变量。
                retry = [t for t in tags
                         if (b.get(t) or {}).get("v") is None and not (b.get(t) or {}).get("err")]
                if retry and len(retry) <= 80:
                    b2 = rest_read_batch(retry)
                    if b2:
                        b.update(b2)
                vals = {}
                anyok = False
                for t in tags:
                    r = b.get(t)
                    if r and r.get("v") is not None:
                        vals[t] = r["v"]
                        anyok = True
                    else:
                        vals[t] = None
                        _last_errs[t] = (r or {}).get("err") or "无数据" 
                if anyok:
                    _rest_cache["path"] = "batch:/tagManagement/Values"
                    return vals, _rest_cache["path"]
    except Exception:
        pass

    # ② 兜底：逐个读
    tpls = [_rest_cache["path"]] if _rest_cache.get("path") and not str(_rest_cache.get("path", "")).startswith("batch:") else []
    tpls += [t for t in _REST_TPL if t not in tpls]
    for tpl in tpls:
        vals = {}
        ok = 0

        def _one(t):
            try:
                return t, _pick_value(_rest_get(t, tpl)), True, ""
            except Exception as e:
                return t, None, False, str(e)[:120]

        try:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=6) as ex:
                for t, v, good, errtxt in ex.map(_one, tags):
                    vals[t] = v
                    if good:
                        ok += 1
                    else:
                        _last_errs[t] = errtxt or "读取失败" 
        except Exception:
            for t in tags:
                t2, v, good, errtxt = _one(t)
                vals[t2] = v
                if good:
                    ok += 1
                else:
                    _last_errs[t2] = errtxt or "读取失败" 
        if ok:
            _rest_cache["path"] = tpl
            return vals, tpl
    return None, None


def live_data(extra=None, ttl=None, fresh=False):
    """统一出口：REST 优先，失败退回 WinCC 动作写出的 data.json
    extra: 额外要读的变量名列表（配置台预览用），会并进返回的 values。"""
    now = time.time()
    extra = [t for t in (extra or []) if t]
    if extra:
        # 带 extra 的请求不走缓存（变量集合不同）
        tags = read_tags() + [t for t in extra if t not in read_tags()]
        vals, _tpl = rest_read_all(tags)
        if vals is not None and any(v is not None for v in vals.values()):
            return {"ts": int(now), "src": "rest", "values": vals, "errors": dict(_last_errs)}
        p = _read_json(DATA_FILE, {"ts": 0, "values": {}})
        return {"ts": p.get("ts", 0), "src": "file", "values": p.get("values", {})}

    if ttl is None:
        try:
            ttl = float(_cfg.get("liveTtl", 0)) / 1000.0 or REST_TTL
        except Exception:
            ttl = REST_TTL
    ttl = max(0.0, min(5.0, ttl))
    if (not fresh) and _rest_cache["data"] is not None and now - _rest_cache["t"] < ttl:
        return _rest_cache["data"]

    tags = read_tags()
    vals, tpl = rest_read_all(tags)
    if vals is not None and any(v is not None for v in vals.values()):
        payload = {"ts": int(now), "src": "rest", "values": vals, "errors": dict(_last_errs)}
        if _rest_cache.get("note") != "rest-ok":
            _rest_cache["note"] = "rest-ok"
            print("[live] REST 已接管，路径模板: %s" % tpl, flush=True)
        _rest_cache.update(t=now, data=payload)
        return payload

    # 退回文件
    p = _read_json(DATA_FILE, {"ts": 0, "values": {}})
    payload = {"ts": p.get("ts", 0), "src": "file", "values": p.get("values", {})}
    _rest_cache.update(t=now, data=payload)
    return payload


# ===================== 变量表页（tags.html）用的 REST 代理 =====================
VARTABLE_FILE = os.path.join(DATA_DIR, "vartable.json")
ARCHIVES_FILE = os.path.join(DATA_DIR, "archives.json")


def _rest_call(method, path, body=None, timeout=15):
    """调一次 WinCC REST，返回 (http_status, dict_or_text)。"""
    url = cfg_rest_base() + path
    data = None
    headers = {"Authorization": cfg_rest_auth(), "Accept": "application/json"}
    if body is not None:
        # 注意：WinCC REST 不解析 JSON 的 \uXXXX 转义，必须发 UTF-8 原文
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return 0, {"error": "conn: %s" % e}
    try:
        return code, json.loads(raw)
    except Exception:
        return code, {"error": "badjson", "raw": raw[:300]}


def rest_read_one(name):
    code, j = _rest_call("GET", "/tagManagement/Value/" + urllib.parse.quote(name, safe=""))
    return {"n": name, "http": code, "value": j.get("value"), "q": j.get("qualitycode"),
            "ts": j.get("timestamp"), "err": j.get("error")}


def rest_write_one(name, value):
    code, j = _rest_call("PUT", "/tagManagement/Value/" + urllib.parse.quote(name, safe=""),
                         {"value": str(value)})
    return {"n": name, "http": code, "errorcode": j.get("errorcode"), "err": j.get("error")}


def rest_history(body):
    """body: {"archives":[{"name":..,"variables":[{"name":..,"range":秒,"maxValues":N}]}]}"""
    url = cfg_rest_base() + "/tagLogging/Values"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {"Authorization": cfg_rest_auth(), "Accept": "application/json",
               "Content-Type": "application/json; charset=utf-8"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=40, context=_ssl_ctx()) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        code = e.code
    except Exception as e:
        return {"http": 0, "error": "conn: %s" % e}
    try:
        return {"http": code, "data": json.loads(raw)}
    except Exception:
        return {"http": code, "error": "badjson", "raw": raw[:400]}


def rest_read_many(names, workers=6):
    """批量读多个变量（优先 /tagManagement/Values 批量端点，失败再逐变量并行）"""
    names = [n for n in names if n][:400]
    if not names:
        return []
    try:
        if len(names) >= 2 and _rest_alive():
            b = rest_read_batch(names)
            if b:
                out = []
                for n in names:
                    r = b.get(n) or {}
                    v = r.get("v")
                    out.append({
                        "n": n,
                        "http": 200 if v is not None else 404,
                        "value": v,
                        "q": r.get("q"),
                        "ts": r.get("ts"),
                        "err": r.get("err"),
                    })
                return out
    except Exception:
        pass
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(rest_read_one, names))



# ================= 多项目 / 模型对象树 / 变量检索 =================
PROJECTS_DIR = os.path.join(ROOT, "projects")
VARTABLE = os.path.join(DATA_DIR, "vartable.json")

# 字模类对象名（这些是 3D 文字，不做绑定）
GLYPH_PREFIX = ("个", "分", "十", "千", "点", "百", "厘", "数字t", "单位t", "文本")


def proj_dir(pid):
    safe = "".join(c for c in (pid or "") if c.isalnum() or c in "-_")
    return os.path.join(PROJECTS_DIR, safe)


def proj_load(pid):
    d = proj_dir(pid)
    pj = os.path.join(d, "project.json")
    if not os.path.isfile(pj):
        return None
    return {
        "project":  _read_json(pj, {}),
        "bindings": _read_json(os.path.join(d, "bindings.json"), {"items": []}),
        "panels":   _read_json(os.path.join(d, "panels.json"), {"items": []}),
        "pipes":    _read_json(os.path.join(d, "pipes.json"), {"items": []}),
        "panelTpls": _read_json(os.path.join(d, "panelTpls.json"), []),
        "pipeTpls":  _read_json(os.path.join(d, "pipeTpls.json"), []),
    }


def proj_list():
    out = []
    if not os.path.isdir(PROJECTS_DIR):
        return out
    for name in sorted(os.listdir(PROJECTS_DIR)):
        pj = os.path.join(PROJECTS_DIR, name, "project.json")
        if not os.path.isfile(pj):
            continue
        p = _read_json(pj, {})
        out.append({
            "id": name,
            "name": p.get("name", name),
            "model": p.get("model", ""),
            "updated": p.get("updated", 0),
            "dir": os.path.join(PROJECTS_DIR, name),
            "modelFile": os.path.join(PROJECTS_DIR, name, "model", p.get("model", "")) if p.get("model") else "",
        })
    return out




def _similar_name(what, names, limit=5):
    """按名称相似度给出候选：区别性词元重合权重最高，其次整体字符串相似度。"""
    import difflib as _dl

    GENERIC = {"df", "wincc", "zt", "object", "line", "group", "mesh", "scene",
               "theobject", "box", "cylinder", "circle", "obj"}

    import re as _re
    SYSRE = _re.compile(r"^[A-Za-z]{1,4}\d*数据_?")      # 形如“某某数据_XXX”的系统前缀

    def _strip_sys(x):
        return SYSRE.sub("", str(x or ""))

    def _toks(x):
        x = str(x or "").lower().replace("-", "_").replace(".", "_")
        return [p for p in x.split("_") if p and p not in GENERIC]

    wt = _toks(what)
    wset = set(wt)
    wlow = str(what).lower()

    def _lcp(a, b):
        m = min(len(a), len(b))
        i = 0
        while i < m and a[i] == b[i]:
            i += 1
        return i

    scored = []
    for n in names:
        nt = set(_toks(n))
        inter = wset & nt
        tok = (len(inter) / float(len(wset))) if wset else 0.0
        nlow = str(n).lower()
        ratio = _dl.SequenceMatcher(None, wlow, nlow).ratio()
        # 公共前缀越长，越可能是同一台设备（XLF2 → XLF1 优于 XL2）
        lcp = _lcp(wlow, nlow)
        pfx = (lcp / float(len(wlow) or 1))
        sc = ratio * 0.45 + tok * 0.75 + pfx * 0.40
        # 去掉系统前缀后再比一次：Demo_SB1_YX1 ↔ Demo_SB1_YX1 应视为同一变量
        sw, sn = _strip_sys(what), _strip_sys(n)
        if sw and sw.lower() == sn.lower():
            mw = SYSRE.match(str(what or ""))
            mn = SYSRE.match(str(n or ""))
            dw = (mw.end() if mw else 0)
            dn = (mn.end() if mn else 0)
            sc += 1.20 - 0.04 * abs(dw - dn)                # 余部一致强命中；前缀长度差越小越好
        elif sw and sn:
            sc2 = (_dl.SequenceMatcher(None, sw.lower(), sn.lower()).ratio() * 0.45
                   + (len(wset & set(_toks(sn))) / float(len(wset) or 1)) * 0.75)
            if sc2 > sc:
                sc = sc2
        scored.append((sc, n))
    scored.sort(key=lambda x: -x[0])
    out = []
    for sc, n in scored[:limit]:
        if sc <= 0.18:
            continue
        out.append({"name": n, "score": round(sc, 3)})
    return out

def proj_validate(pid):
    """检查项目里引用的对象名 / 变量名在当前模型与变量表里是否还存在。"""
    pj = proj_load(pid)
    if not pj:
        return None
    pjd = pj.get("project") or {}
    model = pjd.get("model") or ""
    mp = os.path.join(proj_dir(pid), "model", model)
    nodes = []
    if os.path.isfile(mp):
        try:
            nodes = [n.get("name") for n in glb_nodes(mp)]
        except Exception:
            nodes = []
    nset = set(n for n in nodes if n)

    def _norm_obj(x):
        # three.js/GLTFLoader 会删掉节点名里的 . [ ] : / 与空白，这里两边都归一化再比
        return "".join(ch for ch in str(x) if ch not in '.[]:/ \t')

    vt = _read_json(VARTABLE, {"vars": []})
    tset = set(v.get("n") for v in (vt.get("vars") or []))

    issues = []
    obj_used = 0
    tag_used = 0

    _nset_norm = set(_norm_obj(n) for n in nset)

    def chk_obj(kind, idx, name, extra=None):
        nonlocal obj_used
        if not name:
            return
        obj_used += 1
        if name not in nset and _norm_obj(name) not in _nset_norm:
            d = {"kind": kind, "idx": idx, "what": name}
            if extra:
                d.update(extra)
            try:
                d["cands"] = _similar_name(name, nset, 5)
            except Exception:
                d["cands"] = []
            issues.append(d)

    import re as _re2
    _ident = _re2.compile(r"^[A-Za-z_\u4e00-\u9fa5]")

    _live_cache = {}

    def _tag_live(name):
        """变量表里没有时，用 REST 复核一次：真的能读到就不算失效（变量表可能过期）。"""
        if name in _live_cache:
            return _live_cache[name]
        ok = False
        try:
            r = rest_read_one(name)
            ok = (r.get("http") == 200 and not r.get("err"))
        except Exception:
            ok = False
        _live_cache[name] = ok
        return ok

    def chk_tag(kind, idx, name, extra=None):
        nonlocal tag_used
        if not name:
            return
        name = str(name).strip()
        if not _ident.match(name):
            return          # 纯数字/表达式常量（例如变量框里填了 50），不算变量名
        tag_used += 1
        if name in tset:
            return
        if _tag_live(name):
            return          # 变量表过期，但 WinCC 里真实存在
        d = {"kind": kind, "idx": idx, "what": name}
        # 变量名也给出候选（同族/相似命名，便于一键换绑）
        try:
            d["cands"] = _similar_name(name, list(tset), 4)
        except Exception:
            d["cands"] = []
        if extra:
            d.update(extra)
        issues.append(d)

    for i, b in enumerate((pj.get("bindings") or {}).get("items") or []):
        chk_obj("绑定对象", i, b.get("obj"), {"btype": b.get("type")})
        for _f, t in (("tag", b.get("tag")), ("gate", b.get("gate")), ("speedVar", b.get("speedVar"))):
            chk_tag("绑定变量", i, t, {"field": _f})
        for _j, r in enumerate(b.get("rules") or []):
            chk_tag("规则变量", i, r.get("tag"), {"sub": _j})

    for i, p in enumerate((pj.get("panels") or {}).get("items") or []):
        chk_obj("展板对象", i, p.get("obj"), {"btype": "panel", "title": p.get("title")})
        for _j, it in enumerate(p.get("items") or []):
            chk_tag("展板变量", i, it.get("tag"), {"sub": _j})

    import re as _re
    for i, p in enumerate((pj.get("pipes") or {}).get("items") or []):
        for j2, o2 in enumerate(p.get("objs") or []):
            chk_obj("管线对象", i, o2, {"title": p.get("name"), "sub": j2})
        for t in _re.findall(r"[A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*(?:\[[^\]]*\])?", p.get("expr") or ""):
            if t not in ("true", "false"):
                chk_tag("管线变量", i, t)

    return {
        "ok": len(issues) == 0,
        "model": model,
        "nodes": len(nset),
        "tags": len(tset),
        "objChecked": obj_used,
        "tagChecked": tag_used,
        "issues": issues,
    }



_PROJ_PARTS = ("bindings", "panels", "pipes", "panelTpls", "pipeTpls", "project")


# ---------------- 项目版本历史（整项目快照） ----------------
HIST_SUB = "history"
HIST_KEEP_DEFAULT = 40
HIST_PARTS = ("bindings", "panels", "pipes", "panelTpls", "pipeTpls", "project")


def proj_hist_dir(pid, create=False):
    """返回 <项目>/history 目录。create=True 时才建，但前提是「项目目录已经存在」——
    否则调用方传错 id 会把不存在的项目凭空建出来。"""
    d = proj_dir(pid)
    if not d or not pid:
        return None
    if not os.path.isdir(d):
        return None
    h = os.path.join(d, HIST_SUB)
    if create:
        try:
            os.makedirs(h, exist_ok=True)
        except OSError:
            return None
    return h


def _hist_counts(d):
    """读一份快照目录，返回各 part 的条目数。"""
    def n_items(part):
        obj = _read_json(os.path.join(d, part + ".json"), None)
        if obj is None:
            return 0
        if isinstance(obj, list):
            return len(obj)
        if isinstance(obj, dict):
            if isinstance(obj.get("items"), list):
                return len(obj["items"])
            return len(obj)
        return 0
    return {p: n_items(p) for p in ("bindings", "panels", "pipes", "panelTpls", "pipeTpls")}


_HIST_VOLATILE = ("updated", "lastOpen", "lastEdit")


def _hist_hash(d):
    """由各 part 文件内容算一个指纹，用于「内容没变就不新增快照」。
    注意：project.json 的 updated 每次保存都会变，必须剔除，否则等于没去重。"""
    try:
        md5 = hashlib.md5()
        for part in HIST_PARTS:
            fp = os.path.join(d, part + ".json")
            md5.update(part.encode("utf-8"))
            if not os.path.isfile(fp):
                continue
            if part == "project":
                obj = _read_json(fp, {})
                if isinstance(obj, dict):
                    for k in _HIST_VOLATILE:
                        obj.pop(k, None)
                    md5.update(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8"))
                    continue
            with open(fp, "rb") as f:
                md5.update(f.read())
        return md5.hexdigest()
    except Exception:
        return ""


def proj_hist_list(pid):
    h = proj_hist_dir(pid)
    if not h or not os.path.isdir(h):
        return {"ok": True, "id": pid, "items": [], "keep": HIST_KEEP_DEFAULT, "dir": ""}
    items = []
    try:
        names = sorted(os.listdir(h), reverse=True)
    except OSError:
        names = []
    for nm in names:
        if nm.startswith("."):
            continue
        sd = os.path.join(h, nm)
        if not os.path.isdir(sd):
            continue
        meta = _read_json(os.path.join(sd, "meta.json"), {}) or {}
        try:
            size = sum(os.path.getsize(os.path.join(sd, f)) for f in os.listdir(sd))
        except OSError:
            size = 0
        cnt = meta.get("counts") or _hist_counts(sd)
        items.append({
            "snap": nm,
            "ts": meta.get("ts") or int(os.path.getmtime(sd)),
            "time": meta.get("time") or time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(sd))),
            "reason": meta.get("reason") or "save",
            "counts": cnt,
            "hash": meta.get("hash") or "",
            "size": size,
        })
    keep = _read_json(os.path.join(h, "_keep.json"), {}) or {}
    return {"ok": True, "id": pid, "keep": int(keep.get("keep") or HIST_KEEP_DEFAULT), "dir": h, "items": items}


def proj_snapshot(pid, reason="save", force=False):
    """给项目打一份整项目快照；内容与最新一份相同则跳过（force=True 强制）。"""
    d = proj_dir(pid)
    h = proj_hist_dir(pid, create=True)
    if not d or not h:
        return {"ok": False, "error": "项目不存在"}
    hv = _hist_hash(d)
    if not hv:
        return {"ok": False, "error": "读取项目文件失败"}
    # 与最新一份比较
    try:
        olds = sorted([x for x in os.listdir(h) if os.path.isdir(os.path.join(h, x))], reverse=True)
    except OSError:
        olds = []
    if olds and not force:
        last = _read_json(os.path.join(h, olds[0], "meta.json"), {}) or {}
        if last.get("hash") == hv:
            return {"ok": True, "skipped": True, "snap": olds[0], "reason": "内容未变，不新增快照"}
    now = time.time()
    snap = time.strftime("%Y%m%d_%H%M%S", time.localtime(now)) + "_%03d" % int((now % 1) * 1000)
    sd = os.path.join(h, snap)
    try:
        os.makedirs(sd, exist_ok=True)
        for part in HIST_PARTS:
            src = os.path.join(d, part + ".json")
            if os.path.isfile(src):
                shutil.copyfile(src, os.path.join(sd, part + ".json"))
        _write_json(os.path.join(sd, "meta.json"), {
            "ts": int(now),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
            "reason": str(reason or "save"),
            "hash": hv,
            "counts": _hist_counts(sd),
        })
    except Exception as e:
        return {"ok": False, "error": str(e)}
    pr = proj_hist_prune(pid)
    return {"ok": True, "snap": snap, "reason": reason, "pruned": pr.get("removed", [])}


def proj_hist_prune(pid, keep=None):
    """只保留最近 keep 份快照。"""
    h = proj_hist_dir(pid, create=True)
    if not h:
        return {"ok": False, "error": "项目不存在"}
    if keep is None:
        cfg = _read_json(os.path.join(h, "_keep.json"), {}) or {}
        keep = int(cfg.get("keep") or HIST_KEEP_DEFAULT)
    keep = max(1, min(500, int(keep)))
    try:
        dirs = sorted([x for x in os.listdir(h) if os.path.isdir(os.path.join(h, x))], reverse=True)
    except OSError:
        dirs = []
    removed = []
    for nm in dirs[keep:]:
        try:
            shutil.rmtree(os.path.join(h, nm))
            removed.append(nm)
        except OSError:
            pass
    _write_json(os.path.join(h, "_keep.json"), {"keep": keep})
    return {"ok": True, "keep": keep, "removed": removed, "total": min(len(dirs), keep)}


def proj_hist_restore(pid, snap):
    """恢复到某一快照（恢复前先把当前状态也存一份，保证可再回退）。"""
    h = proj_hist_dir(pid)
    d = proj_dir(pid)
    if not h or not d:
        return {"ok": False, "error": "项目不存在"}
    nm = os.path.basename(snap or "")
    sd = os.path.join(h, nm)
    if not nm or not os.path.isdir(sd):
        return {"ok": False, "error": "快照不存在：" + str(snap)}
    pre = proj_snapshot(pid, "before-restore", force=True)
    try:
        for part in HIST_PARTS:
            src = os.path.join(sd, part + ".json")
            if os.path.isfile(src):
                dst = os.path.join(d, part + ".json")
                _backup_before_write(dst)
                shutil.copyfile(src, dst)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "snap": nm, "before": pre.get("snap")}


def proj_hist_delete(pid, snap):
    h = proj_hist_dir(pid)
    if not h:
        return {"ok": False, "error": "项目不存在"}
    nm = os.path.basename(snap or "")
    sd = os.path.join(h, nm)
    if not nm or not os.path.isdir(sd):
        return {"ok": False, "error": "快照不存在"}
    try:
        shutil.rmtree(sd)
        return {"ok": True, "snap": nm}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def proj_hist_api(rec):
    """POST /api/projhistory 的统一入口。"""
    pid = (rec.get("id") or "").strip()
    if not pid:
        return {"ok": False, "error": "缺少项目 id"}
    act = (rec.get("action") or "list").strip()
    if act == "list":
        return proj_hist_list(pid)
    if act == "snapshot":
        return proj_snapshot(pid, rec.get("reason") or "manual", force=bool(rec.get("force")))
    if act == "restore":
        return proj_hist_restore(pid, rec.get("snap") or "")
    if act == "delete":
        return proj_hist_delete(pid, rec.get("snap") or "")
    if act == "prune":
        return proj_hist_prune(pid, rec.get("keep"))
    return {"ok": False, "error": "未知 action：" + act}


def proj_backups(pid):
    """列出各 part 的 .bak_* 备份（新的在前）。"""
    d = proj_dir(pid)
    if not d or not os.path.isdir(d):
        return None
    out = {}
    for part in _PROJ_PARTS:
        base = part + ".json.bak_"
        try:
            fs = [f for f in os.listdir(d) if f.startswith(base)]
        except OSError:
            fs = []
        fs.sort(reverse=True)
        lst = []
        for f in fs[:10]:
            fp = os.path.join(d, f)
            try:
                st = os.stat(fp)
                lst.append({"file": f, "size": st.st_size,
                            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime))})
            except OSError:
                pass
        if lst:
            out[part] = lst
    return {"ok": True, "id": pid, "backups": out}


def proj_restore(pid, part, filename):
    """把某个备份文件回滚成当前 part（回滚前先备份当前文件）。"""
    d = proj_dir(pid)
    if not d or not os.path.isdir(d):
        return {"ok": False, "error": "项目不存在"}
    if part not in _PROJ_PARTS:
        return {"ok": False, "error": "part 不合法"}
    fn = os.path.basename(filename or "")
    if not fn.startswith(part + ".json.bak_"):
        return {"ok": False, "error": "不是该 part 的备份文件"}
    src = os.path.join(d, fn)
    if not os.path.isfile(src):
        return {"ok": False, "error": "备份不存在：" + fn}
    dst = os.path.join(d, part + ".json")
    try:
        _backup_before_write(dst)
        shutil.copyfile(src, dst)
        return {"ok": True, "part": part, "file": fn}
    except Exception as e:
        return {"ok": False, "error": str(e)}

def proj_create(pid, name):
    d = proj_dir(pid)
    if not d or os.path.abspath(d) == os.path.abspath(PROJECTS_DIR):
        raise ValueError("项目 id 不合法（只允许字母数字 - _）")
    if os.path.isdir(d):
        raise ValueError("项目已存在：" + pid)
    os.makedirs(os.path.join(d, "model"), exist_ok=True)
    os.makedirs(os.path.join(d, "data"), exist_ok=True)
    _write_json(os.path.join(d, "project.json"), {
        "id": pid, "name": name or pid, "model": "", "rest": {},
        "created": int(time.time()), "updated": int(time.time()),
    })
    _write_json(os.path.join(d, "bindings.json"), {"items": []})
    _write_json(os.path.join(d, "panels.json"), {"items": []})
    _write_json(os.path.join(d, "pipes.json"), {"items": []})
    return proj_load(pid)


def glb_nodes(path):
    """只解析 GLB 的 JSON chunk，拿到节点树（不加载几何，秒回）。"""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        raise ValueError("不是 GLB 文件")
    pos = 12
    js = None
    while pos < len(data):
        clen, ctype = struct.unpack("<I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + clen]
        if ctype == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
            break
        pos += 8 + clen + ((4 - clen % 4) % 4)
    if js is None:
        raise ValueError("GLB 里没有 JSON chunk")
    nodes = js.get("nodes", [])
    meshes = js.get("meshes", [])
    out = []
    for i, n in enumerate(nodes):
        nm = n.get("name", "")
        mi = n.get("mesh")
        prim = meshes[mi]["primitives"] if (mi is not None and mi < len(meshes)) else []
        tris = 0
        for p in prim:
            acc = p.get("indices")
            if acc is not None and acc < len(js.get("accessors", [])):
                tris += js["accessors"][acc].get("count", 0) // 3
        out.append({"name": nm, "mesh": mi is not None, "tris": tris,
                    "children": len(n.get("children", []))})
    return out



def glb_check(path):
    """体检 GLB：材质数、贴图数、灯光强度、没有材质的三角面占比（全白模型的真凶）。"""
    import struct
    with open(path, "rb") as f:
        data = f.read()
    if data[:4] != b"glTF":
        raise ValueError("不是 GLB 文件")
    pos, js = 12, None
    while pos < len(data):
        clen, ctype = struct.unpack("<I4s", data[pos:pos + 8])
        chunk = data[pos + 8:pos + 8 + clen]
        if ctype == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
            break
        pos += 8 + clen + ((4 - clen % 4) % 4)
    if js is None:
        raise ValueError("GLB 里没有 JSON chunk")
    meshes = js.get("meshes", [])
    accs = js.get("accessors", [])
    mats = js.get("materials", [])
    lights = (js.get("extensions", {}).get("KHR_lights_punctual", {}) or {}).get("lights", [])

    total_tris = 0
    no_mat_tris = 0
    no_mat_prims = 0
    prims = 0
    for m in meshes:
        for p in m.get("primitives", []):
            prims += 1
            t = 0
            ai = p.get("indices")
            if ai is not None and ai < len(accs):
                t = accs[ai].get("count", 0) // 3
            total_tris += t
            if "material" not in p:
                no_mat_tris += t
                no_mat_prims += 1

    white_mats = 0
    for m in mats:
        pbr = m.get("pbrMetallicRoughness", {}) or {}
        bc = pbr.get("baseColorFactor", [1, 1, 1, 1])
        if len(bc) >= 3 and bc[0] > 0.9 and bc[1] > 0.9 and bc[2] > 0.9:
            white_mats += 1

    hints = []
    if not mats:
        hints.append("这份 GLB 里一个材质都没有：导出时把「材质」设为导出（不要选「无」），"
                     "或者在 Blender 里给对象指定材质。")
    elif no_mat_prims:
        hints.append("有 %d 个图元（约 %d 个三角面）没有指定材质，会显示成纯白。"
                     % (no_mat_prims, no_mat_tris))
    if white_mats and white_mats == len(mats):
        hints.append("所有材质底色都接近纯白（baseColor≈1），加上本页的补光会直接顶成白色。")
    hot = [l for l in lights if (l.get("intensity") or 0) > 50]
    if hot:
        hints.append("模型自带灯光强度 %.0f 量级（Blender 导出未用 RAW），已由页面自动剥离；"
                     "重新导出时建议用 RAW，或干脆不导灯光。"
                     % max((l.get("intensity") or 0) for l in hot))

    return {
        "nodes": len(js.get("nodes", [])),
        "meshes": len(meshes),
        "primitives": prims,
        "materials": len(mats),
        "whiteMaterials": white_mats,
        "images": len(js.get("images", [])),
        "lights": [{"name": l.get("name"), "type": l.get("type"),
                    "intensity": round(l.get("intensity") or 0, 2)} for l in lights],
        "triangles": total_tris,
        "noMaterialPrims": no_mat_prims,
        "noMaterialTris": no_mat_tris,
        "hints": hints,
    }



def _decode_any(raw):
    """按 BOM 判断编码；没有 BOM 时依次试 utf-8 / gbk（不会再盲试 utf-16）。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16"), "utf-16(BOM)"
    if raw[:3] == b"\xef\xbb\xbf":
        return raw.decode("utf-8-sig"), "utf-8-sig(BOM)"
    for enc in ("utf-8", "gbk"):
        try:
            t = raw.decode(enc)
            if t.count("\ufffd") < 5:
                return t, enc
        except Exception:
            continue
    # 无 BOM 的 UTF-16：ASCII 与中文交替出现 \x00，用出现频率粗略识别
    if raw.count(b"\x00") > len(raw) * 0.2:
        try:
            return raw.decode("utf-16"), "utf-16(no BOM)"
        except Exception:
            pass
    return raw.decode("utf-8", "replace"), "utf-8(replace)"


def parse_vartable(raw):
    """把上传内容解析成 {systems, vars:[{n,c,t,s,w}]}。返回 (表, 说明)。"""
    text, enc = _decode_any(raw)
    lines = text.splitlines()
    if not lines:
        raise ValueError("文件是空的")

    # ---- A. WinCC 导出：找到 DmTag 段和表头 ----
    tag_i = None
    for i, ln in enumerate(lines):
        if ln.startswith("DmTag\t") or ln.startswith("DmTag,"):
            tag_i = i
            break
    if tag_i is not None:
        sep = "\t" if "\t" in lines[tag_i] else ","
        hdr = None
        for i in range(tag_i + 1, min(tag_i + 6, len(lines))):
            cols = lines[i].split(sep)
            if "名称" in cols and ("读授权" in cols or "写授权" in cols):
                hdr = cols
                start = i + 1
                break
        if hdr is None:
            raise ValueError("找到 DmTag 段，但没找到含『名称 / 读授权』的表头行")
        I = {n.strip(): k for k, n in enumerate(hdr)}
        recs = []
        for ln in lines[start:]:
            c = ln.split(sep)
            if len(c) <= max(I.get("名称", 0), I.get("读授权", 0)):
                continue
            name = c[I["名称"]].strip() if "名称" in I else ""
            if not name:
                continue
            readable = c[I["读授权"]].strip() not in ("", "0") if "读授权" in I else True
            writable = c[I["写授权"]].strip() not in ("", "0") if "写授权" in I else False
            if not readable:
                continue
            recs.append({
                "n": name,
                "c": c[I["注释"]].strip() if "注释" in I else "",
                "t": c[I["数据类型"]].strip() if "数据类型" in I else "",
                "g": c[I["组"]].strip() if "组" in I else "",
                "p": c[I["连接"]].strip() if "连接" in I else "",
                "w": 1 if writable else 0,
            })
        return _finish_vartable(recs), "WinCC 导出（%s，%d 行原始记录）" % (enc, len(lines))

    # ---- B. 简单 CSV ----
    import csv as _csv
    rows = list(_csv.reader(io.StringIO(text)))
    if not rows:
        raise ValueError("读不到任何行")
    hdr = [h.strip().lower() for h in rows[0]]
    def col(*names):
        for nm in names:
            for k, h in enumerate(hdr):
                if nm in h:
                    return k
        return None
    c_name = col("变量名", "名称", "name", "tag")
    if c_name is None:
        raise ValueError("CSV 表头里找不到『变量名』列（也可以直接上传 WinCC 导出的 .txt）")
    c_cmt = col("注释", "说明", "comment", "desc")
    c_wr = col("可写", "写授权", "write", "writable", "rw")
    recs = []
    for r in rows[1:]:
        if len(r) <= c_name:
            continue
        name = r[c_name].strip()
        if not name or name.startswith("#"):
            continue
        w = 0
        if c_wr is not None and len(r) > c_wr:
            v = r[c_wr].strip().lower()
            w = 1 if v in ("1", "true", "yes", "y", "可写", "是") else 0
        recs.append({"n": name, "c": r[c_cmt].strip() if (c_cmt is not None and len(r) > c_cmt) else "",
                     "t": "", "g": "", "w": w})
    return _finish_vartable(recs), "简单 CSV（%d 行）" % (len(rows) - 1)


def _finish_vartable(recs):
    """补系统名（组优先，空组按名称前缀多数票），排序，产出最终结构。"""
    from collections import Counter, defaultdict
    if not recs:
        raise ValueError("没有解析出任何可用变量（是不是所有变量的『读授权』都是 0？）")
    by_pre = defaultdict(Counter)
    for r in recs:
        if r.get("g"):
            by_pre[r["n"].split("_")[0]][r["g"]] += 1
    pre_map = {}
    for pre, cnt in by_pre.items():
        g, n = cnt.most_common(1)[0]
        if n >= 0.5 * sum(cnt.values()):
            pre_map[pre] = g
    for r in recs:
        r["s"] = r["g"] or pre_map.get(r["n"].split("_")[0], "其他/内部")
    cnt = Counter(r["s"] for r in recs)
    slim = [{"n": r["n"], "c": r.get("c", ""), "t": r.get("t", ""), "s": r["s"], "w": r["w"]} for r in recs]
    slim.sort(key=lambda r: (r["s"], r["n"]))
    return {"systems": [k for k, _ in cnt.most_common()], "vars": slim}



def vars_search(q, limit=300):
    vt = _read_json(VARTABLE, {"vars": []})
    vs = vt.get("vars", [])
    q = (q or "").strip().lower()
    if q:
        vs = [v for v in vs if q in v.get("n", "").lower() or q in (v.get("c") or "").lower()]
    return vs[:limit]


def _norm(s):
    import re as _re
    return _re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", (s or "").lower())


def autobind(pid):
    """按名绑定：精确名 → 归一化名 → 前缀 → 包含关系；未命中的给候选。"""
    p = proj_load(pid)
    if not p:
        raise ValueError("项目不存在")
    model = p["project"].get("model", "")
    mp = os.path.join(proj_dir(pid), "model", model)
    if not model or not os.path.isfile(mp):
        raise ValueError("项目还没有模型")

    vt = _read_json(VARTABLE, {"vars": []})
    vs = vt.get("vars", [])
    by_name = {}
    by_norm = {}
    for v in vs:
        n = v.get("n", "")
        if not n:
            continue
        by_name.setdefault(n, v)
        by_norm.setdefault(_norm(n), v)
    # 归一化后的变量名列表（用于包含匹配）
    norm_pairs = [(k, v) for k, v in by_norm.items() if len(k) >= 4]

    nodes = glb_nodes(mp)
    items, unmatched = [], []
    for nd in nodes:
        nm = nd["name"]
        if not nm or nm.startswith(GLYPH_PREFIX) or nm.startswith("_"):
            continue
        hit = None
        why = ""
        if nm in by_name:
            hit, why = by_name[nm], "精确"
        elif _norm(nm) in by_norm:
            hit, why = by_norm[_norm(nm)], "归一"
        else:
            nn = _norm(nm)
            best = None
            for vn in by_name:
                if vn and nm.startswith(vn) and len(vn) >= 4:
                    if best is None or len(vn) > len(best):
                        best = vn
            if best:
                hit, why = by_name[best], "前缀"
            else:
                for nk, vv in norm_pairs:
                    if nk in nn:
                        if hit is None or len(nk) > len(_norm(hit["n"])):
                            hit, why = vv, "包含"
        if hit:
            items.append({"obj": nm, "type": "color", "tag": hit["n"],
                          "min": 1, "max": 1, "color": "#ff2d2d", "how": why})
            continue
        # 没匹配上：给前 3 个候选
        nn = _norm(nm)
        cands = []
        for nk, vv in norm_pairs:
            score = 0
            if nk[:6] and nk[:6] in nn:
                score += 6
            if nn and nn[:6] in nk:
                score += 4
            if score:
                cands.append((score, vv["n"]))
        cands.sort(reverse=True)
        cands = [c[1] for c in cands[:3]]
        if nd.get("mesh"):
            unmatched.append({"obj": nm, "tris": nd.get("tris", 0), "cands": cands})

    return {"matched": items, "unmatched": unmatched[:400], "total_nodes": len(nodes)}


class Handler(SimpleHTTPRequestHandler):
    server_version = "FillViewer/1.0"

    def __init__(self, *a, **kw):
        super().__init__(*a, directory=ROOT, **kw)

    # ---------------- GET ----------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- 严格请求体解析（P2-4）与同源校验（P0-3）----
    def _body_json(self, limit=8 * 1024 * 1024):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0:
            return {}
        if n > limit:
            raise ValueError("请求体过大")
        raw = self.rfile.read(n)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("请求体不是合法 UTF-8")
        try:
            obj = json.loads(text or "{}")
        except Exception:
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(obj, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return obj

    def _same_origin(self):
        o = (self.headers.get("Origin") or "").strip()
        if not o:
            return True
        host = (self.headers.get("Host") or "").strip()
        allow = {"http://" + host, "https://" + host}
        try:
            for x in (sec("allowOrigins") or []):
                if isinstance(x, str) and x.strip():
                    allow.add(x.strip().rstrip("/"))
        except Exception:
            pass
        return o.rstrip("/") in allow

    def do_GET(self):
        if self.path.startswith("/api/runtime"):
            rt = runtime_cfg()
            self._json({
                "ok": True,
                "runtime": rt,
                "autostart": {"enabled": autostart_enabled(),
                              "path": autostart_path()},
                "projects": [p.get("id") for p in proj_list()],
                "models": list_models(),
                "service": {"task": SERVICE_TASK, "exists": service_task_exists(),
                            "cmds": list(service_cmds())},
                "exe": exe_info(),
                "configFile": CONFIG_FILE,
            })
            return
        if self.path.startswith("/api/config"):
            # P0-2：永不回显 restPass，只告诉前端"是否已配置"
            _pw = _cfg.get("restPass") or REST_PASS or ""
            self._json({
                "restBase": cfg_rest_base(),
                "restUser": _cfg.get("restUser") or REST_USER,
                "hasPass": bool(_pw),
                "passLen": len(_pw),
                "fromFile": os.path.isfile(CONFIG_FILE),
                "file": CONFIG_FILE,
                "state": rest_state(),
                "security": {"readOnly": bool(sec("readOnly")),
                             "requireWriteToken": bool(sec("requireWriteToken")),
                             "maxUploadMB": int(sec("maxUploadMB") or 256),
                             "restInsecure": bool(sec("restInsecure")),
                             "restSkipHostname": bool(sec("restSkipHostname")),
                             "restCaFile": sec("restCaFile") or ""},
            })
            return
        if self.path.startswith("/api/write/token"):
            # 一次性写值确认 token（P0-3）
            self._json({"ok": True, "t": write_token_new(self.client_address[0])})
            return
        if self.path.startswith("/api/rest/probe"):
            # 供配置台「测试连接」用：真正打一次 REST
            try:
                r = rest_probe()
            except Exception as e:
                r = {"ok": False, "stage": "exception", "err": str(e), "ms": 0}
            self._json(r)
            return
        if self.path.startswith("/api/live"):
            try:
                extra = []
                if "extra=" in self.path:
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    raw = (qs.get("extra") or [""])[0]
                    extra = [x.strip() for x in raw.split(",") if x.strip()]
                ttl = None
                fresh = False
                try:
                    from urllib.parse import urlparse, parse_qs
                    qs = parse_qs(urlparse(self.path).query)
                    if "ttl" in qs:
                        ttl = float(qs["ttl"][0]) / 1000.0
                    fresh = (qs.get("fresh") or ["0"])[0] in ("1", "true", "yes")
                except Exception:
                    pass
                self._json(live_data(extra, ttl=ttl, fresh=fresh))
            except Exception as e:
                self._json({"ts": 0, "src": "error", "err": str(e), "values": {}})
            return
        if self.path.startswith("/api/resttest"):
            tags = read_tags()
            vals, tpl = rest_read_all(tags)
            self._json({"ok": vals is not None, "path": tpl, "tags": tags, "values": vals})
            return
        if self.path.startswith("/api/vt/import/sample"):
            sample = "变量名,注释,可写\nDemo_AMDL1_BJ21,卧式搅拌机A电机电流,1\nDemo_NM1LW1_PJ1,浓密机泥层高度,0\n"
            body = sample.encode("utf-8-sig")
            self.send_response(200)
            self.send_header("Content-Type", "text/csv; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="vartable_sample.csv"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/vt"):
            body = json.dumps(_read_json(VARTABLE_FILE, {"systems": [], "vars": []}),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/arch"):
            body = json.dumps(_read_json(ARCHIVES_FILE, {"archives": [], "vars": []}),
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/api/read"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            names = [x for x in (q.get("n") or []) if x]
            one = (q.get("one") or [None])[0]
            if one:
                self._json(rest_read_one(one))
            else:
                self._json({"items": rest_read_many(names)})
            return
        if self.path.startswith("/api/projects"):
            self._json({"projects": proj_list()})
            return
        if self.path.startswith("/api/glbcheck"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("id") or [""])[0]
            pj = proj_load(pid)
            if not pj or not (pj.get("project") or {}).get("model"):
                return self._json({"error": "项目没有模型"}, 400)
            path = os.path.join(proj_dir(pid), "model", pj["project"]["model"])
            if not os.path.exists(path):
                return self._json({"error": "模型文件不存在"}, 404)
            try:
                return self._json(glb_check(path))
            except Exception as e:
                return self._json({"error": str(e)}, 500)
        if self.path.startswith("/api/projhistory"):
            from urllib.parse import urlparse, parse_qs
            pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0].strip()
            try:
                self._json(proj_hist_list(pid))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/backups"):
            _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (_q.get("id") or [""])[0]
            r = proj_backups(pid)
            if r is None:
                self._json({"ok": False, "error": "项目不存在"}, 404)
            else:
                self._json(r, 200)
            return

        if self.path.startswith("/api/project/validate"):
            _q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (_q.get("id") or [""])[0]
            r = proj_validate(pid)
            if r is None:
                self._json({"ok": False, "error": "项目不存在"}, 404)
            else:
                self._json(r)
            return

        if self.path.startswith("/api/project/objects"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("id") or [""])[0]
            p = proj_load(pid)
            if not p:
                self._json({"error": "no project"}, 404)
                return
            mp = os.path.join(proj_dir(pid), "model", p["project"].get("model", ""))
            try:
                self._json({"nodes": glb_nodes(mp), "model": p["project"].get("model", "")})
            except Exception as e:
                self._json({"error": str(e)}, 500)
            return
        if self.path.startswith("/api/project"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("id") or [""])[0]
            p = proj_load(pid)
            if not p:
                self._json({"error": "no project"}, 404)
                return
            self._json(p)
            return
        if self.path.startswith("/api/vars"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            q = (qs.get("q") or [""])[0]
            lim = int((qs.get("limit") or ["300"])[0])
            self._json({"vars": vars_search(q, lim)})
            return
        if self.path.startswith("/api/behaviors"):
            self._json(_read_json(os.path.join(DATA_DIR, "behaviors.json"),
                                  {"behaviors": []}))
            return
        if self.path.startswith("/api/data"):
            body = json.dumps(_read_json(DATA_FILE, {"ts": 0}), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    # ---------------- POST ----------------
    def do_POST(self):
        if self.path.startswith("/api/config"):
            if not self._same_origin():
                return self._json({"ok": False, "error": "跨站请求被拒绝"}, 403)
            try:
                b = self._body_json()
                cur = dict(_cfg)
                touched_rest = False
                for k in ("restBase", "restUser", "restPass"):
                    if k in b and b[k] is not None:
                        cur[k] = str(b[k])
                        touched_rest = True
                for k in ("restInsecure", "restSkipHostname", "restCaFile",
                          "readOnly", "requireWriteToken", "maxUploadMB",
                          "writeRatePer10s", "logMaxMB"):
                    if k in b and b[k] is not None:
                        cur[k] = b[k]
                if isinstance(b.get("allowOrigins"), list):
                    cur["allowOrigins"] = [str(x) for x in b["allowOrigins"]]
                _write_json(CONFIG_FILE, cur)
                reload_cfg()
                check = rest_probe() if touched_rest else None
                self._json({"ok": True, "restBase": cfg_rest_base(),
                            "hasPass": bool(_cfg.get("restPass") or REST_PASS),
                            "check": check, "state": rest_state()})
            except ValueError as e:
                self._json({"ok": False, "error": str(e)}, 400)
            except Exception as e:
                print("[config] 保存失败：%s" % e, flush=True)
                self._json({"ok": False, "error": "保存失败（详见服务端日志）"}, 500)
            return
        if self.path.startswith("/api/rest/trust"):
            # 一键信任当前 REST 服务端的自签证书（校验证书链、跳过主机名）
            if not self._same_origin():
                return self._json({"ok": False, "error": "跨站请求被拒绝"}, 403)
            try:
                info = trust_rest_cert()
                _write_json(CONFIG_FILE, dict(_cfg))
                reload_cfg()
                self._json({"ok": True, "cert": info, "check": rest_probe(),
                            "security": {"restCaFile": sec("restCaFile") or "",
                                         "restSkipHostname": bool(sec("restSkipHostname")),
                                         "restInsecure": bool(sec("restInsecure"))}})
            except Exception as e:
                print("[rest] 信任证书失败：%s" % e, flush=True)
                self._json({"ok": False, "error": "抓取证书失败（详见服务端日志）"}, 500)
            return
        if self.path.startswith("/api/runtime"):
            try:
                n = int(self.headers.get("Content-Length", 0))
                rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
                rt = save_runtime(rec)
                self._json({"ok": True, "runtime": rt,
                            "autostart": {"enabled": autostart_enabled(),
                                          "path": autostart_path()}})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/build_exe"):
            try:
                exe, size = build_exe()
                self._json({"ok": True, "exe": {"path": exe, "size": size}})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
            return
        if self.path.startswith("/api/history"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            self._json(rest_history(rec))
            return
        if self.path.startswith("/api/vt/import"):
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            try:
                table, how = parse_vartable(raw)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            old = _read_json(VARTABLE, None)
            if old:
                bak = VARTABLE + ".bak"
                try:
                    _write_json(bak, old)
                except Exception:
                    pass
            _write_json(VARTABLE, table)
            cnt = {}
            for v in table["vars"]:
                cnt[v["s"]] = cnt.get(v["s"], 0) + 1
            self._json({
                "ok": True, "how": how,
                "total": len(table["vars"]),
                "writable": sum(1 for v in table["vars"] if v.get("w")),
                "systems": [{"name": k, "count": cnt[k]} for k in table["systems"] if k in cnt],
                "file": VARTABLE,
            })
            return
        if self.path.startswith("/api/write"):
            # P0-3：同源校验 → 只读/白名单 → 一次性 token → 速率限制 → 审计
            if not self._same_origin():
                return self._json({"ok": False, "error": "跨站请求被拒绝"}, 403)
            try:
                rec = self._body_json()
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            name, val = str(rec.get("n", "")).strip(), rec.get("v")
            ip = self.client_address[0]
            if not name:
                return self._json({"ok": False, "error": "缺少变量名"}, 400)
            okw, why = write_permitted(name)
            if not okw:
                write_audit(ip, name, val, None, "denied:" + why)
                msg = {"readOnly": "服务端处于只读模式，已拒绝写值",
                       "vartable-readonly": "该变量在变量表里标记为不可写",
                       "not-in-vartable": "该变量不在变量表里，服务端拒绝写值"}.get(why, "不允许写该变量")
                return self._json({"ok": False, "error": msg, "reason": why}, 403)
            if sec("requireWriteToken"):
                if not write_token_ok(str(rec.get("t", "")), ip):
                    return self._json({"ok": False, "error": "写值确认已过期，请刷新页面重试",
                                       "reason": "token"}, 403)
            if not write_rate_ok(ip):
                write_audit(ip, name, val, None, "denied:rate")
                return self._json({"ok": False, "error": "写值过于频繁，请稍后再试"}, 429)
            old = None
            try:
                old = rest_read_one(name).get("value")
            except Exception:
                pass
            r = rest_write_one(name, val)
            ok = (r.get("http") == 200 and r.get("errorcode") in (None, 0) and not r.get("err"))
            write_audit(ip, name, val, old, ("ok" if ok else "fail:%s" % (r.get("err") or r.get("errorcode"))))
            self._json(r)
            return
        if self.path.startswith("/api/projects"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            try:
                self._json({"ok": True, "project": proj_create(rec.get("id", ""), rec.get("name", ""))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/project/upload"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            n = int(self.headers.get("Content-Length", 0))
            try:
                d = proj_dir(pid)
                if not os.path.isdir(d):
                    raise ValueError("项目不存在")
                # P0-4：严格文件名白名单 + 大小/磁盘余量
                name = safe_glb_name(name)
                maxb = int(sec("maxUploadMB") or 256) * 1024 * 1024
                if n <= 0:
                    raise ValueError("没有收到文件内容")
                if n > maxb:
                    raise ValueError("文件过大：%.1f MB，上限 %d MB"
                                     % (n / 1048576.0, int(sec("maxUploadMB") or 256)))
                mdir = os.path.join(d, "model")
                os.makedirs(mdir, exist_ok=True)
                dst = os.path.join(mdir, name)
                if not inside(mdir, dst):
                    raise ValueError("目标路径越界")
                try:
                    free = shutil.disk_usage(mdir).free
                    if free < n * 2:
                        raise ValueError("磁盘余量不足：剩余 %.1f MB" % (free / 1048576.0))
                except OSError:
                    pass
                left = n
                with open(dst, "wb") as f:
                    while left > 0:
                        buf = self.rfile.read(min(1024 * 1024, left))
                        if not buf:
                            break
                        f.write(buf)
                        left -= len(buf)
                pj = _read_json(os.path.join(d, "project.json"), {})
                pj["model"] = name
                pj["updated"] = int(time.time())
                _write_json(os.path.join(d, "project.json"), pj)
                self._json({"ok": True, "model": name, "size": os.path.getsize(dst),
                            "nodes": len(glb_nodes(dst))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/project/model"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            pid, src = rec.get("id", ""), rec.get("src", "")
            try:
                d = proj_dir(pid)
                if not os.path.isdir(d):
                    raise ValueError("项目不存在")
                if not os.path.isfile(src):
                    raise ValueError("源文件不存在")
                # P0-4：只允许从受信模型库（models/ 或任何 projects/*/model）复制
                base = safe_glb_name(os.path.basename(src))
                trusted = [os.path.join(ROOT, "models")]
                pdir = os.path.join(ROOT, "projects")
                if os.path.isdir(pdir):
                    for _p in os.listdir(pdir):
                        trusted.append(os.path.join(pdir, _p, "model"))
                if not any(inside(t, src) for t in trusted):
                    raise ValueError("只能从模型库（models/ 或已有项目的 model/）复制模型")
                if os.path.getsize(src) > int(sec("maxUploadMB") or 256) * 1024 * 1024:
                    raise ValueError("模型文件超过上传上限")
                mdir = os.path.join(d, "model")
                os.makedirs(mdir, exist_ok=True)
                dst = os.path.join(mdir, base)
                if not inside(mdir, dst):
                    raise ValueError("目标路径越界")
                if os.path.realpath(src) == os.path.realpath(dst):
                    # 源与目标同一个文件：直接返回成功，不重复复制
                    self._json({"ok": True, "model": base, "size": os.path.getsize(dst),
                                "nodes": len(glb_nodes(dst)), "note": "同一个文件，未复制"})
                    return
                shutil.copy2(src, dst)
                pj = _read_json(os.path.join(d, "project.json"), {})
                pj["model"] = base
                pj["updated"] = int(time.time())
                _write_json(os.path.join(d, "project.json"), pj)
                self._json({"ok": True, "model": base, "size": os.path.getsize(dst),
                            "nodes": len(glb_nodes(dst))})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/project/autobind"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            try:
                self._json(autobind(rec.get("id", "")))
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 400)
            return
        if self.path.startswith("/api/project"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            pid = (rec.get("id") or "").strip()
            if not pid:
                from urllib.parse import urlparse, parse_qs
                pid = (parse_qs(urlparse(self.path).query).get("id") or [""])[0].strip()
            if not pid:
                self._json({"ok": False, "error": "缺少项目 id（PATCH: body 或 ?id=）"}, 400)
                return
            rec["id"] = pid
            d = proj_dir(pid)
            if not os.path.isdir(d):
                self._json({"ok": False, "error": "项目不存在"}, 404)
                return
            for part in ("bindings", "panels", "pipes", "panelTpls", "pipeTpls"):
                if part in rec:
                    _fp = os.path.join(d, part + ".json")
                    _backup_before_write(_fp)
                    _write_json(_fp, rec[part])
            if "project" in rec:
                pj = _read_json(os.path.join(d, "project.json"), {})
                pj.update(rec["project"] or {})
                pj["updated"] = int(time.time())
                _write_json(os.path.join(d, "project.json"), pj)
            snap = None
            try:
                snap = proj_snapshot(pid, "save")
            except Exception:
                snap = None
            self._json({"ok": True, "snapshot": snap})
            return

        if self.path.startswith("/api/projhistory"):
            n = int(self.headers.get("Content-Length", 0))
            try:
                rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            except Exception:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
            try:
                r = proj_hist_api(rec)
            except Exception as e:
                r = {"ok": False, "error": str(e)}
            self._json(r, 200 if r.get("ok") else 400)
            return
        if self.path.startswith("/api/restore"):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n) if n else b"{}"
            try:
                rec = json.loads(body.decode("utf-8"))
            except Exception:
                self._json({"ok": False, "error": "bad json"}, 400)
                return
            r = proj_restore(rec.get("id") or "", rec.get("part") or "", rec.get("file") or "")
            self._json(r, 200 if r.get("ok") else 400)
            return

        if self.path.startswith("/api/behaviors"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            items = rec.get("behaviors") or []
            _write_json(os.path.join(DATA_DIR, "behaviors.json"), {"behaviors": items})
            self._json({"ok": True, "count": len(items)})
            return
        if self.path.startswith("/api/tags"):
            # 页面把所有看板用到的变量名汇总过来，写成 tags.txt 给 WinCC 全局脚本读
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            tags = [str(t).strip() for t in rec.get("tags", []) if str(t).strip()]
            seen, uniq = set(), []
            for t in tags:
                if t not in seen:
                    seen.add(t)
                    uniq.append(t)
            # WinCC 的 FileSystemObject 用 TristateTrue(Unicode) 读，所以写 UTF-16 LE
            with open(os.path.join(DATA_DIR, "tags.txt"), "w", encoding="utf-16") as f:
                f.write("\n".join(uniq))
            body = json.dumps({"ok": True, "count": len(uniq)}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path.startswith("/api/labels"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            items = rec.get("labels", [])
            _write_json(os.path.join(DATA_DIR, "labels.json"), items)
            body = json.dumps({"ok": True, "count": len(items)}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
            return
        if self.path.startswith("/api/annotate"):
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
            mp = _read_json(MAP_FILE, {})
            if not isinstance(mp, dict):
                mp = {}
            if rec.get("obj"):
                mp[rec["obj"]] = {"tag": rec.get("tag", ""), "kind": rec.get("kind", ""), "note": rec.get("note", "")}
                _write_json(MAP_FILE, mp)
            body = json.dumps({"ok": True, "count": len(mp)}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if not self.path.startswith("/api/click"):
            self.send_error(404)
            return
        try:
            n = int(self.headers.get("Content-Length", 0))
            rec = json.loads(self.rfile.read(n).decode("utf-8", "ignore") or "{}")
        except Exception:
            self.send_error(400)
            return
        rec["ts"] = time.time()
        items = _read_json(CLICK_FILE, [])
        if not isinstance(items, list):
            items = []
        items.append(rec)
        _write_json(CLICK_FILE, items[-50:])
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---------------- 其它 ----------------
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt, *args):
        pass


def _rotate_log(path, max_mb):
    """P2-1：超过上限就把 server.log 滚成 .1 ... .5"""
    try:
        if not os.path.isfile(path):
            return
        if os.path.getsize(path) < max_mb * 1024 * 1024:
            return
        for i in range(4, 0, -1):
            src = "%s.%d" % (path, i)
            if os.path.isfile(src):
                try:
                    os.replace(src, "%s.%d" % (path, i + 1))
                except OSError:
                    pass
        os.replace(path, path + ".1")
    except Exception:
        pass


def _port_owner(port):
    """P1-3：端口被占用时，找出占用进程信息（Windows 用 netstat/tasklist）。"""
    try:
        import subprocess
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                             timeout=8, errors="ignore").stdout
        pid = ""
        for line in out.splitlines():
            if (":%d" % port) in line and "LISTENING" in line.upper():
                parts = line.split()
                if parts:
                    pid = parts[-1]
                    break
        if not pid:
            return ""
        info = subprocess.run(["tasklist", "/FI", "PID eq %s" % pid],
                              capture_output=True, text=True, timeout=8,
                              errors="ignore").stdout
        name = ""
        for line in info.splitlines():
            if pid in line:
                name = line.split()[0]
                break
        return ("PID %s %s" % (pid, name)).strip()
    except Exception:
        return ""


def _port_in_use(host, port):
    """Windows 的 SO_REUSEADDR 允许重复绑定，必须主动连一下才可靠。"""
    _h = "127.0.0.1" if host in ("0.0.0.0", "", None) else host
    try:
        s1 = socket.create_connection((_h, port), timeout=0.5)
        s1.close()
        return True
    except Exception:
        return False


if __name__ == "__main__":
    host = os.environ.get("FILLVIEWER_HOST") or (_cfg.get("host") or "127.0.0.1")
    port = int(os.environ.get("FILLVIEWER_PORT") or _cfg.get("port") or 8099)

    # 日志：写到 logs\server.log；超过 logMaxMB 自动滚存（P2-1）
    _logdir = os.path.join(ROOT, "logs")
    os.makedirs(_logdir, exist_ok=True)
    _logpath = os.path.join(_logdir, "server.log")
    _rotate_log(_logpath, int(sec("logMaxMB") or 5))
    _logf = open(_logpath, "a", encoding="utf-8", buffering=1)
    _logf.write("\n===== start %s  python %s =====\n"
                % (time.strftime("%Y-%m-%d %H:%M:%S"), sys.version.split()[0]))

    class _Tee:
        def write(self, s):
            try:
                _logf.write(s)
            except Exception:
                pass
            try:
                sys.__stderr__.write(s)
            except Exception:
                pass

        def flush(self):
            try:
                _logf.flush()
            except Exception:
                pass

    sys.stderr = _Tee()

    print("FillViewer ready:  http://%s:%d/index.html" % (host, port))
    print("静态根目录: %s" % ROOT)
    print("REST 生效地址: %s  （证书校验：%s）"
          % (cfg_rest_base(), "关闭（restInsecure=true）" if sec("restInsecure") else "开启"))
    print("写值策略: %s；一次性 token: %s；上传上限: %d MB"
          % ("只读" if sec("readOnly") else "白名单可写",
             "开" if sec("requireWriteToken") else "关",
             int(sec("maxUploadMB") or 256)))
    if _port_in_use(host, port):
        print("[FATAL] 端口 %d 已被占用（可能 FillViewer 已经在跑）" % port)
        print("[FATAL] 占用进程：%s" % (_port_owner(port) or "未识别"))
        print("[FATAL] 结束该进程，或设环境变量 FILLVIEWER_PORT 换一个端口")
        raise SystemExit(2)
    try:
        ThreadingHTTPServer((host, port), Handler).serve_forever()
    except OSError as e:
        owner = _port_owner(port)
        print("[FATAL] 端口 %d 被占用或无法绑定：%s" % (port, e))
        if owner:
            print("[FATAL] 占用进程：%s（结束它或改用 FILLVIEWER_PORT 换端口）" % owner)
        else:
            print("[FATAL] 未找到占用进程；可设环境变量 FILLVIEWER_PORT 换端口")
        raise SystemExit(2)



